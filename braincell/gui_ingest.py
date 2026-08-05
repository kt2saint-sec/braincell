# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
gui_ingest.py — ingestion management for the Memory-Map GUI.

Write-gated endpoints mounted by gui.create_app(allow_writes=True):

  GET  /api/fs                 embedded folder navigator (directories only)
  POST /api/pick-folder        native Qt folder-selection dialog
  POST /api/ingest             start an ingest (build) job for a directory
  GET  /api/ingest/status      poll the current/last ingest job
  POST /api/clear              wipe a project's ingested docs/chunks (+notes opt-in)
  GET  /api/schedule           list ingest schedules
  POST /api/schedule           set/remove a schedule for a path

Ingest runs the real CLI (``python -m braincell.cli build <path>``) in a
subprocess — full isolation from the GUI's event loop and store, and the job
log is simply the build's stdout. One job at a time (409 when busy).

Schedules persist in ``<xdg>/<namespace>/gui-schedules.json`` and are driven
by an asyncio loop that runs while the GUI server is up (the GUI is a local
tool — there is no daemon; a schedule fires on the next tick after it is due).
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from . import config
from .catalog_io import atomic_write_json, catalog_lock
from .gui_mutation import GuiMutationBusy, GuiMutationCoordinator
from .log import get as _get_log
from .project_registry import load_path_registry

log = _get_log("braincell.gui_ingest")

_LEDGER_FILENAME = "transcript_ingest_ledger.json"
_LOG_TAIL = 200          # lines of job log kept/returned
_SCHED_TICK_S = 60.0     # scheduler wake-up interval
_SCHED_FAILURE_RETRY_S = 300.0


# ── Request bodies ─────────────────────────────────────────────────────────────

class IngestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    reembed: bool = False


class ClearBody(BaseModel):
    project_id: str
    include_notes: bool = False
    skip_backup: bool = False


class ScheduleBody(BaseModel):
    path: str
    interval_minutes: int  # 0 → remove the schedule


# ── Ingest job manager ─────────────────────────────────────────────────────────

_SHUTDOWN_GRACE_S = 5.0   # SIGTERM → this long → SIGKILL on GUI shutdown


def _pdeathsig_preexec(parent_pid: int):
    """Return a preexec_fn tying the build child's life to the GUI's (Linux).

    Runs in the forked child before exec: PR_SET_PDEATHSIG makes the kernel
    deliver SIGKILL to the child the moment the GUI process dies — including
    HARD deaths (SIGKILL, crash, "Event loop is closed") where no cleanup code
    runs at all. That hard-death case is what actually orphaned builds: a
    GUI-spawned `braincell build` survived its dead parent for 24+ minutes,
    held the SQLite write lock, and peaked at 4.9 GB RSS. SIGKILL (not TERM)
    because nothing escorts the child after a hard parent death and the store
    is WAL/crash-safe; the getppid check closes the fork-vs-parent-death race
    (pdeathsig only arms AFTER prctl — a parent that died in between means the
    child is already reparented, so it must exit itself). Returns None off
    Linux (prctl is Linux-only; preexec must never break spawning).
    """
    if sys.platform != "linux":
        return None

    def _preexec() -> None:  # pragma: no cover — runs in the forked child
        try:
            import ctypes
            import signal as _signal
            libc = ctypes.CDLL(None, use_errno=True)
            libc.prctl(1, _signal.SIGKILL, 0, 0, 0)  # 1 = PR_SET_PDEATHSIG
            if os.getppid() != parent_pid:
                os._exit(112)  # parent died before prctl armed
        except Exception:  # noqa: BLE001, S110 — never abort the spawn from preexec; logging is unsafe post-fork
            pass

    return _preexec


