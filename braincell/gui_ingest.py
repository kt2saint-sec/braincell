# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
gui_ingest.py — ingestion management for the Memory-Map GUI.

Write-gated endpoints mounted by gui.create_app(allow_writes=True):

  GET  /api/fs                 server-side folder browser (directories only)
  POST /api/pick-folder        native GNOME folder-selection dialog (zenity)
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
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from . import config
from .log import get as _get_log
from .project_registry import load_path_registry

log = _get_log("braincell.gui_ingest")

_LEDGER_FILENAME = "transcript_ingest_ledger.json"
_LOG_TAIL = 200          # lines of job log kept/returned
_SCHED_TICK_S = 60.0     # scheduler wake-up interval


# ── Request bodies ─────────────────────────────────────────────────────────────

class IngestBody(BaseModel):
    path: str
    mode: Literal["project", "global"] = "project"
    reembed: bool = False


class ClearBody(BaseModel):
    project_id: str
    include_notes: bool = False


class ScheduleBody(BaseModel):
    path: str
    interval_minutes: int  # 0 → remove the schedule


# ── Ingest job manager ─────────────────────────────────────────────────────────

@dataclass
class IngestJob:
    path: str
    state: str = "running"           # running | done | error
    log: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: Optional[float] = None
    returncode: Optional[int] = None
    mode: str = "project"            # project | global (build target brain)
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

    def __init__(self) -> None:
        self.job: Optional[IngestJob] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task] = None

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
            if job.mode == "global":
                cmd += ["--mode", "global"]
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
            )
            self._proc = proc
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                job.log.append(line.decode("utf-8", "replace").rstrip())
                del job.log[:-_LOG_TAIL or None]
            job.returncode = await proc.wait()
            job.state = "done" if job.returncode == 0 else "error"
        except Exception as exc:  # spawn failure etc. — never crash the GUI
            job.log.append(f"ingest failed to run: {exc!r}")
            job.state = "error"
        finally:
            job.finished = time.time()
            self._proc = None

    async def wait(self) -> None:
        """Await the in-flight job (test/scheduler helper)."""
        if self._task is not None:
            await self._task


# ── Schedules (persisted JSON + asyncio driver) ────────────────────────────────

def schedules_path() -> Path:
    return config._xdg_data_home() / config.DATA_NAMESPACE / "gui-schedules.json"


def load_schedules() -> list[dict]:
    p = schedules_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        log.warning("Unreadable gui-schedules.json — starting empty.")
        return []


def save_schedules(schedules: list[dict]) -> None:
    p = schedules_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(schedules, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def schedule_due(sched: dict, now: Optional[float] = None) -> bool:
    """True when the schedule's interval has elapsed since last_run (or never ran)."""
    now = time.time() if now is None else now
    interval_s = float(sched.get("interval_minutes", 0)) * 60.0
    if interval_s <= 0:
        return False
    last = sched.get("last_run")
    return last is None or (now - float(last)) >= interval_s


async def scheduler_loop(manager: IngestManager) -> None:
    """Fire due schedules while the GUI server runs. Cancelled on shutdown."""
    while True:
        try:
            await asyncio.sleep(_SCHED_TICK_S)
            if manager.busy:
                continue
            schedules = load_schedules()
            for sched in schedules:
                if schedule_due(sched):
                    sched["last_run"] = time.time()
                    save_schedules(schedules)
                    log.info("Scheduled ingest firing for %s", sched["path"])
                    await manager.start(sched["path"])
                    break  # one at a time; others fire on later ticks
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


# ── Native folder picker (zenity, with /api/fs as the always-present fallback) ─

_PICKER_TIMEOUT_S = 180.0  # bound a stuck/abandoned dialog
_picker_lock = asyncio.Lock()  # one dialog at a time


async def pick_folder_native() -> dict:
    """Open a native GNOME folder-selection dialog via ``zenity``.

    Returns exactly one of:
      ``{"path": str}``                      — a directory was picked.
      ``{"cancelled": True}``                 — the user hit Cancel (rc != 0).
      ``{"unavailable": True, "reason": str}`` — no display, no zenity, a
        timeout, or the picked path wasn't a directory; callers should fall
        back to ``GET /api/fs``.
    """
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        return {"unavailable": True, "reason": "no display (headless/SSH session)"}
    zen = shutil.which("zenity")
    if zen is None:
        return {"unavailable": True, "reason": "zenity not installed"}
    try:
        proc = await asyncio.create_subprocess_exec(
            zen, "--file-selection", "--directory", "--title=Select a repository to ingest",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        return {"unavailable": True, "reason": f"failed to launch zenity: {exc!r}"}
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), _PICKER_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"unavailable": True, "reason": "picker timed out"}
    if proc.returncode != 0:
        return {"cancelled": True}
    picked = Path(stdout.decode("utf-8", "replace").strip()).expanduser()
    if not picked.is_dir():
        return {"unavailable": True, "reason": "not a directory"}
    return {"path": str(picked.resolve())}


