# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
test_contradiction_guard.py — v6 warn-only contradiction guard + offline audit.

Covers: `store.find_conflicts` (threshold, k cap, active-only, disable switch),
the `remember` MCP tool surfacing potential_conflicts without ever blocking the
write, and the read-only `contradictions` audit with an injected judge.
"""

import asyncio
from unittest.mock import MagicMock

import numpy as np

from tests.conftest import fake_vec, make_store


def _near(vec: np.ndarray, eps: float = 0.05) -> np.ndarray:
    """A unit vector slightly rotated from *vec* (cosine ≈ 1-eps²/2 > 0.99)."""
    out = vec + eps * fake_vec(999)
    return (out / np.linalg.norm(out)).astype(np.float32)


class TestFindConflicts:
    def test_close_active_note_is_surfaced(self, tmp_path):
        store = make_store(tmp_path)
        base = fake_vec(1)

        async def _run():
            nid = int(await store.remember("use Redis", "decision", "P1",
                                           embedding=base))
            return nid, await store.find_conflicts("P1", _near(base))

        nid, hits = asyncio.run(_run())
        assert [c.id for c in hits] == [nid]
        assert hits[0].cosine > 0.95
        assert hits[0].kind == "decision"

    def test_unrelated_and_retired_notes_are_not_conflicts(self, tmp_path):
        store = make_store(tmp_path)
        base = fake_vec(1)

        async def _run():
            near_id = int(await store.remember("close truth", "note", "P1",
                                               embedding=base))
            # Unrelated vector — far below any sane threshold.
            await store.remember("far away", "note", "P1", embedding=fake_vec(7))
            # Tombstone the close one: retired truth is not a conflict.
            await store.forget(near_id, "P1", hard=False)
            return await store.find_conflicts("P1", _near(base))

        assert asyncio.run(_run()) == []

    def test_superseded_note_is_not_a_conflict_its_replacement_is(self, tmp_path):
        store = make_store(tmp_path)
        base = fake_vec(1)

        async def _run():
            old = int(await store.remember("old wording", "note", "P1",
                                           embedding=base))
            new = await store.supersede(old, "new wording", "P1",
                                        embedding=_near(base))
            return new, await store.find_conflicts("P1", _near(base, 0.04))

        new, hits = asyncio.run(_run())
        assert [c.id for c in hits] == [new]  # only current truth competes

    def test_disable_switch_and_null_embedding(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            await store.remember("something", "note", "P1", embedding=fake_vec(1))
            none_vec = await store.find_conflicts("P1", None)
            k_zero = await store.find_conflicts("P1", fake_vec(1), k=0)
            return none_vec, k_zero

        none_vec, k_zero = asyncio.run(_run())
        assert none_vec == [] and k_zero == []


class TestRememberToolSurfacesConflicts:
    def test_conflicts_returned_and_write_never_blocked(self, tmp_path, monkeypatch):
        import braincell.server as srv

        store = make_store(tmp_path)
        ctx = MagicMock()
        ctx.request_context.lifespan_context = srv.AppState(store=store)
        base = fake_vec(1)

        async def fixed_embed(texts):
            return [_near(base)]

        monkeypatch.setattr(srv, "embed_texts_async", fixed_embed)
        monkeypatch.setattr(srv, "_pin_write_project", lambda p: p or "P1")

        async def _run():
            first = int(await store.remember("original decision", "decision", "P1",
                                             embedding=base))
            result = await srv.remember("contradicting decision", "decision",
                                        ctx=ctx)
            notes_after = await store.recall(None, "P1", k=10, qtext=None)
            return first, result, notes_after

        first, result, notes_after = asyncio.run(_run())
        assert [c.note_id for c in result.potential_conflicts] == [first]
        assert result.potential_conflicts[0].snippet.startswith("original decision")
        # Warn-only: BOTH notes persisted — nothing was blocked or auto-superseded.
        assert len(notes_after) == 2

    def test_scan_failure_never_blocks_the_write(self, tmp_path, monkeypatch):
        import braincell.server as srv

        store = make_store(tmp_path)
        ctx = MagicMock()
        ctx.request_context.lifespan_context = srv.AppState(store=store)

        async def fixed_embed(texts):
            return [fake_vec(1)]

        async def broken_scan(*a, **kw):
            raise RuntimeError("scan exploded")

        monkeypatch.setattr(srv, "embed_texts_async", fixed_embed)
        monkeypatch.setattr(srv, "_pin_write_project", lambda p: p or "P1")
        monkeypatch.setattr(store, "find_conflicts", broken_scan)

        async def _run():
            return await srv.remember("still persists", "note", ctx=ctx)

        result = asyncio.run(_run())
        assert result.note_id and result.potential_conflicts == []


class TestContradictionsAudit:
    def test_judged_pairs_and_read_only(self, tmp_path):
        from braincell.contradictions import find_contradictions

        store = make_store(tmp_path)
        base = fake_vec(1)

        async def _run():
            a = int(await store.remember("keep the v1 auth flow", "decision", "P1",
                                         embedding=base))
            b = int(await store.remember("drop the v1 auth flow", "decision", "P1",
                                         embedding=_near(base)))
            await store.remember("unrelated topic", "note", "P1",
                                 embedding=fake_vec(7))
            mem = await store._conn_get()
            before = await (await mem.execute(
                "SELECT id, status, superseded_by, deleted_at FROM memory_notes "
                "ORDER BY id")).fetchall()
            report = await find_contradictions(
                store, "P1",
                judge_fn=lambda x, y: "contradicts",
            )
            after = await (await mem.execute(
                "SELECT id, status, superseded_by, deleted_at FROM memory_notes "
                "ORDER BY id")).fetchall()
            return a, b, report, before, after

        a, b, report, before, after = asyncio.run(_run())
        assert report.notes_scanned == 3
        assert [(p.id_a, p.id_b, p.verdict) for p in report.pairs] == \
            [(a, b, "contradicts")]
        assert report.contradictions and report.pairs_judged == 1
        assert after == before  # the audit wrote NOTHING

    def test_no_judge_means_unjudged_never_coerced(self, tmp_path):
        from braincell.contradictions import find_contradictions

        store = make_store(tmp_path)
        base = fake_vec(1)

        async def _run():
            await store.remember("one", "note", "P1", embedding=base)
            await store.remember("two", "note", "P1", embedding=_near(base))
            no_judge = await find_contradictions(store, "P1", judge_fn=None)
            off_script = await find_contradictions(
                store, "P1", judge_fn=lambda x, y: None,
            )
            return no_judge, off_script

        no_judge, off_script = asyncio.run(_run())
        assert [p.verdict for p in no_judge.pairs] == ["unjudged"]
        assert [p.verdict for p in off_script.pairs] == ["unjudged"]
        assert off_script.pairs_judged == 0

    def test_limit_caps_judging_by_highest_cosine(self, tmp_path):
        from braincell.contradictions import find_contradictions

        store = make_store(tmp_path)
        base = fake_vec(1)

        async def _run():
            await store.remember("a", "note", "P1", embedding=base)
            await store.remember("b", "note", "P1", embedding=_near(base, 0.02))
            await store.remember("c", "note", "P1", embedding=_near(base, 0.30))
            return await find_contradictions(
                store, "P1", limit=1, judge_fn=lambda x, y: "consistent",
            )

        report = asyncio.run(_run())
        assert len(report.pairs) == 1
        assert report.pairs_over_threshold >= 2  # the cap is visible, not silent
        assert report.pairs[0].cosine > 0.99  # the tightest pair won the slot
