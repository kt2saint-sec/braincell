# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""Accounting stays read-only; retention apply is explicit, locked, fail-closed."""

from __future__ import annotations

import os
import sqlite3
from collections import namedtuple

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
        with (
            mutation_lock(database, operation="test-holder"),
            pytest.raises(MutationBusyError),
        ):
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


class TestDatabaseDiagnostics:
    """BUGS.md stats/storage diagnostics: freelist, embedding, foreign-document,
    and WAL-starvation detail, all read-only additions to storage_report()."""

    def test_reports_freelist_and_embedding_detail(self, tmp_path):
        import asyncio

        from braincell.storage_accounting import storage_report
        from braincell.store import SqliteStore
        from tests.conftest import _insert_doc_and_chunk

        project_id, database = _bootstrapped_project(tmp_path)
        store = SqliteStore(database)

        async def _seed():
            await _insert_doc_and_chunk(
                store, project=project_id, doc_key="d1", text="hello"
            )
            await store.aclose()

        asyncio.run(_seed())

        report = storage_report(project_id)
        diag = report["database_diagnostics"]
        assert diag["page_count"] is not None
        assert diag["freelist_pages"] is not None
        assert diag["embedding"]["chunks_embedded"] == 1
        assert diag["embedding"]["embedding_bytes"] > 0
        assert diag["foreign_documents"] == 0

    def test_foreign_owned_documents_are_counted_not_deleted(self, tmp_path):
        """A doc row whose project_id differs from the report's project is
        surfaced as a count only — storage_report never touches rows."""
        from braincell.storage_accounting import storage_report

        project_id, database = _bootstrapped_project(tmp_path)
        con = sqlite3.connect(str(database))
        try:
            con.execute(
                "INSERT INTO bc_documents(project_id, doc_key, title, "
                "content_hash, content_type) VALUES ('other-project', 'k', "
                "'t', X'00', 'cell')"
            )
            con.commit()
        finally:
            con.close()

        report = storage_report(project_id)
        assert report["database_diagnostics"]["foreign_documents"] == 1

        # Read-only: the foreign row is still there afterward.
        con = sqlite3.connect(str(database))
        try:
            count = con.execute(
                "SELECT COUNT(*) FROM bc_documents WHERE project_id='other-project'"
            ).fetchone()[0]
        finally:
            con.close()
        assert count == 1

    def test_wal_starvation_flagged_past_the_ratio_and_floor(self, tmp_path):
        from braincell.storage_accounting import storage_report

        project_id, database = _bootstrapped_project(tmp_path)
        wal = database.parent / (database.name + "-wal")
        # Small WAL relative to a tiny db: below the byte floor, never flagged.
        wal.write_bytes(b"0" * (1024 * 1024))
        assert storage_report(project_id)["database_diagnostics"]["wal"]["starved"] is False

        # Past both the floor and the ratio against the tiny bootstrapped db.
        wal.write_bytes(b"0" * (11 * 1024 * 1024))
        report = storage_report(project_id)
        assert report["database_diagnostics"]["wal"]["starved"] is True
        assert report["database_diagnostics"]["wal"]["wal_bytes"] == 11 * 1024 * 1024

    def test_missing_database_returns_empty_diagnostics_not_a_crash(self, tmp_path):
        from braincell.storage_accounting import storage_report

        project_id = "01NEVERBUILTAAAAAAAAAAAAAA"
        report = storage_report(project_id)
        diag = report["database_diagnostics"]
        assert diag["page_count"] is None
        assert diag["embedding"]["chunks_embedded"] == 0
        assert diag["wal"]["starved"] is False

    def test_storage_report_includes_conservative_snapshot_and_compaction_impact(
        self, tmp_path, monkeypatch
    ):
        from braincell import storage_accounting
        from braincell.storage_accounting import storage_report

        project_id, database = _bootstrapped_project(tmp_path)
        wal = database.parent / (database.name + "-wal")
        wal.write_bytes(b"w" * 100)
        DiskUsage = namedtuple("DiskUsage", "total used free")
        monkeypatch.setattr(
            storage_accounting.shutil,
            "disk_usage",
            lambda _path: DiskUsage(total=10_000, used=9_500, free=500),
        )

        report = storage_report(project_id)

        impact = report["storage_impact"]
        source_bytes = database.stat().st_size + 100
        assert impact["filesystem"] == {
            "total_bytes": 10_000,
            "used_bytes": 9_500,
            "free_bytes": 500,
        }
        assert impact["local_snapshot"] == {
            "estimated_retained_bytes": source_bytes,
            "fits_available_space": source_bytes <= 500,
        }
        assert impact["compaction"]["conservative_temporary_bytes"] == source_bytes * 2
        assert impact["compaction"]["estimated_reclaimable_bytes"] == (
            report["database_diagnostics"]["freelist_bytes"]
        )
        assert impact["memory_estimate_bytes"] is None
        assert "cannot be reliably estimated" in impact["memory_notice"]


class TestOrphansSurfacedInStorageReport:
    def test_storage_report_includes_the_orphan_inventory(self, tmp_path):
        from braincell.project_registry import register_path
        from braincell.storage_accounting import storage_report

        project_id, _database = _bootstrapped_project(tmp_path)

        stale_root = tmp_path / "deleted-repo"
        stale_root.mkdir()
        register_path(str(stale_root), "01ORPHANAAAAAAAAAAAAAAAAAA")
        stale_root.rmdir()

        report = storage_report(project_id)
        orphaned = report["orphans"]["orphaned_registry_entries"]
        assert {"path": str(stale_root), "project_id": "01ORPHANAAAAAAAAAAAAAAAAAA"} in orphaned
