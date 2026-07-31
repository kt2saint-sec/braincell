# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
test_ingest_orphan_protection.py — GUI-spawned builds must die with the GUI.

Defect (2026-07-25): a `braincell build` child spawned by the GUI's ingest
manager survived its parent's HARD death ("Event loop is closed"), was
reparented to systemd, ran 24+ minutes at up to 4.9 GB RSS, and held the
SQLite write lock. Two protections now exist:

  - PR_SET_PDEATHSIG (gui_ingest._pdeathsig_preexec): kernel-delivered
    SIGKILL on parent death — covers SIGKILL/crash where no cleanup runs.
  - IngestManager.shutdown(): graceful TERM→grace→KILL from the GUI
    lifespan's finally — covers normal window-close/server-stop.

These tests exercise REAL processes (real fork/exec, real prctl, real
SIGKILL) — no faked commands, no mocked kills.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="PR_SET_PDEATHSIG is Linux-only"
)


def _pid_dead(pid: int) -> bool:
    """True when *pid* no longer exists (or is a reaped-pending zombie)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as fh:
            return fh.read().split(") ", 1)[1].split()[0] == "Z"
    except OSError:
        return True
    return False


_PARENT_SCRIPT = """
import asyncio, sys
from braincell.gui_ingest import IngestManager

class SleepManager(IngestManager):
    def command_for(self, path):
        return ["sleep", "300"]

async def main():
    m = SleepManager()
    await m.start("/tmp")
    while m._proc is None:
        await asyncio.sleep(0.01)
    print(m._proc.pid, flush=True)
    await asyncio.sleep(600)

asyncio.run(main())
"""


class TestHardParentDeath:
    def test_sigkilled_parent_takes_the_build_child_with_it(self):
        """SIGKILL the GUI-analog parent — the kernel must kill the child.

        This is the case that actually bit: no Python cleanup code runs at
        all, so only PR_SET_PDEATHSIG can save it. On pre-fix code the sleep
        child survives its dead parent and this test fails at the deadline.
        """
        from pathlib import Path

        import braincell

        parent = subprocess.Popen(
            [sys.executable, "-c", _PARENT_SCRIPT],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd="/",
            # Pin the subprocess to the SAME braincell package this test
            # imported — otherwise an editable install shadows the tree under
            # test and the subprocess quietly exercises different code.
            env={
                **os.environ,
                "PYTHONPATH": str(Path(braincell.__file__).parents[1]),
            },
        )
        try:
            line = parent.stdout.readline().strip()
            assert line.isdigit(), (
                f"parent never reported a child pid: {line!r} "
                f"/ stderr: {parent.stderr.read()!r}"
            )
            child_pid = int(line)
            assert not _pid_dead(child_pid), "child should be running pre-kill"

            os.kill(parent.pid, signal.SIGKILL)  # hard death — no cleanup runs
            parent.wait(timeout=5)

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if _pid_dead(child_pid):
                    return
                time.sleep(0.05)
            os.kill(child_pid, signal.SIGKILL)  # don't leak the orphan we proved
            pytest.fail("build child survived its parent's SIGKILL (orphaned)")
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=5)


class TestGracefulShutdown:
    def test_shutdown_terminates_inflight_build(self):
        from braincell.gui_ingest import IngestManager

        class SleepManager(IngestManager):
            def command_for(self, path):
                return ["sleep", "300"]

        async def scenario():
            m = SleepManager()
            await m.start("/tmp")
            while m._proc is None:
                await asyncio.sleep(0.01)
            pid = m._proc.pid
            await m.shutdown()
            return m, pid

        m, pid = asyncio.run(scenario())
        assert _pid_dead(pid), "shutdown() left the build child running"
        assert m.job.finished is not None
        assert m.job.returncode == -signal.SIGTERM  # grace path, not SIGKILL
        assert any("cancelled" in ln for ln in m.job.log)

    def test_shutdown_without_job_is_a_noop(self):
        from braincell.gui_ingest import IngestManager

        asyncio.run(IngestManager().shutdown())  # must not raise

    def test_shutdown_after_job_completed_is_a_noop(self):
        from braincell.gui_ingest import IngestManager

        class EchoManager(IngestManager):
            def command_for(self, path):
                return ["true"]

        async def scenario():
            m = EchoManager()
            await m.start("/tmp")
            await m.wait()
            state = m.job.state
            await m.shutdown()
            return m, state

        m, state = asyncio.run(scenario())
        assert state == "done"
        assert m.job.state == "done"  # shutdown didn't rewrite a finished job
