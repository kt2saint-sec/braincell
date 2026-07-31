# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
rerank.py — optional local reranker over the fused top-k.

A lightweight, opt-in final stage: after hybrid fusion, take the top-M candidates
and re-score each (query, text) pair with a small local model (Ollama), then
reorder and return the top-k. Off by default (``BRAINCELL_RERANK=off``) so the
latency budget and today's ranking are untouched. If the reranker is unavailable
(model missing, timeout, parse failure) the fused order is kept — never raises.

The scorer is injectable (``score_fn``) so tests run without Ollama.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable, Sequence

from .log import get as _get_log

log = _get_log("braincell.rerank")

_RERANK: str = os.environ.get("BRAINCELL_RERANK", "off")
try:
    _RERANK_M: int = int(os.environ.get("BRAINCELL_RERANK_M", "20") or 20)
except ValueError:
    _RERANK_M = 20
_RERANK_MODEL: str = os.environ.get("BRAINCELL_RERANK_MODEL", "qwen2.5:7b")
try:
    _RERANK_CONCURRENCY = max(
        1, min(8, int(os.environ.get("BRAINCELL_RERANK_CONCURRENCY", "4")))
    )
except ValueError:
    _RERANK_CONCURRENCY = 4

# score_fn: (query, text) -> float | None (None = unavailable for this item).
ScoreFn = Callable[[str, str], "float | None | Awaitable[float | None]"]


def rerank_enabled() -> bool:
    return _RERANK == "ollama"


def rerank_window() -> int:
    return _RERANK_M


_SCORE_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _ollama_score(query: str, text: str, model: str = _RERANK_MODEL) -> float | None:
    """Score one (query, text) pair in [0,100] via Ollama. None on any failure."""
    try:
        import ollama
        prompt = (
            "Rate how well the passage answers the query on a scale of 0 to 100. "
            "Reply with ONLY the number.\n\n"
            f"Query: {query}\n\nPassage: {text}\n\nScore:"
        )
        resp = ollama.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            options={"num_predict": 8},
        )
        m = _SCORE_RE.search(resp.message.content or "")
        return float(m.group(0)) if m else None
    except Exception as exc:  # noqa: BLE001 — any scorer outage keeps the fused order, never fails Search
        log.warning("rerank score failed (%r) — keeping fused order.", exc)
        return None


async def _order_by_score(
    query: str,
    items: Sequence[tuple[int, str]],
    score_fn: ScoreFn | None,
) -> list[int] | None:
    """Return item indices reordered by descending score, or None if unavailable.

    ``items`` is a sequence of (index, text). If ANY item scores None the whole
    rerank is abandoned (return None) so the caller keeps the fused order — a
    partial rerank would be worse than none.
    """
    fn = score_fn or (lambda q, t: _ollama_score(q, t))
    semaphore = asyncio.Semaphore(_RERANK_CONCURRENCY)

    async def _score(idx: int, text: str) -> tuple[int, float | None]:
        async with semaphore:
            if asyncio.iscoroutinefunction(fn):
                value = await fn(query, text)
            else:
                # Ollama's Python client is synchronous. Keep it off the MCP
                # event loop and bound parallel requests to avoid fan-out.
                value = await asyncio.to_thread(fn, query, text)
                if asyncio.iscoroutine(value):
                    value = await value
        return idx, None if value is None else float(value)

    scored_results = await asyncio.gather(
        *(_score(idx, text) for idx, text in items)
    )
    scored: list[tuple[int, float]] = []
    for idx, s in scored_results:
        if s is None:
            return None
        scored.append((idx, float(s)))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [idx for idx, _ in scored]


async def rerank_hits(query, hits, *, top_k, score_fn: ScoreFn | None = None):
    """Rerank a list of search Hits; return the top_k. Fused order on failure."""
    if not hits or not query:
        return hits[:top_k]
    items = [(i, f"{h.title}\n{h.snippet}") for i, h in enumerate(hits)]
    order = await _order_by_score(query, items, score_fn)
    if order is None:
        return hits[:top_k]
    return [hits[i] for i in order][:top_k]


async def rerank_notes(query, notes, *, top_k, score_fn: ScoreFn | None = None):
    """Rerank a list of Notes; return the top_k. Fused order on failure."""
    if not notes or not query:
        return notes[:top_k]
    items = [(i, n.content) for i, n in enumerate(notes)]
    order = await _order_by_score(query, items, score_fn)
    if order is None:
        return notes[:top_k]
    return [notes[i] for i in order][:top_k]
