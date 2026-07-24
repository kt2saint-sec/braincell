#!/usr/bin/env python3
"""bench_harness.py — deterministic embedding-quality scorer.

Reads an eval-set JSON, embeds the shared passage corpus + every query with each
model config (reloading embed_spec per config), retrieves by cosine, and scores
Recall@1, Recall@5, MRR@10, nDCG@10 against the labeled relevant ids — overall and
per test type (T1/T2/T3). Also reports embed latency (ms/item) and dim.

eval JSON shape:
  {"passages":[{"id": "T1_p1", "text": "..."}, ...],
   "queries":[{"id":"T1_q1","text":"...","relevant_ids":["T1_p1"],"test":"T1"}, ...]}

Usage: BENCH pins the model list; run `python bench_harness.py eval.json`.
Prints a JSON blob (machine-readable) then a human table.
"""
from __future__ import annotations

import importlib
import json
import math
import sys
import time
from collections import defaultdict

import numpy as np

CONFIGS = [
    ("qwen3-embedding:4b", 1024),      # A: current default (MRL-truncated)
    ("qwen3-embedding:4b", 2560),      # B: native 4b (full fidelity)
    ("qwen3-embedding:0.6b", 1024),    # C: light/native
    ("nomic-embed-text", 768),         # D: cross-family baseline
    ("bge-m3", 1024),                  # E: long-context (8K), multilingual
    ("mxbai-embed-large", 1024),       # F: strong general, short (512-tok) context
]


def _embed_passages_and_queries(p_texts, q_texts, model, dim):
    """Reload embed_spec+embed for (model, dim) so the per-model prefix registry
    (P0-2) applies, then embed passages via the DOCUMENT path (embed_texts →
    DOC_PREFIX) and queries via the QUERY path (embed_query → QUERY_PREFIX).

    Embedding queries through embed_query is what makes the asymmetric prefix
    take effect — the reader-side query prefix is only ever applied there, never
    in embed_texts. For symmetric models (bge-m3 default) both prefixes are empty,
    so this is byte-identical to the old single-call path. Reloading embmod also
    clears embed_query's lru_cache, so no prior config's cached query vectors leak.

    Returns (pmat, qmat, ms_per_item) where ms_per_item averages the document and
    query embed rates.
    """
    import os
    os.environ["BRAINCELL_EMBED_PROVIDER"] = "ollama"
    os.environ["BRAINCELL_EMBED_MODEL"] = model
    os.environ["BRAINCELL_EMBED_DIM"] = str(dim)
    from braincell import embed_spec
    importlib.reload(embed_spec)
    from braincell import embed as embmod
    importlib.reload(embmod)

    t0 = time.perf_counter()
    p_vecs = embmod.embed_texts(p_texts)
    p_ms = (time.perf_counter() - t0) * 1000.0 / max(len(p_texts), 1)

    t1 = time.perf_counter()
    q_vecs = [embmod.embed_query(q) for q in q_texts]
    q_ms = (time.perf_counter() - t1) * 1000.0 / max(len(q_texts), 1)

    pmat = np.vstack([v for v in p_vecs])
    qmat = np.vstack([np.asarray(v, dtype=np.float32) for v in q_vecs])
    return pmat, qmat, (p_ms + q_ms) / 2.0


def _ndcg_at_k(ranked_ids, relevant, k=10):
    dcg = 0.0
    for i, pid in enumerate(ranked_ids[:k]):
        if pid in relevant:
            dcg += 1.0 / math.log2(i + 2)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    return dcg / ideal if ideal > 0 else 0.0


