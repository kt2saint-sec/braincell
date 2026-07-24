#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
federate_bench.py — real-corpus latency benchmark for federated family recall.

Validates the spec's §9 performance risk with actual numbers: builds a family of
N per-project brains with M notes each (offline — fake unit vectors, so the timing
is embedder-independent and needs no Ollama), then times, over R queries:

  - baseline: single-store recall on the seed brain (M notes)
  - federated: federated_recall fanned out across the whole family (N brains)

Reports mean / p95 wall-clock for each. The vector search (NumPy brute-force
cosine) is the real cost being measured; fan-out adds N concurrent read-only
opens + the RRF merge. The Ollama-reranked path is timed only when
BRAINCELL_RERANK=ollama is set and the daemon is reachable (else reported skipped).

Run:  .venv/bin/python scripts/federate_bench.py --members 6 --notes 200 --queries 20
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import tempfile
import time
from pathlib import Path

# ── Isolate XDG BEFORE importing braincell so we never touch the real store ────
_TMP = tempfile.mkdtemp(prefix="braincell-bench-")
os.environ["XDG_DATA_HOME"] = str(Path(_TMP) / "xdg")
os.environ.setdefault("BRAINCELL_DATA_NAMESPACE", "braincell_bench")
os.environ.setdefault("BRAINCELL_EMBED_PROVIDER", "ollama")
os.environ["BRAINCELL_FEDERATE"] = "on"

import numpy as np  # noqa: E402

from braincell import embed_spec  # noqa: E402
from braincell.config import get_db_path, get_project_id  # noqa: E402
from braincell.federate import federated_recall, plan_for_seed  # noqa: E402
from braincell.project_registry import add_family_members  # noqa: E402
from braincell.store import SqliteStore  # noqa: E402

_WORDS = ("caching latency schema vector recall fusion index embedding dedup token "
          "async pipeline migration rerank cosine family pool merge scope provenance").split()


def _fake_vec(rng: np.random.Generator) -> np.ndarray:
    v = rng.standard_normal(embed_spec.DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def _build_member(root: Path, n_notes: int, rng: np.random.Generator) -> str:
    pid = get_project_id(root)
    db = get_db_path(pid)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.assert_schema_version()

    async def _seed() -> None:
        for i in range(n_notes):
            words = " ".join(rng.choice(_WORDS, size=8))
            await store.remember(f"note {i}: {words}", "note", pid, embedding=_fake_vec(rng))

    asyncio.run(_seed())
    store.close()
    return pid


async def _time_async(coro_factory, reps: int) -> list[float]:
    times = []
    for _ in range(reps):
        t0 = time.perf_counter()
        await coro_factory()
        times.append((time.perf_counter() - t0) * 1000.0)
    return times


def _p95(xs: list[float]) -> float:
    ordered = sorted(xs)
    return ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--members", type=int, default=6)
    ap.add_argument("--notes", type=int, default=200)
    ap.add_argument("--queries", type=int, default=20)
    ap.add_argument("--k", type=int, default=10)
    args = ap.parse_args()

    rng = np.random.default_rng(1)
    roots = []
    pids = []
    print(f"building {args.members} brains × {args.notes} notes …")
    for m in range(args.members):
        root = Path(_TMP) / f"proj{m}"
        root.mkdir(parents=True, exist_ok=True)
        pids.append(_build_member(root, args.notes, rng))
        roots.append(str(root))
    add_family_members("bench", roots)
    seed = pids[0]
    plan = plan_for_seed(seed)
    qvec = _fake_vec(rng)

    async def _baseline():
        store = SqliteStore(get_db_path(seed))
        try:
            await store.recall(qvec, seed, args.k, qtext="caching latency", rerank=False)
        finally:
            await store.aclose()

    async def _federated():
        store = SqliteStore(get_db_path(seed))
        try:
            await federated_recall(store, plan, qvec, args.k, qtext="caching latency")
        finally:
            await store.aclose()

    # warm-up (open connections, page cache) then measure
    asyncio.run(_time_async(_baseline, 3))
    asyncio.run(_time_async(_federated, 3))
    base = asyncio.run(_time_async(_baseline, args.queries))
    fed = asyncio.run(_time_async(_federated, args.queries))

    print(f"\nembedder space: {embed_spec.FINGERPRINT}  |  family={len(plan.targets)} brains  "
          f"|  k={args.k}  |  {args.queries} queries\n")
    print(f"{'path':<28}{'mean ms':>12}{'p95 ms':>12}")
    print("-" * 52)
    print(f"{'single-store (seed only)':<28}{statistics.mean(base):>12.2f}{_p95(base):>12.2f}")
    print(f"{'federated (whole family)':<28}{statistics.mean(fed):>12.2f}{_p95(fed):>12.2f}")
    print(f"\nfan-out overhead: {statistics.mean(fed) - statistics.mean(base):.2f} ms mean "
          f"for {len(plan.targets) - 1} extra brains\n")
    if os.environ.get("BRAINCELL_RERANK") == "ollama":
        print("(BRAINCELL_RERANK=ollama set — federated path includes the LLM rerank)")
    else:
        print("(rerank path not timed — set BRAINCELL_RERANK=ollama with a live daemon to include it)")


if __name__ == "__main__":
    main()
