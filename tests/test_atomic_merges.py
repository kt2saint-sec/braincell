# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
test_atomic_merges.py — v6 all-or-nothing cluster merges.

Pre-v6, consolidate/reflect --apply committed each snapshot, supersede and
tombstone separately; a crash mid-cluster left duplicate truth (live synthesis +
live sources) or an un-undoable synthesis (absent from the op-log). These tests
inject a failure INSIDE the cluster transaction and assert the store is
byte-for-byte untouched — plus pin the happy paths and the SupersedeConflict
rollback of `consolidate_cluster_atomic`'s LLM path.
"""

import asyncio

import pytest

from braincell.store import SupersedeConflict
from tests.conftest import fake_vec, make_store


class _InjectedCrash(RuntimeError):
    pass


def _fail_tombstone_on(store, target_id: int):
    """Patch the store so tombstoning `target_id` raises mid-transaction."""
    original = store._tombstone_note_tx

    async def _failing(mem, note_id, project_id):
        if note_id == target_id:
            raise _InjectedCrash(f"injected crash tombstoning note {note_id}")
        return await original(mem, note_id, project_id)

    store._tombstone_note_tx = _failing


async def _snapshot_notes(store) -> dict:
    mem = await store._conn_get()
    rows = await (await mem.execute(
        "SELECT id, status, superseded_by, deleted_at FROM memory_notes ORDER BY id"
    )).fetchall()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


async def _op_note_count(store, op_id: int) -> int:
    mem = await store._conn_get()
    row = await (await mem.execute(
        "SELECT COUNT(*) FROM bc_operation_notes WHERE op_id = ?", (op_id,),
    )).fetchone()
    return row[0]


class TestReflectClusterAtomic:
    def test_happy_path_retires_sources_and_records_op(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            a = int(await store.remember("fact A", "note", "P1", embedding=fake_vec(1)))
            b = int(await store.remember("fact B", "note", "P1", embedding=fake_vec(2)))
            op = await store.begin_operation("reflect", "P1")
            synth = await store.reflect_cluster_atomic(
                op, "P1", [a, b], "A and B, unified", embedding=fake_vec(3),
            )
            return a, b, op, synth, await _snapshot_notes(store), \
                await _op_note_count(store, op)

        a, b, _op, synth, notes, op_rows = asyncio.run(_run())
        # Sources: superseded_by → synth, tombstoned (tombstone dominates).
        for src in (a, b):
            status, sup, deleted = notes[src]
            assert (status, sup) == ("tombstoned", synth)
            assert deleted is not None
        assert notes[synth][0] == "active"
        assert op_rows == 3  # 1 created + 2 superseded

    def test_crash_mid_cluster_leaves_store_untouched(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            a = int(await store.remember("fact A", "note", "P1", embedding=fake_vec(1)))
            b = int(await store.remember("fact B", "note", "P1", embedding=fake_vec(2)))
            op = await store.begin_operation("reflect", "P1")
            before = await _snapshot_notes(store)
            _fail_tombstone_on(store, b)  # first source succeeds, second crashes
            with pytest.raises(_InjectedCrash):
                await store.reflect_cluster_atomic(
                    op, "P1", [a, b], "A and B, unified", embedding=fake_vec(3),
                )
            return op, before, await _snapshot_notes(store), \
                await _op_note_count(store, op)

        _op, before, after, op_rows = asyncio.run(_run())
        # The whole cluster reverted: no synthesis row, source A NOT superseded,
        # and — the pre-v6 failure mode — no orphaned op-log rows either.
        assert after == before
        assert op_rows == 0


class TestConsolidateClusterAtomic:
    def test_deterministic_crash_rolls_back_all_drops(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            keep = int(await store.remember("keep me", "note", "P1"))
            d1 = int(await store.remember("dup one", "note", "P1"))
            d2 = int(await store.remember("dup two", "note", "P1"))
            op = await store.begin_operation("consolidate", "P1")
            before = await _snapshot_notes(store)
            _fail_tombstone_on(store, d2)
            with pytest.raises(_InjectedCrash):
                await store.consolidate_cluster_atomic(op, "P1", [keep, d1, d2], keep)
            return op, before, await _snapshot_notes(store), \
                await _op_note_count(store, op)

        _op, before, after, op_rows = asyncio.run(_run())
        assert after == before  # d1 was NOT tombstoned despite preceding d2
        assert op_rows == 0

    def test_llm_path_supersede_conflict_rolls_back_whole_cluster(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            rep = int(await store.remember("rep note", "note", "P1"))
            d1 = int(await store.remember("dup", "note", "P1"))
            # A concurrent writer beats the merge to the representative.
            await store.supersede(rep, "raced replacement", "P1")
            op = await store.begin_operation("consolidate", "P1")
            before = await _snapshot_notes(store)
            with pytest.raises(SupersedeConflict):
                await store.consolidate_cluster_atomic(
                    op, "P1", [rep, d1], rep, merged_content="merged body",
                )
            return op, before, await _snapshot_notes(store), \
                await _op_note_count(store, op)

        _op, before, after, op_rows = asyncio.run(_run())
        assert after == before  # no merged note persisted, d1 still active
        assert op_rows == 0

    def test_llm_path_happy_merge_is_undoable(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            rep = int(await store.remember("rep note", "decision", "P1"))
            d1 = int(await store.remember("dup", "decision", "P1"))
            op = await store.begin_operation("consolidate", "P1")
            new_id = await store.consolidate_cluster_atomic(
                op, "P1", [rep, d1], rep, merged_content="merged truth",
            )
            await store.finalize_operation(op)
            merged = await _snapshot_notes(store)
            undo = await store.undo_operation(op, "P1")
            return rep, d1, new_id, merged, undo, await _snapshot_notes(store)

        rep, d1, new_id, merged, undo, restored = asyncio.run(_run())
        # Merged state: both members tombstoned, rep superseded by the new note,
        # the new note inherits the representative's kind and is live.
        assert merged[rep] == ("tombstoned", new_id, merged[rep][2])
        assert merged[d1][0] == "tombstoned"
        assert merged[new_id][0] == "active"
        # Undo: members restored to active, the merged note tombstoned.
        assert restored[rep] == ("active", None, None)
        assert restored[d1] == ("active", None, None)
        assert restored[new_id][0] == "tombstoned"
        assert set(undo["restored"]) == {rep, d1, new_id}
