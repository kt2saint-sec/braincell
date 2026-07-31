# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_pool_sync.py — M8: `braincell pool` CONVERGES the global brain on its sources.

The failure these lock down: pooling used to be copy-once. A note pooled while it
was current kept `superseded_by = NULL` in the global brain forever, so the global
brain went on answering with decisions the owning project had already retracted —
and an edited document stayed indexed under its original text.

Covers:
  - re-pool propagates supersession, tombstones and edited note content;
  - re-pool replaces the chunks of a document whose content_hash changed;
  - note_uid survives project → global and is stable across re-pools;
  - global recall returns current truth after a re-pool (end-to-end);
  - --prune mirrors source hard-deletes; the default run does not;
  - --prune spares notes born in the global brain, and cascades edges / clears
    inbound supersession pointers with foreign keys enforced;
  - a note_uid claimed by two different projects is refused, not merged;
  - id remapping holds when source ids and global ids DIVERGE (see
    TestPoolIdRemapping — a fresh global brain aligns them 1:1 and hides the bug).
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3

from braincell.config import get_db_path, get_global_db_path
from braincell.pool import pool_into_global
from braincell.store import SqliteStore
from tests.conftest import _insert_doc_and_chunk, fake_vec

PA = "01POOLPROJECTA0000000000"
PB = "01POOLPROJECTB0000000000"


def _init_global() -> None:
    g = SqliteStore(get_global_db_path())
    g.assert_schema_version()
    g.close()


def _pool(pid: str = PA, **kw) -> list:
    return pool_into_global([(pid, get_db_path(pid))], get_global_db_path(), **kw)


async def _source(pid: str = PA) -> SqliteStore:
    store = SqliteStore(get_db_path(pid))
    store.assert_schema_version()
    return store


def _global_query(sql: str, params: tuple = ()):
    con = sqlite3.connect(str(get_global_db_path()))
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


class TestPoolPropagatesMutations:
    def test_supersede_after_pool_propagates_on_repool(self):
        """The headline bug: pool → supersede → pool again."""
        async def go():
            src = await _source()
            old = int(await src.remember("Use Redis", "decision", PA, embedding=fake_vec(1)))
            await src.aclose()
            _init_global()
            _pool()
            # Global copy is current at this point — superseded_by must be NULL.
            assert _global_query("SELECT superseded_by FROM memory_notes")[0][0] is None

            src = await _source()
            await src.supersede(old, "Use an in-process cache", PA, embedding=fake_vec(2))
            await src.aclose()
            _pool()

        asyncio.run(go())
        rows = _global_query(
            "SELECT content, superseded_by FROM memory_notes ORDER BY id"
        )
        assert len(rows) == 2, f"expected old + replacement in global, got {rows}"
        by_content = {r[0]: r[1] for r in rows}
        assert by_content["Use an in-process cache"] is None
        assert by_content["Use Redis"] is not None, (
            "the pooled copy never learned it had been superseded — this is the copy-once bug"
        )

    def test_global_recall_returns_current_truth_after_repool(self):
        """End-to-end: the global brain answers with the replacement, not the retraction."""
        async def go():
            src = await _source()
            old = int(await src.remember("Use Redis for caching", "decision", PA,
                                         embedding=fake_vec(1)))
            await src.aclose()
            _init_global()
            _pool()
            src = await _source()
            await src.supersede(old, "Caching is in-process now", PA, embedding=fake_vec(2))
            await src.aclose()
            _pool()

            g = SqliteStore(get_global_db_path())
            g.assert_schema_version()
            notes = await g.recall(None, None, k=5, qtext="Redis")
            await g.aclose()
            return notes

        notes = asyncio.run(go())
        assert notes, "global recall returned nothing"
        assert all(n.superseded_by is None for n in notes)
        assert "in-process" in notes[0].content

    def test_tombstone_after_pool_propagates_on_repool(self):
        async def go():
            src = await _source()
            note = int(await src.remember("retract me", "note", PA, embedding=fake_vec(1)))
            await src.aclose()
            _init_global()
            _pool()
            assert _global_query("SELECT deleted_at FROM memory_notes")[0][0] is None

            src = await _source()
            await src.forget(note, PA)
            await src.aclose()
            _pool()

        asyncio.run(go())
        assert _global_query("SELECT deleted_at FROM memory_notes")[0][0] is not None, (
            "a retraction at the source never reached the pooled copy"
        )

    def test_edited_document_replaces_chunks_on_repool(self):
        async def go():
            src = await _source()
            await _insert_doc_and_chunk(src, project=PA, doc_key="d1",
                                        text="original text", seed=1)
            await src.aclose()
            _init_global()
            _pool()
            assert [r[0] for r in _global_query("SELECT chunk_text FROM bc_chunks")] == [
                "original text"
            ]

            src = await _source()
            await _insert_doc_and_chunk(src, project=PA, doc_key="d1",
                                        text="rewritten text", seed=2)
            await src.aclose()
            return _pool()

        stats = asyncio.run(go())
        chunks = [r[0] for r in _global_query("SELECT chunk_text FROM bc_chunks")]
        assert chunks == ["rewritten text"], (
            f"the global brain is still indexing the old document text: {chunks}"
        )
        assert stats[0].docs_updated == 1
        assert stats[0].chunks_replaced == 1
        assert _global_query("SELECT COUNT(*) FROM bc_documents")[0][0] == 1

    def test_unchanged_document_is_skipped(self):
        async def go():
            src = await _source()
            await _insert_doc_and_chunk(src, project=PA, doc_key="d1", text="stable", seed=1)
            await src.aclose()
            _init_global()
            _pool()
            return _pool()

        stats = asyncio.run(go())
        assert stats[0].docs_skipped == 1
        assert stats[0].docs_updated == 0
        assert stats[0].chunks_replaced == 0


