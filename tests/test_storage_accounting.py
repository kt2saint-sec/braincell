# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""Accounting stays read-only; retention apply is explicit, locked, fail-closed."""

from __future__ import annotations

import os
import sqlite3

import pytest


def _bootstrapped_project(tmp_path):
    """Mint an isolated project + schema'd database; return (project_id, db)."""
    from braincell.config import get_db_path, get_project_id
    from braincell.store import SqliteStore

    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    project_id = get_project_id(project)
    database = get_db_path(project_id)
    store = SqliteStore(database)
    store.assert_schema_version()
    store.close()
    return project_id, database


def _record_operation(database, backup_path=None, *, created_at=None, note_ids=()):
    """Insert one bc_operations row (optionally aged) plus undo-history notes."""
    con = sqlite3.connect(str(database))
    try:
        cur = con.execute(
            "INSERT INTO bc_operations(kind, project_id, backup_path, created_at) "
            "VALUES ('consolidate', 'proj', ?, COALESCE(?, datetime('now')))",
            (backup_path, created_at),
        )
        op_id = cur.lastrowid
        for note_id in note_ids:
            con.execute(
                "INSERT INTO bc_operation_notes(op_id, note_id, action) "
                "VALUES (?, ?, 'tombstoned')",
                (op_id, note_id),
            )
        con.commit()
        return op_id
    finally:
        con.close()


def _insert_note(database, note_id, *, status, deleted_at=None):
    con = sqlite3.connect(str(database))
    try:
        con.execute(
            "INSERT INTO memory_notes(id, project_id, kind, content, status, "
            "deleted_at, note_uid) VALUES (?, 'proj', 'note', ?, ?, ?, ?)",
            (note_id, f"note body {note_id}", status, deleted_at, f"uid-{note_id}"),
        )
        con.execute(
            "INSERT INTO memory_fts(rowid, content) VALUES (?, ?)",
            (note_id, f"note body {note_id}"),
        )
        con.commit()
    finally:
        con.close()


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


class TestUndoReferencedSnapshotProtection:
    """BC-23: snapshots referenced by undo history are never deletion candidates."""

    def test_plan_lists_referenced_snapshot_as_protected(self, tmp_path):
        from braincell.storage_accounting import storage_report

        project_id, database = _bootstrapped_project(tmp_path)
        referenced = database.parent / "braincell-preconsolidate-20260101.db"
        unreferenced = database.parent / "braincell-backup-20260102.db"
        referenced.write_bytes(b"r")
        unreferenced.write_bytes(b"u")
        os.utime(referenced, ns=(1, 1))
        os.utime(unreferenced, ns=(2, 2))
        _record_operation(database, str(referenced))

        report = storage_report(project_id, keep_backups=0)

        candidate_paths = {
            item["path"] for item in report["retention_plan"]["candidates"]
        }
        protected = report["retention_plan"]["protected"]
        assert str(unreferenced) in candidate_paths
        assert str(referenced) not in candidate_paths
        assert protected == [
            {
                "path": str(referenced),
                "bytes": 1,
                "reason": "referenced-by-undo-history",
            }
        ]

    def test_apply_deletes_candidates_and_keeps_referenced(self, tmp_path):
        from braincell.storage_accounting import apply_retention

        project_id, database = _bootstrapped_project(tmp_path)
        referenced = database.parent / "braincell-preconsolidate-20260101.db"
        unreferenced = database.parent / "braincell-backup-20260102.db"
        referenced.write_bytes(b"r")
        unreferenced.write_bytes(b"u")
        os.utime(referenced, ns=(1, 1))
        os.utime(unreferenced, ns=(2, 2))
        _record_operation(database, str(referenced))

        result = apply_retention(project_id, keep_backups=0)

        assert not unreferenced.exists()
        assert referenced.exists()
        assert result["removed_backups"] == [str(unreferenced)]
        assert result["protected"][0]["path"] == str(referenced)

    def test_apply_fails_closed_when_history_is_unreadable(self, tmp_path):
        from braincell.storage_accounting import (
            RetentionRefusedError,
            apply_retention,
        )

        project_id, database = _bootstrapped_project(tmp_path)
        backup = database.parent / "braincell-backup-20260101.db"
        backup.write_bytes(b"b")
        database.write_bytes(b"this is not a sqlite database at all")

        with pytest.raises(RetentionRefusedError, match="unreadable"):
            apply_retention(project_id, keep_backups=0)
        assert backup.exists()


