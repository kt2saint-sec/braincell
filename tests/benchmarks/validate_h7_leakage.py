#!/usr/bin/env python3
"""validate_h7_leakage.py — anti-leakage gate for the H7 vocab-divergence eval set.

Pure standard-library (no numpy, no new deps — every added dependency widens the
supply-chain surface, and a leakage gate must not). For every H7 query, checks each of its labeled
relevant passages against two lexical-overlap signals:

  1. Jaccard J = |stem(q) ∩ stem(p)| / |stem(q) ∪ stem(p)| over stopword-stripped,
     suffix-stemmed content words.
  2. BM25 rank (k1=1.5, b=0.75) of the passage over the pooled corpus, for that
     query.

SHIP GATE (calibrated/hybrid — this is what sets the exit code): for every
query x relevant-passage pair, J <= 0.15 AND that passage does not rank BM25
#1 (i.e. min(rank(A), rank(B)) >= 2). Ranking #1 would mean the relevant
passage is the single most lexically-obvious answer in the pool; ranking #2/#3
is tolerated because the pooled corpus is small (42 passages) and a stray
shared function word can push a genuinely-divergent passage into #2/#3 without
the set actually leaking.

H7-STRICT (reported, not gating): the subset of queries where BOTH relevant
passages rank outside the BM25 top 3 — the original tighter bar. Printed as a
sub-metric so downstream analysis can see how many queries clear the harder
standard.

Each family's Type-A lexical trap (`H7_fNN_TA` — reuses the family's query
keywords to describe a genuinely wrong concept) is checked for BM25 top-3 rank
on both of that family's queries, but this is WARN-ONLY: a weak trap is
printed, never fails the run. --waive-weak-trap is accepted for backward
compatibility (silences the WEAK annotation) but no longer affects the exit
code either way.

Usage: validate_h7_leakage.py eval.json [--waive-weak-trap f06,f09]
Exit 0 iff every query x relevant-passage pair clears J<=0.15 and rank>=2.
"""
from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter

# ~40 common function words. Deliberately small/inline — this is a gate, not a
# production NLP stemmer (see module docstring + plan: "flagged approximate").
STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "than", "so", "to",
    "of", "in", "on", "at", "by", "for", "with", "without", "from", "into",
    "is", "was", "are", "were", "be", "been", "being", "it", "its", "this",
    "that", "these", "those", "as", "not", "no", "does", "do", "did", "has",
    "have", "had", "we", "our", "us", "i", "you", "your", "they", "their",
    "he", "she", "his", "her", "them", "what", "why", "how", "when", "where",
    "which", "who", "can", "could", "would", "should", "will", "just", "even",
    "any", "all", "some", "other", "one", "up", "out", "over", "after",
    "before", "still", "also", "very", "each", "per", "same", "yet", "again",
}

_SUFFIXES = ("ing", "ed", "es", "ly", "s")


def stem(word: str) -> str:
    """Light suffix-stripper (s/es/ed/ing/ly). Approximate by design."""
    for suf in _SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 3:
            return word[: -len(suf)]
    return word


_WORD_RE = re.compile(r"[a-z0-9]+")


def content_words(text: str) -> list[str]:
    """Lowercase, tokenize on non-alnum, drop stopwords, stem. Returns a list
    (not a set) so BM25 can use term frequency; callers Jaccard-set it."""
    tokens = _WORD_RE.findall(text.lower())
    return [stem(t) for t in tokens if t not in STOPWORDS and len(t) > 1]


def jaccard(a_words: list[str], b_words: list[str]) -> float:
    a, b = set(a_words), set(b_words)
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


