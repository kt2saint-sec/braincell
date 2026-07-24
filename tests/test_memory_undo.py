# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_memory_undo.py — the v5 merge operation log and `braincell memory undo`.

`consolidate --apply` and `reflect --apply` are SOFT (tombstone / supersede), so the
rows always survived — but nothing recorded WHICH rows an operation touched or what
their prior state was, leaving hand-written SQL as the only recovery route. These
tests pin the recording + reversal contract:

  - an --apply run records exactly one operation covering the whole run
  - undo restores each note's EXACT prior deleted_at/superseded_by
  - undo of a reflect also tombstones the synthesis (else sources AND replacement
    would both be live)
  - undo REFUSES rather than clobbers a note a later writer changed
  - undo is not repeatable, and cannot reach another project's operation

Offline and deterministic: near-duplicate vectors are built analytically so cluster
membership does not depend on a live embedder.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from braincell.schema import MEMORY_SCHEMA_VERSION
from braincell.store import SqliteStore
from tests.conftest import fake_vec, make_store


def _near_dup_vec(base: np.ndarray, other: np.ndarray, target_cos: float) -> np.ndarray:
    """Unit float32 vector whose cosine to `base` is exactly `target_cos`."""
    perp = other.astype(np.float64) - float(np.dot(other, base)) * base.astype(np.float64)
    perp_norm = float(np.linalg.norm(perp))
    if perp_norm < 1e-8:
        return base.copy()
    perp_hat = perp / perp_norm
    v = (target_cos * base.astype(np.float64)
         + np.sqrt(max(0.0, 1.0 - target_cos ** 2)) * perp_hat)
    return (v / float(np.linalg.norm(v))).astype(np.float32)


async def _state(store: SqliteStore, note_id: int) -> tuple:
    """(deleted_at, superseded_by) straight from the row — the values undo restores."""
    mem = await store._conn_get()
    row = await (await mem.execute(
        "SELECT deleted_at, superseded_by FROM memory_notes WHERE id = ?", (note_id,),
    )).fetchone()
    return (row[0], row[1]) if row else (None, None)


# ── Schema ─────────────────────────────────────────────────────────────────────

def test_v5_tables_exist_and_version_bumped(tmp_path):
    store = make_store(tmp_path)

    async def _run():
        mem = await store._conn_get()
        names = {r[0] for r in await (await mem.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )).fetchall()}
        assert "bc_operations" in names
        assert "bc_operation_notes" in names

    asyncio.run(_run())
    # v5 shipped the op-log tables; later versions must keep them. >= not == so
    # this test pins the FEATURE, not the current version number.
    assert MEMORY_SCHEMA_VERSION >= 5


def test_reopen_is_idempotent(tmp_path):
    """Re-opening a v5 brain must not re-run the migration or duplicate anything."""
    store = make_store(tmp_path)
    asyncio.run(store.begin_operation("consolidate", "p1"))
    store.close()

    store2 = make_store(tmp_path)
    store2.assert_schema_version()
    ops = asyncio.run(store2.list_operations("p1"))
    assert len(ops) == 1, "re-open duplicated or lost operation rows"


# ── Recording ──────────────────────────────────────────────────────────────────

def test_empty_operation_is_dropped(tmp_path):
    """A run that records nothing must not litter `memory log` with a phantom entry."""
    store = make_store(tmp_path)

    async def _run():
        op = await store.begin_operation("consolidate", "p1")
        assert await store.finalize_operation(op) == 0
        assert await store.list_operations("p1") == []

    asyncio.run(_run())


def test_record_snapshots_prior_state_not_mutated_state(tmp_path):
    """The snapshot must capture state BEFORE the mutation. Recording twice keeps the
    FIRST snapshot, so a re-record after mutating cannot poison the restore value."""
    store = make_store(tmp_path)

    async def _run():
        nid = int(await store.remember("n", "note", "p1", embedding=fake_vec(1)))
        op = await store.begin_operation("consolidate", "p1")
        await store.record_operation_note(op, nid, "tombstoned")   # live: deleted_at NULL
        await store.forget(nid, "p1", hard=False)                  # now tombstoned
        await store.record_operation_note(op, nid, "tombstoned")   # must be ignored

        mem = await store._conn_get()
        rows = await (await mem.execute(
            "SELECT prev_deleted_at FROM bc_operation_notes WHERE op_id = ?", (op,),
        )).fetchall()
        assert len(rows) == 1, "duplicate snapshot rows for one (op, note, action)"
        assert rows[0][0] is None, "snapshot captured the POST-mutation state"

    asyncio.run(_run())


