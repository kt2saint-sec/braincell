# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
test_foreign_document_reconciliation.py — regression tests for BUGS.md
"foreign transcript cleanup": a preview-only inventory of `bc_documents` rows
sitting in one Project's database under a DIFFERENT Project's identity, plus
an explicit opt-in migration of those rows into their true owners' own
databases (`transcript_ingest.preview_foreign_documents` /
`apply_foreign_document_migration`).

Mirrors the house pattern from test_legacy_recovery.py and
test_project_orphans.py: fault injection (stale approval digest, unregistered
owner, destination conflict) and adversarial non-mutation checks (a refused
apply must leave the source database byte-identical).
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3

import pytest

from tests.conftest import _insert_doc_and_chunk


def _seed_source(tmp_path):
    """One project database ('01SOURCE') holding: its own native document, one
    row foreign-owned by a REGISTERED sibling ('01OWNER'), and one row
    foreign-owned by an UNREGISTERED identity ('01GHOST')."""
    from braincell.config import get_db_path
    from braincell.project_registry import register_path
    from braincell.store import SqliteStore

    for project_id in ("01SOURCE", "01OWNER"):
        root = tmp_path / project_id
        root.mkdir()
        register_path(root, project_id)

    database = get_db_path("01SOURCE")
    store = SqliteStore(database)
    store.assert_schema_version()

    async def seed():
        await _insert_doc_and_chunk(
            store, project="01SOURCE", doc_key="native-doc",
            text="lives here natively", seed=1,
        )
        await _insert_doc_and_chunk(
            store, project="01OWNER", doc_key="owner-doc",
            text="belongs to 01OWNER", seed=2,
        )
        await _insert_doc_and_chunk(
            store, project="01GHOST", doc_key="ghost-doc",
            text="belongs to nobody registered", seed=3,
        )

    asyncio.run(seed())
    store.close()
    return database


def _row_bytes(path):
    return path.read_bytes()


class TestPreviewForeignDocuments:
    def test_no_database_is_an_empty_valid_report(self, tmp_path):
        from braincell.config import get_db_path
        from braincell.project_registry import register_path
        from braincell.transcript_ingest import preview_foreign_documents

        root = tmp_path / "01EMPTY"
        root.mkdir()
        register_path(root, "01EMPTY")
        report = preview_foreign_documents("01EMPTY")
        assert report["owners"] == {}
        assert report["unattributable_owners"] == []
        assert len(report["approval_digest"]) == 64
        assert not get_db_path("01EMPTY").exists()

    def test_native_rows_are_never_listed_as_foreign(self, tmp_path):
        from braincell.transcript_ingest import preview_foreign_documents

        _seed_source(tmp_path)
        report = preview_foreign_documents("01SOURCE")
        for owner_detail in report["owners"].values():
            assert all(doc["doc_key"] != "native-doc" for doc in owner_detail["documents"])

    def test_classifies_migratable_vs_unattributable_owners(self, tmp_path):
        from braincell.transcript_ingest import preview_foreign_documents

        _seed_source(tmp_path)
        report = preview_foreign_documents("01SOURCE")

        assert set(report["owners"]) == {"01OWNER", "01GHOST"}
        assert report["owners"]["01OWNER"]["migratable"] is True
        assert report["owners"]["01GHOST"]["migratable"] is False
        assert report["unattributable_owners"] == ["01GHOST"]
        assert [doc["doc_key"] for doc in report["owners"]["01OWNER"]["documents"]] == ["owner-doc"]
        assert report["owners"]["01OWNER"]["conflicts"] == []

    def test_preview_never_mutates_the_source_database(self, tmp_path):
        """Read-only by construction: byte-identical before/after preview."""
        from braincell.transcript_ingest import preview_foreign_documents

        database = _seed_source(tmp_path)
        before = _row_bytes(database)
        preview_foreign_documents("01SOURCE")
        assert _row_bytes(database) == before

    def test_destination_content_conflict_is_flagged(self, tmp_path):
        from braincell.config import get_db_path
        from braincell.transcript_ingest import preview_foreign_documents

        database = _seed_source(tmp_path)
        # Poison 01OWNER's OWN database with a same-key, different-content row —
        # a real destination collision the apply step must refuse to touch.
        from braincell.store import SqliteStore

        owner_db = get_db_path("01OWNER")
        owner_store = SqliteStore(owner_db)
        owner_store.assert_schema_version()
        owner_store.close()
        connection = sqlite3.connect(owner_db)
        connection.execute(
            "INSERT INTO bc_documents (project_id,doc_key,title,content_hash,content_type) "
            "VALUES (?,?,?,?,?)",
            ("01OWNER", "owner-doc", "owner-doc", hashlib.sha256(b"different").digest(), "cell"),
        )
        connection.commit()
        connection.close()

        report = preview_foreign_documents("01SOURCE")
        assert report["owners"]["01OWNER"]["conflicts"] == [{"doc_key": "owner-doc"}]
        assert _row_bytes(database)  # source itself still untouched by preview


