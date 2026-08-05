# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
test_gui_ingest_parent_death_guard.py — regression tests for BUGS.md
"cross-platform parent-death cleanup": Linux is already covered by
`_pdeathsig_preexec`'s PR_SET_PDEATHSIG (unchanged, guarded off-Linux); this
covers the POST-spawn guards for Windows (Job Object / KILL_ON_JOB_CLOSE via
ctypes) and macOS (a detached parent-pid-polling watchdog subprocess).

Windows/macOS code paths are exercised here via `sys.platform` monkeypatching
and mocked ctypes/subprocess seams (this suite runs on Linux); the portable
polling LOOP itself (`_run_parent_death_watchdog`) is exercised for real with
live subprocesses, since its only OS dependency is `os.kill(pid, 0)`/SIGKILL,
which behave the same way on Linux and macOS.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest


def _reap_in_background(proc: subprocess.Popen) -> threading.Thread:
    """`_run_parent_death_watchdog`'s `os.kill(pid, 0)` sees a zombie as alive
    (POSIX keeps a dead-but-unwaited child's PID slot until it is reaped) —
    fine in production, where the watchdog is never the dead process's real
    parent and so never controls its reaping, but a test spawning "parent" as
    its OWN child must reap it concurrently or the poll loop never observes
    the exit. A background thread blocked in wait() mirrors "some other
    process reaps it promptly", which is what actually happens for a real GUI
    process's OS-assigned parent."""
    thread = threading.Thread(target=proc.wait, daemon=True)
    thread.start()
    return thread


# ── _pid_alive / _run_parent_death_watchdog (real subprocesses) ────────────────

class TestPidAlive:
    def test_refuses_to_probe_on_a_non_posix_platform(self, monkeypatch):
        """os.kill(pid, 0) on Windows is GenerateConsoleCtrlEvent(CTRL_C_EVENT),
        which interrupts the whole console (it aborted entire pytest runs in
        CI) — the probe must refuse rather than ever issuing it."""
        from braincell import gui_ingest

        monkeypatch.setattr(gui_ingest.os, "name", "nt")
        with pytest.raises(RuntimeError, match="POSIX-only"):
            gui_ingest._pid_alive(12345)

    @pytest.mark.skipif(sys.platform == "win32", reason="_pid_alive is POSIX-only; Windows uses Job Objects")
    def test_true_for_a_live_process(self):
        from braincell.gui_ingest import _pid_alive

        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            assert _pid_alive(proc.pid) is True
        finally:
            proc.kill()
            proc.wait()

    @pytest.mark.skipif(sys.platform == "win32", reason="_pid_alive is POSIX-only; Windows uses Job Objects")
    def test_false_for_a_reaped_process(self):
        from braincell.gui_ingest import _pid_alive

        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        assert _pid_alive(proc.pid) is False


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the watchdog is POSIX-only product surface; its _pid_alive probe "
    "(os.kill(pid, 0)) sends CTRL_C_EVENT to the whole console on Windows",
)
class TestRunParentDeathWatchdog:
    def test_kills_child_once_parent_exits(self):
        """The exact scenario this closes: an orphaned build must not survive
        its dead parent — here bounded to seconds, not the original 24+ minutes."""
        from braincell.gui_ingest import _run_parent_death_watchdog

        parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.1)"])
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        reaper = _reap_in_background(parent)
        try:
            _run_parent_death_watchdog(parent.pid, child.pid, poll_interval=0.02)
            reaper.join(timeout=5)
            child.wait(timeout=5)
            assert child.returncode == -9  # SIGKILL, matching the Linux prctl posture
        finally:
            for proc in (parent, child):
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

    def test_exits_quietly_when_child_finishes_first(self):
        """No parent-death action once the build already ended on its own."""
        from braincell.gui_ingest import _run_parent_death_watchdog

        parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            child.wait(timeout=5)
            _run_parent_death_watchdog(parent.pid, child.pid, poll_interval=0.02)
            # Parent was never touched.
            assert parent.poll() is None
        finally:
            parent.kill()
            parent.wait()