# ── Clear (wipe a project's ingested memory) ───────────────────────────────────

def clear_project(db_path: Path, project_id: str, include_notes: bool) -> dict:
    """Wipe docs/chunks (and optionally notes) for project_id in db_path.

    Also removes the transcript-ingest ledgers (open-db sibling + the
    project's own db sibling) so the next build re-absorbs everything.
    Sync sqlite3 on purpose — called via a worker thread.
    """
    import sqlite3

    from .store import SqliteStore

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
                # FK: clear inbound supersession pointers BEFORE the delete —
                # with foreign keys enforced, removing a note that another row still
                # references raises IntegrityError (rows are checked as they go).
                cf.execute(
                    f"UPDATE memory_notes SET superseded_by = NULL "
                    f"WHERE superseded_by IN ({ph})", ids,
                )
                cf.execute(f"DELETE FROM memory_notes WHERE id IN ({ph})", ids)
                notes_removed = len(ids)
            cf.commit()
        finally:
            cf.close()

    ledgers_removed = 0
    for ledger in {
        db_path.parent / _LEDGER_FILENAME,
        config.get_db_path(project_id).parent / _LEDGER_FILENAME,
    }:
        try:
            if ledger.exists():
                ledger.unlink()
                ledgers_removed += 1
        except OSError:
            pass
    return {
        "docs_removed": docs_removed,
        "notes_removed": notes_removed,
        "ledgers_removed": ledgers_removed,
    }


# ── Route mounting (called by gui.create_app when allow_writes=True) ──────────

def mount_ingest_api(app: FastAPI, *, db_path: Path, manager: IngestManager) -> None:
    """Register the ingestion-management routes on *app*."""

    @app.get("/api/fs")
    async def api_fs(path: str = "") -> dict:  # type: ignore[type-arg]
        return list_dirs(path)

    @app.post("/api/pick-folder")
    async def api_pick_folder() -> dict:  # type: ignore[type-arg]
        if _picker_lock.locked():
            raise HTTPException(409, "a picker dialog is already open")
        async with _picker_lock:
            return await pick_folder_native()

    @app.post("/api/ingest")
    async def api_ingest(body: IngestBody) -> dict:  # type: ignore[type-arg]
        p = Path(body.path).expanduser()
        if not p.is_dir():
            raise HTTPException(400, f"Not a directory: {body.path}")
        try:
            job = await manager.start(
                str(p.resolve()), mode=body.mode, reembed=body.reembed
            )
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return {"started": True, "job": job.as_dict()}

    @app.get("/api/ingest/status")
    async def api_ingest_status() -> dict:  # type: ignore[type-arg]
        return {"job": manager.job.as_dict() if manager.job else None}

    @app.post("/api/clear")
    async def api_clear(request: Request, body: ClearBody) -> dict:  # type: ignore[type-arg]
        registry = load_path_registry()
        if body.project_id not in set(registry.values()):
            raise HTTPException(404, f"Unknown project {body.project_id!r}.")
        import anyio
        result = await anyio.to_thread.run_sync(
            clear_project, db_path, body.project_id, body.include_notes
        )
        return {"ok": True, **result}

    @app.get("/api/schedule")
    async def api_schedule_list() -> dict:  # type: ignore[type-arg]
        return {"schedules": load_schedules()}

    @app.post("/api/schedule")
    async def api_schedule_set(body: ScheduleBody) -> dict:  # type: ignore[type-arg]
        if body.interval_minutes < 0:
            raise HTTPException(400, "interval_minutes must be >= 0.")
        norm = str(Path(body.path).expanduser().resolve())
        schedules = [s for s in load_schedules() if s.get("path") != norm]
        if body.interval_minutes > 0:
            schedules.append({
                "path": norm,
                "interval_minutes": body.interval_minutes,
                "last_run": None,
            })
        save_schedules(schedules)
        return {"ok": True, "schedules": schedules}