class TestPoolIdRemapping:
    """Remapping tests MUST force source ids and global ids apart.

    Every other pool test pools into a fresh global brain, where the global ids
    happen to come out identical to the source ids (1→1, 2→2, …). Under that
    alignment a bug that wrote a SOURCE-local `superseded_by` straight into the
    global brain would still dereference to the right row by coincidence — the test
    would pass while the data was wrong. These tests pool a decoy project FIRST so
    the ids are offset, which is the only arrangement that can actually observe the
    remap.
    """

    def test_supersede_remap_survives_id_divergence(self):
        async def go():
            # Decoy project: consumes global ids 1..2 so PA's rows land at 3+.
            decoy = await _source(PB)
            await decoy.remember("decoy one", "note", PB, embedding=fake_vec(8))
            await decoy.remember("decoy two", "note", PB, embedding=fake_vec(9))
            await decoy.aclose()

            src = await _source(PA)
            old = int(await src.remember("Use Redis", "decision", PA, embedding=fake_vec(1)))
            await src.aclose()

            _init_global()
            _pool(PB)
            _pool(PA)

            src = await _source(PA)
            new_local = int(await src.supersede(
                old, "Use an in-process cache", PA, embedding=fake_vec(2)
            ))
            mem = await src._conn_get()
            uid_row = await (await mem.execute(
                "SELECT note_uid FROM memory_notes WHERE id = ?", (new_local,)
            )).fetchone()
            await src.aclose()
            _pool(PA)
            return old, new_local, uid_row[0]

        _old_local, new_local, new_uid = asyncio.run(go())

        global_new_id = _global_query(
            "SELECT id FROM memory_notes WHERE note_uid = ?", (new_uid,)
        )[0][0]
        pointer = _global_query(
            "SELECT superseded_by FROM memory_notes WHERE content = 'Use Redis'"
        )[0][0]

        assert global_new_id != new_local, (
            "fixture failed to diverge the ids — this test proves nothing unless the "
            f"global id ({global_new_id}) differs from the source id ({new_local})"
        )
        assert pointer == global_new_id, (
            f"superseded_by points at {pointer}, but the replacement lives at global id "
            f"{global_new_id} — a source-local id was written into the global brain"
        )
        assert pointer != new_local, "the raw source-local id leaked into the global brain"

    def test_note_links_remap_survives_id_divergence(self):
        """The same masking hazard applies to the copied note graph."""
        async def go():
            decoy = await _source(PB)
            await decoy.remember("decoy one", "note", PB, embedding=fake_vec(8))
            await decoy.remember("decoy two", "note", PB, embedding=fake_vec(9))
            await decoy.aclose()

            src = await _source(PA)
            a = int(await src.remember("anchor", "note", PA, embedding=fake_vec(1)))
            b = int(await src.remember("target", "note", PA, embedding=fake_vec(2)))
            mem = await src._conn_get()
            await mem.execute(
                "INSERT INTO bc_note_links (src_id, dst_id, kind, weight) "
                "VALUES (?, ?, 'related', 0.7)", (a, b),
            )
            await mem.commit()
            await src.aclose()

            _init_global()
            _pool(PB)
            _pool(PA)
            return a, b

        src_a, src_b = asyncio.run(go())
        edges = _global_query("SELECT src_id, dst_id FROM bc_note_links")
        anchor = _global_query(
            "SELECT id FROM memory_notes WHERE content = 'anchor'")[0][0]
        target = _global_query(
            "SELECT id FROM memory_notes WHERE content = 'target'")[0][0]
        assert (anchor, target) != (src_a, src_b), "fixture failed to diverge the ids"
        assert edges == [(anchor, target)], (
            f"note-link ids were not remapped: edges={edges}, expected [({anchor}, {target})]"
        )


