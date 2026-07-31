# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
contradictions.py — `braincell contradictions`: offline contradiction audit.

The write-time guard (`store.find_conflicts` behind `remember`) can only say
"these notes are embedding-close" — cosine cannot separate a contradiction from
a paraphrase (measured 2026-07-23: contradictory note pairs 0.86–0.98, true
paraphrases ~0.94 on qwen3-embedding:4b@1024). This audit adds the judgment: it
pairs up close ACTIVE notes and asks a local LLM (Ollama) whether each pair
CONTRADICTS, DUPLICATES, or is CONSISTENT.

Deliberately READ-ONLY — there is no --apply. Resolution stays a human/model
decision via `supersede`/`forget`: auto-resolving from corpus-derived text is
a memory-poisoning escalation the MCP design explicitly rejects. Mirrors reflect.py's Ollama
discipline: best-effort, injectable judge, never raises out of the audit loop.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from .log import get as _get_log
from .store import _CONFLICT_COS, SqliteStore, _blob_to_vec

log = _get_log("braincell.contradictions")

# judge_fn(content_a, content_b) -> verdict string or None (judge unavailable).
JudgeFn = Callable[[str, str], str | None]

_VERDICTS = ("contradicts", "duplicate", "consistent")


@dataclass
class ContradictionPair:
    """One judged (or unjudged) pair of embedding-close active notes."""
    id_a: int
    id_b: int
    cosine: float
    content_a: str
    content_b: str
    verdict: str  # 'contradicts' | 'duplicate' | 'consistent' | 'unjudged'


@dataclass
class ContradictionReport:
    """Outcome of one audit pass. Read-only — nothing was written."""
    pairs: list[ContradictionPair] = field(default_factory=list)
    notes_scanned: int = 0
    pairs_over_threshold: int = 0
    pairs_judged: int = 0

    @property
    def contradictions(self) -> list[ContradictionPair]:
        return [p for p in self.pairs if p.verdict == "contradicts"]


def _default_model() -> str:
    return os.environ.get("BRAINCELL_LLM_MODEL", "qwen2.5:7b")


def ollama_judge(content_a: str, content_b: str,
                 model: str | None = None) -> str | None:
    """Best-effort single-pair judgment via local Ollama. Never raises.

    Returns a verdict in ``_VERDICTS`` or None when the model is unavailable or
    answers off-script (an off-script answer must read as "unjudged", never be
    coerced into a verdict).
    """
    try:
        import ollama
        resp = ollama.chat(
            model=model or _default_model(),
            messages=[{
                "role": "user",
                "content": (
                    "Two notes from a project memory follow. Judge their factual "
                    "relationship.\nAnswer with exactly ONE word:\n"
                    "CONTRADICTS — they cannot both be true\n"
                    "DUPLICATE — same claim, different wording\n"
                    "CONSISTENT — related or unrelated, but not in conflict\n\n"
                    f"Note A:\n{content_a}\n\nNote B:\n{content_b}"
                ),
            }],
            options={"num_predict": 8},
        )
        word = (resp.message.content or "").strip().split()[0].strip(".,:").lower()
        # 'duplicates' / 'contradicts.' etc. normalise by prefix match.
        for verdict in _VERDICTS:
            if word.startswith(verdict[:8]):
                return verdict
        return None
    except Exception as exc:  # noqa: BLE001 — any judge outage leaves the pair unjudged, never fails the caller
        log.warning("ollama judge unavailable (%r) — pair left unjudged.", exc)
        return None


async def find_contradictions(
    store: SqliteStore,
    project_id: str,
    *,
    threshold: float | None = None,
    limit: int = 50,
    judge_fn: JudgeFn | None = None,
) -> ContradictionReport:
    """Pair up embedding-close ACTIVE notes and judge each pair.

    Candidate generation is the same O(n²) cosine scan as
    ``store.find_note_clusters`` (fine at curated-note scale); only pairs with
    cosine ≥ *threshold* (default BRAINCELL_CONFLICT_COS = 0.85) are judged,
    highest cosine first, capped at *limit* (the cap is reported, never silent).
    ``judge_fn=None`` means "no judge available" → pairs report 'unjudged';
    pass ``ollama_judge`` (the CLI does) for LLM verdicts.
    """
    threshold = _CONFLICT_COS if threshold is None else threshold
    mem = await store._conn_get()
    rows = await (await mem.execute(
        "SELECT id, content, embedding FROM memory_notes "
        "WHERE embedding IS NOT NULL AND status = 'active' AND project_id = ? "
        "ORDER BY id",
        (project_id,),
    )).fetchall()
    report = ContradictionReport(notes_scanned=len(rows))
    if len(rows) < 2:
        return report

    ids = [r[0] for r in rows]
    contents = {r[0]: r[1] for r in rows}
    mat = np.stack([_blob_to_vec(bytes(r[2])) for r in rows])
    sims = mat @ mat.T

    candidates: list[tuple[float, int, int]] = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            cos = float(sims[i, j])
            if cos >= threshold:
                candidates.append((cos, ids[i], ids[j]))
    candidates.sort(reverse=True)
    report.pairs_over_threshold = len(candidates)
    if len(candidates) > limit:
        log.warning(
            "contradictions: %d pair(s) over threshold, judging only the top %d "
            "(raise --limit to cover the rest).",
            len(candidates), limit,
        )
    for cos, id_a, id_b in candidates[:limit]:
        verdict = "unjudged"
        if judge_fn is not None:
            got = judge_fn(contents[id_a], contents[id_b])
            if got in _VERDICTS:
                verdict = got
                report.pairs_judged += 1
        report.pairs.append(ContradictionPair(
            id_a=id_a, id_b=id_b, cosine=cos,
            content_a=contents[id_a], content_b=contents[id_b],
            verdict=verdict,
        ))
    return report


def print_report(report: ContradictionReport, verbose: bool = False) -> None:
    """Human-readable audit output with per-verdict guidance."""
    print(
        f"{report.notes_scanned} active note(s) scanned; "
        f"{report.pairs_over_threshold} pair(s) over threshold; "
        f"{report.pairs_judged} judged."
    )
    if not report.pairs:
        print("No embedding-close pairs — nothing to audit.")
        return
    for p in report.pairs:
        tag = p.verdict.upper()
        print(f"\n[{tag}] notes {p.id_a} <-> {p.id_b} (cosine {p.cosine:.3f})")
        cap = None if verbose else 100
        print(f"  A ({p.id_a}): {p.content_a[:cap]!r}")
        print(f"  B ({p.id_b}): {p.content_b[:cap]!r}")
        if p.verdict == "contradicts":
            print("  → resolve deliberately: supersede the stale note with the "
                  "current truth (MCP `supersede`), or `forget` the wrong one.")
        elif p.verdict == "duplicate":
            print("  → `braincell consolidate` merges near-duplicates.")
    n = len(report.contradictions)
    if n:
        print(f"\n{n} contradiction(s) found. This audit writes NOTHING — "
              f"resolution is always an explicit supersede/forget.")