# ── Cross-platform parent-death guard (Windows / macOS) ────────────────────────
#
# `_pdeathsig_preexec` above covers Linux via `preexec_fn`, which Python's
# subprocess module refuses to accept AT ALL on Windows (ValueError) and which
# cannot carry a PR_SET_PDEATHSIG equivalent on macOS (no such syscall exists
# there). Both platforms therefore need a mechanism that attaches AFTER the
# child is spawned, driven from `_run()` below rather than from a preexec_fn.

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_void_p),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _win32_job_kill_on_close(pid: int):
    """Create a Windows Job Object with KILL_ON_JOB_CLOSE, assign *pid* to it,
    and return the open job HANDLE — or None on any failure (never raises;
    same fail-open posture as a failed prctl in `_pdeathsig_preexec` — a
    broken guard must never block the spawn it is protecting).

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE terminates every process still in the
    job the moment the LAST handle to the job object closes. Windows itself
    closes every handle owned by a process when that process dies for ANY
    reason, including a hard crash or `TerminateProcess` — so, unlike
    PR_SET_PDEATHSIG, this needs no code running inside the GUI at the moment
    of its own death, which is exactly the case ``_pdeathsig_preexec``
    documents as the one that actually orphaned a build for 24+ minutes.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _JobObjectExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job, _JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(info), ctypes.sizeof(info),
    ):
        kernel32.CloseHandle(job)
        return None
    process = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
    if not process:
        kernel32.CloseHandle(job)
        return None
    try:
        if not kernel32.AssignProcessToJobObject(job, process):
            kernel32.CloseHandle(job)
            return None
    finally:
        kernel32.CloseHandle(process)
    return job


_WATCHDOG_POLL_S = 2.0  # coarse on purpose — see _run_parent_death_watchdog


def _pid_alive(pid: int) -> bool:
    """POSIX liveness probe: True unless the kernel confirms the pid is gone."""
    if os.name != "posix":
        # os.kill(pid, 0) is NOT a probe on Windows: CPython maps signal 0 to
        # GenerateConsoleCtrlEvent(CTRL_C_EVENT, ...), which interrupts every
        # process sharing the console — observed aborting an entire pytest run
        # with KeyboardInterrupt in CI. Windows parent-death protection uses a
        # Job Object (_win32_job_kill_on_close); this probe must never run there.
        raise RuntimeError(
            "_pid_alive is POSIX-only; Windows parent-death protection uses a "
            "Job Object"
        )
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    return True


def _run_parent_death_watchdog(
    parent_pid: int, child_pid: int, *, poll_interval: float = _WATCHDOG_POLL_S,
) -> None:
    """Poll until the ORIGINAL parent (the GUI) disappears, then SIGKILL the
    orphaned build child; exits quietly once the child itself is already gone.

    macOS has no PR_SET_PDEATHSIG equivalent, so this cannot live in
    `_pdeathsig_preexec`'s `preexec_fn` even in principle: `preexec_fn` runs
    in the forked child immediately before `exec()`, and `exec()` replaces
    the ENTIRE process image — any thread (or asyncio task) started there is
    destroyed before it could ever observe a later, hard parent death. A
    watchdog living inside the GUI process itself has the same problem in
    reverse: it would die alongside the GUI in exactly the crash case it
    exists to catch. So this runs as its own DETACHED subprocess instead
    (spawned by `_spawn_macos_watchdog`), independent of both.

    Polling — not kqueue's EVFILT_PROC/NOTE_EXIT — is the deliberate choice:
    the same `os.kill(pid, 0)` loop is plain POSIX and therefore portable
    (this exact function is exercised under test on Linux, not just macOS),
    whereas NOTE_EXIT is macOS/BSD-only stdlib surface this repo cannot run
    or verify on its Linux dev/CI hosts. The bug this closes — an orphaned
    build surviving its dead parent for 24+ MINUTES — tolerates a
    multi-second poll interval; NOTE_EXIT's near-instant delivery buys
    nothing a human would notice here.
    """
    while _pid_alive(parent_pid) and _pid_alive(child_pid):
        try:
            time.sleep(poll_interval)
        except KeyboardInterrupt:
            return  # process shutting down; exit cleanly
    if not _pid_alive(parent_pid) and _pid_alive(child_pid):
        try:
            os.kill(child_pid, signal.SIGKILL)
        except OSError:
            pass


def _spawn_macos_watchdog(parent_pid: int, child_pid: int) -> subprocess.Popen:
    """Start `_run_parent_death_watchdog` in its own detached subprocess."""
    return subprocess.Popen(
        [sys.executable, "-m", "braincell.gui_ingest", str(parent_pid), str(child_pid)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _start_parent_death_guard(parent_pid: int, child_pid: int):
    """Best-effort, platform-appropriate guard tying *child_pid* to the life
    of *parent_pid* (the GUI). Linux is already covered by
    `_pdeathsig_preexec`'s PR_SET_PDEATHSIG at spawn time (returns None
    there — nothing more to attach). Returns an opaque handle for
    `_release_parent_death_guard` to release, or None when no guard was
    installed — an unsupported platform, or the attempt itself failed; either
    way the build spawn must never be blocked by a broken guard.
    """
    if sys.platform == "win32":
        try:
            return _win32_job_kill_on_close(child_pid)
        except OSError:
            log.warning(
                "Windows Job Object parent-death guard failed for pid %s",
                child_pid, exc_info=True,
            )
            return None
    if sys.platform == "darwin":
        try:
            return _spawn_macos_watchdog(parent_pid, child_pid)
        except OSError:
            log.warning(
                "macOS parent-death watchdog failed to start for pid %s",
                child_pid, exc_info=True,
            )
            return None
    return None


def _release_parent_death_guard(guard: object) -> None:
    """Release a guard from `_start_parent_death_guard` once the build ends
    on its own — best effort, never raises."""
    if guard is None:
        return
    if sys.platform == "win32":
        try:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(guard)
        except OSError:
            pass
    elif sys.platform == "darwin":
        # The build already ended by itself; ask the watchdog to stop polling
        # rather than let it idle until it notices the child is gone too.
        try:
            guard.terminate()
        except (ProcessLookupError, OSError):
            pass


def _watchdog_main(argv: list[str]) -> int:
    """Entry point for the detached macOS watchdog subprocess — invoked as
    ``python -m braincell.gui_ingest <parent_pid> <child_pid>`` by
    `_spawn_macos_watchdog`. Never imported/called any other way."""
    if len(argv) != 2:
        return 2
    _run_parent_death_watchdog(int(argv[0]), int(argv[1]))
    return 0


@dataclass
class IngestJob:
    path: str
    state: str = "running"           # running | done | error
    log: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: float | None = None
    returncode: int | None = None
    mode: str = "project"
    reembed: bool = False

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "state": self.state,
            "log": self.log[-_LOG_TAIL:],
            "started": self.started,
            "finished": self.finished,
            "returncode": self.returncode,
            "mode": self.mode,
            "reembed": self.reembed,
        }


class IngestManager:
    """Runs one build subprocess at a time; keeps the last job for polling."""

    def __init__(self, coordinator: GuiMutationCoordinator | None = None) -> None:
        self.job: IngestJob | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._coordinator = coordinator or GuiMutationCoordinator()

    # Overridable seam (tests swap in a trivial command).
    def command_for(self, path: str) -> list[str]:
        return [sys.executable, "-m", "braincell.cli", "build", path]

    @property
    def busy(self) -> bool:
        return self.job is not None and self.job.state == "running"

    async def start(
        self, path: str, *, mode: str = "project", reembed: bool = False,
    ) -> IngestJob:
        if self.busy:
            raise RuntimeError("An ingest job is already running.")
        self._coordinator.claim("ingest")
        job = IngestJob(path=path, mode=mode, reembed=reembed)
        self.job = job
        self._task = asyncio.ensure_future(self._run(job))
        return job

    async def _run(self, job: IngestJob) -> None:
        try:
            # Flags append AFTER the command_for() seam: tests that swap in a
            # `python -c` fake see them as inert extra argv, while the real
            # `braincell build` command receives them as its own options.
            cmd = list(self.command_for(job.path))
            if job.reembed:
                cmd.append("--reembed")
            # PYTHONUNBUFFERED: with stdout piped (no tty) the child
            # block-buffers, so the whole build log used to arrive only at
            # completion — the GUI's live log/chip sat empty for the entire
            # run ("I don't see the ingest happening"). Unbuffered, lines
            # stream into job.log as the build prints them.
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                # Tie the child to the GUI's lifetime: a dead GUI (even
                # SIGKILL'd) must never leave a build running forever.
                # Linux: PR_SET_PDEATHSIG arms INSIDE the child via preexec_fn
                # (returns None off Linux — preexec_fn itself is unsupported
                # on Windows and carries no macOS equivalent).
                preexec_fn=_pdeathsig_preexec(os.getpid()),
            )
            self._proc = proc
            # Windows/macOS: attach the platform-appropriate guard AFTER spawn
            # (Job Object / detached watchdog — see _start_parent_death_guard).
            # No-op (returns None) on Linux, where preexec_fn above is enough.
            guard = _start_parent_death_guard(os.getpid(), proc.pid)
            try:
                assert proc.stdout is not None
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    job.log.append(line.decode("utf-8", "replace").rstrip())
                    del job.log[:-_LOG_TAIL or None]
                job.returncode = await proc.wait()
                job.state = "done" if job.returncode == 0 else "error"
            finally:
                _release_parent_death_guard(guard)
        except Exception as exc:  # noqa: BLE001 — spawn failure etc. — never crash the GUI
            job.log.append(f"ingest failed to run: {exc!r}")
            job.state = "error"
        finally:
            job.finished = time.time()
            self._proc = None
            self._coordinator.release("ingest")

    async def wait(self) -> None:
        """Await the in-flight job (test/scheduler helper)."""
        if self._task is not None:
            await self._task

    async def shutdown(self) -> None:
        """Terminate any in-flight build; called from the GUI lifespan finally.

        Product decision: closing the GUI CANCELS a running build rather than
        letting it continue detached — the GUI is the only place the build is
        observable, an invisible orphan holds the SQLite write lock (the exact
        failure that made the taskbar icon look dead), and builds are
        incremental (ledger + content-hash skip), so a cancelled build resumes
        where it left off on the next run. Grace: SIGTERM, then SIGKILL after
        _SHUTDOWN_GRACE_S. The store stays sane either way (WAL — an
        interrupted transaction rolls back). This covers GRACEFUL shutdown;
        hard parent death is covered by _pdeathsig_preexec.
        """
        proc = self._proc
        if proc is not None and proc.returncode is None:
            if self.job is not None and self.job.state == "running":
                self.job.log.append(
                    "build cancelled: GUI shutting down (rerun to resume)"
                )
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), _SHUTDOWN_GRACE_S)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        if self._task is not None and not self._task.done():
            # _run finishes promptly once the child is dead (EOF + wait);
            # bound it anyway so shutdown can never hang the lifespan.
            try:
                await asyncio.wait_for(self._task, _SHUTDOWN_GRACE_S)
            except TimeoutError:
                self._task.cancel()


# ── Schedules (persisted JSON + asyncio driver) ────────────────────────────────

def schedules_path() -> Path:
    return config._xdg_data_home() / config.DATA_NAMESPACE / "gui-schedules.json"


def load_schedules() -> list[dict]:
    p = schedules_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list) and all(isinstance(item, dict) for item in data):
            return data
        log.warning("gui-schedules.json has an invalid format — ignoring it.")
        return []
    except (OSError, ValueError):
        log.warning("Unreadable gui-schedules.json — starting empty.")
        return []


def _load_schedules_for_mutation(path: Path) -> list[dict]:
    """Load schedules without converting persisted corruption into data loss."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"Schedule catalog is unreadable; refusing to mutate {path}: {exc}"
        ) from exc
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise RuntimeError(
            f"Schedule catalog has an invalid format; refusing to mutate {path}"
        )
    return data