class TestPoolIdentity:
    def test_note_uid_survives_and_is_stable_across_repools(self):
        async def go():
            src = await _source()
            await src.remember("stable identity", "note", PA, embedding=fake_vec(1))
            src_uid = None
            mem = await src._conn_get()
            src_uid = (await (await mem.execute(
                "SELECT note_uid FROM memory_notes"
            )).fetchone())[0]
            await src.aclose()
            _init_global()
            _pool()
            first = _global_query("SELECT id, note_uid FROM memory_notes")
            _pool()
            second = _global_query("SELECT id, note_uid FROM memory_notes")
            return src_uid, first, second

        src_uid, first, second = asyncio.run(go())
        assert len(first) == 1 and len(second) == 1, "re-pool duplicated the note"
        assert first[0][1] == src_uid, "the pooled copy was given a different identity"
        assert first == second, "the note's global id/uid changed on re-pool"

    def test_uid_claimed_by_two_projects_is_refused(self):
        """One note belongs to exactly one project — never merge across owners."""
        async def go():
            src_a = await _source(PA)
            await src_a.remember("shared uid note", "note", PA, embedding=fake_vec(1))
            mem = await src_a._conn_get()
            uid = (await (await mem.execute("SELECT note_uid FROM memory_notes")).fetchone())[0]
            await src_a.aclose()

            src_b = await _source(PB)
            await src_b.remember("impostor", "note", PB, embedding=fake_vec(2))
            memb = await src_b._conn_get()
            await memb.execute("UPDATE memory_notes SET note_uid = ?", (uid,))
            await memb.commit()
            await src_b.aclose()

            _init_global()
            _pool(PA)
            return _pool(PB)

        stats = asyncio.run(go())
        assert stats[0].conflicts == 1
        contents = [r[0] for r in _global_query("SELECT content FROM memory_notes")]
        assert contents == ["shared uid note"], (
            f"a foreign project overwrote another project's note: {contents}"
        )


