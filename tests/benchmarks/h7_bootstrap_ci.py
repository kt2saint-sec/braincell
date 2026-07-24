#!/usr/bin/env python3
"""h7_bootstrap_ci.py — cluster-bootstrap CI + BM25 baseline for the H7 vocabulary-
divergence eval.

Re-embeds the H7 eval set with the default embedder (bge-m3@1024, reusing braincell's
own embed_texts exactly like bench_harness.py does), computes per-query nDCG@10 and
Coverage@5 (identical formulas to bench_harness.py:53-83), then runs a cluster
bootstrap (B=1000) resampling the 14 lesson FAMILIES — not the 28 queries — since
each family's two query phrasings are correlated and must move together as a unit.

Also scores a pure keyword BM25 baseline over the same 42-passage pool, reusing
validate_h7_leakage.py's tokenizer/stemmer/BM25 (no new dependency), so H7 reports
the embedder's LIFT over keyword search rather than an absolute number in isolation.
The delta (bge-m3 − BM25) is the headline signal; the BM25 baseline is deterministic
so it gets no bootstrap CI. A third slice, "H7-strict", restricts to the queries
where BOTH relevant passages rank outside BM25 top-3 — the purest vocabulary-
bridging subset (small n; reported as such).

bench_harness.py's per-config grid (H7 plan step 5) is the CANONICAL headline number;
this script only quantifies the uncertainty around it and adds the keyword-baseline
comparison.

Usage: .venv/bin/python h7_bootstrap_ci.py [eval.json]
"""
from __future__ import annotations

import importlib
import json
import math
import os
import re
import sys

import numpy as np

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
# noqa: E402 — MUST follow the sys.path.insert above; hoisting this breaks the import.
from validate_h7_leakage import BM25, content_words  # noqa: E402  (tokenizer/stemmer/BM25)

# Resolved relative to THIS file (_THIS_DIR, already computed above): the previous
# absolute path only existed on the original author's machine, so the default
# argument was dead everywhere else.
DEFAULT_EVAL_PATH = os.path.join(_THIS_DIR, "eval-H7-vocab-divergence-2026-07-08.json")
MODEL, DIM = "bge-m3", 1024
B_RESAMPLES = 1000
SEED = 0

_QID_FAMILY_RE = re.compile(r"^H7_q(\d+)[ab]$")


def _embed_all(texts):
    """Embed via the project's own embed_texts, reloading embed_spec for (model, dim) —
    identical reuse pattern to bench_harness.py:37-50."""
    os.environ["BRAINCELL_EMBED_PROVIDER"] = "ollama"
    os.environ["BRAINCELL_EMBED_MODEL"] = MODEL
    os.environ["BRAINCELL_EMBED_DIM"] = str(DIM)
    from braincell import embed_spec
    importlib.reload(embed_spec)
    from braincell import embed as embmod
    importlib.reload(embmod)
    try:
        vecs = embmod.embed_texts(texts)
    except Exception as exc:
        print(f"ERROR: embedding failed ({type(exc).__name__}: {exc}).")
        print(f"Pull {MODEL} first: `ollama pull {MODEL}` (and confirm Ollama is running).")
        sys.exit(2)
    return np.vstack([v for v in vecs])


def _ndcg_at_k(ranked_ids, relevant, k=10):
    """Binary-relevance nDCG@k — identical formula to bench_harness.py:53-59."""
    dcg = 0.0
    for i, pid in enumerate(ranked_ids[:k]):
        if pid in relevant:
            dcg += 1.0 / math.log2(i + 2)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    return dcg / ideal if ideal > 0 else 0.0


def _score_ranked(ranked_ids, relevant, k=10):
    """(nDCG@k, Coverage@5) for an already-ranked id list — cov5 formula identical
    to bench_harness.py:81-83. Shared by the embedder and BM25 scoring paths so
    both report the exact same metrics."""
    cov5 = (sum(1 for pid in ranked_ids[:5] if pid in relevant) / len(relevant)) if relevant else 0.0
    ndcg = _ndcg_at_k(ranked_ids, relevant, k)
    return ndcg, cov5


def _family_of(query_id):
    m = _QID_FAMILY_RE.match(query_id)
    if not m:
        raise ValueError(f"query id {query_id!r} doesn't match H7_qNN[a|b]")
    return m.group(1)


def _score_queries_embed(passages, queries):
    """Per-query nDCG@10 + Coverage@5 from the bge-m3 embedder.

    Returns {qid: {"ndcg":, "cov5":, "family":}}.
    """
    p_texts = [p["text"] for p in passages]
    p_ids = [p["id"] for p in passages]
    q_texts = [q["text"] for q in queries]
    allmat = _embed_all(p_texts + q_texts)
    pmat = allmat[: len(p_texts)]
    qmat = allmat[len(p_texts):]

    per_query = {}
    for qi, q in enumerate(queries):
        sims = pmat @ qmat[qi]
        order = np.argsort(sims)[::-1]
        ranked = [p_ids[j] for j in order]
        ndcg, cov5 = _score_ranked(ranked, set(q["relevant_ids"]))
        per_query[q["id"]] = {"ndcg": ndcg, "cov5": cov5, "family": _family_of(q["id"])}
    return per_query