class TestExplicitRetentionApply:
    """BC-12: expiry is opt-in, dry-run-first, and excluded by the mutation lock."""

    def test_apply_refuses_without_any_configured_axis(self, tmp_path):
        from braincell.storage_accounting import (
            RetentionRefusedError,
            apply_retention,
        )

        project_id, _database = _bootstrapped_project(tmp_path)
        with pytest.raises(RetentionRefusedError, match="no retention axis"):
            apply_retention(project_id)

    def test_apply_is_excluded_by_the_destination_mutation_lock(self, tmp_path):
        from braincell.catalog_io import MutationBusyError, mutation_lock
        from braincell.storage_accounting import apply_retention

        project_id, database = _bootstrapped_project(tmp_path)
        with mutation_lock(database, operation="test-holder"):
            with pytest.raises(MutationBusyError):
                apply_retention(project_id, keep_backups=0)

    def test_operation_expiry_removes_rows_but_not_the_snapshot_this_run(
        self, tmp_path
    ):
        """Expiring an operation and pruning backups in ONE run must not delete
        the snapshot that operation still referenced at plan time; the file
        becomes an ordinary candidate only on the NEXT run."""
        from braincell.storage_accounting import apply_retention

        project_id, database = _bootstrapped_project(tmp_path)
        snapshot = database.parent / "braincell-preconsolidate-20200101.db"
        snapshot.write_bytes(b"s")
        _insert_note(database, 11, status="tombstoned", deleted_at="2020-01-01 00:00:00")
        _record_operation(
            database, str(snapshot), created_at="2020-01-01 00:00:00", note_ids=(11,)
        )

        first = apply_retention(
            project_id, keep_backups=0, expire_operations_days=30
        )
        assert first["operations_expired"] == 1
        assert snapshot.exists()
        assert first["removed_backups"] == []
        con = sqlite3.connect(str(database))
        try:
            assert con.execute("SELECT COUNT(*) FROM bc_operations").fetchone()[0] == 0
            assert (
                con.execute("SELECT COUNT(*) FROM bc_operation_notes").fetchone()[0]
                == 0
            )
        finally:
            con.close()

        second = apply_retention(project_id, keep_backups=0)
        assert not snapshot.exists()
        assert second["removed_backups"] == [str(snapshot)]

    def test_tombstone_purge_protects_undo_referenced_notes(self, tmp_path):
        from braincell.storage_accounting import apply_retention, storage_report

        project_id, database = _bootstrapped_project(tmp_path)
        _insert_note(database, 1, status="tombstoned", deleted_at="2020-01-01 00:00:00")
        _insert_note(database, 2, status="tombstoned", deleted_at="2020-01-02 00:00:00")
        _insert_note(database, 3, status="active")
        _record_operation(database, note_ids=(2,))

        plan = storage_report(project_id, expire_tombstones_days=30)
        history = plan["retention_plan"]["history"]
        assert history["tombstoned_notes"] == [1]
        assert history["protected_notes"] == [2]

        result = apply_retention(project_id, expire_tombstones_days=30)
        assert result["notes_purged"] == 1
        assert result["protected_notes"] == [2]

        con = sqlite3.connect(str(database))
        try:
            remaining = {
                row[0]
                for row in con.execute("SELECT id FROM memory_notes").fetchall()
            }
            assert remaining == {2, 3}
            fts_rows = {
                row[0]
                for row in con.execute("SELECT rowid FROM memory_fts").fetchall()
            }
            assert 1 not in fts_rows
        finally:
            con.close()

    def test_fresh_tombstones_are_not_planned(self, tmp_path):
        from braincell.storage_accounting import storage_report

        project_id, database = _bootstrapped_project(tmp_path)
        _insert_note(database, 5, status="tombstoned", deleted_at="2020-01-01 00:00:00")

        # A large window keeps even the 2020 tombstone out of the plan.
        plan = storage_report(project_id, expire_tombstones_days=36500)
        assert plan["retention_plan"]["history"]["tombstoned_notes"] == []