def save_schedules(schedules: list[dict]) -> None:
    p = schedules_path()
    if not isinstance(schedules, list) or not all(
        isinstance(item, dict) for item in schedules
    ):
        raise ValueError("Schedules must be a list of objects.")
    with catalog_lock(p):
        _load_schedules_for_mutation(p)
        atomic_write_json(p, schedules)


def schedule_due(sched: dict, now: float | None = None) -> bool:
    """True when success interval or bounded failure-retry delay has elapsed."""
    now = time.time() if now is None else now
    interval_s = float(sched.get("interval_minutes", 0)) * 60.0
    if interval_s <= 0:
        return False
    last_success = sched.get("last_success", sched.get("last_run"))
    last_attempt = sched.get("last_attempt")
    if last_attempt is not None and (
        last_success is None or float(last_attempt) > float(last_success)
    ):
        return (now - float(last_attempt)) >= _SCHED_FAILURE_RETRY_S
    return last_success is None or (now - float(last_success)) >= interval_s


def _update_schedule(path: str, **fields) -> None:
    catalog = schedules_path()
    with catalog_lock(catalog):
        schedules = _load_schedules_for_mutation(catalog)
        for schedule in schedules:
            if schedule.get("path") == path:
                schedule.update(fields)
                atomic_write_json(catalog, schedules)
                return


