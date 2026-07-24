# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
tests/test_evals.py — Phase L: pooling-value eval suite (E1–E10).

PROVES that multi-project pooling improves recall vs per-project scope.

Each eval class asserts a CONTRAST:
  - pooled scope retrieves a note/chunk
  - narrower scope (self / single-project) misses it

Fully offline + deterministic: fake_vec seeds, explicit created_at for recency
tests, no Ollama.  Reuses conftest helpers; does not duplicate store logic.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import numpy as np
import pytest

from braincell.project_registry import register_path, resolve_family_ulids, save_families
from tests.conftest import _insert_doc_and_chunk, fake_vec, make_store

# ── Project constants ─────────────────────────────────────────────────────────

ULID_A = "01ACMEWEB000000000000000AA"  # acme-web — family seed (self)
ULID_B = "01ACMEAPI000000000000000BB"  # acme-api — family peer
ULID_C = "01UNRELATED0000000000000CC"  # wholly unrelated project
ULID_NONE = "01DOESNOTEXIST000000000ZZ"  # never seeded — negative-control project

FAMILY_NAME = "acme"

# Deterministic query vectors — each aligns with a specific cohort of seeded notes.
Q_VEC = fake_vec(1)    # hits n_a1, n_b1, n_b_tomb; used for most tests
Q_VEC_3 = fake_vec(3)  # hits n_a2 (fresh) and n_b2 (stale) — recency test
Q_VEC_C = fake_vec(50) # hits n_c1 in unrelated project C — scope-all / family test


# ── Module-level helpers ──────────────────────────────────────────────────────

def _low_cosine_vec(
    base_seed: int = 1, ortho_seed: int = 2, cosine: float = 0.1
) -> np.ndarray:
    """Return a unit vector with exactly `cosine` cosine to fake_vec(base_seed).

    Uses Gram-Schmidt to build a component orthogonal to base, then blends at the
    requested angle.  Deterministic — depends only on the two seeds and cosine value.
    """
    v1 = fake_vec(base_seed)
    v2 = fake_vec(ortho_seed)
    # Subtract the v1 component to get a vector orthogonal to v1.
    v_orth = v2 - float(np.dot(v1, v2)) * v1
    v_orth = (v_orth / np.linalg.norm(v_orth)).astype(np.float32)
    v = cosine * v1 + np.sqrt(max(0.0, 1.0 - cosine ** 2)) * v_orth
    return (v / np.linalg.norm(v)).astype(np.float32)


async def _set_created_at(store, note_id: int, ts: str) -> None:
    """Directly update created_at on a memory_notes row (for recency-decay tests)."""
    cf = await store._conn_get()
    await cf.execute(
        "UPDATE memory_notes SET created_at = ? WHERE id = ?", (ts, note_id)
    )
    await cf.commit()


# ── Shared pooled fixture ─────────────────────────────────────────────────────