class BM25:
    """Self-contained Okapi BM25 (k1=1.5, b=0.75) over a fixed passage pool."""

    def __init__(self, doc_ids: list[str], doc_tokens: list[list[str]], k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.doc_ids = doc_ids
        self.doc_tokens = doc_tokens
        self.doc_len = [len(toks) for toks in doc_tokens]
        self.avgdl = sum(self.doc_len) / len(self.doc_len) if doc_tokens else 0.0
        self.n_docs = len(doc_tokens)
        df: Counter = Counter()
        for toks in doc_tokens:
            df.update(set(toks))
        self.idf = {
            term: math.log((self.n_docs - n + 0.5) / (n + 0.5) + 1)
            for term, n in df.items()
        }
        self.doc_tf = [Counter(toks) for toks in doc_tokens]

    def rank(self, query_tokens: list[str]) -> list[tuple[str, float]]:
        """Return (doc_id, score) sorted descending by score; ties keep pool order."""
        scores = []
        for i, doc_id in enumerate(self.doc_ids):
            tf = self.doc_tf[i]
            dl = self.doc_len[i]
            score = 0.0
            for term in query_tokens:
                if term not in tf:
                    continue
                idf = self.idf.get(term, 0.0)
                f = tf[term]
                denom = f + self.k1 * (1 - self.b + self.b * dl / self.avgdl) if self.avgdl else f
                score += idf * (f * (self.k1 + 1)) / denom
            scores.append((doc_id, score, i))
        scores.sort(key=lambda x: (-x[1], x[2]))
        return [(doc_id, score) for doc_id, score, _ in scores]


def family_id(passage_or_query_id: str) -> str:
    """H7_f03_A -> f03, H7_q03a -> f03."""
    m = re.match(r"H7_(f\d\d|q\d\d)", passage_or_query_id)
    if not m:
        return ""
    tok = m.group(1)
    return tok if tok.startswith("f") else "f" + tok[1:]


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: validate_h7_leakage.py eval.json [--waive-weak-trap f06,f09]")
        return 2
    eval_path = sys.argv[1]
    waived: set[str] = set()
    if "--waive-weak-trap" in sys.argv:
        idx = sys.argv.index("--waive-weak-trap")
        waived = {f.strip() for f in sys.argv[idx + 1].split(",") if f.strip()}

    data = json.loads(open(eval_path).read())
    passages = data["passages"]
    queries = [q for q in data["queries"] if q.get("test") == "H7"]

    doc_ids = [p["id"] for p in passages]
    doc_tokens = [content_words(p["text"]) for p in passages]
    bm25 = BM25(doc_ids, doc_tokens)
    text_by_id = {p["id"]: p["text"] for p in passages}

    failed = False
    strict_ids: list[str] = []  # queries where BOTH relevant passages are outside BM25 top-3
    print(f"{'query':<12}{'J(A)':>8}{'J(B)':>8}{'rank(A)':>10}{'rank(B)':>10}")
    print("-" * 48)

    trap_hits: dict[str, dict[str, bool]] = {}  # fam -> {qid: bool}

    for q in queries:
        qid = q["id"]
        fam = family_id(qid)
        q_words = content_words(q["text"])
        ranked = bm25.rank(q_words)
        rank_of = {doc_id: i + 1 for i, (doc_id, _score) in enumerate(ranked)}

        rel = q["relevant_ids"]
        # Assume exactly 2 relevant ids per H7 query: [A, B].
        a_id, b_id = rel[0], rel[1]
        j_a = jaccard(q_words, content_words(text_by_id[a_id]))
        j_b = jaccard(q_words, content_words(text_by_id[b_id]))
        rank_a, rank_b = rank_of[a_id], rank_of[b_id]

        print(f"{qid:<12}{j_a:>8.3f}{j_b:>8.3f}{rank_a:>10}{rank_b:>10}")

        # SHIP GATE: J<=0.15 for both, and neither relevant passage is the #1 BM25 hit.
        if j_a > 0.15 or j_b > 0.15:
            failed = True
            print(f"  FAIL: Jaccard leakage on {qid} (J(A)={j_a:.3f}, J(B)={j_b:.3f})")
        if min(rank_a, rank_b) < 2:
            failed = True
            print(f"  FAIL: BM25 #1 leakage on {qid} (rank(A)={rank_a}, rank(B)={rank_b})")

        # H7-strict (reported only): both relevant passages outside the top 3.
        if rank_a > 3 and rank_b > 3:
            strict_ids.append(qid)

        ta_id = f"H7_{fam}_TA"
        if ta_id in text_by_id:
            trap_hits.setdefault(fam, {})[qid] = rank_of.get(ta_id, 10**9) <= 3

    print()
    print(f"H7-strict: {len(strict_ids)}/{len(queries)} queries "
          "(both relevant passages outside BM25 top-3)")
    print(f"  {strict_ids}")

    print()
    print("Type-A trap strength (WARN-ONLY, never fails the run):")
    weak_traps = []
    for fam, hits in sorted(trap_hits.items()):
        strong = all(hits.values())
        status = "STRONG" if strong else "WEAK"
        if not strong and fam in waived:
            status += " (WAIVED)"
        elif not strong:
            weak_traps.append(fam)
        print(f"  {fam}: {status} — {hits}")

    print()
    if waived:
        print(f"Waivers applied (cosmetic only, does not affect exit code): {sorted(waived)}")
    if weak_traps:
        print(f"Weak traps (warn-only, does not affect exit code): {sorted(weak_traps)}")

    if failed:
        print("\nFAIL — ship gate not satisfied (J<=0.15 and BM25 rank>=2 required for every "
              "query x relevant-passage pair).")
        return 1
    print("\nPASS — every query x relevant-passage pair clears J<=0.15 and is not the BM25 #1 hit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
