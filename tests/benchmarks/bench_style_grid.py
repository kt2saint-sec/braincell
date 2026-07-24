#!/usr/bin/env python3
"""bench_style_grid.py — client-query-style × embedder factorial scorer (Fable step 2/3).

Answers the router question numerically: does the BEST embedder change depending on
which client model wrote the query? Reads eval-style-grid-2026-07-08.json (42 frozen
H7 passages; 14 lesson families each queried by 5 client models), sweeps every embedder
in CONFIGS, and reports:

  * per-embedder OVERALL nDCG@10 / Coverage@5 (across all 70 queries),
  * the embedder × client MATRIX (step 3: per-client reporting),
  * each client's WINNING embedder, and
  * a family-cluster bootstrap (B=1000) on the winner-vs-default gap per client, so a
    "flip" is only called real when its CI excludes 0.

Decision rule (printed as VERDICT):
  - If ONE embedder is best (or within CI of best) for EVERY client → single default is
    Pareto-optimal; a per-client router is NOT justified.
  - If winners flip across clients AND the gap CI excludes 0 → the router hypothesis has
    evidence; only then does per-client routing warrant its (heavy) cost.

Reuses braincell's own embed path exactly like bench_harness.py / h7_bootstrap_ci.py
(reload embed_spec per config so the per-model prefix registry fires; passages via the
document path, queries via the query path). Persists every run under ~/braincell-benchmarks.

Run from repo root (a stale site-packages/braincell/ otherwise shadows the fixed source):
  .venv/bin/python tests/benchmarks/bench_style_grid.py
"""
from __future__ import annotations

import datetime as _dt
import importlib
import json
import math
import os
import sys
from collections import defaultdict

import numpy as np

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_EVAL = os.path.join(_THIS_DIR, "eval-style-grid-2026-07-08.json")

# Same six configs as bench_harness.py, so numbers are directly comparable.
CONFIGS = [
    ("qwen3-embedding:4b", 1024),      # DEFAULT (current code default)
    ("qwen3-embedding:4b", 2560),      # native 4b
    ("qwen3-embedding:0.6b", 1024),    # light/native
    ("nomic-embed-text", 768),         # cross-family baseline
    ("bge-m3", 1024),                  # long-context multilingual
    ("mxbai-embed-large", 1024),       # strong general, short context
]
DEFAULT_CONFIG = ("qwen3-embedding:4b", 1024)
B_RESAMPLES = 1000
SEED = 0


def _embed(p_texts, q_texts, model, dim):
    """Embed passages via the document path and queries via the query path, reloading
    embed_spec+embed for (model, dim) so the asymmetric per-model prefix applies."""
    os.environ["BRAINCELL_EMBED_PROVIDER"] = "ollama"
    os.environ["BRAINCELL_EMBED_MODEL"] = model
    os.environ["BRAINCELL_EMBED_DIM"] = str(dim)
    from braincell import embed_spec
    importlib.reload(embed_spec)
    from braincell import embed as embmod
    importlib.reload(embmod)
    pmat = np.vstack([v for v in embmod.embed_texts(p_texts)])
    qmat = np.vstack([np.asarray(embmod.embed_query(q), dtype=np.float32) for q in q_texts])
    return pmat, qmat


def _ndcg_at_k(ranked_ids, relevant, k=10):
    dcg = sum(1.0 / math.log2(i + 2) for i, pid in enumerate(ranked_ids[:k]) if pid in relevant)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    return dcg / ideal if ideal > 0 else 0.0


def _score(ranked_ids, relevant):
    cov5 = (sum(1 for pid in ranked_ids[:5] if pid in relevant) / len(relevant)) if relevant else 0.0
    r5 = 1.0 if any(pid in relevant for pid in ranked_ids[:5]) else 0.0
    return _ndcg_at_k(ranked_ids, relevant, 10), cov5, r5