@pytest.fixture
def pooled_store(tmp_path, monkeypatch):
    """Global store seeded with notes + chunks attributed to 3 projects.

    Projects:
      ULID_A (acme-web) ─┬── "acme" family
      ULID_B (acme-api) ─┘
      ULID_C (unrelated-project)  — NOT in the family

    Memory notes (memory_notes):
      n_a1    : ULID_A, fake_vec(1), fresh   — "RRF ranking decision"
      n_b1    : ULID_B, fake_vec(1), fresh   — "Hybrid BM25 precision" (near-dup of n_a1)
      n_b2    : ULID_B, fake_vec(3), STALE   — "Old architecture decision" (1 yr ago)
      n_a2    : ULID_A, fake_vec(3), fresh   — "Fresh architecture note"
      n_b_tomb: ULID_B, fake_vec(1), DELETED — soft-tombstoned
      n_b_low : ULID_B, cosine=0.1 to Q_VEC  — low-relevance cross-project noise
      n_c1    : ULID_C, fake_vec(50), fresh  — "PostgreSQL pgvector tuning"

    Chunks (bc_chunks, for search() tests):
      doc-a : ULID_A, fake_vec(1)
      doc-b : ULID_B, fake_vec(1)
      doc-c : ULID_C, fake_vec(50)
    """
    monkeypatch.setenv("BRAINCELL_MODE", "global")
    monkeypatch.setenv("BRAINCELL_PROJECT_ID", ULID_A)

    store = make_store(tmp_path)

    # Register paths → ULIDs so resolve_family_ulids can traverse the path registry.
    path_a = str(tmp_path / "acme-web")
    path_b = str(tmp_path / "acme-api")
    path_c = str(tmp_path / "unrelated-project")
    register_path(path_a, ULID_A)
    register_path(path_b, ULID_B)
    register_path(path_c, ULID_C)

    # Declare the family: A + B only; C is deliberately excluded.
    save_families({FAMILY_NAME: [path_a, path_b]})

    ids: dict[str, int] = {}
    _stale_ts = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S")

    async def _seed() -> None:
        # n_a1: decision in A — high-similarity to Q_VEC
        ids["n_a1"] = int(await store.remember(
            "Use RRF fusion for braincell search ranking",
            "decision", ULID_A, embedding=fake_vec(1),
        ))
        # n_b1: near-duplicate of n_a1 in B, phrased differently (cross-project semantic hit)
        ids["n_b1"] = int(await store.remember(
            "Hybrid vector plus BM25 boosts search precision",
            "note", ULID_B, embedding=fake_vec(1),
        ))
        # n_b2: stale note in B with seed=3 (for recency-decay ranking contrast)
        ids["n_b2"] = int(await store.remember(
            "Old monolith architecture decision from project B",
            "decision", ULID_B, embedding=fake_vec(3), confidence=0.9,
        ))
        # n_a2: fresh note in A with same seed=3 (should outrank stale n_b2)
        ids["n_a2"] = int(await store.remember(
            "Fresh project A architecture note",
            "decision", ULID_A, embedding=fake_vec(3), confidence=0.9,
        ))
        # n_b_tomb: will be soft-tombstoned immediately after insertion
        ids["n_b_tomb"] = int(await store.remember(
            "Tombstoned note in B must not surface in any pool",
            "note", ULID_B, embedding=fake_vec(1),
        ))
        # n_b_low: engineered to have cosine=0.1 to Q_VEC — low-relevance noise
        ids["n_b_low"] = int(await store.remember(
            "Low relevance cross-project noise note from B",
            "note", ULID_B, embedding=_low_cosine_vec(1, 2, 0.1),
        ))
        # n_c1: note in unrelated project C (only visible under scope=all / project=C)
        ids["n_c1"] = int(await store.remember(
            "PostgreSQL pgvector query tuning for project C",
            "note", ULID_C, embedding=fake_vec(50),
        ))

        # Soft-tombstone n_b_tomb.
        await store.forget(ids["n_b_tomb"], ULID_B)

        # Age n_b2 to 1 year ago so M5 recency decay suppresses it vs fresh n_a2.
        await _set_created_at(store, ids["n_b2"], _stale_ts)

        # Seed one chunk per project (for search() pooling tests in E4).
        for ulid, doc_key, text, seed in [
            (ULID_A, "doc-a", "RRF search ranking chunk from project A", 1),
            (ULID_B, "doc-b", "Hybrid BM25 vector chunk from project B", 1),
            (ULID_C, "doc-c", "PostgreSQL pgvector chunk from project C", 50),
        ]:
            await _insert_doc_and_chunk(
                store, project=ulid, doc_key=doc_key, text=text, seed=seed
            )

    asyncio.run(_seed())

    # Resolve the A+B family ULID set once so tests don't repeat the logic.
    family_ulids: list[str] = sorted(resolve_family_ulids(ULID_A))

    yield store, ids, family_ulids

    store.close()


# ── E1: Cross-project family recall ──────────────────────────────────────────

class TestE1CrossProjectFamilyRecall:
    """E1: A decision in project B surfaces via family scope but not via self (A only).

    Proves: family pooling [A+B] is the minimal scope that captures cross-project
    knowledge; narrowing to self=A makes project-B notes invisible.
    """

    def test_family_scope_finds_b_note(self, pooled_store):
        store, ids, family_ulids = pooled_store
        notes = asyncio.run(store.recall(Q_VEC, family_ulids, k=10, dedup=False))
        assert ids["n_b1"] in {n.id for n in notes}, (
            "family scope [A+B] must return n_b1 from ULID_B"
        )

    def test_self_scope_misses_b_note(self, pooled_store):
        """Contrast: self scope (A only) cannot see n_b1 regardless of relevance."""
        store, ids, _ = pooled_store
        notes = asyncio.run(store.recall(Q_VEC, ULID_A, k=10, dedup=False))
        assert ids["n_b1"] not in {n.id for n in notes}, (
            "self scope (ULID_A only) must NOT return n_b1 — it lives in ULID_B"
        )