class TestApplyForeignDocumentMigration:
    def test_requires_at_least_one_owner(self, tmp_path):
        from braincell.transcript_ingest import (
            ForeignDocumentReconciliationError,
            apply_foreign_document_migration,
        )

        _seed_source(tmp_path)
        with pytest.raises(ForeignDocumentReconciliationError, match="at least one"):
            apply_foreign_document_migration(
                "01SOURCE", owner_project_ids=[], approval_digest="whatever",
            )

    def test_stale_approval_digest_is_refused(self, tmp_path):
        from braincell.transcript_ingest import (
            ForeignDocumentReconciliationError,
            apply_foreign_document_migration,
        )

        database = _seed_source(tmp_path)
        before = _row_bytes(database)
        with pytest.raises(ForeignDocumentReconciliationError, match="digest"):
            apply_foreign_document_migration(
                "01SOURCE", owner_project_ids=["01OWNER"], approval_digest="wrong",
            )
        assert _row_bytes(database) == before

    def test_unattributable_owner_is_always_refused(self, tmp_path):
        from braincell.transcript_ingest import (
            ForeignDocumentReconciliationError,
            apply_foreign_document_migration,
            preview_foreign_documents,
        )

        database = _seed_source(tmp_path)
        before = _row_bytes(database)
        report = preview_foreign_documents("01SOURCE")
        with pytest.raises(ForeignDocumentReconciliationError, match="unattributable"):
            apply_foreign_document_migration(
                "01SOURCE",
                owner_project_ids=["01GHOST"],
                approval_digest=report["approval_digest"],
            )
        # Fail-closed: an unattributable owner in the selection refuses the
        # WHOLE apply, including the migratable owner in the same call.
        assert _row_bytes(database) == before

    def test_conflicted_owner_is_refused_and_source_untouched(self, tmp_path):
        from braincell.config import get_db_path
        from braincell.transcript_ingest import (
            ForeignDocumentReconciliationError,
            apply_foreign_document_migration,
            preview_foreign_documents,
        )

        database = _seed_source(tmp_path)
        from braincell.store import SqliteStore

        owner_db = get_db_path("01OWNER")
        owner_store = SqliteStore(owner_db)
        owner_store.assert_schema_version()
        owner_store.close()
        connection = sqlite3.connect(owner_db)
        connection.execute(
            "INSERT INTO bc_documents (project_id,doc_key,title,content_hash,content_type) "
            "VALUES (?,?,?,?,?)",
            ("01OWNER", "owner-doc", "owner-doc", hashlib.sha256(b"different").digest(), "cell"),
        )
        connection.commit()
        connection.close()
        owner_before = _row_bytes(owner_db)
        source_before = _row_bytes(database)

        report = preview_foreign_documents("01SOURCE")
        with pytest.raises(ForeignDocumentReconciliationError, match="conflict"):
            apply_foreign_document_migration(
                "01SOURCE",
                owner_project_ids=["01OWNER"],
                approval_digest=report["approval_digest"],
            )
        assert _row_bytes(database) == source_before
        assert _row_bytes(owner_db) == owner_before

    def test_migrates_documents_and_chunks_and_removes_them_from_source(self, tmp_path):
        from braincell.config import get_db_path
        from braincell.transcript_ingest import (
            apply_foreign_document_migration,
            preview_foreign_documents,
        )

        database = _seed_source(tmp_path)
        report = preview_foreign_documents("01SOURCE")

        result = apply_foreign_document_migration(
            "01SOURCE",
            owner_project_ids=["01OWNER"],
            approval_digest=report["approval_digest"],
        )

        assert result["migrated"]["01OWNER"] == {
            "documents_migrated": 1,
            "chunks_migrated": 1,
            "documents_removed_from_source": 1,
        }
        # A pre-mutation backup of the SOURCE was retained before deletion.
        backup_path = database.parent / result["source_backup"].rsplit("/", 1)[-1]
        assert backup_path.is_file()

        # Removed from source.
        with sqlite3.connect(database) as connection:
            remaining = connection.execute(
                "SELECT project_id, doc_key FROM bc_documents ORDER BY project_id"
            ).fetchall()
        assert remaining == [("01GHOST", "ghost-doc"), ("01SOURCE", "native-doc")]

        # Landed, correct, and searchable in the OWNER's own database.
        owner_db = get_db_path("01OWNER")
        with sqlite3.connect(owner_db) as connection:
            connection.row_factory = sqlite3.Row
            doc = connection.execute(
                "SELECT * FROM bc_documents WHERE project_id='01OWNER' AND doc_key='owner-doc'"
            ).fetchone()
            assert doc is not None
            chunk = connection.execute(
                "SELECT chunk_text FROM bc_chunks WHERE document_id=?", (doc["id"],)
            ).fetchone()
            assert chunk[0] == "belongs to 01OWNER"
            fts_hit = connection.execute(
                "SELECT rowid FROM bc_chunks_fts WHERE bc_chunks_fts MATCH 'belongs'"
            ).fetchall()
            assert fts_hit  # FTS index rebuilt after the migration insert

    def test_migration_creates_destinations_own_database_if_missing(self, tmp_path):
        from braincell.config import get_db_path
        from braincell.transcript_ingest import (
            apply_foreign_document_migration,
            preview_foreign_documents,
        )

        database = _seed_source(tmp_path)
        assert not get_db_path("01OWNER").exists()

        report = preview_foreign_documents("01SOURCE")
        apply_foreign_document_migration(
            "01SOURCE",
            owner_project_ids=["01OWNER"],
            approval_digest=report["approval_digest"],
        )
        assert get_db_path("01OWNER").is_file()
        assert database.is_file()  # source database itself is never removed

    def test_partial_selection_leaves_the_unselected_owner_foreign(self, tmp_path):
        """Selecting only one owner never touches the other owner's rows."""
        from braincell.transcript_ingest import (
            apply_foreign_document_migration,
            preview_foreign_documents,
        )

        _seed_source(tmp_path)
        report = preview_foreign_documents("01SOURCE")
        apply_foreign_document_migration(
            "01SOURCE",
            owner_project_ids=["01OWNER"],
            approval_digest=report["approval_digest"],
        )
        after = preview_foreign_documents("01SOURCE")
        assert set(after["owners"]) == {"01GHOST"}