def _score_queries_bm25(passages, queries):
    """Pure-keyword BM25 baseline (k1=1.5, b=0.75) over the 42-passage pool — same
    tokenizer/stopword/stemmer as validate_h7_leakage.py, no new dependency.

    Returns {qid: {"ndcg":, "cov5":, "strict":}} where "strict" marks queries whose
    relevant passages ALL rank outside BM25 top-3 (the H7-strict subset).
    """
    doc_ids = [p["id"] for p in passages]
    doc_tokens = [content_words(p["text"]) for p in passages]
    bm25 = BM25(doc_ids, doc_tokens)

    per_query = {}
    for q in queries:
        ranked = [doc_id for doc_id, _score in bm25.rank(content_words(q["text"]))]
        rank_of = {doc_id: i + 1 for i, doc_id in enumerate(ranked)}
        rel = set(q["relevant_ids"])
        ndcg, cov5 = _score_ranked(ranked, rel)
        strict = all(rank_of.get(rid, 10**9) > 3 for rid in rel)
        per_query[q["id"]] = {"ndcg": ndcg, "cov5": cov5, "strict": strict}
    return per_query


def _cluster_bootstrap(by_family, metric_idx, rng, b=B_RESAMPLES):
    """Resample the families with replacement (both queries of a picked family move
    together); recompute the mean over every query pulled in, per resample; return
    (bootstrap mean, 2.5%ile, 97.5%ile)."""
    families = sorted(by_family)
    n = len(families)
    means = np.empty(b)
    for i in range(b):
        picked = rng.choice(families, size=n, replace=True)
        vals = [v[metric_idx] for fam in picked for v in by_family[fam]]
        means[i] = float(np.mean(vals))
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(np.mean(means)), float(lo), float(hi)


def main():
    eval_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EVAL_PATH
    data = json.loads(open(eval_path).read())
    passages = data["passages"]
    queries = [q for q in data["queries"] if q.get("test") == "H7"]

    # --- bge-m3 embedder: per-query scores + family-cluster bootstrap CI ---
    embed_per_query = _score_queries_embed(passages, queries)
    by_family: dict[str, list[tuple[float, float]]] = {}
    for v in embed_per_query.values():
        by_family.setdefault(v["family"], []).append((v["ndcg"], v["cov5"]))
    n_families = len(by_family)
    n_queries = sum(len(v) for v in by_family.values())

    rng = np.random.default_rng(SEED)
    ndcg_mean, ndcg_lo, ndcg_hi = _cluster_bootstrap(by_family, 0, rng)
    cov5_mean, cov5_lo, cov5_hi = _cluster_bootstrap(by_family, 1, rng)

    # --- BM25 keyword baseline (deterministic — no bootstrap needed) ---
    bm25_per_query = _score_queries_bm25(passages, queries)
    bm25_ndcg = float(np.mean([v["ndcg"] for v in bm25_per_query.values()]))
    bm25_cov5 = float(np.mean([v["cov5"] for v in bm25_per_query.values()]))

    print(f"H7 cluster-bootstrap CI — {MODEL}@{DIM}, B={B_RESAMPLES}, seed={SEED}")
    print(f"bge-m3   nDCG@10 {ndcg_mean:.4f} [{ndcg_lo:.4f}, {ndcg_hi:.4f}]   "
          f"Cov@5 {cov5_mean:.4f} [{cov5_lo:.4f}, {cov5_hi:.4f}]  "
          f"n={n_queries} ({n_families} families)")
    print(f"BM25     nDCG@10 {bm25_ndcg:.4f}{'':>18}   "
          f"Cov@5 {bm25_cov5:.4f}{'':>18}  n={len(queries)}")
    print(f"lift (bge-m3 - BM25): nDCG {ndcg_mean - bm25_ndcg:+.4f}, "
          f"Cov {cov5_mean - bm25_cov5:+.4f}")

    # --- H7-strict subset: queries where ALL relevant passages rank outside BM25
    # top-3 — the purest vocabulary-bridging cases. Small n; labeled as such. ---
    strict_ids = [qid for qid, v in bm25_per_query.items() if v["strict"]]
    n_strict = len(strict_ids)
    print(f"\nH7-strict subset (n={n_strict}/{len(queries)}) — both relevant passages "
          f"rank outside BM25 top-3:")
    if n_strict:
        strict_bge_ndcg = float(np.mean([embed_per_query[qid]["ndcg"] for qid in strict_ids]))
        strict_bge_cov5 = float(np.mean([embed_per_query[qid]["cov5"] for qid in strict_ids]))
        strict_bm25_ndcg = float(np.mean([bm25_per_query[qid]["ndcg"] for qid in strict_ids]))
        strict_bm25_cov5 = float(np.mean([bm25_per_query[qid]["cov5"] for qid in strict_ids]))
        print(f"  bge-m3 nDCG@10 {strict_bge_ndcg:.4f}, BM25 nDCG@10 {strict_bm25_ndcg:.4f}")
        print(f"  bge-m3 Cov@5   {strict_bge_cov5:.4f}, BM25 Cov@5   {strict_bm25_cov5:.4f}")
    else:
        print("  (no query qualifies — every query has a relevant passage inside BM25 top-3)")

    print("\nNote: bench_harness.py's per-config grid (step 5) is the CANONICAL "
          "headline number for H7 — this script quantifies the uncertainty around "
          "the bge-m3 means via a family-cluster bootstrap, and reports its lift "
          "over a pure-keyword BM25 baseline (the delta is the headline signal).")


if __name__ == "__main__":
    main()
