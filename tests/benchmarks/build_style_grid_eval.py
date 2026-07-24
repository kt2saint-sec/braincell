#!/usr/bin/env python3
"""build_style_grid_eval.py — assemble the client-query-style factorial eval.

Fable step 2: the SAME 42 frozen H7 passages, but each of the 14 lesson families
is queried by 5 different CLIENT models (3 Claude sizes + 2 local coding models),
each authoring in its own natural voice from a neutral seed (banned-token list
enforced → no lexical leakage). This isolates the client-query-style axis from the
embedder axis: bench_style_grid.py then sweeps embedders × clients and checks
whether the winning embedder FLIPS by client (the router decision rule).

This script:
  1. copies the 42 passages verbatim from the H7 eval (frozen corpus),
  2. attaches each client's authored query per family (relevant_ids from the seeds),
  3. stems every query against its family's `avoid` list (reusing
     validate_h7_leakage.content_words) and reports any banned-token leak,
  4. writes eval-style-grid-2026-07-08.json.

Query authoring provenance (frozen, 2026-07-08):
  haiku          = Claude Haiku 4.5   (subagent)
  sonnet         = Claude Sonnet 5    (subagent)
  opus           = Claude Opus 4.8    (subagent)
  qwen3-coder    = qwen3-coder:30b    (local, Ollama)
  qwen2.5-coder  = qwen2.5-coder:7b   (local, Ollama)

Run from repo root:  .venv/bin/python tests/benchmarks/build_style_grid_eval.py
"""
from __future__ import annotations

import json
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
# noqa: E402 — MUST follow the sys.path.insert above; hoisting this breaks the import.
from validate_h7_leakage import content_words  # noqa: E402  (shared tokenizer/stemmer)

H7_EVAL = os.path.join(_THIS_DIR, "eval-H7-vocab-divergence-2026-07-08.json")
SEEDS = os.path.join(_THIS_DIR, "style-grid-seeds.json")
OUT = os.path.join(_THIS_DIR, "eval-style-grid-2026-07-08.json")

# family "NN" -> query text, per client. Authored 2026-07-08; frozen as static data.
CLIENTS: dict[str, dict[str, str]] = {
    "haiku": {
        "01": "desktop window renders but doesn't respond to clicks",
        "02": "adding more workers decreased performance instead of improving it",
        "03": "long operations don't finish when the app loses focus",
        "04": "adding a field to optimize made queries slower instead",
        "05": "unique per-request identifiers caused the whole system to crash",
        "06": "preventing a flood of similar events from overwhelming the handler",
        "07": "handling the same request processed multiple times safely",
        "08": "requests queue up even though the system doesn't look overloaded",
        "09": "page makes one query per row instead of loading everything together",
        "10": "producer faster than consumer without overflow handling",
        "11": "one service failure took down the entire application",
        "12": "synchronized failure recovery causing immediate cascade crash",
        "13": "changes don't appear in the UI after being saved",
        "14": "losing unsaved work when the application terminates",
    },
    "sonnet": {
        "01": "packaged desktop app window opens and looks fine but buttons and menu clicks do nothing at all - what typically causes a UI to become completely unresponsive to input",
        "02": "added more worker processes to clear a growing job backlog but the backlog grew faster afterward - why would adding more workers slow throughput instead of helping",
        "03": "a long-running task gets silently killed or never completes once the app is no longer the active foreground app - how to make it finish reliably regardless",
        "04": "chose a column to speed up lookups and it turned into a hot spot that slowed everything down instead - what's the right way to pick that column",
        "05": "tagging every request with a unique id for tracing caused memory and storage usage to blow up and the metrics/logging system to fall over",
        "06": "the same event fires many times in rapid succession and each one triggers expensive work, overwhelming the handler - how do I collapse rapid repeated triggers into one",
        "07": "a network call gets sent twice because of a client resend and it needs to be safe to run the same operation more than once without duplicating effects",
        "08": "requests are stuck waiting even though CPU and memory look fine, like everything is queued up for some limited resource that's all checked out",
        "09": "a single page load triggers a huge number of small individual data lookups instead of one combined query - why is it making so many separate calls",
        "10": "the producing side generates data much faster than the consuming side can process it and nothing slows the producer down, so memory grows unbounded",
        "11": "one downstream service starts failing and that failure cascades and takes down unrelated parts of the whole system instead of staying contained",
        "12": "after an outage clears, every client reconnects or reattempts at the exact same instant and the flood of simultaneous attempts knocks the service back down",
        "13": "I just wrote an update but when I read it back or reload the page I still see the old stale value for a while",
        "14": "when the service is stopped or restarted, whatever request or job was in flight at that moment just gets lost instead of finishing or being resumed",
    },
    "opus": {
        "01": "native app window opens and renders fine but every button and click is completely dead with no reaction",
        "02": "added more parallel processing capacity and the pending queue grew larger instead of shrinking",
        "03": "a long-running job stalls or never completes once the app loses focus or the screen is off",
        "04": "chose a column to make lookups faster but it turned into the slowest part of the query",
        "05": "storing a distinct value per event blew up memory and brought the metrics/storage layer to its knees",
        "06": "a flood of nearly identical events fires within milliseconds and swamps the callback",
        "07": "the same operation may run more than once and must stay correct no matter how many times it executes",
        "08": "callers stall waiting to acquire a resource yet no component appears busy or saturated",
        "09": "a single view fires dozens of separate fetches to assemble its data instead of one",
        "10": "producer emits far quicker than the downstream can process and nothing throttles the flow",
        "11": "a single failing downstream service drags the entire application down with it",
        "12": "all clients recover simultaneously and immediately knock the service over again",
        "13": "just wrote an update but reads keep returning the previous stale value",
        "14": "unfinished in-flight work vanishes when the process is shut down mid-task",
    },
    "qwen3-coder": {
        "01": "UI appears but user interactions are unresponsive",
        "02": "Increasing worker count worsened task processing delays",
        "03": "Tasks fail to complete when app is not in active state",
        "04": "Choosing a field for optimization introduced new performance issues",
        "05": "Adding unique identifiers caused system-wide instability",
        "06": "Rapid duplicate events overwhelm the processing logic",
        "07": "Same operation executes multiple times without side effects",
        "08": "Requests are stuck waiting despite no apparent resource limits",
        "09": "Screen makes excessive data calls during rendering",
        "10": "Data generation outpaces consumption with no flow control",
        "11": "System failure propagates due to single point of failure",
        "12": "Multiple failures occur simultaneously after recovery",
        "13": "Recent updates are not visible across all system instances",
        "14": "Unfinished operations are lost during shutdown process",
    },
    "qwen2.5-coder": {
        "01": "UI elements load but don't respond to user actions",
        "02": "Scaling up workers worsens system performance",
        "03": "Long-running tasks fail when app is not in focus",
        "04": "Optimizing a query creates unexpected bottlenecks",
        "05": "Adding unique identifiers causes system failure",
        "06": "Handling rapid repeated duplicate events efficiently",  # repaired: dropped banned "bursts" (was "rapid bursts of"), style preserved
        "07": "Ensuring safe execution of repeated actions",
        "08": "Requests are waiting for resources without apparent overload",
        "09": "Single screen making excessive data requests",
        "10": "One side producing data faster than the other can handle",
        "11": "Broken dependencies causing cascading failures",
        "12": "System recovering together but breaking again immediately",
        "13": "App displays old data after change is saved",
        "14": "Work in progress lost when process stops",
    },
}


