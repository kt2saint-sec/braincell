# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
test_service_install.py — opt-in systemd --user "always-on Map" service.

Covers braincell/install.py {install,uninstall}_service + service_status and the
GUI /api/service endpoint. All systemctl calls are faked (monkeypatched
_run_systemctl) so the real user manager is never touched, and the unit dir is
redirected via BRAINCELL_SYSTEMD_USER_DIR.
"""

from __future__ import annotations

import sys

import pytest
from fastapi.testclient import TestClient

from braincell import install


@pytest.fixture
def svc_env(tmp_path, monkeypatch):
    """Redirect the unit dir to tmp and record (never run) systemctl calls."""
    unit_dir = tmp_path / "systemd-user"
    monkeypatch.setenv("BRAINCELL_SYSTEMD_USER_DIR", str(unit_dir))
    calls: list[list[str]] = []

    def fake_systemctl(args):
        calls.append(args)
        # is-active/is-enabled report "active"/"enabled" once the unit exists.
        if args and args[0] in ("is-active", "is-enabled"):
            return (0 if (unit_dir / install._SERVICE_UNIT).exists() else 3), ""
        return 0, ""

    monkeypatch.setattr(install, "_run_systemctl", fake_systemctl)
    return unit_dir, calls


class TestUnitText:
    def test_execstart_is_absolute_global_writable_no_browser(self):
        unit = install._service_unit_text(8765, "braincell")
        assert f"ExecStart={sys.executable} -m braincell.cli gui" in unit
        assert "--mode global" in unit
        assert "--allow-writes" in unit
        assert "--no-browser" in unit
        assert "--port 8765" in unit
        assert "Environment=BRAINCELL_DATA_NAMESPACE=braincell" in unit
        assert "Restart=on-failure" in unit
        assert "WantedBy=default.target" in unit

    def test_port_is_honoured(self):
        assert "--port 9001" in install._service_unit_text(9001, "ns")


class TestInstallService:
    def test_writes_unit_enables_and_reports_active(self, svc_env):
        unit_dir, calls = svc_env
        res = install.install_service(port=8765)
        unit = unit_dir / install._SERVICE_UNIT
        assert unit.exists()
        assert res["installed"] is True
        assert res["active"] is True and res["enabled"] is True
        # daemon-reload THEN enable --now were issued.
        assert ["daemon-reload"] in calls
        assert ["enable", "--now", install._SERVICE_UNIT] in calls

    def test_idempotent_rewrite(self, svc_env):
        install.install_service()
        res = install.install_service()  # second run must not error, still installed
        assert res["installed"] is True

    def test_systemctl_failure_reported_not_raised(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BRAINCELL_SYSTEMD_USER_DIR", str(tmp_path / "sd"))
        monkeypatch.setattr(install, "_run_systemctl",
                            lambda args: (1, "Failed to connect to bus"))
        res = install.install_service()
        assert res["installed"] is True          # unit file still written
        assert "Failed to connect to bus" in res["detail"]
        assert res["active"] is False


class TestUninstallService:
    def test_removes_unit(self, svc_env):
        unit_dir, _ = svc_env
        install.install_service()
        assert (unit_dir / install._SERVICE_UNIT).exists()
        res = install.uninstall_service()
        assert res["removed"] is True
        assert not (unit_dir / install._SERVICE_UNIT).exists()
        assert res["installed"] is False

    def test_uninstall_when_absent_is_noop(self, svc_env):
        res = install.uninstall_service()
        assert res["removed"] is False


class TestServiceStatus:
    def test_reflects_unit_presence(self, svc_env):
        assert install.service_status()["installed"] is False
        install.install_service()
        assert install.service_status()["installed"] is True


# ── GUI endpoint + toolbar button ──────────────────────────────────────────────

def _writable_app(tmp_path):
    from braincell.gui import create_app
    return create_app(db_path=tmp_path / "braincell.db", allow_writes=True,
                      auth_token="s3cret")


class TestServiceEndpoint:
    def test_status_action(self, tmp_path, svc_env):
        with TestClient(_writable_app(tmp_path)) as client:
            r = client.post("/api/service?t=s3cret", json={"action": "status"})
        assert r.status_code == 200
        assert r.json()["installed"] is False

    def test_install_then_uninstall(self, tmp_path, svc_env):
        unit_dir, _ = svc_env
        with TestClient(_writable_app(tmp_path)) as client:
            r = client.post("/api/service?t=s3cret", json={"action": "install"})
            assert r.status_code == 200 and r.json()["installed"] is True
            assert (unit_dir / install._SERVICE_UNIT).exists()
            r2 = client.post("/api/service?t=s3cret", json={"action": "uninstall"})
            assert r2.status_code == 200 and r2.json()["installed"] is False

    def test_rejects_unknown_action(self, tmp_path, svc_env):
        with TestClient(_writable_app(tmp_path)) as client:
            r = client.post("/api/service?t=s3cret", json={"action": "bogus"})
        assert r.status_code == 422  # Literal enum rejects it

    def test_endpoint_absent_when_read_only(self, tmp_path):
        from braincell.gui import create_app
        app = create_app(db_path=tmp_path / "braincell.db", allow_writes=False,
                         auth_token="s3cret")
        with TestClient(app) as client:
            r = client.post("/api/service?t=s3cret", json={"action": "status"})
        assert r.status_code == 404  # install API not mounted in read-only

    def test_toolbar_has_service_toggle(self):
        from braincell.gui_template import INDEX_HTML
        assert 'id="service-btn"' in INDEX_HTML
        assert 'onclick="toggleService()"' in INDEX_HTML
