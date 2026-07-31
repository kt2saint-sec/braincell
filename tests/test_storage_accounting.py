# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""Persistent-state accounting is read-only and retention is plan-only."""

from __future__ import annotations

import os


def test_storage_report_accounts_and_plans_without_deleting(tmp_path):
    from braincell.config import get_db_path, get_project_id
    from braincell.storage_accounting import storage_report
    from braincell.store import SqliteStore

    project = tmp_path / "project"
    project.mkdir()
    project_id = get_project_id(project)
    database = get_db_path(project_id)
    SqliteStore(database).assert_schema_version()
    first = database.parent / "braincell-backup-20260101.db"
    second = database.parent / "braincell-backup-20260102.db"
    first.write_bytes(b"a")
    second.write_bytes(b"bb")
    os.utime(first, ns=(1, 1))
    os.utime(second, ns=(2, 2))

    report = storage_report(project_id, keep_backups=1)

    assert report["categories"]["backups"] == {"files": 2, "bytes": 3}
    assert report["retention_plan"]["dry_run"] is True
    assert report["retention_plan"]["candidates"] == [
        {"path": str(first), "bytes": 1}
    ]
    assert first.exists() and second.exists()


def test_storage_report_includes_auto_and_external_recovery_snapshots(tmp_path):
    from braincell.config import get_project_id
    from braincell.storage_accounting import storage_report

    project = tmp_path / "project"
    project.mkdir()
    project_id = get_project_id(project)
    state = tmp_path / "external"
    state.mkdir()
    snapshots = [
        state / "braincell-preconsolidate-20260101.db",
        state / "braincell-prereflect-20260102.db",
        state / "legacy-backup-20260103.db",
        state / "destination-backup-20260104.db",
    ]
    for snapshot in snapshots:
        snapshot.write_bytes(b"x")

    report = storage_report(
        project_id, keep_backups=0, backup_roots=[state]
    )

    assert report["categories"]["backups"]["files"] >= 4
    candidates = {item["path"] for item in report["retention_plan"]["candidates"]}
    assert {str(item.resolve()) for item in snapshots} <= candidates
