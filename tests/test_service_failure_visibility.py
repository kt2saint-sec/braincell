# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
test_service_failure_visibility.py — the crash-loop + invisible-failure fixes.

Defect (2026-07-25): braincell-map.service crash-looped 800+ times on a
PERMANENT embedder-fingerprint mismatch because systemd's default start-limit
window (10 s / burst 5) can mathematically never trip with RestartSec=3, and
the only explanation was buried in a traceback mid-journal.

Covers:
  - the unit template's [Unit] StartLimit directives (loop-breaker)
  - store.EmbedderMismatchError (typed, mode-aware rebuild hint)
  - install.service_status() surfacing state/failing/failure
All systemctl calls are faked via install._run_systemctl (the one seam) and
the unit dir is redirected via BRAINCELL_SYSTEMD_USER_DIR — the real user
manager is never touched.
"""

from __future__ import annotations

import sqlite3

import pytest

from braincell import install


# ── Unit template: the start limit must be able to trip ───────────────────────

class TestUnitStartLimit:
    def test_unit_section_has_start_limit(self):
        unit = install._service_unit_text(8765, "braincell")
        unit_section = unit.split("[Service]")[0]
        assert "StartLimitIntervalSec=" in unit_section
        assert "StartLimitBurst=" in unit_section

    def test_limit_actually_trips_with_restart_cadence(self):
        """burst * (RestartSec + a generous per-attempt runtime) must fit inside
        the interval — otherwise the limit is decorative (the original bug:
        default 10 s interval vs 5 restarts spaced 3 s = never trips)."""
        unit = install._service_unit_text(8765, "braincell")
        props = dict(
            line.split("=", 1)
            for line in unit.splitlines()
            if "=" in line and not line.startswith(("[", "#"))
        )
        interval = float(props["StartLimitIntervalSec"])
        burst = int(props["StartLimitBurst"])
        restart_sec = float(props["RestartSec"])
        attempt_runtime_budget = 10.0  # observed failing start ≈ 2-3 s; margin
        assert burst * (restart_sec + attempt_runtime_budget) < interval
        assert props["Restart"] == "on-failure"  # transient failures still retry


# ── EmbedderMismatchError: typed, legible, mode-aware ─────────────────────────

class TestEmbedderMismatchError:
    def _stomp_fingerprint(self, db_path):
        from braincell.store import SqliteStore
        store = SqliteStore(db_path)
        store.assert_schema_version()
        store.close()
        con = sqlite3.connect(str(db_path))
        con.execute(
            "UPDATE embed_fingerprint SET fingerprint = 'ollama:bge-m3:1024'"
        )
        con.commit()
        con.close()

    def test_typed_error_with_fields(self, tmp_path):
        from braincell.store import EmbedderMismatchError, SqliteStore
        db_path = tmp_path / "braincell.db"
        self._stomp_fingerprint(db_path)
        with pytest.raises(EmbedderMismatchError) as ei:
            SqliteStore(db_path).assert_schema_version()
        exc = ei.value
        assert exc.built_with == "ollama:bge-m3:1024"
        assert exc.configured  # current embed_spec.FINGERPRINT
        assert exc.built_with in str(exc) and exc.configured in str(exc)
        assert isinstance(exc, RuntimeError)  # existing handlers keep working

    def test_global_brain_hint_says_mode_global(self, tmp_path):
        from braincell.store import EmbedderMismatchError, SqliteStore
        db_path = tmp_path / "global" / "braincell.db"
        db_path.parent.mkdir(parents=True)
        self._stomp_fingerprint(db_path)
        with pytest.raises(EmbedderMismatchError) as ei:
            SqliteStore(db_path).assert_schema_version()
        assert ei.value.rebuild_cmd == "braincell build --mode global --reembed"
        assert "braincell build --mode global --reembed" in str(ei.value)

    def test_project_brain_hint_has_no_mode_global(self, tmp_path):
        from braincell.store import EmbedderMismatchError, SqliteStore
        db_path = tmp_path / "braincell.db"
        self._stomp_fingerprint(db_path)
        with pytest.raises(EmbedderMismatchError) as ei:
            SqliteStore(db_path).assert_schema_version()
        assert ei.value.rebuild_cmd == "braincell build --reembed"
        assert "--mode global" not in str(ei.value)


# ── service_status: a failing unit must be visible, with a reason ─────────────

@pytest.fixture
def svc_dir(tmp_path, monkeypatch):
    unit_dir = tmp_path / "systemd-user"
    unit_dir.mkdir()
    (unit_dir / install._SERVICE_UNIT).write_text("[Unit]\n", encoding="utf-8")
    monkeypatch.setenv("BRAINCELL_SYSTEMD_USER_DIR", str(unit_dir))
    return unit_dir


class TestServiceStatusFailureVisibility:
    def test_crash_looping_unit_reports_failing_and_reason(
        self, svc_dir, monkeypatch,
    ):
        def fake_systemctl(args):
            if args and args[0] == "show":
                return 0, (
                    "ActiveState=activating\nSubState=auto-restart\n"
                    "Result=exit-code\nNRestarts=845\n"
                )
            return 3, ""  # is-active / is-enabled: not active while looping

        monkeypatch.setattr(install, "_run_systemctl", fake_systemctl)
        monkeypatch.setattr(
            install, "_service_failure_reason",
            lambda: "FATAL: BrainCell embedding-space mismatch in …",
        )
        st = install.service_status()
        assert st["installed"] is True
        assert st["failing"] is True
        assert st["restarts"] == 845
        assert st["failure"].startswith("FATAL:")

    def test_failed_state_reports_failing(self, svc_dir, monkeypatch):
        def fake_systemctl(args):
            if args and args[0] == "show":
                return 0, (
                    "ActiveState=failed\nSubState=failed\n"
                    "Result=start-limit-hit\nNRestarts=5\n"
                )
            return 3, ""

        monkeypatch.setattr(install, "_run_systemctl", fake_systemctl)
        monkeypatch.setattr(install, "_service_failure_reason", lambda: "")
        st = install.service_status()
        assert st["failing"] is True
        assert st["result"] == "start-limit-hit"
        assert "failure" not in st  # empty reason → key absent, never ""

    def test_healthy_unit_is_not_failing(self, svc_dir, monkeypatch):
        def fake_systemctl(args):
            if args and args[0] == "show":
                return 0, (
                    "ActiveState=active\nSubState=running\n"
                    "Result=success\nNRestarts=0\n"
                )
            return 0, ""

        monkeypatch.setattr(install, "_run_systemctl", fake_systemctl)
        st = install.service_status()
        assert st["failing"] is False
        assert st["state"] == "active"
        assert "failure" not in st

    def test_faked_empty_show_degrades_to_original_shape(
        self, svc_dir, monkeypatch,
    ):
        """The pre-existing test fake returns (0, "") for everything — the new
        keys must simply be absent, never crash (the tests' seam contract)."""
        monkeypatch.setattr(install, "_run_systemctl", lambda args: (0, ""))
        st = install.service_status()
        assert set(st) == {"unit_path", "installed", "active", "enabled"}
