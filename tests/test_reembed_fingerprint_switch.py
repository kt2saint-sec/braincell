# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
test_reembed_fingerprint_switch.py — Tests for fingerprint reset escape hatch.

Verifies that EmbedderMismatchError can be escaped via reset_embedding_space() and
that build --reembed can recover from a fingerprint mismatch.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3

import pytest

from braincell.cli import _run_build
from braincell.store import EmbedderMismatchError, SqliteStore
from tests.conftest import fake_vec


class TestResetEmbeddingSpace:
    """SqliteStore.reset_embedding_space() wipes docs/chunks and restamps fingerprint."""

    def test_reset_embedding_space_restamps_and_wipes(self, tmp_path):
        """reset_embedding_space clears all vectors and restamps the fingerprint."""
        import braincell.embed_spec as es

        db_path = tmp_path / "braincell.db"
        store = SqliteStore(db_path)
        store.assert_schema_version()

        # Insert one document and one chunk with embedding.
        async def _seed():
            await store.replace_document(
                project_id="test-proj",
                doc_key="doc-1",
                title="Test Doc",
                content_hash=hashlib.sha256(b"test").digest(),
                content_type="cell",
                chunks=[("test chunk", fake_vec(0))],
            )

        asyncio.run(_seed())

        # Insert a memory note with embedding.
        con = sqlite3.connect(str(db_path))
        embedding_blob = fake_vec(1).tobytes()
        con.execute(
            "INSERT INTO memory_notes(project_id, kind, content, embedding) "
            "VALUES (?, ?, ?, ?)",
            ("proj-a", "note", "test note", embedding_blob),
        )
        con.commit()
        con.close()

        # Verify docs and notes exist before reset.
        con = sqlite3.connect(str(db_path))
        doc_count_before = con.execute("SELECT COUNT(*) FROM bc_documents").fetchone()[0]
        chunk_count_before = con.execute("SELECT COUNT(*) FROM bc_chunks").fetchone()[0]
        note_embedding_count_before = con.execute(
            "SELECT COUNT(*) FROM memory_notes WHERE embedding IS NOT NULL"
        ).fetchone()[0]
        con.close()

        assert doc_count_before >= 1
        assert chunk_count_before >= 1
        assert note_embedding_count_before >= 1

        # Call reset_embedding_space.
        stats = store.reset_embedding_space()

        # Verify return values.
        assert stats["docs_wiped"] == doc_count_before
        assert stats["note_embeddings_cleared"] == note_embedding_count_before
        assert stats["fingerprint"] == es.FINGERPRINT

        # Verify docs and chunks are empty.
        con = sqlite3.connect(str(db_path))
        doc_count_after = con.execute("SELECT COUNT(*) FROM bc_documents").fetchone()[0]
        chunk_count_after = con.execute("SELECT COUNT(*) FROM bc_chunks").fetchone()[0]
        note_embedding_count_after = con.execute(
            "SELECT COUNT(*) FROM memory_notes WHERE embedding IS NOT NULL"
        ).fetchone()[0]
        fingerprint_after = con.execute(
            "SELECT fingerprint FROM embed_fingerprint"
        ).fetchone()[0]
        con.close()

        assert doc_count_after == 0
        assert chunk_count_after == 0
        assert note_embedding_count_after == 0
        assert fingerprint_after == es.FINGERPRINT

        # Verify assert_schema_version now passes (no more mismatch).
        new_store = SqliteStore(db_path)
        new_store.assert_schema_version()  # must not raise

    def test_build_reembed_escapes_fingerprint_mismatch(self, tmp_path, monkeypatch):
        """build --reembed can recover from a fingerprint mismatch."""
        import braincell.embed_spec as es

        # Set BRAINCELL_DATA_NAMESPACE to isolate this test's data dir.
        test_ns = "test_reembed_escape"
        monkeypatch.setenv("BRAINCELL_DATA_NAMESPACE", test_ns)

        root = tmp_path / "project"
        root.mkdir()

        # First build with the current embedder.
        _run_build(
            root, skip_transcripts=True, reembed=False, verbose=False, mode=None
        )

        # Stomp the fingerprint to simulate a mismatch.
        from braincell.config import get_db_path, get_project_id

        project_id = get_project_id(root)
        db_path = get_db_path(project_id)

        con = sqlite3.connect(str(db_path))
        con.execute(
            "UPDATE embed_fingerprint SET fingerprint = 'ollama:other-model:1024'"
        )
        con.commit()
        con.close()

        # Verify that build without --reembed raises.
        with pytest.raises(EmbedderMismatchError):
            _run_build(root, skip_transcripts=True, reembed=False, verbose=False)

        # Verify that build --reembed succeeds and restamps.
        _run_build(
            root, skip_transcripts=True, reembed=True, verbose=False, mode=None
        )

        # Verify the fingerprint is now correct.
        con = sqlite3.connect(str(db_path))
        fingerprint = con.execute(
            "SELECT fingerprint FROM embed_fingerprint"
        ).fetchone()[0]
        con.close()

        assert fingerprint == es.FINGERPRINT

        # Verify a subsequent build without --reembed works (no mismatch).
        _run_build(root, skip_transcripts=True, reembed=False, verbose=False)
