# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_fusion.py — B1 convex-combination (CC) fusion tests.

_cc_fuse normalization edge cases + alpha extremes, and the _fuse_hits dispatch
(default env = RRF, byte-identical to _rrf_fuse). Pure functions, no DB.
"""

from __future__ import annotations

import pytest

from braincell import store
from braincell.store import _cc_fuse, _fuse_hits, _rrf_fuse


# ── _cc_fuse edge cases ───────────────────────────────────────────────────────

def test_cc_empty_both():
    assert _cc_fuse([], []) == []


def test_cc_single_item_each_side():
    # single item normalizes to 1.0 on its side (span 0)
    out = dict(_cc_fuse([(1, 0.9)], [(1, -3.0)], alpha=0.5))
    assert out[1] == pytest.approx(1.0)  # 0.5*1 + 0.5*1


def test_cc_all_equal_scores():
    out = _cc_fuse([(1, 0.5), (2, 0.5), (3, 0.5)], [], alpha=1.0)
    # every id normalizes to 1.0; order preserved (stable sort on equal keys)
    assert [cid for cid, _ in out] == [1, 2, 3]
    assert all(s == pytest.approx(1.0) for _, s in out)


def test_cc_ties_do_not_crash():
    out = _cc_fuse([(1, 0.8), (2, 0.8)], [(1, -1.0), (2, -1.0)], alpha=0.5)
    assert {cid for cid, _ in out} == {1, 2}


def test_cc_alpha_zero_is_lexical_order():
    # vec disagrees with fts; alpha=0 → pure lexical (fts) order
    vec = [(1, 0.99), (2, 0.10)]        # semantic prefers 1
    fts = [(2, -0.5), (1, -5.0)]        # lexical prefers 2 (higher = better)
    out = [cid for cid, _ in _cc_fuse(vec, fts, alpha=0.0)]
    assert out == [2, 1]


def test_cc_alpha_one_is_semantic_order():
    vec = [(1, 0.99), (2, 0.10)]        # semantic prefers 1
    fts = [(2, -0.5), (1, -5.0)]        # lexical prefers 2
    out = [cid for cid, _ in _cc_fuse(vec, fts, alpha=1.0)]
    assert out == [1, 2]


def test_cc_agreement_ranks_agreed_doc_first():
    # both lists rank doc 7 top → it must be #1 under CC
    vec = [(7, 0.95), (3, 0.40), (5, 0.20)]
    fts = [(7, -0.1), (3, -2.0), (5, -9.0)]
    out = [cid for cid, _ in _cc_fuse(vec, fts, alpha=0.5)]
    assert out[0] == 7


def test_cc_missing_from_one_list_contributes_zero():
    # doc 2 only in vec, doc 3 only in fts; doc 1 in both should win
    vec = [(1, 1.0), (2, 1.0)]
    fts = [(1, 0.0), (3, 0.0)]
    out = dict(_cc_fuse(vec, fts, alpha=0.5))
    assert out[1] == pytest.approx(1.0)  # 0.5*1 + 0.5*1
    assert out[2] == pytest.approx(0.5)  # semantic only
    assert out[3] == pytest.approx(0.5)  # lexical only


# ── _fuse_hits dispatch ───────────────────────────────────────────────────────

def test_default_env_selects_rrf():
    assert store._FUSION == "rrf", "default fusion must stay RRF"
    vec = [(1, 0.9), (2, 0.5)]
    fts = [(2, -0.1), (3, -0.9)]
    assert _fuse_hits(vec, fts) == _rrf_fuse(vec, fts)


def test_fuse_hits_cc_when_flagged(monkeypatch):
    monkeypatch.setattr(store, "_FUSION", "cc")
    monkeypatch.setattr(store, "_FUSION_ALPHA", 1.0)
    vec = [(1, 0.99), (2, 0.10)]
    fts = [(2, -0.5), (1, -5.0)]
    out = [cid for cid, _ in _fuse_hits(vec, fts)]
    assert out == [1, 2]  # alpha=1 → semantic order