def _stems(text: str) -> set[str]:
    return set(content_words(text))


def main() -> int:
    h7 = json.loads(open(H7_EVAL).read())
    seeds = {f["family"]: f for f in json.loads(open(SEEDS).read())["families"]}
    passages = h7["passages"]  # 42, frozen verbatim

    # Pre-stem each family's banned tokens once.
    banned_stems: dict[str, set[str]] = {
        fam: {s for tok in seeds[fam]["avoid"] for s in content_words(tok)}
        for fam in seeds
    }

    queries = []
    leaks = []
    for client, per_family in CLIENTS.items():
        for fam, text in per_family.items():
            qstems = _stems(text)
            hit = qstems & banned_stems[fam]
            if hit:
                leaks.append((client, fam, sorted(hit), text))
            queries.append({
                "id": f"SG_{client}_q{fam}",
                "text": text,
                "relevant_ids": seeds[fam]["relevant_ids"],
                "test": "STYLE",
                "client": client,
                "family": fam,
            })

    print(f"passages={len(passages)}  clients={len(CLIENTS)}  queries={len(queries)}")
    if leaks:
        print(f"\n!! LEAKAGE — {len(leaks)} quer(ies) contain a banned-token stem:")
        for client, fam, hit, text in leaks:
            print(f"   [{client} f{fam}] banned {hit}: {text!r}")
        print("\nRepair these (minimal edit preserving the client's style) and re-run.")
    else:
        print("leakage check: CLEAN (no banned-token stems in any query)")

    out = {
        "_doc": "Client-query-style factorial (Fable step 2). 42 frozen H7 passages; "
                "each of 14 families queried by 5 client models. test='STYLE'; each "
                "query tagged with 'client' and 'family'. Scored by bench_style_grid.py.",
        "provenance": {
            "haiku": "Claude Haiku 4.5 (subagent)",
            "sonnet": "Claude Sonnet 5 (subagent)",
            "opus": "Claude Opus 4.8 (subagent)",
            "qwen3-coder": "qwen3-coder:30b (local, Ollama)",
            "qwen2.5-coder": "qwen2.5-coder:7b (local, Ollama)",
        },
        "passages": passages,
        "queries": queries,
    }
    with open(OUT, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\nwrote {OUT}")
    return 1 if leaks else 0


if __name__ == "__main__":
    sys.exit(main())