# ── E2: Explicit projects=[A,B] returns union; [A] alone scopes to A ──────────

class TestE2ExplicitProjectsListUnion:
    """E2: projects=[A,B] explicit filter pools both projects; projects=[A] gates to A.

    Proves: the projects list is respected exactly — neither more nor less.
    """

    def test_projects_ab_returns_notes_from_both_projects(self, pooled_store):
        store, ids, _ = pooled_store
        notes = asyncio.run(store.recall(Q_VEC, [ULID_A, ULID_B], k=10, dedup=False))
        pids = {n.project_id for n in notes}
        assert ULID_A in pids and ULID_B in pids, (
            "projects=[A,B] must surface notes from both ULID_A and ULID_B"
        )

    def test_projects_a_only_contains_only_a_notes(self, pooled_store):
        """Contrast: projects=[A] returns only ULID_A notes; n_b1 is absent."""
        store, ids, _ = pooled_store
        notes = asyncio.run(store.recall(Q_VEC, [ULID_A], k=10, dedup=False))
        assert all(n.project_id == ULID_A for n in notes), (
            "projects=[A] must return ONLY notes from ULID_A"
        )
        assert ids["n_b1"] not in {n.id for n in notes}


# ── E3: scope=all includes unrelated C; family (A+B) excludes it ──────────────

class TestE3ScopeAllIncludesC:
    """E3: project=None (widest scope) retrieves C; family scope [A+B] does not.

    Proves: scope=all genuinely crosses family boundaries; family scope is a
    bounded pool, not a synonym for all.
    """

    def test_all_scope_includes_c_note(self, pooled_store):
        store, ids, _ = pooled_store
        notes = asyncio.run(store.recall(Q_VEC_C, None, k=10))
        assert ids["n_c1"] in {n.id for n in notes}, (
            "project=None (all) must include n_c1 from unrelated ULID_C"
        )

    def test_family_scope_excludes_c_note(self, pooled_store):
        """Contrast: family [A+B] is bounded — n_c1 from C is invisible."""
        store, ids, family_ulids = pooled_store
        notes = asyncio.run(store.recall(Q_VEC_C, family_ulids, k=10))
        assert ids["n_c1"] not in {n.id for n in notes}, (
            "family scope [A+B] must NOT include n_c1 — ULID_C is not a family member"
        )


# ── E4: Semantic pooling surfaces differently-worded cross-project notes ───────

class TestE4SemanticPoolingViaVector:
    """E4: n_b1 is phrased differently from n_a1 but shares fake_vec(1).  Pooled
    vector recall surfaces it; self-scope A cannot see it; keyword search on A alone
    for n_a1's phrasing would also miss it.

    Also verifies store.search() (chunk search) honours a ULID list project filter.
    """

    def test_pooled_vector_recall_finds_differently_worded_b_note(self, pooled_store):
        store, ids, family_ulids = pooled_store
        # n_b1 says "Hybrid vector plus BM25" — zero keyword overlap with n_a1.
        # But embedding = fake_vec(1) → cosine 1.0 to Q_VEC → surfaces via pooled recall.
        notes = asyncio.run(store.recall(Q_VEC, family_ulids, k=10, dedup=False))
        assert ids["n_b1"] in {n.id for n in notes}, (
            "pooled family recall must surface n_b1 via vector similarity "
            "even though its text differs from n_a1"
        )

    def test_self_scope_misses_differently_worded_b_note(self, pooled_store):
        """Contrast: ULID_A alone cannot see n_b1 regardless of semantic similarity."""
        store, ids, _ = pooled_store
        notes = asyncio.run(store.recall(Q_VEC, ULID_A, k=10, dedup=False))
        assert ids["n_b1"] not in {n.id for n in notes}

    def test_search_pools_chunks_from_family_but_not_c(self, pooled_store):
        """store.search() with a ULID list pools doc-a + doc-b; doc-c stays out."""
        store, _, family_ulids = pooled_store
        hits = asyncio.run(store.search(Q_VEC, "", family_ulids, k=10, mode="semantic"))
        doc_keys = {h.doc_key for h in hits}
        assert "doc-a" in doc_keys and "doc-b" in doc_keys, (
            "pooled family search must return chunks from both ULID_A (doc-a) "
            "and ULID_B (doc-b)"
        )
        assert "doc-c" not in doc_keys, (
            "family scope must NOT include doc-c from unrelated ULID_C"
        )


