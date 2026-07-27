# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_pool.py — `braincell pool`: merge existing per-project brains into the global
brain without re-embedding.

Covers:
  - pool_into_global copies documents, chunks, and notes with project_id intact.
  - Stored embeddings are reused (recall over the global DB returns pooled notes).
  - Idempotent: a second pool skips already-present rows (no duplicates).
  - Supersede chains are remapped to the new global note ids.
  - Embedding-fingerprint mismatch raises PoolError (no vector-space mixing).
  - CLI: `main(["pool", ...])` resolves sources via the path registry (--all).
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from braincell.config import get_db_path, get_global_db_path
from braincell.pool import PoolError, pool_into_global
from braincell.project_registry import register_path
from braincell.store import SqliteStore
from tests.conftest import _insert_doc_and_chunk, fake_vec


def _init_global() -> None:
    g = SqliteStore(get_global_db_path())
    g.assert_schema_version()
    g.close()


async def _build_source(pid: str, *, doc_key: str, text: str, note: str, seed: int) -> None:
    src = SqliteStore(get_db_path(pid))
    src.assert_schema_version()
    await _insert_doc_and_chunk(src, project=pid, doc_key=doc_key, text=text, seed=seed)
    await src.remember(note, "note", pid, embedding=fake_vec(seed + 100))
    await src.aclose()


def _global_counts() -> tuple[int, int, int]:
    con = sqlite3.connect(str(get_global_db_path()))
    try:
        docs = con.execute("SELECT COUNT(*) FROM bc_documents").fetchone()[0]
        chunks = con.execute("SELECT COUNT(*) FROM bc_chunks").fetchone()[0]
        notes = con.execute("SELECT COUNT(*) FROM memory_notes").fetchone()[0]
        return docs, chunks, notes
    finally:
        con.close()


class TestPoolMerge:
    def test_copies_docs_chunks_notes(self):
        async def go():
            await _build_source("PROJA", doc_key="a1", text="alpha beta", note="note-a", seed=1)
            await _build_source("PROJB", doc_key="b1", text="gamma delta", note="note-b", seed=2)
        asyncio.run(go())
        _init_global()

        stats = pool_into_global(
            [("PROJA", get_db_path("PROJA")), ("PROJB", get_db_path("PROJB"))],
            get_global_db_path(),
        )
        assert sum(s.docs_copied for s in stats) == 2
        assert sum(s.chunks_copied for s in stats) == 2
        assert sum(s.notes_copied for s in stats) == 2
        assert _global_counts() == (2, 2, 2)

    def test_project_id_preserved_and_recall_uses_pooled_vectors(self):
        async def go():
            await _build_source("PROJA", doc_key="a1", text="alpha beta", note="alpha note", seed=1)
            await _build_source("PROJB", doc_key="b1", text="gamma delta", note="gamma note", seed=2)
        asyncio.run(go())
        _init_global()
        pool_into_global(
            [("PROJA", get_db_path("PROJA")), ("PROJB", get_db_path("PROJB"))],
            get_global_db_path(),
        )

        async def check():
            g = SqliteStore(get_global_db_path())
            try:
                # Recall scoped to PROJA only — uses the copied embeddings.
                notes = await g.recall(fake_vec(101), "PROJA", k=5)
                pids = {n.project_id for n in notes}
                assert pids == {"PROJA"}, pids
                # Pooling across both projects returns notes from both.
                both = await g.recall(None, ["PROJA", "PROJB"], k=10)
                assert {n.project_id for n in both} == {"PROJA", "PROJB"}
            finally:
                await g.aclose()
        asyncio.run(check())

    def test_idempotent_second_pool_skips(self):
        async def go():
            await _build_source("PROJA", doc_key="a1", text="alpha beta", note="note-a", seed=1)
        asyncio.run(go())
        _init_global()
        src = [("PROJA", get_db_path("PROJA"))]

        pool_into_global(src, get_global_db_path())
        assert _global_counts() == (1, 1, 1)

        stats2 = pool_into_global(src, get_global_db_path())
        assert stats2[0].docs_copied == 0
        assert stats2[0].docs_skipped == 1
        assert stats2[0].notes_copied == 0
        assert stats2[0].notes_skipped == 1
        # No duplicate rows after the second run.
        assert _global_counts() == (1, 1, 1)

    def test_supersede_chain_remapped(self):
        async def go():
            src = SqliteStore(get_db_path("PROJS"))
            src.assert_schema_version()
            old_id = await src.remember("original", "note", "PROJS", embedding=fake_vec(1))
            await src.supersede(int(old_id), "updated", "PROJS", embedding=fake_vec(2))
            await src.aclose()
        asyncio.run(go())
        _init_global()
        pool_into_global([("PROJS", get_db_path("PROJS"))], get_global_db_path())

        con = sqlite3.connect(str(get_global_db_path()))
        try:
            rows = con.execute(
                "SELECT id, content, superseded_by FROM memory_notes ORDER BY id"
            ).fetchall()
        finally:
            con.close()
        assert len(rows) == 2
        by_content = {r[1]: r for r in rows}
        old_row, new_row = by_content["original"], by_content["updated"]
        # The OLD note's superseded_by must point at the NEW note's global id.
        assert old_row[2] == new_row[0]

    def test_fingerprint_mismatch_raises(self):
        async def go():
            await _build_source("PROJX", doc_key="x1", text="alpha", note="n", seed=1)
        asyncio.run(go())
        # Corrupt the source fingerprint to simulate a different embedder.
        con = sqlite3.connect(str(get_db_path("PROJX")))
        try:
            con.execute("UPDATE embed_fingerprint SET fingerprint = 'bogus-model:99'")
            con.commit()
        finally:
            con.close()
        _init_global()

        with pytest.raises(PoolError):
            pool_into_global([("PROJX", get_db_path("PROJX"))], get_global_db_path())


