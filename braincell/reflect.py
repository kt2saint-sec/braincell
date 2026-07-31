# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
reflect.py — `braincell reflect`: LLM consolidation / reflection pass.

Where ``consolidate`` merges *near-duplicate* notes deterministically, ``reflect``
takes clusters of *related* notes and asks a local LLM (Ollama) to synthesize a
single higher-level, higher-confidence note — the "reflection" idea from
Generative Agents (arXiv:2304.03442). The synthesized note is written, the source
notes are marked ``superseded_by`` it AND soft-tombstoned (retained for audit,
hidden from recall), so a re-run finds nothing new (idempotent).

Design:
  - Clustering reuses ``store.find_note_clusters`` (embedding cosine).
  - Synthesis is best-effort and fully offline (local Ollama). If the model is
    unavailable the cluster is skipped gracefully — never raises (mirrors the
    embedder-down handling elsewhere).
  - Dry-run (default) prints the proposed clusters and writes nothing; ``--apply``
    performs the synthesis + supersede.
  - The LLM call and the embedder are injectable (``synth_fn`` / ``embed_fn``) so
    tests run without Ollama.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from .log import get as _get_log
from .store import SqliteStore

log = _get_log("braincell.reflect")

_DATETIME_FMT = "%Y-%m-%d %H:%M:%S"

# Type aliases for the injectable seams.
SynthFn = Callable[[list[str]], str | None]
EmbedFn = Callable[[str], Awaitable[np.ndarray]]


@dataclass
class ReflectResult:
    """Outcome of a reflect pass."""
    clusters_considered: int = 0
    synthesized: int = 0
    skipped: int = 0
    written_note_ids: list[int] = field(default_factory=list)


def _default_model() -> str:
    return os.environ.get("BRAINCELL_LLM_MODEL", "qwen2.5:7b")


def ollama_synthesize(contents: list[str], model: str | None = None,
                      verbose: bool = False) -> str | None:
    """Best-effort Ollama synthesis of one higher-level note. Never raises.

    Returns the synthesized note text, or None on any failure (model missing,
    timeout, empty output, import error) so the caller skips the cluster.
    """
    model = model or _default_model()
    try:
        import ollama  # declared dependency; lazy so a missing model is graceful
        joined = "\n\n---\n\n".join(contents)
        prompt = (
            "The following memory notes are related. Synthesize ONE higher-level, "
            "higher-confidence note that captures the durable lesson or decision "
            "spanning them. Be concise and specific. Output ONLY the synthesized "
            "note text, with no preamble.\n\n"
            f"{joined}"
        )
        if verbose:
            print(f"  [reflect] synthesising with {model}…")
        resp = ollama.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            options={"num_predict": 512},
        )
        body = resp.message.content.strip()
        return body or None
    except Exception as exc:  # noqa: BLE001 — a synthesis outage skips one cluster, never aborts reflection
        log.warning("reflect synthesis failed (%r) — skipping cluster.", exc)
        return None


async def _cluster_created_at(store: SqliteStore, note_id: int) -> str | None:
    mem = await store._conn_get()
    row = await (await mem.execute(
        "SELECT created_at FROM memory_notes WHERE id = ?", (note_id,)
    )).fetchone()
    return row[0] if row else None


def _within_days(created_at: str | None, since_days: int | None, now: datetime) -> bool:
    if since_days is None:
        return True
    if not created_at:
        return True
    try:
        created = datetime.strptime(created_at, _DATETIME_FMT)
    except (ValueError, TypeError):
        return True
    return (now - created).total_seconds() <= since_days * 86400.0


async def _cluster_contents(store: SqliteStore, cluster: list[int]) -> dict[int, str]:
    mem = await store._conn_get()
    ph = ",".join("?" * len(cluster))
    rows = await (await mem.execute(
        f"SELECT id, content FROM memory_notes WHERE id IN ({ph})", cluster
    )).fetchall()
    return {r[0]: r[1] for r in rows}


async def reflect(
    store: SqliteStore,
    project_id: str,
    *,
    threshold: float = 0.85,
    since_days: int | None = None,
    apply: bool = False,
    model: str | None = None,
    synth_fn: SynthFn | None = None,
    embed_fn: EmbedFn | None = None,
    now: datetime | None = None,
    verbose: bool = False,
    backup_path: str | None = None,
) -> ReflectResult:
    """Run a reflection pass over clusters of related notes.

    dry-run (apply=False, default): print proposed clusters, write nothing.
    apply=True: synthesize one note per cluster, mark sources superseded +
    tombstoned. Idempotent: superseded/tombstoned sources drop out of the next
    clustering, so a re-run produces no new notes.
    """
    now = now or datetime.now()
    synth_fn = synth_fn or (lambda contents: ollama_synthesize(contents, model, verbose))

    clusters = await store.find_note_clusters(project_id, threshold=threshold)
    result = ReflectResult()

    # Filter to clusters whose representative (newest) note is within the window.
    selected: list[list[int]] = []
    for cluster in clusters:
        created = await _cluster_created_at(store, cluster[0])
        if _within_days(created, since_days, now):
            selected.append(cluster)
    result.clusters_considered = len(selected)

    if not selected:
        print(f"No clusters to reflect on (threshold={threshold:.2f}"
              + (f", since={since_days}d" if since_days is not None else "") + ").")
        return result

    if not apply:
        print(f"{len(selected)} cluster(s) would be reflected "
              f"(threshold={threshold:.2f}). Dry-run — nothing written.")
        for i, cluster in enumerate(selected, 1):
            by_id = await _cluster_contents(store, cluster)
            print(f"\nCluster {i} ({len(cluster)} notes):")
            for nid in cluster:
                snippet = (by_id.get(nid, "") or "")[:100].replace("\n", " ")
                print(f"  note {nid}: {snippet!r}")
        print("\nRe-run with --apply to synthesize and supersede the sources.")
        return result

    # One operation covers this whole --apply run so `memory undo <n>` reverses
    # it as a unit. Opened only on the apply path — a dry-run writes nothing at all.
    op_id = await store.begin_operation("reflect", project_id, backup_path)

    for cluster in selected:
        by_id = await _cluster_contents(store, cluster)
        contents = [by_id.get(nid, "") for nid in cluster]
        text = synth_fn(contents)
        if not text:
            result.skipped += 1
            if verbose:
                print(f"  [reflect] cluster {cluster} skipped (no synthesis).")
            continue

        embedding: np.ndarray | None = None
        if embed_fn is not None:
            try:
                embedding = await embed_fn(text)
            except Exception as exc:  # noqa: BLE001 — embedder down → store FTS-only, still works
                log.warning("reflect embed failed (%r) — storing note without vector.", exc)
                embedding = None

        # One atomic transaction per cluster — synthesis insert, op-log
        # snapshots, supersession pointers and source tombstones commit together
        # or not at all. The LLM call and the embedding above stay OUTSIDE the
        # transaction, so the write lock is never held across a model invocation.
        synth_id = await store.reflect_cluster_atomic(
            op_id, project_id, cluster, text, embedding=embedding,
        )

        result.synthesized += 1
        result.written_note_ids.append(synth_id)
        if verbose:
            print(f"  [reflect] synthesized note {synth_id} from {cluster} "
                  f"(sources superseded + tombstoned).")

    recorded = await store.finalize_operation(op_id)
    print(f"Reflection complete: {result.synthesized} synthesized, "
          f"{result.skipped} skipped.")
    if recorded:
        print(f"  Undo this with: braincell memory undo {op_id}")
    return result
