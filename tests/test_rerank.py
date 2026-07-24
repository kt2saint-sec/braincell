# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_rerank.py — B5 optional local reranker.

Covers:
  - off (default) = passthrough (rerank_enabled False, byte-identical order);
  - on with a stub scorer reorders deterministically;
  - reranker unavailable (score_fn returns None) → fused order kept + warning.

Pure functions with a lightweight Hit/Note stand-in; no Ollama.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


from braincell import rerank as rr


def _run(coro):
    return asyncio.run(coro)


@dataclass
class _Hit:
    chunk_id: int
    title: str
    snippet: str


@dataclass
class _Note:
    id: int
    content: str


def test_default_env_is_off():
    assert rr._RERANK == "off"
    assert rr.rerank_enabled() is False


def test_rerank_hits_reorders_with_stub(monkeypatch):
    hits = [_Hit(1, "a", "alpha"), _Hit(2, "b", "beta"), _Hit(3, "c", "gamma")]
    # stub: prefer the hit whose snippet == "gamma"
    scores = {"alpha": 1.0, "beta": 2.0, "gamma": 9.0}

    def score_fn(q, text):
        # text is "title\nsnippet"
        snip = text.split("\n", 1)[1]
        return scores[snip]

    out = _run(rr.rerank_hits("q", hits, top_k=3, score_fn=score_fn))
    assert [h.chunk_id for h in out] == [3, 2, 1]


def test_rerank_hits_top_k_truncates(monkeypatch):
    hits = [_Hit(i, str(i), f"s{i}") for i in range(5)]
    out = _run(rr.rerank_hits("q", hits, top_k=2, score_fn=lambda q, t: float(t[-1])))
    assert len(out) == 2
    assert out[0].chunk_id == 4  # highest score s4


def test_rerank_hits_unavailable_keeps_fused_order():
    hits = [_Hit(1, "a", "x"), _Hit(2, "b", "y"), _Hit(3, "c", "z")]
    out = _run(rr.rerank_hits("q", hits, top_k=3, score_fn=lambda q, t: None))
    assert [h.chunk_id for h in out] == [1, 2, 3]  # unchanged (fused order)


def test_rerank_notes_reorders_with_async_stub():
    notes = [_Note(1, "low"), _Note(2, "high")]

    async def score_fn(q, text):
        return 10.0 if text == "high" else 0.0

    out = _run(rr.rerank_notes("q", notes, top_k=2, score_fn=score_fn))
    assert [n.id for n in out] == [2, 1]


def test_rerank_notes_empty_passthrough():
    assert _run(rr.rerank_notes("q", [], top_k=5, score_fn=lambda q, t: 1.0)) == []


def test_partial_none_abandons_rerank():
    # if ANY item scores None, the whole rerank is abandoned (fused order kept)
    hits = [_Hit(1, "a", "x"), _Hit(2, "b", "y")]

    def score_fn(q, text):
        return None if "y" in text else 5.0

    out = _run(rr.rerank_hits("q", hits, top_k=2, score_fn=score_fn))
    assert [h.chunk_id for h in out] == [1, 2]