class TestPoolNoteLinks:
    """B3: bc_note_links are copied + remapped during pooling, no dup on re-pool."""

    def _build_linked_source(self, pid: str) -> tuple[int, int]:
        async def go():
            src = SqliteStore(get_db_path(pid))
            src.assert_schema_version()
            a = int(await src.remember("alpha", "note", pid, embedding=fake_vec(1)))
            b = int(await src.remember("beta", "note", pid, embedding=fake_vec(2)))
            mem = await src._conn_get()
            await mem.execute(
                "INSERT INTO bc_note_links (src_id, dst_id, kind, weight) "
                "VALUES (?, ?, 'related', 0.9)",
                (a, b),
            )
            await mem.commit()
            await src.aclose()
            return a, b
        return asyncio.run(go())

    def _global_links(self):
        con = sqlite3.connect(str(get_global_db_path()))
        try:
            return con.execute(
                "SELECT l.src_id, l.dst_id, l.kind, sn.content, dn.content "
                "FROM bc_note_links l "
                "JOIN memory_notes sn ON sn.id = l.src_id "
                "JOIN memory_notes dn ON dn.id = l.dst_id"
            ).fetchall()
        finally:
            con.close()

    def test_links_copied_and_remapped(self):
        self._build_linked_source("PROJL")
        _init_global()
        stats = pool_into_global([("PROJL", get_db_path("PROJL"))], get_global_db_path())
        assert stats[0].links_copied == 1

        rows = self._global_links()
        assert len(rows) == 1
        _s, _d, kind, src_content, dst_content = rows[0]
        # Remapped to global ids that resolve back to the right note contents.
        assert kind == "related"
        assert src_content == "alpha" and dst_content == "beta"

    def test_repool_no_duplicate_links(self):
        self._build_linked_source("PROJL2")
        _init_global()
        src = [("PROJL2", get_db_path("PROJL2"))]
        pool_into_global(src, get_global_db_path())
        stats2 = pool_into_global(src, get_global_db_path())
        assert stats2[0].links_copied == 0, "re-pool must not duplicate links"
        assert len(self._global_links()) == 1


class TestPoolCli:
    def test_cli_rejects_implicit_all_registered_projects(self, tmp_path):
        from braincell.cli import main

        # Register two repo paths → fake ULIDs, and build their source brains.
        root_a, root_b = tmp_path / "repoA", tmp_path / "repoB"
        root_a.mkdir()
        root_b.mkdir()
        register_path(str(root_a), "PROJA")
        register_path(str(root_b), "PROJB")

        async def go():
            await _build_source("PROJA", doc_key="a1", text="alpha", note="na", seed=1)
            await _build_source("PROJB", doc_key="b1", text="beta", note="nb", seed=2)
        asyncio.run(go())

        with pytest.raises(SystemExit) as exc:
            main(["pool", "--all"])
        assert exc.value.code == 2
        assert not get_global_db_path().exists()