# ── Undo ───────────────────────────────────────────────────────────────────────

def test_undo_restores_tombstoned_notes(tmp_path):
    store = make_store(tmp_path)

    async def _run():
        keep = int(await store.remember("keep", "note", "p1", embedding=fake_vec(0)))
        drop = int(await store.remember("drop", "note", "p1", embedding=fake_vec(1)))

        op = await store.begin_operation("consolidate", "p1")
        await store.record_operation_note(op, drop, "tombstoned")
        await store.forget(drop, "p1", hard=False)
        await store.finalize_operation(op)
        assert (await _state(store, drop))[0] is not None, "precondition: not tombstoned"

        res = await store.undo_operation(op, "p1")
        assert res["restored"] == [drop]
        assert await _state(store, drop) == (None, None), "prior state not restored"
        assert await _state(store, keep) == (None, None), "undo touched an unrelated note"

    asyncio.run(_run())


def test_undo_of_reflect_tombstones_the_synthesis(tmp_path):
    """The load-bearing case: restoring sources while leaving their replacement live
    would make recall return the merged note AND everything it replaced."""
    store = make_store(tmp_path)

    async def _run():
        src = int(await store.remember("source", "note", "p1", embedding=fake_vec(1)))
        synth = int(await store.remember("synthesis", "note", "p1", embedding=fake_vec(2)))

        op = await store.begin_operation("reflect", "p1")
        await store.record_operation_note(op, synth, "created")
        await store.record_operation_note(op, src, "superseded")
        mem = await store._conn_get()
        await mem.execute(
            "UPDATE memory_notes SET superseded_by = ? WHERE id = ?", (synth, src),
        )
        await mem.commit()
        await store.forget(src, "p1", hard=False)
        await store.finalize_operation(op)

        await store.undo_operation(op, "p1")

        assert await _state(store, src) == (None, None), "source not fully restored"
        assert (await _state(store, synth))[0] is not None, (
            "the synthesized note is still live — undo left both it and its sources"
        )

    asyncio.run(_run())


def test_undo_skips_a_note_changed_since_the_merge(tmp_path):
    """A note a later writer already restored must be REPORTED, never clobbered."""
    store = make_store(tmp_path)

    async def _run():
        nid = int(await store.remember("n", "note", "p1", embedding=fake_vec(1)))
        op = await store.begin_operation("consolidate", "p1")
        await store.record_operation_note(op, nid, "tombstoned")
        await store.forget(nid, "p1", hard=False)
        await store.finalize_operation(op)

        # Someone else un-tombstones it before the undo runs.
        mem = await store._conn_get()
        await mem.execute("UPDATE memory_notes SET deleted_at = NULL WHERE id = ?", (nid,))
        await mem.commit()

        res = await store.undo_operation(op, "p1")
        assert res["skipped_changed"] == [nid]
        assert res["restored"] == []

    asyncio.run(_run())


def test_undo_reports_a_hard_deleted_note_as_missing(tmp_path):
    store = make_store(tmp_path)

    async def _run():
        nid = int(await store.remember("n", "note", "p1", embedding=fake_vec(1)))
        op = await store.begin_operation("consolidate", "p1")
        await store.record_operation_note(op, nid, "tombstoned")
        await store.forget(nid, "p1", hard=False)
        await store.finalize_operation(op)
        await store.forget(nid, "p1", hard=True)   # permanently gone

        res = await store.undo_operation(op, "p1")
        assert res["missing"] == [nid]
        assert res["restored"] == []

    asyncio.run(_run())


def test_undo_is_not_repeatable(tmp_path):
    store = make_store(tmp_path)

    async def _run():
        nid = int(await store.remember("n", "note", "p1", embedding=fake_vec(1)))
        op = await store.begin_operation("consolidate", "p1")
        await store.record_operation_note(op, nid, "tombstoned")
        await store.forget(nid, "p1", hard=False)
        await store.finalize_operation(op)

        await store.undo_operation(op, "p1")
        with pytest.raises(ValueError, match="already undone"):
            await store.undo_operation(op, "p1")

    asyncio.run(_run())


def test_undo_refuses_unknown_and_foreign_operations(tmp_path):
    store = make_store(tmp_path)

    async def _run():
        with pytest.raises(ValueError, match="No operation"):
            await store.undo_operation(999, "p1")

        op = await store.begin_operation("consolidate", "p1")
        with pytest.raises(ValueError, match="belongs to project"):
            await store.undo_operation(op, "other-project")

    asyncio.run(_run())


