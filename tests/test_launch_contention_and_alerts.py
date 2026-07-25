# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_launch_contention_and_alerts.py — regression pins for the 2026-07-25
"taskbar icon dead-clicks" incident.

Root cause chain being pinned:
  1. `SqliteStore.assert_schema_version()` probed FTS5 with a REAL write
     (`CREATE VIRTUAL TABLE _bc_fts5_probe`) in the store db on EVERY open.
     While a `braincell build` held the write lock, that probe serialized
     behind it (busy_timeout 30 s) — GUI startup hung past serve_native's
     20 s budget.
  2. The resulting RuntimeError escaped cmd_start uncaught; with the desktop
     icon's Terminal=false, stderr is invisible → the click looked dead.

Fixes pinned here:
  - Opening an ALREADY-CURRENT store performs no writes: it completes fast
    even while another connection holds SQLite's write lock (real db, real
    lock — no mocks).
  - native_shell.alert() is a never-silent, never-raising error surface:
    Qt dialog when available, notify-send fallback when Qt is broken/absent.
  - cmd_start surfaces ANY run_gui failure visibly (alert + stderr +
    SystemExit(1)) instead of dying silently under Terminal=false.
"""

from __future__ import annotations

import argparse
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from braincell import native_shell
from braincell.store import SqliteStore


# ── 1. Store open must not need the write lock ────────────────────────────────

class TestOpenCurrentStoreUnderWriteLock:
    def test_open_completes_while_another_connection_holds_write_lock(
        self, tmp_path: Path
    ):
        """A built, schema-current store opens fast while a concurrent writer
        (stand-in for `braincell build`) holds BEGIN IMMEDIATE.

        Pre-fix this blocked ~30 s per contended statement (busy_timeout) on
        the FTS5 probe write; the native shell's 20 s startup budget expired
        and the desktop icon dead-clicked. The 10 s assertion bound is loose
        for CI noise while still far under the old 30 s failure mode.
        """
        db = tmp_path / "braincell.db"
        first = SqliteStore(db)
        first.assert_schema_version()  # builds the current schema (no lock held)
        assert first._fts5_ok is True

        holder = sqlite3.connect(str(db))
        try:
            holder.execute("BEGIN IMMEDIATE")  # the build's write lock

            result: dict = {}

            def reopen() -> None:
                t0 = time.monotonic()
                store = SqliteStore(db)
                try:
                    store.assert_schema_version()
                    result["elapsed"] = time.monotonic() - t0
                    result["fts5_ok"] = store._fts5_ok
                except Exception as exc:  # pragma: no cover — fail the assert below
                    result["error"] = exc

            th = threading.Thread(target=reopen, daemon=True)
            th.start()
            th.join(timeout=10)
            assert not th.is_alive(), (
                "assert_schema_version blocked behind a held write lock — "
                "opening a current store must not need SQLite's write lock"
            )
            assert "error" not in result, f"open failed: {result.get('error')!r}"
            assert result["elapsed"] < 10.0
            # The in-memory FTS5 probe must still report availability truthfully.
            assert result["fts5_ok"] is True
        finally:
            holder.rollback()
            holder.close()


# ── 2. alert(): visible error with graceful degradation, never raises ─────────

class TestAlert:
    def test_uses_qt_dialog_when_native_available(self, monkeypatch):
        monkeypatch.setattr(native_shell, "native_available", lambda: True)
        dialogs: list = []
        monkeypatch.setattr(
            native_shell, "show_error", lambda msg, **k: dialogs.append(msg)
        )
        ran: list = []
        monkeypatch.setattr(
            "subprocess.run", lambda *a, **k: ran.append(a) or None
        )
        assert native_shell.alert("boom") is True
        assert dialogs == ["boom"]
        assert ran == []  # no notify-send when Qt worked

    def test_falls_back_to_notify_send_when_qt_unavailable(self, monkeypatch):
        monkeypatch.setattr(native_shell, "native_available", lambda: False)
        calls: list = []

        def fake_run(argv, **kwargs):
            calls.append(argv)

        monkeypatch.setattr("subprocess.run", fake_run)
        assert native_shell.alert("boom", title="BrainCell") is True
        assert len(calls) == 1
        assert calls[0][0] == "notify-send"
        assert "BrainCell" in calls[0] and "boom" in calls[0]

    def test_falls_back_to_notify_send_when_qt_dialog_raises(self, monkeypatch):
        """Qt import succeeding but the dialog blowing up (broken plugin,
        dead display) must still surface SOMETHING — not re-raise."""
        monkeypatch.setattr(native_shell, "native_available", lambda: True)

        def broken_dialog(msg, **k):
            raise RuntimeError("no Qt platform plugin")

        monkeypatch.setattr(native_shell, "show_error", broken_dialog)
        calls: list = []
        monkeypatch.setattr("subprocess.run", lambda argv, **k: calls.append(argv))
        assert native_shell.alert("boom") is True
        assert len(calls) == 1 and calls[0][0] == "notify-send"

    def test_never_raises_even_when_everything_fails(self, monkeypatch):
        monkeypatch.setattr(native_shell, "native_available", lambda: False)

        def broken_run(argv, **k):
            raise FileNotFoundError("notify-send absent")

        monkeypatch.setattr("subprocess.run", broken_run)
        assert native_shell.alert("boom") is False  # exhausted, but no raise


# ── 3. cmd_start: a failing launch must be visible, never a dead click ────────

def _start_args(path, **kw):
    defaults = dict(
        path=str(path), port=8765, no_browser=True, global_brain=False,
        native=False,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


class TestStartFailureVisibility:
    def _patch_launchable_preflight(self, monkeypatch):
        from braincell import launch

        monkeypatch.setattr(
            launch,
            "preflight",
            lambda *a, **k: launch.Preflight(action="launch", report_lines=[]),
        )

    def test_native_run_gui_failure_alerts_and_exits_1(
        self, tmp_path, monkeypatch
    ):
        from braincell.cli import cmd_start

        self._patch_launchable_preflight(monkeypatch)
        monkeypatch.setattr(native_shell, "native_available", lambda: True)

        def failing_run_gui(**kwargs):
            raise RuntimeError("GUI server did not start within 20s — busy db")

        monkeypatch.setattr("braincell.gui.run_gui", failing_run_gui)
        alerts: list = []
        monkeypatch.setattr(
            native_shell, "alert", lambda msg, **k: alerts.append(msg) or True
        )
        with pytest.raises(SystemExit) as exc:
            cmd_start(_start_args(tmp_path, native=True))
        assert exc.value.code == 1
        assert len(alerts) == 1
        assert "failed to start" in alerts[0]
        assert "did not start within" in alerts[0]

    def test_native_alert_fires_even_when_qt_is_the_broken_part(
        self, tmp_path, monkeypatch
    ):
        """--native with PySide6 broken: run_gui falls back internally, but if
        it still fails, the alert must fire (it degrades to notify-send) —
        `native` (the request), not `native_ok`, gates the visible surface."""
        from braincell.cli import cmd_start

        self._patch_launchable_preflight(monkeypatch)
        monkeypatch.setattr(native_shell, "native_available", lambda: False)
        monkeypatch.setattr(
            "braincell.gui.run_gui",
            lambda **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        alerts: list = []
        monkeypatch.setattr(
            native_shell, "alert", lambda msg, **k: alerts.append(msg) or True
        )
        with pytest.raises(SystemExit) as exc:
            cmd_start(_start_args(tmp_path, native=True))
        assert exc.value.code == 1
        assert len(alerts) == 1

    def test_non_native_failure_still_exits_1_without_dialog(
        self, tmp_path, monkeypatch
    ):
        """Browser-path `start` runs in a terminal — stderr is visible, no
        dialog needed, but the non-zero exit must be preserved."""
        from braincell.cli import cmd_start

        self._patch_launchable_preflight(monkeypatch)
        monkeypatch.setattr(native_shell, "native_available", lambda: False)
        monkeypatch.setattr(
            "braincell.gui.run_gui",
            lambda **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        alerts: list = []
        monkeypatch.setattr(
            native_shell, "alert", lambda msg, **k: alerts.append(msg) or True
        )
        with pytest.raises(SystemExit) as exc:
            cmd_start(_start_args(tmp_path, native=False))
        assert exc.value.code == 1
        assert alerts == []