# ── E5: M5 recency decay — fresh cross-project note beats stale one ───────────

class TestE5RecencyDecayRanking:
    """E5: n_a2 (fresh, ULID_A) and n_b2 (1-year-old, ULID_B) share fake_vec(3).
    In the pooled result, M5 blending (decay factor ~0.0625 for 365 days at 90-day
    half-life) forces n_b2 far below n_a2 despite equal vector similarity.
    """

    def test_fresh_a_note_ranks_before_stale_b_note_in_pool(self, pooled_store):
        store, ids, _ = pooled_store
        # Both notes score cosine=1.0 against Q_VEC_3; ranking is decided by M5 decay.
        notes = asyncio.run(
            store.recall(Q_VEC_3, [ULID_A, ULID_B], k=10, dedup=False)
        )
        note_ids = [n.id for n in notes]

        assert ids["n_a2"] in note_ids, "fresh n_a2 must appear in pooled recall"
        assert ids["n_b2"] in note_ids, "stale n_b2 must appear in pooled recall"
        assert note_ids.index(ids["n_a2"]) < note_ids.index(ids["n_b2"]), (
            "fresh n_a2 (age ~0 days, decay ~1.0) must rank before "
            "stale n_b2 (age ~365 days, decay ~0.0625) under M5 blending"
        )


# ── E6: Dedup across projects collapses near-duplicate cross-project notes ─────

class TestE6DedupAcrossProjects:
    """E6: n_a1 and n_b1 share embedding fake_vec(1) → pairwise cosine = 1.0.
    dedup=True keeps only one (greedy, fused-score order); dedup=False returns both.
    """

    def test_dedup_true_collapses_near_duplicate_pair(self, pooled_store):
        store, ids, _ = pooled_store
        notes = asyncio.run(
            store.recall(Q_VEC, [ULID_A, ULID_B], k=5, dedup=True)
        )
        present = {n.id for n in notes}
        both_present = ids["n_a1"] in present and ids["n_b1"] in present
        assert not both_present, (
            "dedup=True must drop one of the near-duplicate pair (n_a1, n_b1); "
            "they must not BOTH appear in the pooled [A+B] result"
        )

    def test_dedup_false_returns_both_cross_project_near_duplicates(self, pooled_store):
        """Contrast: with dedup=False both near-duplicates survive in the pool."""
        store, ids, _ = pooled_store
        notes = asyncio.run(
            store.recall(Q_VEC, [ULID_A, ULID_B], k=10, dedup=False)
        )
        present = {n.id for n in notes}
        assert ids["n_a1"] in present and ids["n_b1"] in present, (
            "dedup=False must return BOTH near-duplicates: "
            "n_a1 (ULID_A) and n_b1 (ULID_B)"
        )


# ── E7: Tombstone respected in every pooled scope ─────────────────────────────

class TestE7TombstoneRespectedInPool:
    """E7: n_b_tomb is soft-deleted in ULID_B.  It must be absent from both the
    family pool [A+B] and the widest scope (project=None, all projects).

    Proves tombstone filtering is applied before pool expansion — deletion wins
    over scope.
    """

    def test_tombstone_absent_from_family_pool(self, pooled_store):
        store, ids, family_ulids = pooled_store
        notes = asyncio.run(
            store.recall(Q_VEC, family_ulids, k=20, dedup=False)
        )
        assert ids["n_b_tomb"] not in {n.id for n in notes}, (
            "tombstoned n_b_tomb must not surface in family-scoped pool [A+B]"
        )

    def test_tombstone_absent_from_all_scope(self, pooled_store):
        """Contrast: even the widest all-scope pool never returns a tombstoned note."""
        store, ids, _ = pooled_store
        notes = asyncio.run(
            store.recall(Q_VEC, None, k=20, dedup=False)
        )
        assert ids["n_b_tomb"] not in {n.id for n in notes}, (
            "tombstoned n_b_tomb must not surface even in all-scope (project=None) pool"
        )


