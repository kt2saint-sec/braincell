# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_ranking_internals.py — lock the v0.1 perf/clarity refactors:

  - _cosine_top_k / _cosine_top_k_matrix: argpartition top-k returns the same
    ordering a full sort would (both k<N and k>=N branches).
  - _stack_blobs: contiguous decode round-trips the float32 vectors.
  - embed_query: session cache memoizes repeated queries (one embed call).
  - _env_float / _env_int: env-tunable ranking constants parse + fall back.
"""

from __future__ import annotations

import numpy as np

from braincell.store import (
    _cosine_top_k,
    _cosine_top_k_matrix,
    _env_float,
    _env_int,
    _stack_blobs,
    _vec_to_blob,
)


def _unit(vals: list[float]) -> np.ndarray:
    v = np.array(vals, dtype=np.float32)
    return v / np.linalg.norm(v)


class TestCosineTopK:
    def test_stack_blobs_roundtrip(self):
        vecs = [_unit([1, 0, 0]), _unit([0, 1, 0])]
        matrix = _stack_blobs([_vec_to_blob(v) for v in vecs])
        assert matrix.shape == (2, 3)
        np.testing.assert_allclose(matrix[0], vecs[0], rtol=1e-6)

    def test_topk_ordering_matches_full_sort(self):
        # Query aligned with v2; v0 orthogonal, v1 partial, v2 best, v3 negative.
        ids = [10, 11, 12, 13]
        vecs = [_unit([0, 1, 0]), _unit([1, 1, 0]), _unit([1, 0, 0]), _unit([-1, 0, 0])]
        q = _unit([1, 0, 0])
        blobs = [_vec_to_blob(v) for v in vecs]
        top2 = _cosine_top_k(q, ids, blobs, 2)
        assert [cid for cid, _ in top2] == [12, 11], top2
        # k >= N returns every id in descending score order.
        allk = _cosine_top_k(q, ids, blobs, 10)
        assert [cid for cid, _ in allk] == [12, 11, 10, 13]

    def test_matrix_path_equivalent(self):
        ids = [1, 2, 3]
        vecs = [_unit([1, 0]), _unit([0, 1]), _unit([1, 1])]
        q = _unit([1, 0])
        matrix = np.stack(vecs)
        out = _cosine_top_k_matrix(q, ids, matrix, 2)
        assert [cid for cid, _ in out] == [1, 3]

    def test_empty(self):
        assert _cosine_top_k(_unit([1, 0]), [], [], 5) == []


class TestEmbedQueryCache:
    def test_repeated_query_embeds_once(self, monkeypatch):
        import braincell.embed as emb

        calls = {"n": 0}

        def fake_embed(texts):
            calls["n"] += 1
            return [np.ones(emb.embed_spec.DIM, dtype=np.float32)]

        # The query path goes through the prefix-free core (_embed_normalized),
        # not embed_texts, so the query prefix is never doubled with the document
        # prefix (P0-2). Patch the core to observe the single cached embed call.
        emb._embed_query_cached.cache_clear()
        monkeypatch.setattr(emb, "_embed_normalized", fake_embed)
        try:
            v1 = emb.embed_query("same query")
            v2 = emb.embed_query("same query")
        finally:
            emb._embed_query_cached.cache_clear()

        assert calls["n"] == 1, "second identical query should hit the cache"
        np.testing.assert_array_equal(v1, v2)


class TestEnvTunables:
    def test_env_float_parses_and_falls_back(self, monkeypatch):
        monkeypatch.setenv("BC_TEST_FLOAT", "1.5")
        assert _env_float("BC_TEST_FLOAT", 0.0) == 1.5
        monkeypatch.delenv("BC_TEST_FLOAT", raising=False)
        assert _env_float("BC_TEST_FLOAT", 0.95) == 0.95
        monkeypatch.setenv("BC_TEST_FLOAT", "not-a-number")
        assert _env_float("BC_TEST_FLOAT", 0.95) == 0.95

    def test_env_int_parses_and_falls_back(self, monkeypatch):
        monkeypatch.setenv("BC_TEST_INT", "99")
        assert _env_int("BC_TEST_INT", 60) == 99
        monkeypatch.delenv("BC_TEST_INT", raising=False)
        assert _env_int("BC_TEST_INT", 60) == 60
        monkeypatch.setenv("BC_TEST_INT", "1.5")
        assert _env_int("BC_TEST_INT", 60) == 60
