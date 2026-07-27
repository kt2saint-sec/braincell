# SPDX-License-Identifier: AGPL-3.0-or-later
"""Preview-first legacy migration inventory and backup coverage."""

from __future__ import annotations

import sqlite3

import pytest

from braincell.legacy_migration import (
    apply_legacy_migration,
    backup_legacy_database,
    inspect_legacy_database,
)
from braincell.config import get_db_path
from braincell.store import SqliteStore


def _seed_legacy(path):
    store = SqliteStore(path)
    store.assert_schema_version()
    store.close()
    con = sqlite3.connect(path)
    try:
        con.execute(
            "INSERT INTO memory_notes "
            "(project_id, scope, kind, content, note_uid, pooled_from, status) "
            "VALUES ('A', 'project', 'note', 'pooled', 'UID-A', 'A', 'active')"
        )
        con.execute(
            "INSERT INTO memory_notes "
            "(project_id, scope, kind, content, note_uid, status) "
            "VALUES ('A', 'project', 'note', 'native', 'UID-N', 'active')"
        )
        con.execute(
            "INSERT INTO memory_notes "
            "(project_id, scope, kind, content, note_uid, status) "
            "VALUES ('B', 'project', 'note', 'other', 'UID-B', 'active')"
        )
        con.execute(
            "INSERT INTO bc_documents "
            "(project_id, doc_key, title, pooled_from) VALUES ('A', 'a', 'A', 'A')"
        )
        con.execute(
            "INSERT INTO bc_documents (project_id, doc_key, title) VALUES ('B', 'b', 'B')"
        )
        con.execute(
            "INSERT INTO bc_note_links (src_id, dst_id, kind) VALUES (1, 2, 'related')"
        )
        con.execute(
            "INSERT INTO bc_operations (kind, project_id) VALUES ('reflect', 'A')"
        )
        con.execute(
            "INSERT INTO bc_operation_notes (op_id, note_id, action) VALUES (1, 99, 'created')"
        )
        con.commit()
    finally:
        con.close()


def test_inventory_is_read_only_and_classifies_provenance(tmp_path):
    source = tmp_path / "legacy.db"
    _seed_legacy(source)

    before = source.read_bytes()
    report = inspect_legacy_database(source)

    assert report.readable is True
    assert report.quick_check == "ok"
    assert report.counts["memory_notes"] == 3
    assert report.pooled_rows["memory_notes"] == 1
    assert report.ambiguous_rows["memory_notes"] == 2
    assert report.pooled_rows["bc_documents"] == 1
    assert report.ambiguous_rows["bc_documents"] == 1
    assert report.project_ids == ["A", "B"]
    assert report.link_rows == 1
    assert report.operation_rows == 1
    assert report.operation_note_rows == 1
    assert report.missing_operation_notes == 1
    assert source.read_bytes() == before


def test_backup_is_verified_and_never_overwrites(tmp_path):
    source = tmp_path / "legacy.db"
    destination = tmp_path / "backup.db"
    _seed_legacy(source)

    result = backup_legacy_database(source, destination)

    assert result.verified is True
    assert result.bytes == destination.stat().st_size
    assert result.inventory.quick_check == "ok"
    assert result.inventory.counts == inspect_legacy_database(source).counts
    with pytest.raises(FileExistsError):
        backup_legacy_database(source, destination)


def test_missing_source_is_safe(tmp_path):
    report = inspect_legacy_database(tmp_path / "missing.db")
    assert report.readable is False
    assert report.warnings == ["legacy database does not exist"]


def test_apply_migrates_only_provenance_rows_and_is_idempotent(tmp_path):
    source = tmp_path / "legacy.db"
    backup = tmp_path / "backup.db"
    _seed_legacy(source)
    backup_legacy_database(source, backup)

    first = apply_legacy_migration(source, backup, ["A"])
    assert first[0].notes_migrated == 1
    assert first[0].documents_migrated == 1
    assert first[0].skipped_legacy_unclassified == 3
    destination = get_db_path("A")
    con = sqlite3.connect(destination)
    try:
        assert con.execute("SELECT COUNT(*) FROM memory_notes").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM bc_documents").fetchone()[0] == 1
    finally:
        con.close()

    second = apply_legacy_migration(source, backup, ["A"])
    assert second[0].notes_migrated == 0
    assert second[0].documents_migrated == 0
    assert second[0].notes_skipped == 1
    assert second[0].documents_skipped == 1


def test_apply_rolls_back_destination_on_failure(tmp_path):
    source = tmp_path / "legacy.db"
    backup = tmp_path / "backup.db"
    _seed_legacy(source)
    backup_legacy_database(source, backup)

    with pytest.raises(RuntimeError, match="injected migration failure"):
        apply_legacy_migration(source, backup, ["A"], failure_after=1)
    destination = get_db_path("A")
    con = sqlite3.connect(destination)
    try:
        assert con.execute("SELECT COUNT(*) FROM bc_documents").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM memory_notes").fetchone()[0] == 0
    finally:
        con.close()
