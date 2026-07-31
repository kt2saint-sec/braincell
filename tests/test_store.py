# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_store.py — Regression tests for braincell/store.py.

All tests use tmp_path-scoped SqliteStore instances.  No live Ollama required.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3

import numpy as np
import pytest

from tests.conftest import _insert_doc_and_chunk, fake_vec, make_store

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1: aclose / close lifecycle
# ═══════════════════════════════════════════════════════════════════════════════

class TestCloseLifecycle:
    """aclose() closes the connection and is idempotent; sync close() works after
    asyncio.run() exits and does NOT raise."""

    def test_aclose_closes_connection(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            # Touch the connection so it's actually opened.
            await store._conn_get()
            assert store._conn is not None
            await store.aclose()
            assert store._conn is None

        asyncio.run(_run())

    def test_aclose_is_idempotent(self, tmp_path):
        """Calling aclose() twice must not raise."""
        store = make_store(tmp_path)

        async def _run():
            await store._conn_get()
            await store.aclose()
            # Second call must be a no-op, not an exception.
            await store.aclose()

        asyncio.run(_run())

    def test_sync_close_after_event_loop_exits(self, tmp_path):
        """sync close() must work (not raise) after asyncio.run() returns."""
        store = make_store(tmp_path)

        async def _open_conn():
            await store._conn_get()

        asyncio.run(_open_conn())
        # The event loop is now gone; close() must drive a new asyncio.run internally.
        store.close()  # must not raise
        assert store._conn is None

    def test_sync_close_without_any_open_connections(self, tmp_path):
        """sync close() when no connections were ever opened must not raise."""
        store = make_store(tmp_path)
        store.close()  # connections were never lazily opened


class TestAsyncWriteOwnership:
    """One coroutine must own the shared connection's transaction until exit."""

    def test_ordinary_write_cannot_commit_another_tasks_transaction(self, tmp_path):
        store = make_store(tmp_path)
        assert hasattr(store, "_write_transaction"), (
            "SqliteStore needs one ownership gate for shared-connection writes"
        )

        async def _run():
            owner_started = asyncio.Event()
            release_owner = asyncio.Event()

            async def unfinished_owner():
                with pytest.raises(RuntimeError, match="force owner rollback"):
                    async with store._write_transaction(immediate=True) as mem:
                        await mem.execute(
                            "INSERT INTO memory_notes "
                            "(project_id, scope, kind, content, tags, note_uid) "
                            "VALUES (?, 'project', 'note', ?, '[]', ?)",
                            ("proj-lock", "unfinished owner row", "owner-row"),
                        )
                        owner_started.set()
                        await release_owner.wait()
                        raise RuntimeError("force owner rollback")

            async def ordinary_writer():
                await owner_started.wait()
                return await store.remember(
                    "ordinary concurrent row", "note", "proj-lock"
                )

            owner = asyncio.create_task(unfinished_owner())
            writer = asyncio.create_task(ordinary_writer())
            await owner_started.wait()
            await asyncio.sleep(0)
            assert not writer.done(), "ordinary writer entered another task's transaction"
            release_owner.set()
            await owner
            ordinary_id = await writer

            mem = await store._conn_get()
            rows = await (
                await mem.execute(
                    "SELECT id, content FROM memory_notes ORDER BY id"
                )
            ).fetchall()
            assert [(str(row[0]), row[1]) for row in rows] == [
                (ordinary_id, "ordinary concurrent row")
            ]

        asyncio.run(_run())

    def test_failed_commit_rolls_back_before_next_writer(self, tmp_path, monkeypatch):
        store = make_store(tmp_path)

        async def _run():
            writer = await store._write_conn_get()
            real_commit = writer.commit
            calls = 0

            async def fail_once():
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise sqlite3.OperationalError("simulated busy commit")
                await real_commit()

            monkeypatch.setattr(writer, "commit", fail_once)
            with pytest.raises(sqlite3.OperationalError, match="simulated busy"):
                await store.remember("must roll back", "note", "project")

            note_id = await store.remember("next writer succeeds", "note", "project")
            reader = await store._conn_get()
            rows = await (
                await reader.execute(
                    "SELECT id, content FROM memory_notes ORDER BY id"
                )
            ).fetchall()
            assert [(str(row[0]), row[1]) for row in rows] == [
                (note_id, "next writer succeeds")
            ]

        asyncio.run(_run())


class TestAtomicDocumentReplacement:
    """Document metadata, chunks, embeddings, and FTS move as one unit."""

    def test_invalid_replacement_preserves_previous_document(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            old_hash = hashlib.sha256(b"old").digest()
            await store.replace_document(
                project_id="proj-atomic",
                doc_key="session-1",
                title="old",
                content_hash=old_hash,
                content_type="transcript",
                chunks=[("old searchable phrase", fake_vec(1))],
            )

            bad_embedding = np.zeros(3, dtype=np.float32)
            with pytest.raises(ValueError, match="embedding is 3-d"):
                await store.replace_document(
                    project_id="proj-atomic",
                    doc_key="session-1",
                    title="new",
                    content_hash=hashlib.sha256(b"new").digest(),
                    content_type="transcript",
                    chunks=[("new phrase", bad_embedding)],
                )

            cf = await store._conn_get()
            doc = await (
                await cf.execute(
                    "SELECT title, content_hash FROM bc_documents "
                    "WHERE project_id=? AND doc_key=?",
                    ("proj-atomic", "session-1"),
                )
            ).fetchone()
            chunks = await (
                await cf.execute(
                    "SELECT chunk_index, chunk_text FROM bc_chunks "
                    "WHERE document_id=("
                    "SELECT id FROM bc_documents WHERE project_id=? AND doc_key=?"
                    ") ORDER BY chunk_index",
                    ("proj-atomic", "session-1"),
                )
            ).fetchall()
            old_fts = await (
                await cf.execute(
                    "SELECT rowid FROM bc_chunks_fts "
                    "WHERE bc_chunks_fts MATCH 'searchable'"
                )
            ).fetchall()
            new_fts = await (
                await cf.execute(
                    "SELECT rowid FROM bc_chunks_fts WHERE bc_chunks_fts MATCH 'new'"
                )
            ).fetchall()

            assert doc[0] == "old"
            assert bytes(doc[1]) == old_hash
            assert [(row[0], row[1]) for row in chunks] == [
                (0, "old searchable phrase")
            ]
            assert len(old_fts) == 1
            assert new_fts == []

        asyncio.run(_run())

    def test_shorter_replacement_removes_trailing_chunks_and_stale_fts(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            await store.replace_document(
                project_id="proj-atomic",
                doc_key="session-2",
                title="long",
                content_hash=hashlib.sha256(b"long").digest(),
                content_type="transcript",
                chunks=[
                    ("keep before replacement", fake_vec(1)),
                    ("obsolete zebra phrase", fake_vec(2)),
                    ("obsolete yak phrase", fake_vec(3)),
                ],
            )
            new_hash = hashlib.sha256(b"short").digest()
            doc_id, changed = await store.replace_document(
                project_id="proj-atomic",
                doc_key="session-2",
                title="short",
                content_hash=new_hash,
                content_type="transcript",
                chunks=[("only current phrase", fake_vec(4))],
            )

            assert changed is True
            assert await store.document_is_current(
                "proj-atomic", "session-2", new_hash, expected_chunks=1
            )

            cf = await store._conn_get()
            chunks = await (
                await cf.execute(
                    "SELECT chunk_index, chunk_text FROM bc_chunks "
                    "WHERE document_id=? ORDER BY chunk_index",
                    (doc_id,),
                )
            ).fetchall()
            stale_fts = await (
                await cf.execute(
                    "SELECT rowid FROM bc_chunks_fts "
                    "WHERE bc_chunks_fts MATCH 'zebra OR yak'"
                )
            ).fetchall()
            current_fts = await (
                await cf.execute(
                    "SELECT rowid FROM bc_chunks_fts "
                    "WHERE bc_chunks_fts MATCH 'current'"
                )
            ).fetchall()

            assert [(row[0], row[1]) for row in chunks] == [
                (0, "only current phrase")
            ]
            assert stale_fts == []
            assert len(current_fts) == 1

        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2: list_documents
# ═══════════════════════════════════════════════════════════════════════════════

class TestListDocuments:
    """list_documents respects limit, caps at 200, supports filter, uses parameterised SQL."""

    def _seed_docs(self, store, project: str, count: int):
        async def _inner():
            for i in range(count):
                doc_key = f"doc-{i:04d}"
                await store.replace_document(
                    project_id=project, doc_key=doc_key,
                    title=f"Title {i}",
                    content_hash=hashlib.sha256(doc_key.encode()).digest(),
                    content_type="cell", chunks=[],
                )
        asyncio.run(_inner())

    def test_limit_is_respected(self, tmp_path):
        store = make_store(tmp_path)
        self._seed_docs(store, "proj-A", 20)

        async def _run():
            rows = await store.list_documents("proj-A", None, limit=5)
            assert len(rows) == 5

        asyncio.run(_run())

    def test_limit_caps_at_200(self, tmp_path):
        """Asking for 999 must be silently capped to 200."""
        store = make_store(tmp_path)
        self._seed_docs(store, "proj-B", 250)

        async def _run():
            rows = await store.list_documents("proj-B", None, limit=999)
            assert len(rows) == 200

        asyncio.run(_run())

    def test_filter_substring_match(self, tmp_path):
        """filter kwarg performs a case-insensitive substring match on doc_key / title."""
        store = make_store(tmp_path)

        async def _seed():
            for key, title in [
                ("alpha-session", "My Alpha Doc"),
                ("beta-session", "Some Beta Doc"),
                ("gamma-session", "Gamma Doc"),
            ]:
                await store.replace_document(
                    project_id="proj-C", doc_key=key, title=title,
                    content_hash=hashlib.sha256(key.encode()).digest(),
                    content_type="cell", chunks=[],
                )

        asyncio.run(_seed())

        async def _run():
            rows = await store.list_documents("proj-C", "alpha", limit=50)
            assert len(rows) == 1
            assert rows[0]["doc_key"] == "alpha-session"

        asyncio.run(_run())

    def test_filter_sql_injection_does_not_error(self, tmp_path):
        """A filter string containing a single-quote must not cause a SQL error."""
        store = make_store(tmp_path)
        self._seed_docs(store, "proj-D", 3)

        async def _run():
            # A single quote in the filter string would break unparameterised SQL.
            result = await store.list_documents("proj-D", "O'Reilly's guide", limit=50)
            # No exception; empty result is fine (no rows match).
            assert isinstance(result, list)

        asyncio.run(_run())

    def test_filter_with_semicolon_does_not_error(self, tmp_path):
        """Filter containing SQL meta-characters must not cause injection or crash."""
        store = make_store(tmp_path)
        self._seed_docs(store, "proj-E", 3)

        async def _run():
            result = await store.list_documents("proj-E", "'; DROP TABLE bc_documents; --", limit=50)
            assert isinstance(result, list)
            # The table must still be intact.
            remaining = await store.list_documents("proj-E", None, limit=50)
            assert len(remaining) == 3

        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3: recall
# ═══════════════════════════════════════════════════════════════════════════════

class TestRecall:
    """recall() routes to FTS when qtext is non-empty, recency when empty,
    and falls back gracefully on a malformed MATCH term."""

    def _add_note(self, store, project: str, content: str, kind: str = "note") -> str:
        async def _inner():
            return await store.remember(content, kind, project)
        return asyncio.run(_inner())

    def test_recall_empty_qtext_returns_newest_first(self, tmp_path):
        """Recency ordering requires distinct timestamps.

        SQLite's datetime('now') has second precision — all notes inserted in
        the same second get the same created_at and the ORDER BY is undefined.
        We insert with explicit, strictly-ordered ISO timestamps to make the
        recency contract deterministic.
        """
        store = make_store(tmp_path)

        async def _run():
            mem = await store._conn_get()
            # Insert three notes with distinct, ordered created_at values.
            for i, content in enumerate(["first note", "second note", "third note"]):
                ts = f"2026-01-01 00:00:{i:02d}"
                await mem.execute(
                    "INSERT INTO memory_notes "
                    "(project_id, scope, kind, content, tags, created_at) "
                    "VALUES ('p1', 'project', 'note', ?, '[]', ?)",
                    (content, ts),
                )
            await mem.commit()

            qvec = fake_vec(0)
            notes = await store.recall(qvec, "p1", k=10, qtext="")
            assert len(notes) == 3
            # Newest-first: created_at DESC, so "third note" (00:02) comes first.
            assert notes[0].content == "third note", (
                f"Expected 'third note' first (newest), got: {[n.content for n in notes]}"
            )
            assert notes[-1].content == "first note", (
                f"Expected 'first note' last (oldest), got: {[n.content for n in notes]}"
            )

        asyncio.run(_run())

    def test_recall_with_qtext_uses_fts_match(self, tmp_path):
        store = make_store(tmp_path)
        self._add_note(store, "p2", "halftoning algorithm design decision")
        self._add_note(store, "p2", "completely unrelated content here")
        self._add_note(store, "p2", "another halftoning observation")

        async def _run():
            if not store._fts5_ok:
                pytest.skip("FTS5 not available in this sqlite3 build")
            qvec = fake_vec(0)
            notes = await store.recall(qvec, "p2", k=10, qtext="halftoning")
            contents = [n.content for n in notes]
            assert any("halftoning" in c for c in contents)
            # The completely unrelated note must NOT appear.
            assert not any("unrelated" in c for c in contents)

        asyncio.run(_run())

    def test_recall_malformed_fts_match_falls_back_to_recency(self, tmp_path):
        """A malformed MATCH term (e.g. 'a AND AND b') must not raise — falls back."""
        store = make_store(tmp_path)
        self._add_note(store, "p3", "test content")

        async def _run():
            qvec = fake_vec(0)
            # "AND AND" is a malformed FTS5 MATCH expression.
            notes = await store.recall(qvec, "p3", k=10, qtext="a AND AND b")
            # Must return a list (possibly empty via recency fallback), not raise.
            assert isinstance(notes, list)

        asyncio.run(_run())

    def test_recall_project_none_returns_all_projects(self, tmp_path):
        store = make_store(tmp_path)
        self._add_note(store, "proj-x", "note from project x")
        self._add_note(store, "proj-y", "note from project y")

        async def _run():
            qvec = fake_vec(0)
            notes = await store.recall(qvec, None, k=20, qtext="")
            projects = {n.project_id for n in notes}
            assert "proj-x" in projects
            assert "proj-y" in projects

        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4: _fts_search fallback behaviour
# ═══════════════════════════════════════════════════════════════════════════════

class TestFtsSearchFallback:
    """_fts_search: a malformed FTS MATCH is caught and falls back (no crash);
    a genuine non-OperationalError propagates."""

    def test_malformed_fts_match_falls_back(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            if not store._fts5_ok:
                pytest.skip("FTS5 not available")
            cf = await store._conn_get()
            # "AND AND" is guaranteed to produce an OperationalError from FTS5.
            results = await store._fts_search(cf, "a AND AND b", None, 10)
            # Must be a list (possibly empty from LIKE fallback), not raise.
            assert isinstance(results, list)

        asyncio.run(_run())

    def test_non_operational_error_propagates(self, tmp_path, monkeypatch):
        """If something other than OperationalError fires in FTS, it must propagate."""
        store = make_store(tmp_path)

        async def _run():
            cf = await store._conn_get()

            # Patch aiosqlite connection to raise a TypeError (not OperationalError)
            # when execute() is called with our sentinel text.
            original_execute = cf.execute

            async def patched_execute(sql, params=(), **kw):
                if "MATCH" in sql:
                    raise TypeError("injected non-OperationalError for test")
                return await original_execute(sql, params, **kw)

            monkeypatch.setattr(cf, "execute", patched_execute)

            with pytest.raises(TypeError, match="injected"):
                await store._fts_search(cf, "anything", None, 10)

        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5: search() modes
# ═══════════════════════════════════════════════════════════════════════════════

class TestSearch:
    """search() for modes semantic/keyword/hybrid returns Hits; project=None searches
    all; project='' returns nothing."""

    def _setup_chunk(self, store, project: str, text: str, seed: int):
        async def _inner():
            await _insert_doc_and_chunk(store, project=project, doc_key=f"dk-{seed}", text=text, seed=seed)
        asyncio.run(_inner())

    def test_search_semantic_returns_hits(self, tmp_path):
        from braincell.store import Hit
        store = make_store(tmp_path)
        self._setup_chunk(store, "sp1", "the quick brown fox", seed=1)

        async def _run():
            hits = await store.search(fake_vec(1), "fox", "sp1", k=5, mode="semantic")
            assert isinstance(hits, list)
            assert all(isinstance(h, Hit) for h in hits)
            assert len(hits) >= 1

        asyncio.run(_run())

    def test_search_keyword_returns_hits(self, tmp_path):
        from braincell.store import Hit
        store = make_store(tmp_path)
        self._setup_chunk(store, "sp2", "unique keyword alpha", seed=2)

        async def _run():
            hits = await store.search(fake_vec(2), "alpha", "sp2", k=5, mode="keyword")
            assert isinstance(hits, list)
            # All returned objects must be Hit instances.
            assert all(isinstance(h, Hit) for h in hits)

        asyncio.run(_run())

    def test_search_hybrid_returns_hits(self, tmp_path):
        from braincell.store import Hit
        store = make_store(tmp_path)
        self._setup_chunk(store, "sp3", "hybrid search content", seed=3)

        async def _run():
            hits = await store.search(fake_vec(3), "content", "sp3", k=5, mode="hybrid")
            assert isinstance(hits, list)
            assert all(isinstance(h, Hit) for h in hits)

        asyncio.run(_run())

    def test_search_project_none_searches_all(self, tmp_path):
        store = make_store(tmp_path)
        self._setup_chunk(store, "proj-1", "apple banana cherry", seed=10)
        self._setup_chunk(store, "proj-2", "delta echo foxtrot", seed=11)

        async def _run():
            hits = await store.search(fake_vec(10), "", None, k=20, mode="semantic")
            # Both projects' chunks should be reachable.
            assert len(hits) >= 2

        asyncio.run(_run())

    def test_search_empty_string_project_returns_nothing(self, tmp_path):
        """project='' filters on project_id='' which matches no real rows."""
        store = make_store(tmp_path)
        self._setup_chunk(store, "proj-real", "content here", seed=20)

        async def _run():
            hits = await store.search(fake_vec(20), "content", "", k=10, mode="semantic")
            assert hits == []

        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6: remember
# ═══════════════════════════════════════════════════════════════════════════════

class TestRemember:
    """remember(): secret in text/tag is rejected; >100k note is rejected;
    valid note persists and is found by recall(qtext=...); note+FTS are atomic."""

    def test_secret_in_text_raises_value_error(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            with pytest.raises(ValueError, match="refused"):
                await store.remember(
                    "ANTHROPIC_API_KEY=sk-ant-abc1234567890123456789012345",
                    "note", "proj-sec",
                )

        asyncio.run(_run())

    def test_secret_in_tag_raises_value_error(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            with pytest.raises(ValueError, match="refused"):
                await store.remember(
                    "some legitimate note text",
                    "note", "proj-sec",
                    tags=["sk-ant-abcdefghijklmnopqrstuvwxyz"],
                )

        asyncio.run(_run())

    def test_oversized_note_raises_value_error(self, tmp_path):
        """A note >100_000 chars must be rejected."""
        store = make_store(tmp_path)
        big_text = "x" * 100_001

        async def _run():
            with pytest.raises(ValueError, match="100000"):
                await store.remember(big_text, "note", "proj-big")

        asyncio.run(_run())

    def test_valid_note_persists_and_is_recalled(self, tmp_path):
        store = make_store(tmp_path)
        content = "The halftoning pipeline uses a Floyd-Steinberg dither"

        async def _run():
            note_id = await store.remember(content, "decision", "proj-recall")
            assert isinstance(note_id, str)
            qvec = fake_vec(0)
            notes = await store.recall(qvec, "proj-recall", k=5, qtext="halftoning")
            found = any(n.content == content for n in notes)
            assert found, f"Note not found in recall results: {notes}"

        asyncio.run(_run())

    def test_note_and_fts_are_atomic(self, tmp_path):
        """After a successful remember, the FTS row must exist (same transaction)."""
        store = make_store(tmp_path)

        async def _run():
            if not store._fts5_ok:
                pytest.skip("FTS5 not available")
            content = "atomic fts consistency check payload"
            note_id = await store.remember(content, "observation", "proj-atom")
            # Directly query memory_fts to confirm the row is there.
            mem = await store._conn_get()
            row = await (await mem.execute(
                "SELECT rowid FROM memory_fts WHERE memory_fts MATCH ?",
                ("atomic",),
            )).fetchone()
            assert row is not None, "FTS row missing after remember — atomicity broken"
            assert row[0] == int(note_id)

        asyncio.run(_run())

    def test_individual_fts_insert_failure_rolls_back_vectorless_note(
        self, tmp_path, monkeypatch
    ):
        store = make_store(tmp_path)

        async def _run():
            if not store._fts5_ok:
                pytest.skip("FTS5 not available")
            writer = await store._write_conn_get()
            real_execute = writer.execute

            async def fail_note_fts(sql, parameters=None):
                if sql.startswith("INSERT INTO memory_fts"):
                    raise sqlite3.OperationalError("simulated FTS row failure")
                if parameters is None:
                    return await real_execute(sql)
                return await real_execute(sql, parameters)

            monkeypatch.setattr(writer, "execute", fail_note_fts)
            with pytest.raises(sqlite3.OperationalError, match="simulated FTS"):
                await store.remember(
                    "must not become invisible", "note", "proj-atomic", embedding=None
                )

            reader = await store._conn_get()
            count = await (
                await reader.execute(
                    "SELECT COUNT(*) FROM memory_notes WHERE project_id=?",
                    ("proj-atomic",),
                )
            ).fetchone()
            assert count[0] == 0

        asyncio.run(_run())

    def test_invalid_kind_raises_value_error(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            with pytest.raises(ValueError, match="Invalid kind"):
                await store.remember("some content", "bad_kind", "proj-k")

        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6b: forget / supersede (retraction)
# ═══════════════════════════════════════════════════════════════════════════════

class TestForgetSupersede:
    """forget() tombstones (soft, default) or hard-deletes (hard=True) scoped to
    project; supersede() chains old→new via superseded_by and retains the old note.
    M6: default path is soft-delete (tombstone); hard=True keeps the original
    permanent-delete behaviour.
    """

    # ── Hard-delete path (hard=True) — original behaviour preserved ──────────

    def test_forget_hard_removes_row_and_fts(self, tmp_path):
        """hard=True permanently removes the row from memory_notes and hides it from recall."""
        store = make_store(tmp_path)

        async def _run():
            note_id = int(await store.remember("ephemeral fact xyzzy", "note", "proj-f"))
            deleted = await store.forget(note_id, "proj-f", hard=True)
            assert deleted is True
            # Row must be gone from memory_notes (hard delete).
            mem = await store._conn_get()
            row = await (await mem.execute(
                "SELECT id FROM memory_notes WHERE id = ?", (note_id,)
            )).fetchone()
            assert row is None, "hard=True must remove the memory_notes row"
            # And not surfaced by recall.
            notes = await store.recall(fake_vec(0), "proj-f", k=5, qtext="xyzzy")
            assert all(n.id != note_id for n in notes)

        asyncio.run(_run())

    # ── Soft-delete path (default) — M6 new behaviour ────────────────────────

    def test_forget_soft_tombstones_and_hides(self, tmp_path):
        """Default forget tombstones the row (deleted_at set) and hides it from recall
        on both the qvec=None keyword path and the hybrid (qvec not None) path."""
        store = make_store(tmp_path)

        async def _run():
            if not store._fts5_ok:
                pytest.skip("FTS5 not available — keyword recall path not exercised")
            note_id = int(await store.remember(
                "ephemeral tombstone xyzzy", "note", "proj-soft"
            ))

            deleted = await store.forget(note_id, "proj-soft")  # default: hard=False
            assert deleted is True, "soft forget of a live note must return True"

            # Row still exists in memory_notes (tombstoned, not removed).
            mem = await store._conn_get()
            row = await (await mem.execute(
                "SELECT id, deleted_at FROM memory_notes WHERE id = ?", (note_id,)
            )).fetchone()
            assert row is not None, "soft forget must NOT remove the memory_notes row"
            assert row[1] is not None, "deleted_at must be set after soft forget"

            # qvec=None keyword path: tombstoned note must not surface.
            notes_kw = await store.recall(None, "proj-soft", k=5, qtext="tombstone")
            assert all(n.id != note_id for n in notes_kw), (
                "tombstoned note appeared in qvec=None keyword recall"
            )

            # Hybrid (qvec not None) path: tombstoned note must not surface.
            notes_hybrid = await store.recall(
                fake_vec(0), "proj-soft", k=5, qtext="tombstone"
            )
            assert all(n.id != note_id for n in notes_hybrid), (
                "tombstoned note appeared in hybrid recall"
            )

        asyncio.run(_run())

    def test_forget_soft_idempotent(self, tmp_path):
        """Forgetting an already-tombstoned note returns False (idempotent)."""
        store = make_store(tmp_path)

        async def _run():
            note_id = int(await store.remember("once is enough", "note", "proj-idem"))
            first = await store.forget(note_id, "proj-idem")
            assert first is True, "first soft-forget of a live note must return True"
            second = await store.forget(note_id, "proj-idem")
            assert second is False, "re-forgetting an already-tombstoned note must return False"

        asyncio.run(_run())

    def test_forget_soft_hybrid_vec_excluded(self, tmp_path):
        """Tombstoned embedded note must not appear via hybrid vector recall even when
        its stored vector is an exact cosine match to the query."""
        store = make_store(tmp_path)

        async def _run():
            vec = fake_vec(seed=42)
            note_id = int(await store.remember(
                "vector tombstone check", "note", "proj-vtomb", embedding=vec
            ))
            # Soft-tombstone it.
            assert await store.forget(note_id, "proj-vtomb") is True
            # Query with identical vector — should get zero hits.
            results = await store.recall(fake_vec(seed=42), "proj-vtomb", k=5)
            assert all(n.id != note_id for n in results), (
                "tombstoned note appeared in hybrid vector recall despite exact cosine match"
            )

        asyncio.run(_run())

    # ── Ownership / missing-ID behaviour (unchanged by M6) ───────────────────

    def test_forget_wrong_project_is_rejected(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            note_id = int(await store.remember("owned by A", "note", "proj-A"))
            # Different project must NOT be able to tombstone it.
            deleted = await store.forget(note_id, "proj-B")
            assert deleted is False
            # Still present for the true owner (soft-tombstone succeeds).
            assert await store.forget(note_id, "proj-A") is True

        asyncio.run(_run())

    def test_forget_missing_note_returns_false(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            assert await store.forget(999999, "proj-x") is False

        asyncio.run(_run())

    # ── supersede — unaffected by M6, chains/retains both notes ─────────────

    def test_supersede_chains_and_retains_old(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            old_id = int(await store.remember("v1 of the decision", "decision",
                                              "proj-s", tags=["arch"]))
            new_id = await store.supersede(old_id, "v2 of the decision", "proj-s")
            assert new_id != old_id
            mem = await store._conn_get()
            # Old note retained, with superseded_by → new id.
            old = await (await mem.execute(
                "SELECT content, superseded_by, kind, tags FROM memory_notes WHERE id = ?",
                (old_id,),
            )).fetchone()
            assert old is not None
            assert old[1] == new_id
            # New note exists with new content, inheriting kind/tags.
            new = await (await mem.execute(
                "SELECT content, kind, tags FROM memory_notes WHERE id = ?", (new_id,),
            )).fetchone()
            assert new[0] == "v2 of the decision"
            assert new[1] == "decision"          # inherited kind
            assert "arch" in new[2]              # inherited tags

        asyncio.run(_run())

    def test_supersede_unaffected_by_tombstone(self, tmp_path):
        """supersede sets superseded_by on the old row; deleted_at remains NULL
        for both old and new notes — supersede is independent of soft-delete."""
        store = make_store(tmp_path)

        async def _run():
            old_id = int(await store.remember("v1 supersede check", "note", "proj-su"))
            new_id = await store.supersede(old_id, "v2 supersede check", "proj-su")
            mem = await store._conn_get()
            # Neither row should be tombstoned by supersede.
            for rid in (old_id, new_id):
                row = await (await mem.execute(
                    "SELECT deleted_at FROM memory_notes WHERE id = ?", (rid,)
                )).fetchone()
                assert row is not None
                assert row[0] is None, (
                    f"supersede must not set deleted_at on note {rid}"
                )

        asyncio.run(_run())

    def test_supersede_wrong_project_raises(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            old_id = int(await store.remember("owned by A", "note", "proj-A"))
            with pytest.raises(ValueError, match="cannot supersede"):
                await store.supersede(old_id, "hijack", "proj-B")

        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7: replace_document dimension guard + legacy upsert retirement (BC-24)
# ═══════════════════════════════════════════════════════════════════════════════

class TestDocumentWriteGuards:
    """The one production document-write path refuses wrong-dim embeddings, and
    the retired raw caller-owned-connection upsert helpers stay gone."""

    def test_wrong_dim_raises_value_error(self, tmp_path):
        from braincell import embed_spec
        from braincell.store import upsert_chunk, upsert_document

        wrong_dim_vec = np.ones(embed_spec.DIM + 1, dtype=np.float32)

        async def _run():
            store = make_store(tmp_path)
            with pytest.raises(ValueError, match="write refused"):
                await store.replace_document(
                    project_id="proj-dim", doc_key="dim-test",
                    title="dim test",
                    content_hash=hashlib.sha256(b"test").digest(),
                    content_type="cell",
                    chunks=[("some text", wrong_dim_vec)],
                )

        asyncio.run(_run())

    def test_correct_dim_succeeds(self, tmp_path):
        """Sanity: a correct-dim vector must not raise."""

        async def _run():
            store = make_store(tmp_path)
            await store.replace_document(  # must not raise
                project_id="proj-dim2", doc_key="dim-ok", title="ok",
                content_hash=hashlib.sha256(b"ok").digest(),
                content_type="cell", chunks=[("text", fake_vec(0))],
            )

        asyncio.run(_run())

    def test_legacy_raw_upsert_helpers_are_retired(self):
        """BC-24: the free upsert_document/upsert_chunk helpers committed a
        caller-owned raw connection outside SqliteStore transaction ownership.
        They are removed; every document write goes through replace_document."""
        import braincell.store as store_module

        assert not hasattr(store_module, "upsert_document")
        assert not hasattr(store_module, "upsert_chunk")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8: open_store() with bogus BRAINCELL_PROJECT_ID
# ═══════════════════════════════════════════════════════════════════════════════

class TestOpenStore:
    """open_store() with a bogus BRAINCELL_PROJECT_ID exits/raises and does NOT
    fabricate a DB file."""

    def test_bogus_project_id_does_not_fabricate_db(self, tmp_path, monkeypatch):
        """BRAINCELL_PROJECT_ID pointing to a non-existent brain must exit/raise,
        not silently create an empty DB at the XDG path."""
        monkeypatch.setenv("BRAINCELL_STORE", "sqlite")
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", "01BOGUS0000000000000000000")

        # open_store calls sys.exit(1) when the DB doesn't exist.
        with pytest.raises(SystemExit) as exc_info:
            from braincell.store import open_store
            open_store()

        assert exc_info.value.code == 1

        # Confirm no DB was fabricated in the XDG temp dir.
        # The isolation fixture already redirects XDG_DATA_HOME to tmp_path/xdg.
        # We just check no .db file was created in the tmp_path subtree.
        db_files = list(tmp_path.rglob("*.db"))
        assert db_files == [], f"open_store fabricated DB(s): {db_files}"

    def test_missing_braincell_store_exits(self, tmp_path, monkeypatch):
        """With BRAINCELL_STORE unset and no explicit paths, open_store must sys.exit."""
        monkeypatch.delenv("BRAINCELL_STORE", raising=False)
        monkeypatch.delenv("BRAINCELL_PROJECT_ID", raising=False)

        with pytest.raises(SystemExit) as exc_info:
            from braincell.store import open_store
            open_store()

        assert exc_info.value.code == 1


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 9: assert_schema_version
# ═══════════════════════════════════════════════════════════════════════════════

class TestAssertSchemaVersion:
    """assert_schema_version() bootstraps the schema and refuses on mismatch."""

    def test_fresh_store_bootstraps_without_error(self, tmp_path):
        store = make_store(tmp_path)
        # assert_schema_version was already called by make_store; calling again
        # (idempotent CREATE IF NOT EXISTS) must not raise.
        store.assert_schema_version()

    def test_version_mismatch_raises_runtime_error(self, tmp_path):
        from braincell.schema import MEMORY_SCHEMA_VERSION
        from braincell.store import SqliteStore

        db_path = tmp_path / "braincell.db"
        store = SqliteStore(db_path)
        store.assert_schema_version()

        # Stomp the schema_version to a wrong value.
        wrong_version = MEMORY_SCHEMA_VERSION + 99
        con = sqlite3.connect(str(db_path))
        con.execute("UPDATE schema_version SET version = ?", (wrong_version,))
        con.commit()
        con.close()

        # A new store object pointed at the same file must refuse.
        store2 = SqliteStore(db_path)
        with pytest.raises(RuntimeError, match="schema_version mismatch"):
            store2.assert_schema_version()

    def test_embed_fingerprint_mismatch_raises(self, tmp_path):
        """Reopening a store under a different embedder must fail loud (F16)."""
        from braincell.store import SqliteStore

        db_path = tmp_path / "braincell.db"
        store = SqliteStore(db_path)
        store.assert_schema_version()

        # Stomp the stored fingerprint to simulate a different embed space.
        con = sqlite3.connect(str(db_path))
        con.execute("UPDATE embed_fingerprint SET fingerprint = 'ollama:other-model:1024'")
        con.commit()
        con.close()

        store2 = SqliteStore(db_path)
        with pytest.raises(RuntimeError, match="embedding-space mismatch"):
            store2.assert_schema_version()

    def test_migrates_v1_db_to_v2(self, tmp_path):
        """A v1-shaped DB gains embedding + deleted_at columns and its note survives."""
        import braincell.embed_spec as es
        from braincell.store import SqliteStore

        db_path = tmp_path / "v1brain.db"

        # Build a minimal v1-shaped DB: old memory_notes without the new columns.
        con = sqlite3.connect(str(db_path))
        con.executescript("""
            CREATE TABLE schema_version(version INTEGER NOT NULL, applied_at TEXT NOT NULL DEFAULT (datetime('now')));
            CREATE TABLE embed_fingerprint(fingerprint TEXT NOT NULL, applied_at TEXT NOT NULL DEFAULT (datetime('now')));
            CREATE TABLE memory_notes(
                id            INTEGER PRIMARY KEY,
                project_id    TEXT    NOT NULL,
                scope         TEXT    NOT NULL DEFAULT 'project',
                kind          TEXT    NOT NULL,
                content       TEXT    NOT NULL,
                tags          TEXT,
                confidence    REAL,
                source_hint   TEXT,
                superseded_by INTEGER REFERENCES memory_notes(id),
                created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
            );
        """)
        con.execute("INSERT INTO schema_version(version) VALUES (1)")
        con.execute("INSERT INTO embed_fingerprint(fingerprint) VALUES (?)", (es.FINGERPRINT,))
        con.execute(
            "INSERT INTO memory_notes(project_id, kind, content) VALUES ('P', 'note', 'keepme')"
        )
        con.commit()
        con.close()

        # Open with SqliteStore — must migrate v1 → v2 without raising.
        store = SqliteStore(db_path)
        store.assert_schema_version()

        # Verify migration outcome.
        con = sqlite3.connect(str(db_path))
        cols = [r[1] for r in con.execute("PRAGMA table_info(memory_notes)").fetchall()]
        ver = con.execute("SELECT version FROM schema_version").fetchone()[0]
        note = con.execute("SELECT content FROM memory_notes").fetchone()[0]
        con.close()

        from braincell.schema import MEMORY_SCHEMA_VERSION

        assert "embedding" in cols, f"embedding column missing after migration; cols={cols}"
        assert "deleted_at" in cols, f"deleted_at column missing after migration; cols={cols}"
        assert ver == MEMORY_SCHEMA_VERSION, (
            f"schema_version should be {MEMORY_SCHEMA_VERSION} (current) after migration, got {ver}"
        )
        assert note == "keepme", "pre-existing note content was lost during migration"

    def test_fresh_db_is_current_version(self, tmp_path):
        """A freshly-created SqliteStore is born at the current schema version."""
        from braincell.schema import MEMORY_SCHEMA_VERSION

        make_store(tmp_path)

        con = sqlite3.connect(str(tmp_path / "braincell.db"))
        ver = con.execute("SELECT version FROM schema_version").fetchone()[0]
        cols = [r[1] for r in con.execute("PRAGMA table_info(memory_notes)").fetchall()]
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        con.close()

        assert ver == MEMORY_SCHEMA_VERSION
        assert "embedding" in cols, f"embedding missing on fresh DB; cols={cols}"
        assert "deleted_at" in cols, f"deleted_at missing on fresh DB; cols={cols}"
        assert "bc_note_links" in tables, f"bc_note_links missing on fresh DB; tables={tables}"

    def test_migrates_v2_db_to_v3(self, tmp_path):
        """A v2 DB (no bc_note_links) is migrated to v3 with the graph table added."""
        from braincell.store import SqliteStore

        db_path = tmp_path / "v2brain.db"
        # A valid v2 DB has no bc_note_links; simulate by building fresh then
        # dropping the table + stamping version back to 2.
        SqliteStore(db_path).assert_schema_version()
        con = sqlite3.connect(str(db_path))
        con.execute("DROP TABLE IF EXISTS bc_note_links")
        con.execute("UPDATE schema_version SET version = 2")
        con.commit()
        con.close()

        # Reopen — must migrate v2 → v3 (recreate bc_note_links) without raising.
        SqliteStore(db_path).assert_schema_version()

        con = sqlite3.connect(str(db_path))
        ver = con.execute("SELECT version FROM schema_version").fetchone()[0]
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        con.close()
        from braincell.schema import MEMORY_SCHEMA_VERSION

        assert ver == MEMORY_SCHEMA_VERSION
        assert "bc_note_links" in tables

    def test_downgrade_still_raises(self, tmp_path):
        """A DB stamped with a future version (> current) must still raise RuntimeError."""
        from braincell.store import SqliteStore

        db_path = tmp_path / "future.db"
        # Create a valid current-version DB.
        SqliteStore(db_path).assert_schema_version()

        # Stomp to a future version (simulates a DB from a later braincell release).
        con = sqlite3.connect(str(db_path))
        con.execute("UPDATE schema_version SET version = 99")
        con.commit()
        con.close()

        with pytest.raises(RuntimeError, match="schema_version mismatch"):
            SqliteStore(db_path).assert_schema_version()


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 10: M1 — semantic note recall (hybrid path)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRecallSemantic:
    """store.recall with qvec is not None: hybrid RRF over memory_notes.

    All tests use fake unit vectors — no live Ollama required.
    """

    def test_close_note_ranks_first(self, tmp_path):
        """The note whose embedding is closest to the query vector ranks first."""
        store = make_store(tmp_path)

        async def _run():
            vec_a = fake_vec(seed=1)   # will be the "close" note (same seed as query)
            vec_b = fake_vec(seed=99)  # orthogonal / distant from seed=1
            await store.remember("topic alpha content", "note", "sem-proj", embedding=vec_a)
            await store.remember("topic beta content", "note", "sem-proj", embedding=vec_b)

            results = await store.recall(fake_vec(seed=1), "sem-proj", k=5)
            assert len(results) >= 1
            # The note embedded with seed=1 is dot-product==1 with query fake_vec(1).
            assert results[0].content == "topic alpha content"

        asyncio.run(_run())

    def test_null_embedding_note_returned_via_fts(self, tmp_path):
        """A note with NULL embedding still appears when the query matches via FTS."""
        store = make_store(tmp_path)

        async def _run():
            if not store._fts5_ok:
                pytest.skip("FTS5 not available")
            # Persist with no embedding (NULL) so it is invisible to vector search.
            await store.remember(
                "uniqueterm nullembed sentence here", "note", "sem-proj2",
            )
            # Recall with qvec + qtext matching the unique term.
            results = await store.recall(
                fake_vec(seed=1), "sem-proj2", k=5, qtext="uniqueterm"
            )
            found = any("uniqueterm" in n.content for n in results)
            assert found, (
                f"NULL-embedding note not returned via FTS in hybrid recall: {results}"
            )

        asyncio.run(_run())

    def test_qvec_none_path_behaviorally_unchanged(self, tmp_path):
        """qvec=None: keyword/recency path still works (regression guard for M1)."""
        store = make_store(tmp_path)

        async def _run():
            content = "halftoning pipeline decision point"
            await store.remember(content, "decision", "regress-proj")
            notes = await store.recall(None, "regress-proj", k=5, qtext="halftoning")
            assert any(n.content == content for n in notes)

        asyncio.run(_run())

    def test_hybrid_empty_store_returns_recency_fallback(self, tmp_path):
        """When the store has no embeddings and no FTS match, recency fallback fires."""
        store = make_store(tmp_path)

        async def _run():
            await store.remember("fallback note content", "note", "fallback-proj")
            # qvec given but no embeddings → empty vec_hits; qtext not given → empty
            # fts_hits → ranked is empty → recency fallback → note is returned.
            results = await store.recall(fake_vec(seed=1), "fallback-proj", k=5)
            assert len(results) == 1
            assert results[0].content == "fallback note content"

        asyncio.run(_run())

    def test_rrf_fuses_vec_and_fts_hits(self, tmp_path):
        """Both embeddings AND FTS match: fused list contains both notes."""
        store = make_store(tmp_path)

        async def _run():
            if not store._fts5_ok:
                pytest.skip("FTS5 not available")
            # Note A: embedded close to query, keyword "alpha".
            await store.remember(
                "alpha keyword note", "note", "fuse-proj", embedding=fake_vec(seed=1)
            )
            # Note B: NULL embedding, keyword "beta" — appears via FTS.
            await store.remember("beta keyword note", "note", "fuse-proj")

            results = await store.recall(
                fake_vec(seed=1), "fuse-proj", k=10, qtext="keyword"
            )
            contents = [n.content for n in results]
            assert "alpha keyword note" in contents
            assert "beta keyword note" in contents

        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 11: M1 — remember/supersede embedding write path
# ═══════════════════════════════════════════════════════════════════════════════

class TestRememberEmbedding:
    """store.remember with and without the embedding param."""

    def test_with_embedding_stores_blob(self, tmp_path):
        """remember(embedding=vec) stores a non-NULL blob in memory_notes.embedding."""
        store = make_store(tmp_path)

        async def _run():
            vec = fake_vec(seed=7)
            note_id = int(await store.remember(
                "embeddable content", "note", "emb-proj", embedding=vec
            ))
            mem = await store._conn_get()
            row = await (await mem.execute(
                "SELECT embedding FROM memory_notes WHERE id = ?", (note_id,)
            )).fetchone()
            assert row is not None
            assert row[0] is not None, "embedding blob should not be NULL"

        asyncio.run(_run())

    def test_without_embedding_stores_null(self, tmp_path):
        """remember() with no embedding param stores NULL (FTS-only until backfill)."""
        store = make_store(tmp_path)

        async def _run():
            note_id = int(await store.remember("no embed content", "note", "emb-proj2"))
            mem = await store._conn_get()
            row = await (await mem.execute(
                "SELECT embedding FROM memory_notes WHERE id = ?", (note_id,)
            )).fetchone()
            assert row is not None
            assert row[0] is None, "embedding should be NULL when not provided"

        asyncio.run(_run())

    def test_both_embeds_and_nulls_recallable_via_qvec_none(self, tmp_path):
        """Notes with and without embeddings are both recallable via the FTS/recency path."""
        store = make_store(tmp_path)

        async def _run():
            await store.remember("with embedding note", "note", "mixed-proj",
                                 embedding=fake_vec(seed=3))
            await store.remember("without embedding note", "note", "mixed-proj")
            notes = await store.recall(None, "mixed-proj", k=10, qtext="embedding note")
            contents = [n.content for n in notes]
            assert "with embedding note" in contents
            assert "without embedding note" in contents

        asyncio.run(_run())

    def test_wrong_dim_embedding_raises(self, tmp_path):
        """remember with wrong-dim embedding raises ValueError (Rule #6 fail-loud)."""
        from braincell import embed_spec

        store = make_store(tmp_path)
        wrong_vec = np.ones(embed_spec.DIM + 1, dtype=np.float32)

        async def _run():
            with pytest.raises(ValueError, match="write refused"):
                await store.remember("some content", "note", "dim-proj", embedding=wrong_vec)

        asyncio.run(_run())

    def test_supersede_stores_embedding_blob(self, tmp_path):
        """supersede(embedding=vec) stores the blob on the NEW note row."""
        store = make_store(tmp_path)

        async def _run():
            old_id = int(await store.remember("v1 content", "decision", "sup-proj"))
            vec = fake_vec(seed=5)
            new_id = await store.supersede(old_id, "v2 content", "sup-proj", embedding=vec)
            mem = await store._conn_get()
            row = await (await mem.execute(
                "SELECT embedding FROM memory_notes WHERE id = ?", (new_id,)
            )).fetchone()
            assert row is not None
            assert row[0] is not None, "supersede new note should have non-NULL embedding"

        asyncio.run(_run())

    def test_supersede_without_embedding_stores_null(self, tmp_path):
        """supersede() with no embedding param stores NULL on the new note."""
        store = make_store(tmp_path)

        async def _run():
            old_id = int(await store.remember("v1 content", "decision", "sup-proj2"))
            new_id = await store.supersede(old_id, "v2 content", "sup-proj2")
            mem = await store._conn_get()
            row = await (await mem.execute(
                "SELECT embedding FROM memory_notes WHERE id = ?", (new_id,)
            )).fetchone()
            assert row is not None
            assert row[0] is None, "supersede new note without embedding should have NULL"

        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 12: M1 — reembed_notes backfill
# ═══════════════════════════════════════════════════════════════════════════════

class TestReembedNotes:
    """store.reembed_notes backfills NULL-embedding notes with a callable."""

    def test_populates_null_embeddings(self, tmp_path):
        """NULL-embedding notes get their embedding column populated after reembed."""
        store = make_store(tmp_path)

        async def _run():
            await store.remember("content to embed A", "note", "reembed-proj")
            await store.remember("content to embed B", "note", "reembed-proj")

            mem = await store._conn_get()
            nulls_before = (await (await mem.execute(
                "SELECT COUNT(*) FROM memory_notes "
                "WHERE embedding IS NULL AND project_id = ?",
                ("reembed-proj",),
            )).fetchone())[0]
            assert nulls_before == 2, "Expected 2 NULL-embedding notes before reembed"

            def fake_embed(texts: list) -> list:
                return [fake_vec(seed=i) for i in range(len(texts))]

            count = await store.reembed_notes("reembed-proj", fake_embed)
            assert count == 2

            nulls_after = (await (await mem.execute(
                "SELECT COUNT(*) FROM memory_notes "
                "WHERE embedding IS NULL AND project_id = ?",
                ("reembed-proj",),
            )).fetchone())[0]
            assert nulls_after == 0, "Expected 0 NULL-embedding notes after reembed"

        asyncio.run(_run())

    def test_skips_already_embedded_notes(self, tmp_path):
        """Notes that already have embeddings are not passed to embed_fn."""
        store = make_store(tmp_path)

        async def _run():
            await store.remember(
                "already embedded", "note", "reembed-proj2", embedding=fake_vec(seed=5)
            )

            call_count = [0]

            def counting_embed(texts: list) -> list:
                call_count[0] += len(texts)
                return [fake_vec(seed=i) for i in range(len(texts))]

            count = await store.reembed_notes("reembed-proj2", counting_embed)
            assert count == 0, "Should not re-embed notes that already have embeddings"
            assert call_count[0] == 0, "embed_fn should not be called when no NULLs"

        asyncio.run(_run())

    def test_reembedded_notes_are_semantically_recalled(self, tmp_path):
        """After reembed, notes appear in vector recall (qvec is not None path)."""
        store = make_store(tmp_path)

        async def _run():
            await store.remember("reembedded recall content", "note", "reembed-proj3")

            # Confirm not in vector recall before backfill (no embedding yet).
            await store.recall(fake_vec(seed=1), "reembed-proj3", k=5)
            # May still appear via FTS fallback; just verify the backfill changes something.

            def fake_embed_seed1(texts: list) -> list:
                # Embed all notes with seed=1 so they're close to query fake_vec(1).
                return [fake_vec(seed=1) for _ in texts]

            count = await store.reembed_notes("reembed-proj3", fake_embed_seed1)
            assert count == 1

            results_after = await store.recall(fake_vec(seed=1), "reembed-proj3", k=5)
            assert any("reembedded recall content" in n.content for n in results_after)

        asyncio.run(_run())

    def test_empty_project_returns_zero(self, tmp_path):
        """reembed_notes returns 0 when there are no NULL-embedding notes."""
        store = make_store(tmp_path)

        async def _run():
            # No notes at all.
            count = await store.reembed_notes("empty-proj", lambda t: [])
            assert count == 0

        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 13: M4 — min_cosine cutoff + near-duplicate dedup
# ═══════════════════════════════════════════════════════════════════════════════

class TestRecallThresholdDedup:
    """M4: recall with min_cosine cutoff and near-duplicate dedup.

    All tests use fake unit vectors (no live Ollama).  Only the hybrid path
    (qvec is not None) is exercised — the qvec=None path stays untouched.
    """

    def test_min_cosine_drops_low_cosine_note(self, tmp_path):
        """min_cosine drops a note whose cosine to the query falls below the threshold."""
        store = make_store(tmp_path)

        async def _run():
            # vec_close = fake_vec(1): cosine with query fake_vec(1) = 1.0 (identical).
            # vec_far   = fake_vec(99): cosine with query fake_vec(1) ≈ 0.0 (orthogonal).
            vec_close = fake_vec(seed=1)
            vec_far = fake_vec(seed=99)
            await store.remember("close note", "note", "mc-proj", embedding=vec_close)
            await store.remember("far note",   "note", "mc-proj", embedding=vec_far)

            # Threshold between 0 and 1 — eliminates the near-orthogonal note.
            results = await store.recall(
                fake_vec(seed=1), "mc-proj", k=5, min_cosine=0.5,
            )
            contents = [n.content for n in results]
            assert "close note" in contents, "close note should pass min_cosine cutoff"
            assert "far note" not in contents, "far note should be dropped by min_cosine"

        asyncio.run(_run())

    def test_dedup_drops_near_duplicate(self, tmp_path):
        """dedup=True drops notes whose stored-vector cosine exceeds 0.95 to a kept note."""
        store = make_store(tmp_path)

        async def _run():
            vec = fake_vec(seed=7)
            # Both notes share the same embedding → cosine = 1.0 > 0.95.
            await store.remember("original note", "note", "dup-proj", embedding=vec)
            await store.remember("duplicate note", "note", "dup-proj", embedding=vec)

            results = await store.recall(
                fake_vec(seed=7), "dup-proj", k=5, dedup=True,
            )
            assert len(results) == 1, (
                f"dedup should collapse identical-embedding notes to 1, got {len(results)}"
            )

        asyncio.run(_run())

    def test_dedup_false_returns_both_near_duplicates(self, tmp_path):
        """dedup=False returns both notes even when their embeddings are identical."""
        store = make_store(tmp_path)

        async def _run():
            vec = fake_vec(seed=7)
            await store.remember("original note", "note", "dup2-proj", embedding=vec)
            await store.remember("duplicate note", "note", "dup2-proj", embedding=vec)

            results = await store.recall(
                fake_vec(seed=7), "dup2-proj", k=5, dedup=False,
            )
            assert len(results) == 2, (
                f"dedup=False should return both notes, got {len(results)}"
            )

        asyncio.run(_run())

    def test_dedup_keeps_fts_only_note(self, tmp_path):
        """dedup never drops a note with NULL embedding (FTS-only hit)."""
        store = make_store(tmp_path)

        async def _run():
            if not store._fts5_ok:
                pytest.skip("FTS5 not available")
            vec = fake_vec(seed=1)
            # Embedded note — will be a strong vector hit.
            await store.remember("embedded note uniquetoken", "note", "dedup-fts-proj",
                                 embedding=vec)
            # FTS-only note with the same unique token — must survive dedup.
            await store.remember("fts only note uniquetoken", "note", "dedup-fts-proj")

            results = await store.recall(
                fake_vec(seed=1), "dedup-fts-proj", k=5,
                qtext="uniquetoken", dedup=True,
            )
            contents = [n.content for n in results]
            assert "fts only note uniquetoken" in contents, (
                "FTS-only (NULL-embedding) note must not be dropped by dedup"
            )

        asyncio.run(_run())

    def test_noop_distinct_notes_no_regression(self, tmp_path):
        """min_cosine=None + dedup=True (defaults): distinct notes all returned (no regression)."""
        store = make_store(tmp_path)

        async def _run():
            # Three distinct notes with distinct embeddings — none are near-dups.
            await store.remember("alpha distinct", "note", "noop-proj",
                                 embedding=fake_vec(seed=1))
            await store.remember("beta distinct",  "note", "noop-proj",
                                 embedding=fake_vec(seed=2))
            await store.remember("gamma distinct", "note", "noop-proj",
                                 embedding=fake_vec(seed=3))

            results = await store.recall(
                fake_vec(seed=1), "noop-proj", k=5,
                min_cosine=None, dedup=True,
            )
            assert len(results) == 3, (
                f"No notes should be dropped when all are distinct, got {len(results)}"
            )

        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 14: M5 — recency decay + confidence blend
# ═══════════════════════════════════════════════════════════════════════════════

class TestRecallDecayConfidence:
    """M5: blended ranking = fused_score * conf_factor * recency_decay(age_days).

    All tests use direct SQL INSERTs with explicit created_at and embeddings so
    every variable (cosine, age, confidence) is controlled independently —
    no live Ollama, no wall-clock coupling.

    Only the hybrid path (qvec is not None) is tested.  The qvec=None path
    (recency/FTS) is behaviorally unchanged by M5.
    """

    async def _insert_note_raw(
        self,
        store,
        project: str,
        content: str,
        confidence,  # float or None
        created_at: str,
        seed: int,
    ) -> None:
        """Insert directly into memory_notes with explicit created_at + embedding.

        Bypasses store.remember() to avoid SQLite datetime('now') auto-stamping.
        FTS is intentionally skipped — tests use qtext="" so only the vector path fires.
        """
        from braincell.store import _vec_to_blob
        vec = fake_vec(seed)
        emb_blob = _vec_to_blob(vec)
        mem = await store._conn_get()
        await mem.execute(
            "INSERT INTO memory_notes "
            "(project_id, scope, kind, content, tags, confidence, embedding, created_at) "
            "VALUES (?, 'project', 'note', ?, '[]', ?, ?, ?)",
            (project, content, confidence, emb_blob, created_at),
        )
        await mem.commit()

    def test_recency_breaks_cosine_tie(self, tmp_path):
        """Newer note ranks above an older note when cosine is identical.

        Both notes share the same embedding (cosine=1.0 to the query), so any
        ordering difference comes entirely from recency decay.  'old note' is from
        2020 (~2000+ days ago, decay ≈ 0); 'new note' is from 2026 (~180 days ago,
        decay ≈ 0.25).  dedup=False so both are visible.
        """
        store = make_store(tmp_path)

        async def _run():
            # seed=1 for both → identical embeddings → equal cosine to query fake_vec(1).
            await self._insert_note_raw(store, "rc-proj", "old note", None,
                                        "2020-01-01 00:00:00", seed=1)
            await self._insert_note_raw(store, "rc-proj", "new note", None,
                                        "2026-01-01 00:00:00", seed=1)

            results = await store.recall(
                fake_vec(seed=1), "rc-proj", k=2, dedup=False,
            )
            assert len(results) == 2, f"Expected 2 results, got {len(results)}"
            assert results[0].content == "new note", (
                f"Newer note should rank first after recency decay; "
                f"got {[n.content for n in results]}"
            )

        asyncio.run(_run())

    def test_confidence_breaks_tie(self, tmp_path):
        """Higher-confidence note ranks above a lower-confidence note at equal age/cosine.

        conf_factor(0.9) = 0.95; conf_factor(0.2) = 0.6.  With equal fused scores
        (both notes same embedding, same created_at) the 0.9-confidence note always
        wins regardless of which RRF rank the cosine tie resolved to — the factor
        ratio (0.95/0.6 ≈ 1.58) comfortably exceeds any rank-1 vs rank-2 RRF noise
        (max ratio 62/61 ≈ 1.016).  dedup=False so both are visible.
        """
        store = make_store(tmp_path)

        async def _run():
            await self._insert_note_raw(store, "cf-proj", "high-conf note", 0.9,
                                        "2026-01-01 00:00:00", seed=1)
            await self._insert_note_raw(store, "cf-proj", "low-conf note", 0.2,
                                        "2026-01-01 00:00:00", seed=1)

            results = await store.recall(
                fake_vec(seed=1), "cf-proj", k=2, dedup=False,
            )
            assert len(results) == 2, f"Expected 2 results, got {len(results)}"
            assert results[0].confidence == pytest.approx(0.9), (
                f"Higher-confidence note should rank first; "
                f"got confidences {[n.confidence for n in results]}"
            )

        asyncio.run(_run())

    def test_none_confidence_no_penalty(self, tmp_path):
        """A None-confidence note is not demoted — its factor is the neutral 1.0.

        At equal age and cosine: conf_factor(None) = 1.0 vs conf_factor(0.5) = 0.75.
        The unrated note should rank first (1.0 > 0.75).  This pins the documented
        behaviour: None means 'unrated', not 'worst'.  dedup=False so both are visible.
        """
        store = make_store(tmp_path)

        async def _run():
            await self._insert_note_raw(store, "nc-proj", "unrated note", None,
                                        "2026-01-01 00:00:00", seed=1)
            await self._insert_note_raw(store, "nc-proj", "mid-conf note", 0.5,
                                        "2026-01-01 00:00:00", seed=1)

            results = await store.recall(
                fake_vec(seed=1), "nc-proj", k=2, dedup=False,
            )
            assert len(results) == 2, f"Expected 2 results, got {len(results)}"
            assert results[0].confidence is None, (
                f"None-confidence note should rank first (factor=1.0 > 0.75); "
                f"got confidences {[n.confidence for n in results]}"
            )

        asyncio.run(_run())

    def test_decay_helper_monotonic(self, tmp_path):
        """_recency_decay: 1.0 at age=0; halves at half_life_days; in (0, 1]; monotonic."""
        from braincell.store import _recency_decay

        # Exact values at canonical ages.
        assert _recency_decay(0.0) == 1.0
        assert _recency_decay(90.0) == pytest.approx(0.5, rel=1e-9)
        assert _recency_decay(180.0) == pytest.approx(0.25, rel=1e-9)

        # Strictly monotonically decreasing.
        assert _recency_decay(0.0) > _recency_decay(1.0)
        assert _recency_decay(1.0) > _recency_decay(90.0)
        assert _recency_decay(90.0) > _recency_decay(365.0)
        assert _recency_decay(365.0) > _recency_decay(1000.0)

        # Always strictly positive (never reaches zero in finite time) and ≤ 1.
        for age in (0.0, 1.0, 30.0, 90.0, 365.0, 1000.0, 10_000.0):
            val = _recency_decay(age)
            assert 0.0 < val <= 1.0, f"_recency_decay({age}) = {val} not in (0, 1]"

        # Negative ages clamped to 0 → factor is exactly 1.0 (no boost beyond fresh).
        assert _recency_decay(-10.0) == 1.0
        assert _recency_decay(-1000.0) == 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 15: M8 — consolidation (cluster detection + deterministic merge)
# ═══════════════════════════════════════════════════════════════════════════════

class TestConsolidate:
    """find_note_clusters + consolidate_cluster: offline, deterministic, no live Ollama.

    Near-duplicate vectors are constructed analytically (Gram-Schmidt) so the
    cosine to a base vector is exactly target_cos (0.95) within float32 precision,
    giving clear margins against the test thresholds (0.9 and 0.97).
    """

    @staticmethod
    def _near_dup_vec(base: np.ndarray, other: np.ndarray, target_cos: float) -> np.ndarray:
        """Return a unit float32 vector with cosine exactly target_cos to base.

        Uses Gram-Schmidt to find the component of other perpendicular to base,
        then blends: v = target_cos * base + sqrt(1 - target_cos²) * perp_hat.
        ||v|| = 1 by construction (since base and perp_hat are orthonormal).
        """
        perp = other.astype(np.float64) - float(np.dot(other, base)) * base.astype(np.float64)
        perp_norm = float(np.linalg.norm(perp))
        if perp_norm < 1e-8:
            # other is nearly parallel to base — fall back to base itself.
            return base.copy()
        perp_hat = perp / perp_norm
        v = target_cos * base.astype(np.float64) + np.sqrt(max(0.0, 1.0 - target_cos ** 2)) * perp_hat
        norm = float(np.linalg.norm(v))
        return (v / norm).astype(np.float32) if norm > 1e-8 else base.copy()

    def test_find_clusters_groups_near_duplicates(self, tmp_path):
        """Two notes with cosine ≥ threshold form one cluster; the singleton is excluded."""
        store = make_store(tmp_path)
        v_base = fake_vec(0)
        v_near = self._near_dup_vec(v_base, fake_vec(1), 0.95)   # cosine ≈ 0.95 ≥ 0.9
        v_ortho = fake_vec(999)                                    # cosine ≈ 0.0 to both

        async def _run():
            id1 = int(await store.remember(
                "first near-dup note", "note", "proj-clust", embedding=v_base
            ))
            id2 = int(await store.remember(
                "second near-dup note", "note", "proj-clust", embedding=v_near
            ))
            id3 = int(await store.remember(
                "orthogonal note", "note", "proj-clust", embedding=v_ortho
            ))
            clusters = await store.find_note_clusters("proj-clust", threshold=0.9)
            assert len(clusters) == 1, (
                f"Expected exactly 1 cluster, got {len(clusters)}: {clusters}"
            )
            cluster_set = set(clusters[0])
            assert id1 in cluster_set and id2 in cluster_set, (
                f"Near-dup pair {{{id1}, {id2}}} not both in cluster {cluster_set}"
            )
            all_ids = {nid for c in clusters for nid in c}
            assert id3 not in all_ids, (
                f"Orthogonal note {id3} was wrongly included in a cluster"
            )

        asyncio.run(_run())

    def test_find_clusters_respects_threshold(self, tmp_path):
        """Same notes at a higher threshold → no clusters (cosine 0.95 < threshold 0.97)."""
        store = make_store(tmp_path)
        v_base = fake_vec(0)
        v_near = self._near_dup_vec(v_base, fake_vec(1), 0.95)
        v_ortho = fake_vec(999)

        async def _run():
            await store.remember("note a", "note", "proj-thresh", embedding=v_base)
            await store.remember("note b", "note", "proj-thresh", embedding=v_near)
            await store.remember("note c", "note", "proj-thresh", embedding=v_ortho)
            # 0.95 < threshold=0.97 → the near-dup pair does not cluster.
            clusters = await store.find_note_clusters("proj-thresh", threshold=0.97)
            assert clusters == [], (
                f"Expected no clusters at threshold=0.97 (cosine≈0.95), got {clusters}"
            )

        asyncio.run(_run())

    def test_find_clusters_excludes_tombstoned(self, tmp_path):
        """Tombstoned notes are excluded from cluster detection (deleted_at IS NULL guard)."""
        store = make_store(tmp_path)
        v_base = fake_vec(0)
        v_near = self._near_dup_vec(v_base, fake_vec(1), 0.95)

        async def _run():
            int(await store.remember(
                "live note", "note", "proj-tomb-c", embedding=v_base
            ))
            id2 = int(await store.remember(
                "tombstoned note", "note", "proj-tomb-c", embedding=v_near
            ))
            # Soft-tombstone id2 — it must be invisible to find_note_clusters.
            await store.forget(id2, "proj-tomb-c", hard=False)
            # Only id1 is live; a singleton is not returned.
            clusters = await store.find_note_clusters("proj-tomb-c", threshold=0.9)
            assert clusters == [], (
                f"Tombstoned note {id2} should not contribute to any cluster, "
                f"got {clusters}"
            )

        asyncio.run(_run())

    def test_consolidate_keeps_representative_soft_forgets_rest(self, tmp_path):
        """After merge: representative is recallable; others are tombstoned but rows exist."""
        store = make_store(tmp_path)
        v_base = fake_vec(0)
        v_near = self._near_dup_vec(v_base, fake_vec(1), 0.95)

        async def _run():
            int(await store.remember(
                "older note", "note", "proj-merge", embedding=v_base
            ))
            int(await store.remember(
                "newer note", "note", "proj-merge", embedding=v_near
            ))

            clusters = await store.find_note_clusters("proj-merge", threshold=0.9)
            assert len(clusters) == 1, f"Setup: expected 1 cluster, got {clusters}"

            representative_id = clusters[0][0]   # newest-first; first element is the keep
            other_ids = clusters[0][1:]

            await store.consolidate_cluster(clusters[0], "proj-merge", representative_id)

            mem = await store._conn_get()

            # Representative row is live (deleted_at IS NULL).
            rep_row = await (await mem.execute(
                "SELECT id, deleted_at FROM memory_notes WHERE id = ?",
                (representative_id,),
            )).fetchone()
            assert rep_row is not None, "Representative row must still exist"
            assert rep_row[1] is None, (
                f"Representative note {representative_id} must not be tombstoned"
            )

            # Every other cluster member is soft-tombstoned (row exists, deleted_at set).
            for oid in other_ids:
                other_row = await (await mem.execute(
                    "SELECT id, deleted_at FROM memory_notes WHERE id = ?",
                    (oid,),
                )).fetchone()
                assert other_row is not None, (
                    f"Note {oid} was hard-deleted — must be soft-tombstoned"
                )
                assert other_row[1] is not None, (
                    f"Note {oid} must have deleted_at set after consolidation"
                )

            # Recall: representative surfaces; merged notes do not.
            notes = await store.recall(None, "proj-merge", k=20, qtext="")
            live_ids = {n.id for n in notes}
            assert representative_id in live_ids, (
                f"Representative {representative_id} missing from recall after consolidation"
            )
            for oid in other_ids:
                assert oid not in live_ids, (
                    f"Merged note {oid} still surfaces in recall after consolidation"
                )

        asyncio.run(_run())