def _score_config(passages, queries, model, dim):
    """Return per_query {qid: {ndcg, cov5, r5, client, family}} for one embedder."""
    p_ids = [p["id"] for p in passages]
    pmat, qmat = _embed([p["text"] for p in passages], [q["text"] for q in queries], model, dim)
    per_query = {}
    for qi, q in enumerate(queries):
        order = np.argsort(pmat @ qmat[qi])[::-1]
        ranked = [p_ids[j] for j in order]
        ndcg, cov5, r5 = _score(ranked, set(q["relevant_ids"]))
        per_query[q["id"]] = {
            "ndcg": ndcg, "cov5": cov5, "r5": r5,
            "client": q["client"], "family": q["family"],
        }
    return per_query


def _means(rows, keys=("ndcg", "cov5", "r5")):
    return {k: round(float(np.mean([r[k] for r in rows])), 4) if rows else 0.0 for k in keys}


def _cluster_bootstrap_gap(best_by_fam, def_by_fam, rng, b=B_RESAMPLES):
    """Bootstrap the per-client (best - default) cov5 gap by resampling the 14 families.
    best_by_fam / def_by_fam: {family: cov5}. Returns (mean_gap, lo, hi)."""
    fams = sorted(best_by_fam)
    diffs = np.array([best_by_fam[f] - def_by_fam[f] for f in fams])
    means = np.empty(b)
    n = len(fams)
    for i in range(b):
        idx = rng.integers(0, n, size=n)
        means[i] = float(np.mean(diffs[idx]))
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(np.mean(means)), float(lo), float(hi)