class TestPoolPrune:
    def test_default_run_keeps_source_hard_deletes(self):
        async def go():
            src = await _source()
            note = int(await src.remember("purge me", "note", PA, embedding=fake_vec(1)))
            await src.aclose()
            _init_global()
            _pool()
            src = await _source()
            await src.forget(note, PA, hard=True)
            await src.aclose()
            return _pool()

        stats = asyncio.run(go())
        assert _global_query("SELECT COUNT(*) FROM memory_notes")[0][0] == 1, (
            "the default pool run must not delete pooled rows"
        )
        assert stats[0].notes_pruned == 0

    def test_prune_mirrors_source_hard_deletes(self):
        async def go():
            src = await _source()
            note = int(await src.remember("purge me", "note", PA, embedding=fake_vec(1)))
            await src.remember("keep me", "note", PA, embedding=fake_vec(2))
            await _insert_doc_and_chunk(src, project=PA, doc_key="gone", text="doc", seed=3)
            await src.aclose()
            _init_global()
            _pool()
            src = await _source()
            await src.forget(note, PA, hard=True)
            mem = await src._conn_get()
            await mem.execute("DELETE FROM bc_chunks")
            await mem.execute("DELETE FROM bc_documents")
            await mem.commit()
            await src.aclose()
            return _pool(prune=True)

        stats = asyncio.run(go())
        contents = [r[0] for r in _global_query("SELECT content FROM memory_notes")]
        assert contents == ["keep me"], f"prune removed the wrong rows: {contents}"
        assert stats[0].notes_pruned == 1
        assert stats[0].docs_pruned == 1
        assert _global_query("SELECT COUNT(*) FROM bc_chunks")[0][0] == 0, (
            "pruned documents must take their chunks with them (ON DELETE CASCADE)"
        )

    def test_prune_of_linked_note_leaves_no_orphan_edges(self):
        """Pruning a note with bc_note_links edges must cascade them away.

        With foreign keys ON, a DELETE that ignored the edges would raise; with
        them OFF it would silently orphan the graph. Neither may happen.
        """
        async def go():
            src = await _source(PA)
            n1 = int(await src.remember("doomed", "note", PA, embedding=fake_vec(1)))
            n2 = int(await src.remember("survivor", "note", PA, embedding=fake_vec(2)))
            mem = await src._conn_get()
            for a, b in ((n1, n2), (n2, n1)):
                await mem.execute(
                    "INSERT OR IGNORE INTO bc_note_links (src_id, dst_id, kind, weight) "
                    "VALUES (?, ?, 'related', 0.9)",
                    (a, b),
                )
            await mem.commit()
            await src.aclose()
            _init_global()
            _pool(PA)
            assert _global_query("SELECT COUNT(*) FROM bc_note_links")[0][0] == 2

            src = await _source(PA)
            await src.forget(n1, PA, hard=True)
            await src.aclose()
            return _pool(PA, prune=True)  # must not raise with foreign_keys ON

        stats = asyncio.run(go())
        assert stats[0].notes_pruned == 1
        assert _global_query("SELECT COUNT(*) FROM bc_note_links")[0][0] == 0, (
            "pruning a linked note left orphan edges in bc_note_links"
        )
        con = sqlite3.connect(str(get_global_db_path()))
        try:
            con.execute("PRAGMA foreign_keys=ON")
            assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            con.close()

    def test_prune_spares_notes_born_in_the_global_brain(self):
        """Prune is scoped by PROVENANCE, not ownership.

        A note written directly into the global brain by a global-mode `remember`
        carries the pooled project's project_id but has no counterpart in that
        project's own brain. Pruning on ownership alone deleted it — data loss in an
        opt-in destructive path. Only rows stamped `pooled_from` are candidates.
        """
        async def go():
            src = await _source(PA)
            await src.remember("pooled from the project", "note", PA, embedding=fake_vec(1))
            await src.aclose()
            _init_global()
            g = SqliteStore(get_global_db_path())
            g.assert_schema_version()
            await g.remember("written straight into global", "note", PA,
                             embedding=fake_vec(2))
            await g.aclose()
            _pool(PA, prune=True)

        asyncio.run(go())
        contents = sorted(r[0] for r in _global_query("SELECT content FROM memory_notes"))
        assert contents == ["pooled from the project", "written straight into global"], (
            f"--prune destroyed a note that was born in the global brain: {contents}"
        )

    def test_prune_skips_rows_pooled_before_provenance_existed(self):
        """Legacy pooled rows (pooled_from NULL) are skipped, not deleted — fail-safe."""
        async def go():
            src = await _source(PA)
            note = int(await src.remember("legacy pooled note", "note", PA,
                                          embedding=fake_vec(1)))
            await src.aclose()
            _init_global()
            _pool(PA)
            # Simulate a row pooled by the pre-provenance build.
            con = sqlite3.connect(str(get_global_db_path()))
            try:
                con.execute("UPDATE memory_notes SET pooled_from = NULL")
                con.commit()
            finally:
                con.close()
            src = await _source(PA)
            await src.forget(note, PA, hard=True)
            await src.aclose()
            return _pool(PA, prune=True)

        stats = asyncio.run(go())
        assert stats[0].notes_pruned == 0
        assert _global_query("SELECT COUNT(*) FROM memory_notes")[0][0] == 1, (
            "prune deleted a row whose provenance was unknown — must fail safe"
        )

    def test_prune_clears_inbound_supersession_pointers(self):
        """A pruned note may be the target of another note's superseded_by."""
        async def go():
            src = await _source(PA)
            old = int(await src.remember("v1", "decision", PA, embedding=fake_vec(1)))
            new = int(await src.supersede(old, "v2", PA, embedding=fake_vec(2)))
            await src.aclose()
            _init_global()
            _pool(PA)
            # Purge the REPLACEMENT at the source; the older note still points at it.
            src = await _source(PA)
            await src.forget(new, PA, hard=True)
            await src.aclose()
            return _pool(PA, prune=True)

        asyncio.run(go())
        rows = _global_query("SELECT content, superseded_by FROM memory_notes")
        assert rows == [("v1", None)], (
            f"prune left a dangling supersession pointer: {rows}"
        )

    def test_prune_never_touches_another_project(self):
        async def go():
            src_a = await _source(PA)
            await src_a.remember("project A note", "note", PA, embedding=fake_vec(1))
            await src_a.aclose()
            src_b = await _source(PB)
            await src_b.remember("project B note", "note", PB, embedding=fake_vec(2))
            await src_b.aclose()
            _init_global()
            _pool(PA)
            _pool(PB)
            # Empty project A entirely, then prune ONLY project A.
            src_a = await _source(PA)
            mem = await src_a._conn_get()
            await mem.execute("DELETE FROM memory_notes")
            await mem.commit()
            await src_a.aclose()
            return _pool(PA, prune=True)

        asyncio.run(go())
        contents = [r[0] for r in _global_query("SELECT content FROM memory_notes")]
        assert contents == ["project B note"], (
            f"pruning one project damaged another project's memory: {contents}"
        )


