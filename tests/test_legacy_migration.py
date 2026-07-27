# SPDX-License-Identifier: AGPL-3.0-or-later
"""Preview-first legacy migration inventory and backup coverage."""

from __future__ import annotations

import json
import sqlite3

import pytest

from braincell.legacy_migration import (
    apply_legacy_migration,
    backup_legacy_database,
    inspect_legacy_database,
    write_migration_receipt,
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
            "INSERT INTO bc_chunks (document_id, chunk_index, chunk_text, chunk_hash) "
            "VALUES (1, 0, 'legacy chunk', X'01')"
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
    assert first[0].preserved_global_native == 0
    assert first[0].skipped_legacy_unclassified == 1
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


def test_apply_repairs_missing_chunks_for_an_already_migrated_document(tmp_path):
    source = tmp_path / "legacy.db"
    backup = tmp_path / "backup.db"
    _seed_legacy(source)
    backup_legacy_database(source, backup)
    apply_legacy_migration(source, backup, ["A"])

    destination = get_db_path("A")
    con = sqlite3.connect(destination)
    try:
        con.execute("DELETE FROM bc_chunks")
        con.commit()
    finally:
        con.close()

    repaired = apply_legacy_migration(source, backup, ["A"])
    assert repaired[0].documents_skipped == 1
    assert repaired[0].chunks_migrated == 1
    con = sqlite3.connect(destination)
    try:
        assert con.execute("SELECT chunk_text FROM bc_chunks").fetchone()[0] == "legacy chunk"
    finally:
        con.close()


def test_apply_writes_an_atomic_non_overwriting_recovery_receipt(tmp_path):
    source = tmp_path / "legacy.db"
    backup = tmp_path / "backup.db"
    receipt_path = tmp_path / "receipt.json"
    _seed_legacy(source)
    backup_legacy_database(source, backup)

    results = apply_legacy_migration(source, backup, ["A"])
    receipt = write_migration_receipt(
        source=source,
        backup=backup,
        results=results,
        destination=receipt_path,
    )

    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt.selected_project_ids == ["A"]
    assert payload["backup_sha256"]
    assert payload["results"][0]["project_id"] == "A"
    assert "bc_operations and bc_operation_notes remain" in payload["audit_trail"]
    with pytest.raises(FileExistsError):
        write_migration_receipt(
            source=source,
            backup=backup,
            results=results,
            destination=receipt_path,
        )


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