def _score_config(passages, queries, model, dim):
    p_texts = [p["text"] for p in passages]
    p_ids = [p["id"] for p in passages]
    q_texts = [q["text"] for q in queries]
    # Passages via the document path (DOC_PREFIX), queries via the query path
    # (QUERY_PREFIX) — asymmetric prefix injection (P0-2).
    pmat, qmat, ms = _embed_passages_and_queries(p_texts, q_texts, model, dim)
    p_ms = q_ms = ms

    per_test = defaultdict(lambda: {"r1": [], "r5": [], "mrr": [], "ndcg": [], "cov5": []})
    overall = {"r1": [], "r5": [], "mrr": [], "ndcg": [], "cov5": []}
    for qi, q in enumerate(queries):
        sims = pmat @ qmat[qi]
        order = np.argsort(sims)[::-1]
        ranked = [p_ids[j] for j in order]
        rel = set(q["relevant_ids"])
        r1 = 1.0 if ranked[0] in rel else 0.0
        r5 = 1.0 if any(pid in rel for pid in ranked[:5]) else 0.0
        # coverage@5: fraction of ALL relevant ids retrieved in top-5 — the key
        # metric for multi-relevant / cross-project queries ("did it connect BOTH").
        cov5 = (sum(1 for pid in ranked[:5] if pid in rel) / len(rel)) if rel else 0.0
        mrr = 0.0
        for i, pid in enumerate(ranked[:10]):
            if pid in rel:
                mrr = 1.0 / (i + 1)
                break
        ndcg = _ndcg_at_k(ranked, rel, 10)
        t = q.get("test", "?")
        for bucket in (overall, per_test[t]):
            bucket["r1"].append(r1)
            bucket["r5"].append(r5)
            bucket["mrr"].append(mrr)
            bucket["ndcg"].append(ndcg)
            bucket["cov5"].append(cov5)

    def _mean(d):
        return {k: round(float(np.mean(v)) if v else 0.0, 4) for k, v in d.items()}

    return {
        "model": model, "dim": dim,
        "overall": _mean(overall),
        "per_test": {t: _mean(v) for t, v in per_test.items()},
        "embed_ms_per_item": round((p_ms + q_ms) / 2, 2),
        "n_queries": len(queries), "n_passages": len(passages),
    }


def main():
    eval_path = sys.argv[1]
    data = json.loads(open(eval_path).read())
    passages, queries = data["passages"], data["queries"]
    results = []
    for model, dim in CONFIGS:
        try:
            results.append(_score_config(passages, queries, model, dim))
        except Exception as exc:
            results.append({"model": model, "dim": dim, "error": f"{type(exc).__name__}: {exc}"})

    # Persist EVERY run durably — keep every benchmark, no matter what (outside
    # the session scratchpad so it survives cleanup).
    import os as _os
    import datetime as _dt
    _bench_dir = _os.path.expanduser("~/braincell-benchmarks")
    _os.makedirs(_bench_dir, exist_ok=True)
    _stamp = _dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    _record = {
        "timestamp": _stamp, "eval_file": eval_path,
        "configs": [f"{m}@{d}" for m, d in CONFIGS],
        "eval_size": {"passages": len(passages), "queries": len(queries)},
        "results": results,
    }
    with open(_os.path.join(_bench_dir, "results.jsonl"), "a") as _f:
        _f.write(json.dumps(_record) + "\n")
    with open(_os.path.join(_bench_dir, f"run-{_stamp}.json"), "w") as _f:
        json.dump(_record, _f, indent=2)
    print(f"[persisted benchmark -> {_bench_dir}/results.jsonl + run-{_stamp}.json]")

    print("JSON_RESULTS_START")
    print(json.dumps(results, indent=2))
    print("JSON_RESULTS_END")
    print("\nOVERALL (each config, averaged across all tests)")
    print(f"{'config':<28}{'R@1':>7}{'R@5':>7}{'cov5':>7}{'MRR':>7}{'nDCG':>7}{'ms/item':>9}")
    print("-" * 72)
    for r in results:
        tag = f"{r['model']}@{r['dim']}"
        if "error" in r:
            print(f"{tag:<28}  ERROR: {r['error'][:40]}")
            continue
        o = r["overall"]
        print(f"{tag:<28}{o['r1']:>7.3f}{o['r5']:>7.3f}{o.get('cov5', 0.0):>7.3f}"
              f"{o['mrr']:>7.3f}{o['ndcg']:>7.3f}{r['embed_ms_per_item']:>9.1f}")

    # Per-test grids — EACH MODEL through EACH TEST, for the most telling metrics.
    tests = sorted({t for r in results if "error" not in r for t in r.get("per_test", {})})
    for metric, mname in (("ndcg", "nDCG@10"), ("r1", "Recall@1"), ("cov5", "Coverage@5")):
        print(f"\nPER-TEST {mname} (each model × each test)")
        print(f"{'config':<28}" + "".join(f"{t:>8}" for t in tests))
        print("-" * (28 + 8 * len(tests)))
        for r in results:
            tag = f"{r['model']}@{r['dim']}"
            if "error" in r:
                print(f"{tag:<28}  ERROR")
                continue
            print(f"{tag:<28}" + "".join(
                f"{r['per_test'].get(t, {}).get(metric, 0.0):>8.3f}" for t in tests))


if __name__ == "__main__":
    main()
