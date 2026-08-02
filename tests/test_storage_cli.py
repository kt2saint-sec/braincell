# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
test_storage_cli.py — `braincell storage` CLI surface: the read-only
`--list-orphans` listing (BUGS.md orphan reconciliation) and the WAL-starvation
warning surfaced from the storage report (BUGS.md stats/storage diagnostics).
"""

from __future__ import annotations

import json

import pytest

from braincell.cli import main
from braincell.config import get_db_path, get_project_id
from braincell.project_registry import register_path
from braincell.store import SqliteStore


def test_list_orphans_needs_no_registered_project(tmp_path, capsys):
    """--list-orphans is workspace-wide; it must not require the cwd project
    to be registered (unlike the default storage report)."""
    main(["storage", str(tmp_path / "unregistered"), "--list-orphans"])
    out = json.loads(capsys.readouterr().out)
    assert out == {"orphaned_registry_entries": [], "orphaned_project_databases": []}


def test_list_orphans_reports_a_stale_registry_path(tmp_path, capsys):
    stale = tmp_path / "deleted-repo"
    stale.mkdir()
    register_path(str(stale), "01STORAGECLIAAAAAAAAAAAAA")
    stale.rmdir()

    main(["storage", ".", "--list-orphans"])
    out = json.loads(capsys.readouterr().out)
    assert {"path": str(stale), "project_id": "01STORAGECLIAAAAAAAAAAAAA"} in (
        out["orphaned_registry_entries"]
    )


def test_storage_report_warns_on_wal_starvation(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    project_id = get_project_id(root)
    db = get_db_path(project_id)
    SqliteStore(db).assert_schema_version()
    (db.parent / (db.name + "-wal")).write_bytes(b"0" * (11 * 1024 * 1024))

    main(["storage", str(root)])
    err = capsys.readouterr().err
    assert "WAL" in err and "checkpoint" in err.lower()


def test_storage_report_is_silent_when_wal_is_healthy(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    get_project_id(root)
    SqliteStore(get_db_path(get_project_id(root))).assert_schema_version()

    main(["storage", str(root)])
    err = capsys.readouterr().err
    assert "WAL" not in err


def test_hard_prune_cli_requires_preview_digest_and_final_phrase(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    project_id = get_project_id(root)
    database = get_db_path(project_id)
    SqliteStore(database).assert_schema_version()
    backup = database.parent / "braincell-backup-20200101.db"
    backup.write_bytes(b"eligible")

    with pytest.raises(SystemExit, match="reviewed plan changed"):
        main([
            "storage", str(root), "--hard-prune", "--keep-backups", "0", "--apply",
            "--confirm", "DELETE WITHOUT LOCAL RECOVERY SNAPSHOT",
        ])
    assert backup.exists()

    main(["storage", str(root), "--hard-prune", "--keep-backups", "0"])
    preview = json.loads(capsys.readouterr().out)
    assert preview["selection"]["unprotected_backup_paths"] == [str(backup)]

    main([
        "storage", str(root), "--hard-prune", "--keep-backups", "0", "--apply",
        "--approve", preview["approval_digest"],
        "--confirm", "DELETE WITHOUT LOCAL RECOVERY SNAPSHOT",
    ])
    result = json.loads(capsys.readouterr().out)
    assert result["applied"] is True
    assert not backup.exists()
