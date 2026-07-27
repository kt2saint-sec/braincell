# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cleanup-only coverage for the retired braincell-map.service."""

from __future__ import annotations

import argparse
from pathlib import Path

from fastapi.testclient import TestClient


def _unit_dir(tmp_path: Path, monkeypatch) -> Path:
    unit_dir = tmp_path / "systemd" / "user"
    monkeypatch.setenv("BRAINCELL_SYSTEMD_USER_DIR", str(unit_dir))
    return unit_dir


def test_status_reports_legacy_residue(tmp_path, monkeypatch):
    from braincell import legacy_service

    unit_dir = _unit_dir(tmp_path, monkeypatch)
    unit_dir.mkdir(parents=True)
    (unit_dir / legacy_service.UNIT_NAME).write_text("[Unit]\n")
    monkeypatch.setattr(
        legacy_service,
        "_systemctl",
        lambda args: (0, "") if args[0] == "is-enabled" else (3, ""),
    )
    result = legacy_service.status()
    assert result["installed"] is True
    assert result["enabled"] is True
    assert result["active"] is False


def test_remove_is_exact_and_idempotent(tmp_path, monkeypatch):
    from braincell import legacy_service

    unit_dir = _unit_dir(tmp_path, monkeypatch)
    unit_dir.mkdir(parents=True)
    unit = unit_dir / legacy_service.UNIT_NAME
    unit.write_text("[Unit]\n")
    calls: list[list[str]] = []

    def fake_systemctl(args):
        calls.append(args)
        return (3, "inactive") if args[0].startswith("is-") else (0, "")

    monkeypatch.setattr(legacy_service, "_systemctl", fake_systemctl)
    first = legacy_service.remove()
    second = legacy_service.remove()
    assert first["removed"] is True
    assert second["removed"] is False
    assert not unit.exists()
    assert ["disable", "--now", legacy_service.UNIT_NAME] in calls
    assert ["daemon-reload"] in calls
    assert ["reset-failed", legacy_service.UNIT_NAME] in calls


def test_remove_preserves_unit_when_stop_fails(tmp_path, monkeypatch):
    from braincell import legacy_service

    unit_dir = _unit_dir(tmp_path, monkeypatch)
    unit_dir.mkdir(parents=True)
    unit = unit_dir / legacy_service.UNIT_NAME
    unit.write_text("[Unit]\n")

    def fake_systemctl(args):
        if args[0] == "disable":
            return 1, "failed to connect to user manager"
        return 3, "inactive"

    monkeypatch.setattr(legacy_service, "_systemctl", fake_systemctl)
    result = legacy_service.remove()
    assert result["removed"] is False
    assert result["installed"] is True
    assert unit.exists()
    assert "left intact" in result["detail"]


def test_cli_remove_does_not_touch_mcp(tmp_path, monkeypatch, capsys):
    from braincell import legacy_service
    from braincell.cli import cmd_legacy_service

    _unit_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(
        legacy_service,
        "remove",
        lambda: {
            "removed": True,
            "unit_path": "/legacy/braincell-map.service",
            "detail": "",
        },
    )
    cmd_legacy_service(argparse.Namespace(legacy_service_cmd="remove"))
    assert "removed retired GUI service" in capsys.readouterr().out


def test_normal_start_preflight_never_probes_legacy_service(tmp_path, monkeypatch):
    from braincell import launch, legacy_service

    monkeypatch.setattr(
        legacy_service,
        "status",
        lambda: (_ for _ in ()).throw(AssertionError("normal preflight must not run systemctl")),
    )
    result = launch.preflight(tmp_path)
    assert result.action == "launch"


def test_service_install_surface_is_absent(tmp_path):
    from braincell import install
    from braincell.gui import create_app
    from braincell.gui_template import INDEX_HTML

    assert not hasattr(install, "install_service")
    assert not hasattr(install, "service_status")
    assert "service-btn" not in INDEX_HTML
    with TestClient(
        create_app(db_path=tmp_path / "braincell.db", allow_writes=True)
    ) as client:
        assert client.post("/api/service", json={"action": "status"}).status_code == 404