async def run_due_schedule_once(
    manager: IngestManager, *, now: float | None = None,
) -> bool:
    """Attempt at most one due job and persist attempt/success independently."""
    if manager.busy:
        return False
    attempt_time = time.time() if now is None else now
    for schedule in load_schedules():
        if not schedule_due(schedule, attempt_time):
            continue
        path = str(schedule["path"])
        log.info("Scheduled ingest firing for %s", path)
        try:
            await manager.start(path)
        except GuiMutationBusy:
            # Another GUI mutation is contention, not a failed build attempt.
            return False
        except Exception as exc:  # noqa: BLE001 — a failed scheduled build is recorded and retried, never propagated into the scheduler loop
            _update_schedule(
                path, last_attempt=attempt_time, last_error=str(exc)
            )
            return True
        _update_schedule(path, last_attempt=attempt_time, last_error=None)
        try:
            await manager.wait()
        except Exception as exc:  # noqa: BLE001 — same contract: the wait failure is recorded on the schedule, not raised
            _update_schedule(path, last_error=str(exc))
            return True
        job = manager.job
        if job is not None and job.state == "done" and job.returncode == 0:
            _update_schedule(path, last_success=time.time(), last_error=None)
        else:
            status = job.returncode if job is not None else "unknown"
            _update_schedule(path, last_error=f"build exited with status {status}")
        return True
    return False