def main() -> int:
    eval_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EVAL
    data = json.loads(open(eval_path).read())
    passages, queries = data["passages"], data["queries"]
    clients = sorted({q["client"] for q in queries})

    # config-tag -> per_query dict
    scored = {}
    for model, dim in CONFIGS:
        tag = f"{model}@{dim}"
        try:
            scored[tag] = _score_config(passages, queries, model, dim)
        except Exception as exc:
            print(f"  {tag}: ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
            scored[tag] = None

    def_tag = f"{DEFAULT_CONFIG[0]}@{DEFAULT_CONFIG[1]}"

    # Aggregate: matrix[tag]["overall"] and matrix[tag][client].
    matrix = {}
    for tag, pq in scored.items():
        if pq is None:
            matrix[tag] = None
            continue
        by_client = defaultdict(list)
        for r in pq.values():
            by_client[r["client"]].append(r)
        matrix[tag] = {
            "overall": _means(list(pq.values())),
            "by_client": {c: _means(by_client[c]) for c in clients},
        }

    ok_tags = [t for t in matrix if matrix[t] is not None]

    # Per-client winner (by cov5, tie-break ndcg) + bootstrap gap vs default.
    rng = np.random.default_rng(SEED)
    per_client_winner = {}
    for c in clients:
        ranked = sorted(
            ok_tags,
            key=lambda t: (matrix[t]["by_client"][c]["cov5"], matrix[t]["by_client"][c]["ndcg"]),
            reverse=True,
        )
        best = ranked[0]
        # family-level cov5 for best and default, this client.
        best_fam = {scored[best][qid]["family"]: scored[best][qid]["cov5"]
                    for qid in scored[best] if scored[best][qid]["client"] == c}
        def_fam = {scored[def_tag][qid]["family"]: scored[def_tag][qid]["cov5"]
                   for qid in scored[def_tag] if scored[def_tag][qid]["client"] == c} if scored.get(def_tag) else {}
        gap = lo = hi = None
        if def_fam and best != def_tag:
            gap, lo, hi = _cluster_bootstrap_gap(best_fam, def_fam, rng)
        per_client_winner[c] = {
            "winner": best,
            "winner_cov5": matrix[best]["by_client"][c]["cov5"],
            "default_cov5": matrix[def_tag]["by_client"][c]["cov5"] if matrix.get(def_tag) else None,
            "gap_vs_default": None if gap is None else round(gap, 4),
            "gap_ci95": None if gap is None else [round(lo, 4), round(hi, 4)],
            "gap_significant": None if gap is None else bool(lo > 0),
        }

    winners = {per_client_winner[c]["winner"] for c in clients}
    any_significant_flip = any(
        per_client_winner[c]["winner"] != def_tag and per_client_winner[c]["gap_significant"]
        for c in clients
    )

    # ---- persist ----
    stamp = _dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    bench_dir = os.path.expanduser("~/braincell-benchmarks")
    os.makedirs(bench_dir, exist_ok=True)
    record = {
        "timestamp": stamp, "eval_file": eval_path, "configs": [f"{m}@{d}" for m, d in CONFIGS],
        "clients": clients, "matrix": matrix, "per_client_winner": per_client_winner,
        "distinct_winners": sorted(winners), "any_significant_flip": any_significant_flip,
    }
    with open(os.path.join(bench_dir, "style-grid-results.jsonl"), "a") as f:
        f.write(json.dumps(record) + "\n")
    with open(os.path.join(bench_dir, f"style-grid-{stamp}.json"), "w") as f:
        json.dump(record, f, indent=2)

    # ---- report ----
    print("JSON_RESULTS_START")
    print(json.dumps(record, indent=2))
    print("JSON_RESULTS_END")

    print(f"\nOVERALL per embedder (all {len(queries)} queries)")
    print(f"{'embedder':<26}{'nDCG':>8}{'cov5':>8}{'R@5':>8}")
    print("-" * 50)
    for tag in [f"{m}@{d}" for m, d in CONFIGS]:
        if matrix.get(tag) is None:
            print(f"{tag:<26}   ERROR")
            continue
        o = matrix[tag]["overall"]
        star = "  <- default" if tag == def_tag else ""
        print(f"{tag:<26}{o['ndcg']:>8.3f}{o['cov5']:>8.3f}{o['r5']:>8.3f}{star}")

    for metric in ("cov5", "ndcg"):
        print(f"\nEMBEDDER x CLIENT ({metric}) — does the column winner change by client?")
        print(f"{'embedder':<26}" + "".join(f"{c[:11]:>13}" for c in clients))
        print("-" * (26 + 13 * len(clients)))
        for tag in [f"{m}@{d}" for m, d in CONFIGS]:
            if matrix.get(tag) is None:
                print(f"{tag:<26}  ERROR")
                continue
            cells = []
            for c in clients:
                v = matrix[tag]["by_client"][c][metric]
                mark = "*" if per_client_winner[c]["winner"] == tag else " "
                cells.append(f"{v:>11.3f}{mark} ")
            print(f"{tag:<26}" + "".join(cells))

    print("\nPER-CLIENT WINNER (by cov5) + bootstrap gap vs default "
          f"({def_tag}), family-cluster B={B_RESAMPLES}")
    for c in clients:
        w = per_client_winner[c]
        gap = "" if w["gap_vs_default"] is None else (
            f"  gap +{w['gap_vs_default']:.3f} CI{w['gap_ci95']} "
            f"{'SIGNIFICANT' if w['gap_significant'] else 'n.s.'}")
        tag = "(= default)" if w["winner"] == def_tag else ""
        print(f"  {c:<14} winner {w['winner']:<24} cov5 {w['winner_cov5']:.3f} {tag}{gap}")

    print("\nVERDICT")
    print(f"  distinct per-client winners: {sorted(winners)}")
    if len(winners) == 1:
        print(f"  -> ONE embedder ({next(iter(winners))}) wins for EVERY client. "
              "Single default is Pareto-optimal across client query styles; a per-client "
              "embedder router is NOT justified by this evidence.")
    elif any_significant_flip:
        print("  -> Winners FLIP across clients AND at least one flip's gap CI excludes 0. "
              "The router hypothesis has evidence — investigate before rejecting; weigh the "
              "flip size against the fingerprint/re-embed cost.")
    else:
        print("  -> Winners differ by client but NO flip is statistically significant "
              "(all gap CIs include 0). Not enough evidence to justify a router; the "
              "apparent flips are within noise. Prefer the single strongest default.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