class TestWatchdogMain:
    def test_rejects_wrong_argument_count(self):
        from braincell.gui_ingest import _watchdog_main

        assert _watchdog_main([]) == 2
        assert _watchdog_main(["1"]) == 2

    def test_delegates_to_the_watchdog_loop(self, monkeypatch):
        from braincell import gui_ingest
        from braincell.gui_ingest import _watchdog_main

        calls = []
        monkeypatch.setattr(
            gui_ingest, "_run_parent_death_watchdog",
            lambda parent_pid, child_pid: calls.append((parent_pid, child_pid)),
        )
        assert _watchdog_main(["111", "222"]) == 0
        assert calls == [(111, 222)]

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="spawns the real POSIX-only watchdog loop; see TestRunParentDeathWatchdog",
    )
    def test_invocable_as_a_module(self):
        """`_spawn_macos_watchdog` shells out to exactly this — prove it works
        end to end as a real detached subprocess, not just as a function call."""
        parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.1)"])
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        watchdog = subprocess.Popen(
            [sys.executable, "-m", "braincell.gui_ingest", str(parent.pid), str(child.pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            parent.wait(timeout=5)
            child.wait(timeout=10)
            assert child.returncode == -9
            watchdog.wait(timeout=5)
        finally:
            for proc in (parent, child, watchdog):
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()


# ── Platform dispatch (_start_parent_death_guard / _release_parent_death_guard) ─

class TestStartParentDeathGuardDispatch:
    def test_linux_installs_no_post_spawn_guard(self, monkeypatch):
        """Linux already got its guard via preexec_fn's PR_SET_PDEATHSIG at
        spawn time — nothing more should be attached here."""
        from braincell import gui_ingest

        monkeypatch.setattr(sys, "platform", "linux")
        assert gui_ingest._start_parent_death_guard(1, 2) is None

    def test_windows_installs_a_job_object_guard(self, monkeypatch):
        from braincell import gui_ingest

        monkeypatch.setattr(sys, "platform", "win32")
        calls = []
        monkeypatch.setattr(
            gui_ingest, "_win32_job_kill_on_close",
            lambda pid: calls.append(pid) or 12345,
        )
        assert gui_ingest._start_parent_death_guard(1, 2) == 12345
        assert calls == [2]

    def test_windows_guard_failure_never_raises(self, monkeypatch):
        from braincell import gui_ingest

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            gui_ingest, "_win32_job_kill_on_close",
            lambda pid: (_ for _ in ()).throw(OSError("boom")),
        )
        assert gui_ingest._start_parent_death_guard(1, 2) is None

    def test_macos_spawns_a_watchdog(self, monkeypatch):
        from braincell import gui_ingest

        monkeypatch.setattr(sys, "platform", "darwin")
        calls = []
        sentinel = object()
        monkeypatch.setattr(
            gui_ingest, "_spawn_macos_watchdog",
            lambda parent_pid, child_pid: calls.append((parent_pid, child_pid)) or sentinel,
        )
        assert gui_ingest._start_parent_death_guard(111, 222) is sentinel
        assert calls == [(111, 222)]

    def test_macos_guard_failure_never_raises(self, monkeypatch):
        from braincell import gui_ingest

        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(
            gui_ingest, "_spawn_macos_watchdog",
            lambda parent_pid, child_pid: (_ for _ in ()).throw(OSError("boom")),
        )
        assert gui_ingest._start_parent_death_guard(1, 2) is None


class TestReleaseParentDeathGuard:
    def test_none_guard_is_a_no_op(self, monkeypatch):
        from braincell import gui_ingest

        monkeypatch.setattr(sys, "platform", "win32")
        gui_ingest._release_parent_death_guard(None)  # must not raise

    def test_macos_terminates_the_watchdog_process(self, monkeypatch):
        from braincell import gui_ingest

        monkeypatch.setattr(sys, "platform", "darwin")

        class _FakeWatchdog:
            def __init__(self):
                self.terminated = False

            def terminate(self):
                self.terminated = True

        fake = _FakeWatchdog()
        gui_ingest._release_parent_death_guard(fake)
        assert fake.terminated is True

    def test_windows_closes_the_job_handle(self, monkeypatch):
        from braincell import gui_ingest

        monkeypatch.setattr(sys, "platform", "win32")
        closed = []

        class _FakeKernel32:
            def CloseHandle(self, handle):
                closed.append(handle)

        monkeypatch.setattr(
            __import__("ctypes"), "WinDLL", lambda *a, **k: _FakeKernel32(), raising=False,
        )
        gui_ingest._release_parent_death_guard(999)
        assert closed == [999]


# ── Windows Job Object plumbing (_win32_job_kill_on_close, ctypes mocked) ───────

class _FakeKernel32:
    """Records every call and lets a test script per-method success/failure."""

    def __init__(self, *, fail_at: str | None = None):
        self.fail_at = fail_at
        self.calls: list[str] = []
        self.closed: list[int] = []
        self._next_handle = 1

    def _handle(self) -> int:
        self._next_handle += 1
        return self._next_handle

    def CreateJobObjectW(self, *_a):
        self.calls.append("CreateJobObjectW")
        return 0 if self.fail_at == "CreateJobObjectW" else self._handle()

    def SetInformationJobObject(self, *_a):
        self.calls.append("SetInformationJobObject")
        return 0 if self.fail_at == "SetInformationJobObject" else 1

    def OpenProcess(self, *_a):
        self.calls.append("OpenProcess")
        return 0 if self.fail_at == "OpenProcess" else self._handle()

    def AssignProcessToJobObject(self, *_a):
        self.calls.append("AssignProcessToJobObject")
        return 0 if self.fail_at == "AssignProcessToJobObject" else 1

    def CloseHandle(self, handle):
        self.calls.append("CloseHandle")
        self.closed.append(handle)


class TestWin32JobKillOnClose:
    def _patch(self, monkeypatch, kernel32):
        import ctypes
        monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: kernel32, raising=False)

    def test_happy_path_returns_a_job_handle(self, monkeypatch):
        from braincell.gui_ingest import _win32_job_kill_on_close

        kernel32 = _FakeKernel32()
        self._patch(monkeypatch, kernel32)
        job = _win32_job_kill_on_close(4321)
        assert job is not None
        # The process handle is always closed after AssignProcessToJobObject
        # (success or failure); the job handle itself stays open (caller owns
        # its lifetime via _release_parent_death_guard).
        assert kernel32.calls == [
            "CreateJobObjectW", "SetInformationJobObject",
            "OpenProcess", "AssignProcessToJobObject", "CloseHandle",
        ]

    def test_create_job_object_failure_returns_none(self, monkeypatch):
        from braincell.gui_ingest import _win32_job_kill_on_close

        kernel32 = _FakeKernel32(fail_at="CreateJobObjectW")
        self._patch(monkeypatch, kernel32)
        assert _win32_job_kill_on_close(4321) is None
        assert kernel32.closed == []  # no handle was ever opened

    def test_set_information_failure_closes_the_job_handle(self, monkeypatch):
        from braincell.gui_ingest import _win32_job_kill_on_close

        kernel32 = _FakeKernel32(fail_at="SetInformationJobObject")
        self._patch(monkeypatch, kernel32)
        assert _win32_job_kill_on_close(4321) is None
        assert len(kernel32.closed) == 1

    def test_open_process_failure_closes_the_job_handle(self, monkeypatch):
        from braincell.gui_ingest import _win32_job_kill_on_close

        kernel32 = _FakeKernel32(fail_at="OpenProcess")
        self._patch(monkeypatch, kernel32)
        assert _win32_job_kill_on_close(4321) is None
        assert len(kernel32.closed) == 1

    def test_assign_failure_closes_both_handles(self, monkeypatch):
        from braincell.gui_ingest import _win32_job_kill_on_close

        kernel32 = _FakeKernel32(fail_at="AssignProcessToJobObject")
        self._patch(monkeypatch, kernel32)
        assert _win32_job_kill_on_close(4321) is None
        assert len(kernel32.closed) == 2  # process handle, then job handle


# ── End-to-end: the guard is installed and released around a real ingest run ───

class TestGuardWiredIntoIngestRun:
    def test_guard_installed_with_real_pids_and_released_on_completion(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from braincell.gui import create_app

        app = create_app(db_path=tmp_path / "braincell.db", allow_writes=True)
        proj = tmp_path / "proj"
        proj.mkdir()

        from braincell import gui_ingest

        started, released = [], []
        real_start = gui_ingest._start_parent_death_guard

        def _spy_start(parent_pid, child_pid):
            started.append((parent_pid, child_pid))
            return real_start(parent_pid, child_pid)

        def _spy_release(guard):
            released.append(guard)

        monkeypatch.setattr(gui_ingest, "_start_parent_death_guard", _spy_start)
        monkeypatch.setattr(gui_ingest, "_release_parent_death_guard", _spy_release)

        with TestClient(app) as client:
            mgr = app.state.ingest_manager
            monkeypatch.setattr(
                mgr, "command_for",
                lambda path: [sys.executable, "-c", "print('ok')"],
            )
            client.post("/api/ingest", json={"path": str(proj)})
            deadline = time.time() + 10
            while time.time() < deadline:
                job = client.get("/api/ingest/status").json()["job"]
                if job and job["state"] != "running":
                    break
                time.sleep(0.02)
        assert len(started) == 1
        # Linux: _start_parent_death_guard is a real no-op guard (returns None);
        # what matters here is that it ran with the real GUI/child pids and that
        # release ran exactly once afterward, regardless of what was returned.
        import os
        assert started[0][0] == os.getpid()
        assert len(released) == 1