async def scheduler_loop(manager: IngestManager) -> None:
    """Fire due schedules while the GUI server runs. Cancelled on shutdown."""
    while True:
        try:
            await asyncio.sleep(_SCHED_TICK_S)
            await run_due_schedule_once(manager)
        except asyncio.CancelledError:
            raise
        except Exception:  # keep the loop alive on transient errors
            log.exception("scheduler tick failed")


# ── Filesystem browsing (folder picker backend) ────────────────────────────────

def list_dirs(raw: str) -> dict:
    """List sub-directories of *raw* (defaults to home). Directories only."""
    base = Path(raw).expanduser() if raw else Path.home()
    try:
        base = base.resolve()
    except OSError:
        raise HTTPException(400, f"Unresolvable path: {raw!r}")
    if not base.is_dir():
        raise HTTPException(404, f"Not a directory: {base}")
    dirs = []
    try:
        for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir() and not entry.is_symlink():
                    dirs.append({"name": entry.name, "path": str(entry)})
            except OSError:
                continue
            if len(dirs) >= 500:
                break
    except PermissionError:
        raise HTTPException(403, f"Permission denied: {base}")
    parent = str(base.parent) if base.parent != base else None
    return {"path": str(base), "parent": parent, "home": str(Path.home()), "dirs": dirs}


# ── Native folder picker serialization ────────────────────────────────────────

_picker_lock = asyncio.Lock()  # one dialog at a time


# ── Clear (wipe a project's ingested memory) ───────────────────────────────────

def clear_project(
    db_path: Path, project_id: str, include_notes: bool, no_backup: bool = False,
) -> dict:
    """Wipe docs/chunks (and optionally notes) for project_id in db_path.

    Also removes the transcript-ingest ledgers (open-db sibling + the
    project's own db sibling) so the next build re-absorbs everything.
    Sync sqlite3 on purpose — called via a worker thread.

    Requires the same mandatory pre-wipe safety snapshot consolidate/reflect/
    reembed require, fail-closed (raises if the backup cannot be made) unless
    the caller explicitly opts out with *no_backup* — an off-by-default escape
    hatch mirroring `braincell build --no-backup`.
    """
    import sqlite3

    from .catalog_io import mutation_lock
    from .store import SqliteStore

    with mutation_lock(db_path, operation="clear"):
        backup: str | None = None
        if db_path.is_file():
            if no_backup:
                log.warning(
                    "clear: skip_backup set for project %s — proceeding with NO "
                    "safety snapshot before wiping ingested memory.", project_id,
                )
            else:
                from .cli import _required_auto_backup
                backup = _required_auto_backup(db_path, "clear")

        # Invalidate checkpoints before destructive commits. If DB clearing
        # subsequently fails, the safe failure mode is a full retrying rebuild.
        ledgers_removed = 0
        for ledger in {
            db_path.parent / _LEDGER_FILENAME,
            config.get_db_path(project_id).parent / _LEDGER_FILENAME,
        }:
            if ledger.exists():
                ledger.unlink()
                ledgers_removed += 1

        store = SqliteStore(db_path)
        try:
            docs_removed = store.wipe_project_embeddings(project_id)
        finally:
            store.close()

        notes_removed = 0
        if include_notes:
            cf = sqlite3.connect(str(db_path))
            try:
                cf.execute("PRAGMA busy_timeout=30000")
                cf.execute("PRAGMA foreign_keys=ON")
                ids = [r[0] for r in cf.execute(
                    "SELECT id FROM memory_notes WHERE project_id = ?", (project_id,)
                ).fetchall()]
                if ids:
                    ph = ",".join("?" * len(ids))
                    try:
                        cf.execute(f"DELETE FROM memory_fts WHERE rowid IN ({ph})", ids)
                    except sqlite3.OperationalError:
                        pass  # FTS5 unavailable
                    cf.execute(
                        f"DELETE FROM bc_note_links WHERE src_id IN ({ph}) OR dst_id IN ({ph})",
                        ids + ids,
                    )
                    cf.execute(
                        f"UPDATE memory_notes SET superseded_by = NULL "
                        f"WHERE superseded_by IN ({ph})", ids,
                    )
                    cf.execute(f"DELETE FROM memory_notes WHERE id IN ({ph})", ids)
                    notes_removed = len(ids)
                cf.commit()
            finally:
                cf.close()

    return {
        "docs_removed": docs_removed,
        "notes_removed": notes_removed,
        "ledgers_removed": ledgers_removed,
        "backup": backup,
    }


