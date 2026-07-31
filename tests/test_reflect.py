# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_reflect.py — B4 `braincell reflect` (LLM consolidation).

Offline: the LLM (synth_fn) and embedder (embed_fn) are injected stubs, so no
Ollama is required. Covers:
  - clustering determinism (same fixture → same clusters considered);
  - dry-run writes nothing;
  - apply synthesizes one note per cluster and supersedes + tombstones sources;
  - idempotent re-run produces no new notes;
  - synthesis-unavailable (synth_fn returns None) skips gracefully.
"""

from __future__ import annotations

import asyncio

from braincell.reflect import reflect
from tests.conftest import fake_vec, make_store


def _run(coro):
    return asyncio.run(coro)


async def _seed_cluster(store, project="P"):
    """Two near-identical (auto-clusterable) notes with embeddings."""
    v = fake_vec(1)
    a = int(await store.remember("db timeout was root cause A", "note", project, embedding=v))
    b = int(await store.remember("db timeout was root cause B", "note", project, embedding=v))
    return a, b


def _live_notes(store):
    async def go():
        mem = await store._conn_get()
        rows = await (await mem.execute(
            "SELECT id, content, superseded_by, deleted_at FROM memory_notes ORDER BY id"
        )).fetchall()
        return rows
    return _run(go())


class TestReflectDryRun:
    def test_dry_run_writes_nothing(self, tmp_path):
        s = make_store(tmp_path)

        async def go():
            await _seed_cluster(s)
            before = len(await (await (await s._conn_get()).execute(
                "SELECT id FROM memory_notes")).fetchall())
            res = await reflect(s, "P", threshold=0.9, apply=False,
                                synth_fn=lambda c: "SHOULD NOT BE WRITTEN")
            after = len(await (await (await s._conn_get()).execute(
                "SELECT id FROM memory_notes")).fetchall())
            await s.aclose()
            return res, before, after

        res, before, after = _run(go())
        assert res.synthesized == 0
        assert res.clusters_considered == 1
        assert before == after == 2, "dry-run must not write any notes"


class TestReflectApply:
    def test_apply_synthesizes_and_supersedes(self, tmp_path):
        s = make_store(tmp_path)

        async def go():
            a, b = await _seed_cluster(s)
            res = await reflect(
                s, "P", threshold=0.9, apply=True,
                synth_fn=lambda contents: "SYNTHESIZED: db timeouts need pooling",
                embed_fn=lambda text: _aval(fake_vec(2)),
            )
            await s.aclose()
            return a, b, res

        a, b, res = _run(go())
        assert res.synthesized == 1
        rows = _live_notes(s)
        by_id = {r[0]: r for r in rows}
        synth_id = res.written_note_ids[0]
        # sources superseded_by the synth note AND tombstoned
        assert by_id[a][2] == synth_id and by_id[a][3] is not None
        assert by_id[b][2] == synth_id and by_id[b][3] is not None
        # synth note is live
        assert by_id[synth_id][3] is None
        assert by_id[synth_id][1].startswith("SYNTHESIZED")

    def test_idempotent_rerun_no_new_notes(self, tmp_path):
        s = make_store(tmp_path)

        async def _count():
            mem = await s._conn_get()
            return len(await (await mem.execute("SELECT id FROM memory_notes")).fetchall())

        async def go():
            await _seed_cluster(s)
            await reflect(s, "P", threshold=0.9, apply=True,
                          synth_fn=lambda c: "SYNTH", embed_fn=lambda t: _aval(fake_vec(2)))
            count1 = await _count()
            # Re-run: sources are tombstoned → drop out of clustering → nothing new.
            res2 = await reflect(s, "P", threshold=0.9, apply=True,
                                 synth_fn=lambda c: "SYNTH2", embed_fn=lambda t: _aval(fake_vec(3)))
            count2 = await _count()
            await s.aclose()
            return count1, count2, res2

        count1, count2, res2 = _run(go())
        assert res2.synthesized == 0, "re-run must synthesize nothing"
        assert count1 == count2, "re-run must not add notes"

    def test_synthesis_unavailable_skips_gracefully(self, tmp_path):
        s = make_store(tmp_path)

        async def go():
            await _seed_cluster(s)
            res = await reflect(s, "P", threshold=0.9, apply=True,
                                synth_fn=lambda c: None)  # LLM "down"
            rows = await (await (await s._conn_get()).execute(
                "SELECT id FROM memory_notes")).fetchall()
            await s.aclose()
            return res, len(rows)

        res, n = _run(go())
        assert res.skipped == 1 and res.synthesized == 0
        assert n == 2, "no note written when synthesis returns None"


class TestReflectClusteringDeterminism:
    def test_same_fixture_same_clusters(self, tmp_path):
        s = make_store(tmp_path)

        async def go():
            await _seed_cluster(s)
            r1 = await reflect(s, "P", threshold=0.9, apply=False, synth_fn=lambda c: "x")
            r2 = await reflect(s, "P", threshold=0.9, apply=False, synth_fn=lambda c: "x")
            await s.aclose()
            return r1, r2

        r1, r2 = _run(go())
        assert r1.clusters_considered == r2.clusters_considered == 1


# ── helpers ───────────────────────────────────────────────────────────────────

async def _aval(v):
    return v