# ── E8: Pool count sanity — ingest_status per-project vs full pool ─────────────

class TestE8PoolCountSanity:
    """E8: ingest_status scoped to a single project reflects only that project's
    doc/chunk count; scoped to None reflects the full pool (A+B+C = 3/3).

    Proves the stats surface is pool-aware and can distinguish per-project from
    cross-project totals — useful for health checks and CLI reporting.
    """

    def test_project_a_has_exactly_one_doc(self, pooled_store):
        store, _, _ = pooled_store
        status = asyncio.run(store.ingest_status(ULID_A))
        assert status.doc_count == 1, (
            f"ULID_A must report exactly 1 doc (doc-a), got {status.doc_count}"
        )
        assert status.chunk_count == 1

    def test_project_b_has_exactly_one_doc(self, pooled_store):
        store, _, _ = pooled_store
        status = asyncio.run(store.ingest_status(ULID_B))
        assert status.doc_count == 1, (
            f"ULID_B must report exactly 1 doc (doc-b), got {status.doc_count}"
        )

    def test_full_pool_has_three_docs(self, pooled_store):
        """Contrast: project=None (all) counts across the full pool A+B+C = 3 docs."""
        store, _, _ = pooled_store
        status = asyncio.run(store.ingest_status(None))
        assert status.doc_count == 3, (
            f"full pool (A+B+C) must report 3 docs total, got {status.doc_count}"
        )
        assert status.chunk_count == 3


# ── E9: min_cosine filters low-relevance cross-project noise ──────────────────

class TestE9MinCosineFiltersCrossProjectNoise:
    """E9: n_b_low has cosine=0.1 to Q_VEC — appears in the [A+B] pool without
    min_cosine, but is filtered before RRF fusion when min_cosine=0.5.

    Proves min_cosine is an effective pooling quality gate: it cuts low-relevance
    cross-project notes without touching high-relevance ones.
    """

    def test_without_min_cosine_low_relevance_note_appears(self, pooled_store):
        store, ids, _ = pooled_store
        notes = asyncio.run(
            store.recall(Q_VEC, [ULID_A, ULID_B], k=10, min_cosine=None, dedup=False)
        )
        assert ids["n_b_low"] in {n.id for n in notes}, (
            "without min_cosine, n_b_low (cosine=0.1 to Q_VEC) must appear in "
            "pooled [A+B] results — positive cosine admits it"
        )

    def test_min_cosine_0_5_drops_low_relevance_note(self, pooled_store):
        """Contrast: min_cosine=0.5 drops n_b_low (cosine=0.1 < 0.5) before fusion."""
        store, ids, _ = pooled_store
        notes = asyncio.run(
            store.recall(Q_VEC, [ULID_A, ULID_B], k=10, min_cosine=0.5, dedup=False)
        )
        assert ids["n_b_low"] not in {n.id for n in notes}, (
            "min_cosine=0.5 must filter n_b_low (cosine=0.1) from pooled [A+B] results"
        )


# ── E10: Negative control — empty in every scope when nothing matches ──────────

class TestE10NegativeControl:
    """E10: A project never seeded returns no notes in any query mode, and a ULID
    list containing only that project yields no false bleed from A/B/C.

    Proves pooling is exact — the filter is honoured even when nothing matches.
    """

    def test_never_seeded_project_returns_empty_keyword_path(self, pooled_store):
        """qvec=None + ULID_NONE → recency path on an empty project → [] ."""
        store, _, _ = pooled_store
        notes = asyncio.run(store.recall(None, ULID_NONE, k=10))
        assert notes == [], (
            "a never-seeded project (ULID_NONE) must yield no notes on the recency path"
        )

    def test_never_seeded_project_list_returns_empty_no_bleed(self, pooled_store):
        """projects=[ULID_NONE] must return [] even though A/B/C have matching notes.

        This is the strictest negative-pooling control: confirms that pooling does
        not bleed results from unspecified projects into the requested filter.
        """
        store, _, _ = pooled_store
        notes = asyncio.run(store.recall(Q_VEC, [ULID_NONE], k=10, dedup=False))
        assert notes == [], (
            "projects=[ULID_NONE] must return [] with no cross-project bleed from A/B/C"
        )