# ── Route mounting (called by gui.create_app when allow_writes=True) ──────────

def mount_ingest_api(
    app: FastAPI,
    *,
    db_path: Path,
    manager: IngestManager,
    connected_project_id: str,
    coordinator: GuiMutationCoordinator | None = None,
    pick_folder: Callable[[], dict] | None = None,
) -> None:
    """Register connected-Project ingestion routes on *app*."""
    mutation_coordinator = coordinator or manager._coordinator

    def _require_connected_project(project_id: str) -> None:
        if not connected_project_id:  # isolated factory compatibility for unit tests
            if project_id not in set(load_path_registry().values()):
                raise HTTPException(404, f"Unknown project {project_id!r}.")
            return
        if project_id != connected_project_id:
            raise HTTPException(409, "This operation is limited to the connected Project.")

    def _require_connected_path(path: str) -> Path:
        resolved = Path(path).expanduser().resolve()
        if not connected_project_id:  # isolated factory compatibility for unit tests
            return resolved
        registry = load_path_registry()
        if registry.get(str(resolved)) != connected_project_id:
            raise HTTPException(409, "Build is limited to the connected Project folder.")
        return resolved

    @app.get("/api/fs")
    async def api_fs(path: str = "") -> dict:  # type: ignore[type-arg]
        return list_dirs(path)

    @app.post("/api/pick-folder")
    async def api_pick_folder() -> dict:  # type: ignore[type-arg]
        if _picker_lock.locked():
            raise HTTPException(409, "a picker dialog is already open")
        async with _picker_lock:
            if pick_folder is None:
                return {
                    "unavailable": True,
                    "reason": "native window is not ready",
                }
            import anyio
            return await anyio.to_thread.run_sync(pick_folder)

    @app.post("/api/ingest")
    async def api_ingest(body: IngestBody) -> dict:  # type: ignore[type-arg]
        p = _require_connected_path(body.path)
        if not p.is_dir():
            raise HTTPException(400, f"Not a directory: {body.path}")
        try:
            job = await manager.start(
                str(p), reembed=body.reembed
            )
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return {"started": True, "job": job.as_dict()}

    @app.get("/api/ingest/status")
    async def api_ingest_status() -> dict:  # type: ignore[type-arg]
        return {"job": manager.job.as_dict() if manager.job else None}

    @app.post("/api/clear")
    async def api_clear(request: Request, body: ClearBody) -> dict:  # type: ignore[type-arg]
        _require_connected_project(body.project_id)
        import anyio
        try:
            mutation_coordinator.claim("clear")
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        try:
            try:
                result = await anyio.to_thread.run_sync(
                    clear_project, db_path, body.project_id, body.include_notes,
                    body.skip_backup,
                )
            except RuntimeError as exc:
                raise HTTPException(409, str(exc)) from exc
        finally:
            mutation_coordinator.release("clear")
        return {"ok": True, **result}

    @app.get("/api/schedule")
    async def api_schedule_list() -> dict:  # type: ignore[type-arg]
        return {"schedules": load_schedules()}

    @app.post("/api/schedule")
    async def api_schedule_set(body: ScheduleBody) -> dict:  # type: ignore[type-arg]
        if body.interval_minutes < 0:
            raise HTTPException(400, "interval_minutes must be >= 0.")
        norm = str(_require_connected_path(body.path))
        catalog = schedules_path()
        with catalog_lock(catalog):
            schedules = [
                s for s in _load_schedules_for_mutation(catalog)
                if s.get("path") != norm
            ]
            if body.interval_minutes > 0:
                schedules.append({
                    "path": norm,
                    "interval_minutes": body.interval_minutes,
                    "last_attempt": None,
                    "last_success": None,
                    "last_error": None,
                })
            atomic_write_json(catalog, schedules)
        return {"ok": True, "schedules": schedules}


if __name__ == "__main__":  # pragma: no cover — exercised only via subprocess
    sys.exit(_watchdog_main(sys.argv[1:]))