def test_operation_notes_cascade_on_operation_delete(tmp_path):
    """bc_operation_notes is FK'd to bc_operations ON DELETE CASCADE (no orphans),
    while note_id deliberately has NO FK so the audit trail survives a hard delete."""
    store = make_store(tmp_path)

    async def _run():
        nid = int(await store.remember("n", "note", "p1", embedding=fake_vec(1)))
        op = await store.begin_operation("consolidate", "p1")
        await store.record_operation_note(op, nid, "tombstoned")

        mem = await store._conn_get()
        await mem.execute("DELETE FROM bc_operations WHERE id = ?", (op,))
        await mem.commit()
        left = await (await mem.execute(
            "SELECT COUNT(*) FROM bc_operation_notes WHERE op_id = ?", (op,),
        )).fetchone()
        assert left[0] == 0, "cascade did not clean up operation notes"

        # And the reverse: hard-deleting the NOTE must not erase an audit row.
        op2 = await store.begin_operation("consolidate", "p1")
        await store.record_operation_note(op2, nid, "tombstoned")
        await store.forget(nid, "p1", hard=True)
        kept = await (await mem.execute(
            "SELECT COUNT(*) FROM bc_operation_notes WHERE op_id = ?", (op2,),
        )).fetchone()
        assert kept[0] == 1, "hard-deleting a note erased the audit trail"

    asyncio.run(_run())


def test_undone_operation_is_flagged_in_the_log(tmp_path):
    store = make_store(tmp_path)

    async def _run():
        nid = int(await store.remember("n", "note", "p1", embedding=fake_vec(1)))
        op = await store.begin_operation("consolidate", "p1", backup_path="/tmp/b.db")
        await store.record_operation_note(op, nid, "tombstoned")
        await store.forget(nid, "p1", hard=False)
        await store.finalize_operation(op)

        listed = (await store.list_operations("p1"))[0]
        assert listed["undone_at"] is None
        assert listed["note_count"] == 1
        assert listed["backup_path"] == "/tmp/b.db"

        await store.undo_operation(op, "p1")
        assert (await store.list_operations("p1"))[0]["undone_at"] is not None

    asyncio.run(_run())


# ── End-to-end through consolidate ─────────────────────────────────────────────

def test_consolidate_apply_records_an_undoable_operation(tmp_path):
    """Full path: real cluster merge → operation recorded → undo brings the merged-away
    note back to life so recall can return it again."""
    from braincell.cli import _consolidate_async

    store = make_store(tmp_path)
    v_base = fake_vec(0)
    v_near = _near_dup_vec(v_base, fake_vec(1), 0.95)

    async def _run():
        a = int(await store.remember("older dup", "note", "p1", embedding=v_base))
        b = int(await store.remember("newer dup", "note", "p1", embedding=v_near))

        await _consolidate_async(
            store, "p1", threshold=0.9, apply=True, use_llm=False, verbose=False,
        )
        ops = await store.list_operations("p1")
        assert len(ops) == 1 and ops[0]["kind"] == "consolidate"
        assert ops[0]["note_count"] == 1, "one of the pair should have been merged away"

        # Read WHICH note the operation touched rather than assuming which end of the
        # cluster is the representative — that ordering is consolidate's business.
        mem = await store._conn_get()
        merged_away = [r[0] for r in await (await mem.execute(
            "SELECT note_id FROM bc_operation_notes WHERE op_id = ?", (ops[0]["id"],),
        )).fetchall()]
        assert len(merged_away) == 1
        gone = merged_away[0]
        kept = b if gone == a else a

        assert (await _state(store, gone))[0] is not None, "merged-away note not tombstoned"
        assert await _state(store, kept) == (None, None), "representative was disturbed"

        res = await store.undo_operation(ops[0]["id"], "p1")
        assert res["restored"] == [gone]
        assert await _state(store, gone) == (None, None), "undo did not restore the note"
        assert await _state(store, kept) == (None, None), "undo disturbed the representative"

    asyncio.run(_run())


def test_consolidate_dry_run_records_nothing(tmp_path):
    from braincell.cli import _consolidate_async

    store = make_store(tmp_path)
    v_base = fake_vec(0)
    v_near = _near_dup_vec(v_base, fake_vec(1), 0.95)

    async def _run():
        await store.remember("older dup", "note", "p1", embedding=v_base)
        await store.remember("newer dup", "note", "p1", embedding=v_near)

        await _consolidate_async(
            store, "p1", threshold=0.9, apply=False, use_llm=False, verbose=False,
        )
        assert await store.list_operations("p1") == [], "dry-run wrote an operation"

    asyncio.run(_run())