class TestPoolIntegrity:
    def test_global_brain_passes_foreign_key_check_after_pool(self):
        async def go():
            src = await _source()
            old = int(await src.remember("v1", "decision", PA, embedding=fake_vec(1)))
            await src.supersede(old, "v2", PA, embedding=fake_vec(2))
            await src.aclose()
            _init_global()
            _pool()

        asyncio.run(go())
        con = sqlite3.connect(str(get_global_db_path()))
        try:
            con.execute("PRAGMA foreign_keys=ON")
            assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            con.close()

    def test_pool_migrates_a_pre_v4_source(self):
        """A source not opened since the upgrade still pools (it gets uids first)."""
        async def go():
            src = await _source()
            await src.remember("legacy note", "note", PA, embedding=fake_vec(1))
            await src.aclose()

        asyncio.run(go())
        con = sqlite3.connect(str(get_db_path(PA)))
        try:  # rewind the source to v3
            con.execute("UPDATE memory_notes SET note_uid = NULL")
            con.execute("UPDATE schema_version SET version = 3")
            con.commit()
        finally:
            con.close()

        _init_global()
        stats = _pool()
        assert stats[0].notes_copied == 1
        uid = _global_query("SELECT note_uid FROM memory_notes")[0][0]
        assert uid, "pooled note reached the global brain without a stable identity"

    def test_hash_of_pooled_chunk_text_is_unchanged(self):
        """Embeddings/text are reused verbatim — pooling never re-embeds."""
        async def go():
            src = await _source()
            await _insert_doc_and_chunk(src, project=PA, doc_key="d1", text="verbatim", seed=7)
            mem = await src._conn_get()
            row = await (await mem.execute("SELECT embedding FROM bc_chunks")).fetchone()
            digest = hashlib.sha256(bytes(row[0])).hexdigest()
            await src.aclose()
            _init_global()
            _pool()
            return digest

        digest = asyncio.run(go())
        pooled = _global_query("SELECT embedding FROM bc_chunks")[0][0]
        assert hashlib.sha256(bytes(pooled)).hexdigest() == digest
