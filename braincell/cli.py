# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
cli.py — standalone BrainCell command line.

    braincell build <path>    ingest agent transcripts + curated notes
    braincell sync  <path>    incremental build (new/changed transcripts)
    braincell register <path> mint/confirm the project ULID (no ingest)
    braincell serve           run the FastMCP stdio server (= python -m braincell)

A `build` ingests this repo's agent transcripts into its per-project brain.
Embed-safety (dimension guard, --reembed clean slate, null-embedding warning)
keeps the vector space consistent across runs.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3 as _sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

from . import embed_spec
from .config import (
    get_db_path,
    get_global_db_path,
    get_project_id,
    resolve_project_id_readonly,
)
from .embed import embed_texts, prewarm_embed_model
from .mode import resolve_mode
from .project_registry import register_path
from .store import EmbedderMismatchError, SqliteStore
from .transcript_ingest import _LEDGER_FILENAME, ingest_transcripts


# ── embed-safety helpers ──────────────────────────────────────────────────────


def _embed_dim_guard(project_id: str, db_path: Path) -> None:
    """Fail loud (NO wipe) if the store holds vectors at a dimension other than
    the configured embedder's (embed_spec.DIM). Mixing vector spaces corrupts
    search. Points the user at --reembed."""
    expected = embed_spec.DIM * 4  # float32 → 4 bytes/dim

    async def _lengths() -> list[int]:
        async with aiosqlite.connect(str(db_path)) as cf:
            rows = await (await cf.execute(
                "SELECT DISTINCT LENGTH(c.embedding) FROM bc_chunks c "
                "JOIN bc_documents d ON c.document_id = d.id "
                "WHERE c.embedding IS NOT NULL AND d.project_id = ?",
                (project_id,),
            )).fetchall()
        return [r[0] for r in rows if r[0] is not None]

    mismatched = [b for b in asyncio.run(_lengths()) if b != expected]
    if not mismatched:
        return
    found = sorted({b // 4 for b in mismatched})
    print(
        f"ERROR: embedding dimension mismatch — store has {found}d vectors, but "
        f"the configured embedder {embed_spec.MODEL} is {embed_spec.DIM}d. Refusing "
        f"to mix vector spaces (would corrupt search). Re-run with --reembed to wipe "
        f"and rebuild.",
        file=sys.stderr,
    )
    raise SystemExit(1)


def _clear_transcript_ledger(store: SqliteStore) -> bool:
    """Delete the transcript-ingest ledger so --reembed forces a FULL re-ingest
    (else wiped transcript chunks stay marked 'done' and never come back)."""
    ledger = store._db_path.parent / _LEDGER_FILENAME
    if ledger.exists():
        ledger.unlink()
        return True
    return False


def _reembed_wipe(project_id: str, store: SqliteStore) -> None:
    """Unconditional clean slate for --reembed: drop ALL braincell docs/chunks +
    clear the transcript ledger, so the rebuild re-embeds EVERYTHING with the
    current embedder (handles dimension changes AND null-embedded rows)."""
    removed = store.wipe_project_embeddings(project_id)
    cleared = _clear_transcript_ledger(store)
    print(
        f"  --reembed: wiped {removed} documents + "
        f"{'cleared' if cleared else 'no'} transcript ledger → full clean rebuild."
    )


def _warn_null_embeddings(project_id: str, db_path: Path) -> None:
    """Warn loudly if any chunk ended up with a NULL embedding (embed failures).
    Such chunks are invisible to vector search; only --reembed retries them."""
    async def _nulls() -> int:
        async with aiosqlite.connect(str(db_path)) as cf:
            row = await (await cf.execute(
                "SELECT COUNT(*) FROM bc_chunks c "
                "JOIN bc_documents d ON c.document_id = d.id "
                "WHERE c.embedding IS NULL AND d.project_id = ?",
                (project_id,),
            )).fetchone()
        return row[0] or 0

    nulls = asyncio.run(_nulls())
    if nulls:
        print(
            f"  WARNING: {nulls} chunks have NULL embeddings (embed failures during "
            f"this run). They are invisible to vector search. Re-run with --reembed "
            f"once the embedder is reachable to retry them.",
            file=sys.stderr,
        )


# ── commands ──────────────────────────────────────────────────────────────────


def _run_build(
    root: Path,
    *,
    skip_transcripts: bool,
    reembed: bool,
    verbose: bool,
    mode: str | None = None,
) -> None:
    root = root.resolve()
    project_id = get_project_id(root)  # resolves via path-registry; mints + registers a new ULID if absent
    print(f"BrainCell build for project {project_id} ({root})")
    # Register path→ULID so transcript attribution + family resolution map this
    # repo's own sessions back to this ULID.
    register_path(str(root), project_id)

    # G4: resolve the active mode and pick the target DB accordingly.
    # Attribution (project_id) is ALWAYS the per-path ULID regardless of mode,
    # so chunks are identifiable in the global brain via their project_id column.
    m = resolve_mode(mode)
    db_path = get_global_db_path() if m == "global" else get_db_path(project_id)

    store = SqliteStore(db_path)
    try:
        store.assert_schema_version()
    except EmbedderMismatchError as exc:
        if not reembed:
            raise
        print(
            f"  --reembed: switching embedding space "
            f"{exc.built_with!r} -> {exc.configured!r} "
            f"(wiping all documents/chunks, clearing note embeddings)."
        )
        stats = store.reset_embedding_space()
        print(
            f"  --reembed: reset {stats['docs_wiped']} documents, "
            f"cleared {stats['note_embeddings_cleared']} note embeddings, "
            f"restamped fingerprint."
        )
        store.assert_schema_version()  # must pass now — restamped

    if reembed:
        _reembed_wipe(project_id, store)
    else:
        _embed_dim_guard(project_id, db_path)

    if not skip_transcripts:
        print("  Ingesting agent transcripts...")
        prewarm_embed_model()

        def _progress(msg: str) -> None:
            if verbose:
                print(f"    {msg}")

        stats = asyncio.run(ingest_transcripts(
            store, project_id, incremental=True, progress_cb=_progress,
        ))
        print(
            f"  Transcript ingest: {stats['files_ingested']} ingested, "
            f"{stats['files_skipped']} skipped, {stats.get('files_failed', 0)} failed, "
            f"{stats.get('files_unattributed', 0)} unattributed, "
            f"{stats.get('files_out_of_scope', 0)} out-of-family, "
            f"{stats['chunks_written']} chunks, {stats['secrets_rejected']} secret-rejections, "
            f"{stats.get('skill_docs_created', 0)} skill-docs created."
        )
    else:
        print("  Transcript ingest skipped (--skip-transcripts).")

    _warn_null_embeddings(project_id, db_path)
    store.close()
    print("BrainCell build complete.")


def cmd_build(args: argparse.Namespace) -> None:
    root = Path(args.path).resolve()
    if args.no_mint and resolve_project_id_readonly(root) is None:
        print(f"{root} is not a registered project and --no-mint set. "
              f"Run `braincell register {root}` first, or omit --no-mint.",
              file=sys.stderr)
        raise SystemExit(1)
    _run_build(root, skip_transcripts=args.skip_transcripts, reembed=args.reembed,
               verbose=args.verbose, mode=args.mode)


def cmd_sync(args: argparse.Namespace) -> None:
    # sync == build in incremental mode (no reembed). Build is already incremental
    # (content-hash + mtime→SHA gated), so this is a thin, intention-revealing alias.
    _run_build(Path(args.path).resolve(), skip_transcripts=args.skip_transcripts,
               reembed=False, verbose=args.verbose, mode=getattr(args, "mode", None))


def cmd_register(args: argparse.Namespace) -> None:
    root = Path(args.path).resolve()
    pid = get_project_id(root)            # resolves via path-registry; mints + registers if absent
    register_path(str(root), pid)         # workspace path→ULID map (idempotent re-affirm)
    print(f"Registered {root} → {pid}")


def cmd_serve(_args: argparse.Namespace) -> None:
    from .server import main as serve_main
    serve_main()


def cmd_reembed_notes(args: argparse.Namespace) -> None:
    """Backfill embeddings for memory_notes that have NULL embedding.

    Resolves the project via the path registry (same as 'build'), selects all
    memory_notes with NULL embedding for that project, embeds them in batches
    using the configured embed provider, and UPDATEs the rows. Prints a summary
    line on completion.
    """
    root = Path(args.path).resolve()
    project_id = get_project_id(root)
    register_path(str(root), project_id)

    store = SqliteStore(get_db_path(project_id))
    store.assert_schema_version()

    try:
        count = asyncio.run(store.reembed_notes(project_id, embed_texts))
    finally:
        store.close()

    print(f"Re-embedded {count} notes.")


async def _print_clusters_async(
    store: SqliteStore,
    clusters: list[list[int]],
    verbose: bool,
) -> None:
    """Print a dry-run cluster summary with per-note content snippets."""
    mem = await store._conn_get()
    for i, cluster in enumerate(clusters, 1):
        representative_id = cluster[0]
        placeholders = ",".join("?" * len(cluster))
        rows = await (await mem.execute(
            f"SELECT id, content FROM memory_notes WHERE id IN ({placeholders})",
            cluster,
        )).fetchall()
        by_id = {r[0]: r[1] for r in rows}
        print(f"\nCluster {i} ({len(cluster)} notes) — representative: note {representative_id}")
        for nid in cluster:
            marker = "[keep]" if nid == representative_id else "[merge]"
            content = by_id.get(nid, "")
            snippet = content[:120].replace("\n", " ")
            if len(content) > 120:
                snippet += "…"
            print(f"  {marker} note {nid}: {snippet!r}")


async def _try_llm_merge_async(
    store: SqliteStore,
    project_id: str,
    cluster: list[int],
    representative_id: int,
    verbose: bool,
    op_id: int,
) -> bool:
    """Attempt an ollama chat synthesis for the cluster merge. Best-effort; never raises.

    On any failure (model unavailable, timeout, empty output, import error, etc.)
    logs a warning to stderr and returns False. The caller MUST fall back to the
    deterministic merge (keep representative, tombstone the rest). This path is
    strictly opt-in (--llm flag) and is never required by tests.

    Returns:
        True iff synthesis succeeded and the merge was applied via supersede.
    """
    try:
        import ollama  # declared dependency; lazy here so --llm failure is graceful
        mem = await store._conn_get()
        placeholders = ",".join("?" * len(cluster))
        rows = await (await mem.execute(
            f"SELECT id, content FROM memory_notes WHERE id IN ({placeholders})",
            cluster,
        )).fetchall()
        by_id = {r[0]: r[1] for r in rows}
        notes_text = "\n\n---\n\n".join(
            f"Note {nid}:\n{by_id.get(nid, '')}" for nid in cluster
        )
        prompt = (
            "The following notes are near-duplicates and will be merged. "
            "Write a single concise note capturing all unique information. "
            "Output ONLY the merged note text, no preamble.\n\n"
            f"{notes_text}"
        )
        llm_model = os.environ.get("BRAINCELL_LLM_MODEL", "qwen2.5:7b")
        if verbose:
            print(f"  [llm] synthesising merged note with {llm_model}…")
        # ollama.chat is synchronous; acceptable in a CLI command (no server loop).
        resp = ollama.chat(
            model=llm_model,
            messages=[{"role": "user", "content": prompt}],
            options={"num_predict": 512},
        )
        merged_body = resp.message.content.strip()
        if not merged_body:
            print("  [llm] synthesis returned empty body — falling back.", file=sys.stderr)
            return False
        # One atomic transaction for the whole cluster — snapshots, the
        # superseding merged note, and every tombstone commit together or not at
        # all (the ollama call above stays outside the transaction). Snapshot
        # ordering (BEFORE mutation) and the supersede-chain semantics are
        # unchanged from the earlier per-step flow.
        await store.consolidate_cluster_atomic(
            op_id, project_id, cluster, representative_id,
            merged_content=merged_body,
        )
        if verbose:
            print(
                f"  [llm] merged {len(cluster)} notes into superseding note "
                f"(tombstoned {cluster})."
            )
        return True
    except Exception as exc:
        print(
            f"  [llm] synthesis failed ({exc!r}) — falling back to deterministic merge.",
            file=sys.stderr,
        )
        return False


async def _consolidate_async(
    store: SqliteStore,
    project_id: str,
    threshold: float,
    apply: bool,
    use_llm: bool,
    verbose: bool,
    backup_path: Optional[str] = None,
) -> None:
    """Core async logic for `braincell consolidate`."""
    clusters = await store.find_note_clusters(project_id, threshold=threshold)

    if not clusters:
        print(f"No clusters found at threshold={threshold:.2f}. Nothing to do.")
        return

    total_notes = sum(len(c) for c in clusters)
    print(
        f"{len(clusters)} cluster(s) found ({total_notes} notes), "
        f"threshold={threshold:.2f}."
    )

    if not apply:
        await _print_clusters_async(store, clusters, verbose)
        print(
            f"\n{len(clusters)} cluster(s) found ({total_notes} notes). "
            f"Re-run with --apply to merge (keeps newest note, tombstones the rest)."
        )
        return

    # One operation covers this whole --apply run, so `memory undo <n>` reverses
    # the run as a unit — which is how a user thinks about "undo that merge".
    op_id = await store.begin_operation("consolidate", project_id, backup_path)

    merged_count = 0
    for cluster in clusters:
        representative_id = cluster[0]  # newest-first; cluster[0] is the representative
        other_ids = cluster[1:]
        used_llm = False

        if use_llm:
            used_llm = await _try_llm_merge_async(
                store, project_id, cluster, representative_id, verbose, op_id
            )

        if not used_llm:
            # Snapshots + tombstones for the whole cluster in ONE transaction
            # (snapshot-before-mutation ordering preserved inside the method).
            await store.consolidate_cluster_atomic(
                op_id, project_id, cluster, representative_id
            )
            if verbose:
                print(
                    f"  Merged cluster: kept note {representative_id}, "
                    f"tombstoned {other_ids}."
                )
            else:
                print(f"  Merged: kept {representative_id}, tombstoned {other_ids}.")

        merged_count += 1

    recorded = await store.finalize_operation(op_id)
    print(f"Consolidation complete: {merged_count} cluster(s) merged.")
    if recorded:
        print(f"  Undo this with: braincell memory undo {op_id}")


def cmd_consolidate(args: argparse.Namespace) -> None:
    """Find near-duplicate notes and (opt-in) merge them.

    Default is a DRY-RUN that prints cluster summaries without writing. Pass
    --apply to perform the deterministic merge (keep newest note per cluster,
    soft-tombstone the rest). Pass --llm (with --apply) for an opt-in
    ollama-synthesis pass before the deterministic fallback.
    """
    root = Path(args.path).resolve()
    project_id = get_project_id(root)
    register_path(str(root), project_id)

    db = get_db_path(project_id)
    # Snapshot the brain before any destructive --apply. Cheap insurance, and
    # the coarse counterpart to `memory undo` — if the operation log itself is not
    # enough, the whole pre-merge brain is one file copy away.
    backup_path = None
    if args.apply:
        bp = _auto_backup(db, "consolidate")
        if bp is not None:
            backup_path = str(bp)
            print(f"Pre-merge backup: {bp}")
        else:
            print("Proceeding WITHOUT a pre-merge backup.", file=sys.stderr)

    store = SqliteStore(db)
    store.assert_schema_version()

    try:
        asyncio.run(_consolidate_async(
            store,
            project_id,
            threshold=args.threshold,
            apply=args.apply,
            use_llm=args.llm,
            verbose=args.verbose,
            backup_path=backup_path,
        ))
    finally:
        store.close()


async def _stats_async(store: SqliteStore, iters: int) -> None:
    """Print chunk/doc counts and a vector-search p95 benchmark."""
    import numpy as np

    from . import store as _store_mod

    status = await store.ingest_status(None)
    print(f"chunks: {status.chunk_count}   docs: {status.doc_count}")
    if status.chunk_count == 0:
        print("no chunks — vector-search benchmark skipped.")
        return

    rng = np.random.default_rng(0)
    for _ in range(max(1, iters)):
        v = rng.standard_normal(embed_spec.DIM).astype("float32")
        v /= np.linalg.norm(v)
        await store.search(v, "", None, 10, "semantic")

    p95 = store.vec_search_p95_ms()
    trigger = _store_mod._VEC_P95_TRIGGER_MS
    if p95 is None:
        print("vector-search p95: n/a")
        return
    print(f"vector-search p95: {p95:.2f} ms over {iters} probes "
          f"(brute-force decode+matmul; backend={_store_mod._BACKEND})")
    if p95 > trigger:
        print(f"  ⚠ p95 > {trigger:.0f} ms trigger — consider the sqlite-vec (vec0) "
              f"ANN backend (deferred behind an explicit supply-chain decision — "
              f"it adds a compiled third-party dependency).")
    else:
        print(f"  ✓ under the {trigger:.0f} ms trigger — brute-force is the right "
              f"choice at this scale.")


def cmd_stats(args: argparse.Namespace) -> None:
    """Show store size + a vector-search p95 benchmark (the sqlite-vec adopt-decision instrument)."""
    mode = args.mode if args.mode else resolve_mode()
    if mode == "global":
        db = get_global_db_path()
    else:
        root = Path(args.path).resolve()
        db = get_db_path(get_project_id(root))
    if not db.exists():
        print(f"No brain at {db} — run `braincell build` first.", file=sys.stderr)
        raise SystemExit(1)
    store = SqliteStore(db)
    store.assert_schema_version()
    try:
        asyncio.run(_stats_async(store, iters=args.iters))
    finally:
        store.close()


def cmd_reflect(args: argparse.Namespace) -> None:
    """Synthesize higher-level notes from clusters of related notes (LLM).

    DRY-RUN by default (prints the clusters that would be reflected). With
    --apply, calls a local Ollama model to synthesize one note per cluster and
    marks the source notes superseded + tombstoned. Fully offline; if the model
    is unavailable each cluster is skipped gracefully.
    """
    from .embed import embed_query_async
    from .reflect import reflect

    root = Path(args.path).resolve()
    project_id = get_project_id(root)
    register_path(str(root), project_id)

    db = get_db_path(project_id)
    # Snapshot before any destructive --apply (see cmd_consolidate).
    backup_path = None
    if args.apply:
        bp = _auto_backup(db, "reflect")
        if bp is not None:
            backup_path = str(bp)
            print(f"Pre-reflect backup: {bp}")
        else:
            print("Proceeding WITHOUT a pre-reflect backup.", file=sys.stderr)

    store = SqliteStore(db)
    store.assert_schema_version()

    try:
        asyncio.run(reflect(
            store,
            project_id,
            threshold=args.threshold,
            since_days=args.since,
            apply=args.apply,
            model=args.model,
            embed_fn=embed_query_async if args.apply else None,
            verbose=args.verbose,
            backup_path=backup_path,
        ))
    finally:
        store.close()


def cmd_contradictions(args: argparse.Namespace) -> None:
    """Audit embedding-close ACTIVE note pairs for contradictions (LLM-judged).

    READ-ONLY — there is deliberately no --apply: resolution is always an
    explicit `supersede`/`forget` by the owner (auto-resolving from recalled
    text is the memory-poisoning path the MCP design rejects). --no-llm lists
    the close pairs without judging them.
    """
    from .contradictions import find_contradictions, ollama_judge, print_report

    db, project_id = _open_project_brain(args.path)
    store = SqliteStore(db)
    store.assert_schema_version()

    judge = None
    if not args.no_llm:
        judge = lambda a, b: ollama_judge(a, b, model=args.model)  # noqa: E731
    try:
        report = asyncio.run(find_contradictions(
            store,
            project_id,
            threshold=args.threshold,
            limit=args.limit,
            judge_fn=judge,
        ))
    finally:
        store.close()
    print_report(report, verbose=args.verbose)


def _open_project_brain(path: str) -> tuple[Path, str]:
    """Resolve a project path → (db_path, project_id) read-only, or exit cleanly.

    Never mints an identity — an unregistered path is a user error, not a reason to
    create a brain. Mirrors cmd_recall's resolution.
    """
    root = Path(path).resolve()
    pid = resolve_project_id_readonly(root)
    if pid is None:
        print(f"No brain registered for {root} — run `braincell build` first.",
              file=sys.stderr)
        raise SystemExit(1)
    db = get_db_path(pid)
    if not db.exists():
        print(f"No brain at {db} — run `braincell build` first.", file=sys.stderr)
        raise SystemExit(1)
    return db, pid


def cmd_memory_log(args: argparse.Namespace) -> None:
    """List recorded merge operations (`consolidate --apply` / `reflect --apply`).

    The `id` column is the `<op#>` that `braincell memory undo` takes. Operations
    predating schema v5 are not listed — they were never recorded and cannot be
    undone (the pre-merge backup, if any, is the only route).
    """
    db, pid = _open_project_brain(args.path)
    store = SqliteStore(db)
    store.assert_schema_version()
    try:
        ops = asyncio.run(store.list_operations(pid, limit=args.limit))
    finally:
        store.close()

    if args.json:
        import json
        print(json.dumps(ops, indent=2))
        return
    if not ops:
        print("(no recorded merge operations)")
        return
    for op in ops:
        state = f"UNDONE {op['undone_at']}" if op["undone_at"] else "undoable"
        print(f"{op['id']:>4}  {op['kind']:<11} {op['created_at']}  "
              f"{op['note_count']:>3} note(s)  [{state}]")
        if op["backup_path"]:
            print(f"      backup: {op['backup_path']}")


def cmd_memory_undo(args: argparse.Namespace) -> None:
    """Reverse a recorded merge operation by its number (see `memory log`).

    Restores each affected note's exact pre-merge deleted_at/superseded_by and
    tombstones any note the operation synthesized. Notes changed by a later writer
    are REPORTED, never clobbered.
    """
    db, pid = _open_project_brain(args.path)
    store = SqliteStore(db)
    store.assert_schema_version()
    try:
        result = asyncio.run(store.undo_operation(args.op_id, pid))
    except ValueError as exc:
        print(f"braincell memory undo: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    finally:
        store.close()

    print(f"Undid {result['kind']} operation {result['op_id']}: "
          f"{len(result['restored'])} note(s) restored.")
    if result["skipped_changed"]:
        print(f"  SKIPPED (changed since the merge, left as-is): "
              f"{result['skipped_changed']}")
    if result["missing"]:
        print(f"  MISSING (hard-deleted since, unrecoverable here): "
              f"{result['missing']}")


def _note_to_dict(n) -> dict:
    """Serialize a store Note to a plain JSON-friendly dict (CLI --json output)."""
    return {
        "id": n.id,
        "project_id": n.project_id,
        "scope": n.scope,
        "kind": n.kind,
        "content": n.content,
        "tags": list(n.tags),
        "confidence": n.confidence,
        "source_hint": n.source_hint,
        "superseded_by": n.superseded_by,
        "created_at": n.created_at,
        "expansion": getattr(n, "expansion", False),
        "retrieval_origin": getattr(n, "retrieval_origin", "direct"),
        "resolved_from": getattr(n, "resolved_from", None),
        "history": getattr(n, "history", []),
        "linked_from": getattr(n, "linked_from", None),
        "relation": getattr(n, "relation", None),
        "relation_weight": getattr(n, "relation_weight", None),
    }


def cmd_recall(args: argparse.Namespace) -> None:
    """Recall curated memory notes from the CLI — the SAME engine path as the
    ``mcp__braincell__recall`` tool (server.recall_notes), so ranking/federation
    match exactly. Read-only. Emits a human table or, with --json, machine JSON.

    Brain + seed resolution mirrors `stats`/`reflect`: global mode uses the global
    brain (no seed); project mode resolves the path → ULID read-only (never mints)
    and exports BRAINCELL_PROJECT_ID for the resolved seed so scope='self'/'family'
    and federation resolve consistently. `scope='family'` needs global mode OR
    BRAINCELL_FEDERATE=on in project mode (else the engine raises, surfaced here).
    """
    from .server import recall_notes

    mode = args.mode if args.mode else resolve_mode()
    if mode == "global":
        db = get_global_db_path()
    else:
        root = Path(args.path).resolve()
        pid = resolve_project_id_readonly(root)
        if pid is None:
            print(f"No brain registered for {root} — run `braincell build` first.",
                  file=sys.stderr)
            raise SystemExit(1)
        db = get_db_path(pid)
        # Seed the env so _resolve_scope / build_federation_plan resolve 'self' and
        # 'family' to this project (mirrors the MCP server wrapper's export).
        os.environ["BRAINCELL_PROJECT_ID"] = pid

    if not db.exists():
        print(f"No brain at {db} — run `braincell build` first.", file=sys.stderr)
        raise SystemExit(1)

    store = SqliteStore(db)
    store.assert_schema_version()
    try:
        notes = asyncio.run(recall_notes(
            store, args.query, k=args.k, scope=args.scope,
            min_cosine=args.min_cosine, dedup=not args.no_dedup,
            include_superseded=args.include_superseded,
        ))
    except ValueError as exc:
        # Engine-level rejection (e.g. scope='family' in project mode without
        # BRAINCELL_FEDERATE=on, bad k/min_cosine) → clean stderr, no traceback.
        print(f"braincell recall: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    finally:
        store.close()

    if args.json:
        import json
        print(json.dumps([_note_to_dict(n) for n in notes], indent=2))
        return
    if not notes:
        print("(no matching notes)")
        return
    for n in notes:
        conf = f" conf={n.confidence:.2f}" if n.confidence is not None else ""
        also = " (also-see)" if getattr(n, "expansion", False) else ""
        origin = getattr(n, "retrieval_origin", "direct")
        if origin == "resolved":
            also += f" (replaced note {n.resolved_from})"
        elif n.superseded_by is not None:
            also += f" (superseded by {n.superseded_by})"
        print(f"[{n.kind}]{conf}{also} {n.content}")


def _hit_to_dict(h) -> dict:
    """Serialize a store Hit to a plain JSON-friendly dict (CLI --json output)."""
    return {
        "chunk_id": h.chunk_id,
        "doc_key": h.doc_key,
        "title": h.title,
        "snippet": h.snippet,
        "score": round(h.score, 6),
        "cosine": round(h.cosine, 4) if h.cosine is not None else None,
        "fts_matched": h.fts_matched,
        "source_path": h.source_path,
        "metadata": h.metadata,
    }


def cmd_search(args: argparse.Namespace) -> None:
    """Search ingested documents & transcripts from the CLI — the SAME engine path
    as the ``mcp__braincell__search`` tool (server.search_hits), so ranking and
    federation match exactly. Read-only.

    Distinct from `recall`: `recall` returns curated memory NOTES, `search` returns
    CHUNKS of ingested documents/transcripts. Brain + seed resolution mirrors
    `recall` exactly (see cmd_recall). `--rank` selects the ranking strategy
    (hybrid/semantic/keyword); `--mode` selects the project-vs-global brain, as it
    does in every other subcommand.
    """
    from .server import search_hits

    mode = args.mode if args.mode else resolve_mode()
    if mode == "global":
        db = get_global_db_path()
    else:
        root = Path(args.path).resolve()
        pid = resolve_project_id_readonly(root)
        if pid is None:
            print(f"No brain registered for {root} — run `braincell build` first.",
                  file=sys.stderr)
            raise SystemExit(1)
        db = get_db_path(pid)
        # Seed the env so _resolve_scope / build_federation_plan resolve 'self' and
        # 'family' to this project (mirrors the MCP server wrapper's export).
        os.environ["BRAINCELL_PROJECT_ID"] = pid

    if not db.exists():
        print(f"No brain at {db} — run `braincell build` first.", file=sys.stderr)
        raise SystemExit(1)

    store = SqliteStore(db)
    store.assert_schema_version()
    try:
        hits = asyncio.run(search_hits(
            store, args.query, project=args.project, k=args.k,
            rank=args.rank, scope=args.scope,
        ))
    except ValueError as exc:
        # Engine-level rejection (e.g. scope='family' in project mode without
        # BRAINCELL_FEDERATE=on, bad k/rank) → clean stderr, no traceback.
        print(f"braincell search: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    finally:
        store.close()

    if args.json:
        import json
        print(json.dumps([_hit_to_dict(h) for h in hits], indent=2))
        return
    if not hits:
        print("(no matching chunks)")
        return
    for h in hits:
        # `score` is rank-only in hybrid mode (RRF ~1/(60+rank)) — show `cosine`,
        # the interpretable relevance, and the FTS flag instead of leading with it.
        cos = f" cos={h.cosine:.3f}" if h.cosine is not None else ""
        fts = " +fts" if h.fts_matched else ""
        print(f"[{h.doc_key}]{cos}{fts} {h.title}")
        print(f"    {h.snippet}")


def _backup_source_path(mode: str, path: str) -> Path:
    """Resolve the source DB path for a backup given *mode* and *path* arg."""
    if mode == "global":
        return get_global_db_path()
    # project mode
    root = Path(path).resolve()
    project_id = get_project_id(root, create=False)  # raises ProjectIdentityMissing if unknown
    return get_db_path(project_id)


def _vacuum_into(src: Path, dest: Path) -> Path:
    """Copy *src* to *dest* via SQLite ``VACUUM INTO`` (read-consistent, safe while
    the brain is in use; the source is never modified). Returns *dest*."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    con = _sqlite3.connect(str(src))
    try:
        con.execute("VACUUM INTO ?", (str(dest),))
    finally:
        con.close()
    return dest


def _auto_backup(src: Path, tag: str) -> Optional[Path]:
    """Snapshot *src* before a destructive --apply. Returns the path, or None if the
    backup failed.

    Best-effort by design: a merge must not be blocked because a disk is full, but
    the caller MUST surface a None so the user knows the safety net is missing
    before the merge proceeds.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = src.parent / f"braincell-pre{tag}-{timestamp}.db"
    try:
        return _vacuum_into(src, dest)
    except Exception as exc:  # noqa: BLE001 — never block the merge on backup failure
        print(f"WARNING: pre-{tag} backup failed ({exc}).", file=sys.stderr)
        return None


def cmd_backup(args: argparse.Namespace) -> None:
    """Back up the current brain via SQLite ``VACUUM INTO``.

    Resolves the source from ``--mode`` (or ``BRAINCELL_MODE`` env / default
    ``project``).  In project mode the positional ``path`` arg locates the
    project via the path-registry.  In global mode ``path`` is ignored.

    The backup is a clean, read-consistent copy — safe to copy off-host while
    the brain is in use.  The source DB is never modified.
    """
    mode = args.mode if args.mode else resolve_mode()

    try:
        src = _backup_source_path(mode, args.path)
    except Exception as exc:
        print(f"ERROR: could not resolve source brain — {exc}", file=sys.stderr)
        raise SystemExit(1)

    if not src.exists():
        print(
            f"ERROR: source brain does not exist: {src}\n"
            f"  Run `braincell build` first (or `braincell build --mode global`).",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if args.out:
        dest = Path(args.out).resolve()
    else:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = src.parent / f"braincell-backup-{timestamp}.db"

    if args.verbose:
        print(f"Backing up {src} → {dest} …")

    _vacuum_into(src, dest)
    print(dest)


def cmd_family_add(args: argparse.Namespace) -> None:
    """Add member paths to a family (create if absent)."""
    from .project_registry import add_family_members

    paths = [str(Path(p).resolve()) for p in args.paths]
    families = add_family_members(args.name, paths)
    members = families.get(args.name, [])
    print(f"Family '{args.name}' ({len(members)} member(s)):")
    for m in members:
        print(f"  {m}")


def cmd_family_rm(args: argparse.Namespace) -> None:
    """Remove a family or specific members from it."""
    from .project_registry import remove_family

    paths: list[str] | None = (
        [str(Path(p).resolve()) for p in args.paths] if args.paths else None
    )
    changed = remove_family(args.name, paths)
    if changed:
        if paths is None:
            print(f"Removed family '{args.name}'.")
        else:
            print(f"Removed {len(paths)} member(s) from family '{args.name}'.")
    else:
        print(
            f"Nothing changed (family '{args.name}' not found or members not present)."
        )


def _resolve_pool_sources(args: argparse.Namespace) -> list[tuple[str, Path]]:
    """Resolve the set of (project_id, source_db) to pool from --all / --family / paths.

    Thin wrapper around pool.resolve_pool_sources that preserves the original
    stderr output and SystemExit(1) behaviour for CLI use.
    """
    from .pool import resolve_pool_sources
    try:
        sources, skipped = resolve_pool_sources(
            family=args.family,
            paths=args.paths,
            include_all=bool(args.all),
        )
    except KeyError:
        print(f"ERROR: family {args.family!r} not found.", file=sys.stderr)
        raise SystemExit(1)
    for note in skipped:
        print(f"  {note}", file=sys.stderr)
    return sources


def cmd_pool(args: argparse.Namespace) -> None:
    """Synchronise per-project brains into the global brain (no re-embed).

    Sources are selected via positional paths, ``--family NAME``, and/or ``--all``.
    Each source's documents/chunks/notes are merged into the global DB, reusing the
    already-computed embeddings. Convergent: re-running does not just add new rows,
    it also propagates supersessions, retractions and edited documents. ``--prune``
    additionally removes global rows that no longer exist at the source.
    """
    from .pool import PoolError, pool_into_global

    if not (args.all or args.family or args.paths):
        print(
            "ERROR: nothing selected to pool. Pass project paths, --family NAME, "
            "or --all.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    global_db = get_global_db_path()
    # Ensure the global brain exists + is schema/fingerprint-stamped before merge.
    gstore = SqliteStore(global_db)
    gstore.assert_schema_version()
    gstore.close()

    sources = _resolve_pool_sources(args)
    if not sources:
        print("No source projects resolved to pool. Nothing to do.", file=sys.stderr)
        raise SystemExit(1)

    try:
        stats = pool_into_global(sources, global_db, prune=getattr(args, "prune", False))
    except PoolError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

    tot_docs = sum(s.docs_copied for s in stats)
    tot_chunks = sum(s.chunks_copied for s in stats)
    tot_notes = sum(s.notes_copied for s in stats)
    tot_updated = sum(s.notes_updated + s.docs_updated for s in stats)
    tot_pruned = sum(s.notes_pruned + s.docs_pruned for s in stats)
    for s in stats:
        extra = ""
        if s.docs_updated or s.chunks_replaced:
            extra += f", ~{s.docs_updated} docs re-synced ({s.chunks_replaced} chunks replaced)"
        if s.notes_updated:
            extra += f", ~{s.notes_updated} notes re-synced"
        if s.notes_pruned or s.docs_pruned:
            extra += f", -{s.notes_pruned} notes/-{s.docs_pruned} docs pruned"
        if s.conflicts:
            extra += f", {s.conflicts} uid conflict(s) skipped"
        print(
            f"  {s.project_id}: +{s.docs_copied} docs ({s.docs_skipped} unchanged), "
            f"+{s.chunks_copied} chunks, +{s.notes_copied} notes{extra}"
        )
    print(
        f"Pooled {len(stats)} project(s) into {global_db}: "
        f"{tot_docs} docs, {tot_chunks} chunks, {tot_notes} notes copied; "
        f"{tot_updated} row(s) re-synced, {tot_pruned} pruned."
    )


def cmd_pool_create(args: argparse.Namespace) -> None:
    from .project_registry import create_pool

    create_pool(args.name)
    print(f"Created Pool '{args.name}'.")


def cmd_pool_add(args: argparse.Namespace) -> None:
    from .project_registry import add_to_pool

    members = add_to_pool(args.name, args.project_ids)
    print(f"Pool '{args.name}' now contains {len(members)} project(s).")


def cmd_pool_decouple(args: argparse.Namespace) -> None:
    from .project_registry import decouple_from_pool

    changed = decouple_from_pool(args.name, args.project_id)
    if changed:
        print(
            f"Decoupled project {args.project_id} from Pool '{args.name}'. "
            "Its memory and client connections are unchanged."
        )
    else:
        print(f"Project {args.project_id} is not a member of Pool '{args.name}'.")


def cmd_pool_delete(args: argparse.Namespace) -> None:
    from .project_registry import delete_pool

    if delete_pool(args.name):
        print(f"Deleted Pool '{args.name}'. Project memory and connections are unchanged.")
    else:
        print(f"Pool '{args.name}' does not exist.")


def cmd_pool_list(_args: argparse.Namespace) -> None:
    from .project_registry import load_pools

    pools = load_pools()
    if not pools:
        print("No Pools defined.")
        return
    for name, members in pools.items():
        print(f"[{name}] ({len(members)} project(s))")
        for project_id in members:
            print(f"  {project_id}")


def _pool_connected_project_id(path: str) -> str:
    project_id = resolve_project_id_readonly(Path(path).resolve())
    if project_id is None:
        raise SystemExit("This project has no BrainCell memory yet. Run `braincell build` first.")
    return project_id


def cmd_pool_search(args: argparse.Namespace) -> None:
    """Search one explicitly named Pool without copying or writing any member data."""
    from .embed import embed_query_async
    from .federate import federated_search, plan_for_pool

    connected_project_id = _pool_connected_project_id(args.path)
    plan = plan_for_pool(args.name, connected_project_id)
    qvec = asyncio.run(embed_query_async(args.query))
    hits = asyncio.run(federated_search(None, plan, qvec, args.query, args.k, args.rank))
    if args.json:
        import json
        print(json.dumps([_hit_to_dict(hit) for hit in hits], indent=2))
        return
    if not hits:
        print("(no matching Pool content)")
        return
    for hit in hits:
        print(f"[{hit.doc_key}] {hit.title}\n    {hit.snippet}")


def cmd_pool_recall(args: argparse.Namespace) -> None:
    """Recall from one explicitly named Pool without copying or writing member data."""
    from .embed import embed_query_async
    from .federate import federated_recall, plan_for_pool

    connected_project_id = _pool_connected_project_id(args.path)
    plan = plan_for_pool(args.name, connected_project_id)
    try:
        qvec = asyncio.run(embed_query_async(args.query)) if args.query.strip() else None
    except Exception:
        qvec = None
    notes = asyncio.run(federated_recall(None, plan, qvec, args.k, qtext=args.query))
    if args.json:
        import json
        print(json.dumps([_note_to_dict(note) for note in notes], indent=2))
        return
    if not notes:
        print("(no matching Pool memory)")
        return
    for note in notes:
        print(f"[{note.kind}] {note.content}")


def cmd_gui(args: argparse.Namespace) -> None:
    """Launch the native BrainCell GUI (or install its desktop launcher)."""
    if getattr(args, "install_launcher", False):
        from .gui import install_launcher
        root = Path(args.path).resolve()
        icon, desktop = install_launcher(root)
        print(f"Installed BrainCell Map launcher:\n  icon:    {icon}\n  desktop: {desktop}")
        print(f"The icon runs `braincell start {root}`.")
        print("Open your app menu and search for “BrainCell Map”.")
        return
    if getattr(args, "rotate_token", False):
        from .config import get_gui_token_path
        token_path = get_gui_token_path()
        token_path.unlink(missing_ok=True)
        print(f"GUI token rotated: {token_path} removed; a fresh token will be minted.")
    from .legacy_service import status as legacy_service_status
    if legacy_service_status()["active"]:
        raise SystemExit(
            "The retired braincell-map.service is active. Remove it first with: "
            "braincell legacy-service remove"
        )
    from .gui import run_gui
    run_gui(
        mode=getattr(args, "mode", None),
        port=args.port,
        allow_writes=args.allow_writes,
        path=args.path,
        restart_command="gui",
    )


def cmd_start(args: argparse.Namespace) -> None:
    """`braincell start` — the one-command launcher (NAMINGS "Start").

    ≡ `braincell gui <path> --allow-writes` plus what `gui` doesn't do: a
    single-instance probe (activate the running map instead of dying on "address
    already in use"; refuse the port if a DIFFERENT brain owns it), a
    pre-launch report (embedder first, brain state, MCP registration —
    print-and-continue, NEVER auto-register), and the first-run tour handoff
    (``tour=1`` via run_gui's url_extra_query).
    """
    from . import launch, native_shell

    mode = "project"
    if not native_shell.native_available():
        msg = (
            "BrainCell requires a graphical desktop session with "
            "PySide6/QtWebEngine available."
        )
        print(f"ERROR: {msg}", file=sys.stderr)
        native_shell.alert(msg)
        raise SystemExit(1)
    pre = launch.preflight(Path(args.path), mode=mode, port=args.port)

    if pre.action == "legacy_service":
        msg = "\n".join(pre.report_lines)
        print(f"ERROR: {msg}", file=sys.stderr)
        native_shell.alert(msg)
        raise SystemExit(1)
    if pre.action == "reuse":
        print(
            f"BrainCell GUI already running on port {args.port} "
            f"({pre.expected_db}) — activating its window."
        )
        if not (
            pre.activation_token
            and launch.activate_existing(args.port, pre.activation_token)
        ):
            msg = "The running BrainCell process did not activate its native window."
            print(f"ERROR: {msg}", file=sys.stderr)
            native_shell.alert(msg)
            raise SystemExit(1)
        return
    if pre.action == "conflict":
        conflict_msg = (
            f"Port {args.port} already serves a DIFFERENT brain:\n"
            f"  running: {pre.conflict_db}\n"
            f"  target:  {pre.expected_db or Path(args.path).resolve()}\n"
            f"Pick another port: braincell start --port <port>"
        )
        print(f"ERROR: {conflict_msg}", file=sys.stderr)
        # The desktop icon runs with Terminal=false, so every refusal also
        # needs a visible native notification.
        native_shell.alert(conflict_msg)
        raise SystemExit(1)

    for line in pre.report_lines:
        print(line)
    from .gui import run_gui
    try:
        run_gui(
            mode=mode,
            port=args.port,
            allow_writes=True,
            path=args.path,
            url_extra_query="tour=1" if pre.first_run else None,
            restart_command="start",
        )
    except Exception as exc:  # noqa: BLE001 — Terminal=false: NEVER die silently
        msg = f"BrainCell failed to start: {exc}"
        print(f"ERROR: {msg}", file=sys.stderr)
        native_shell.alert(msg)
        raise SystemExit(1) from exc


def main_map(argv: list[str] | None = None) -> None:
    """Compatibility entry point for the native project Memory Map."""
    p = argparse.ArgumentParser(
        prog="braincell-map",
        description="Open the BrainCell Memory Map for the current project.",
    )
    p.add_argument("--port", type=int, default=8765, help="TCP port (default: 8765).")
    ns = p.parse_args(argv)

    cmd_start(
        argparse.Namespace(
            path=".",
            port=ns.port,
        )
    )


def cmd_family_ls(_args: argparse.Namespace) -> None:
    """List all families and their members, with ULID resolution."""
    from .project_registry import load_families, load_path_registry

    families = load_families()
    if not families:
        print("No families defined.")
        return
    registry = load_path_registry()
    for fname, members in sorted(families.items()):
        print(f"[{fname}] ({len(members)} member(s))")
        for m in members:
            ulid = registry.get(m)
            suffix = ulid if ulid else "(unregistered)"
            print(f"  {m}  -> {suffix}")


def cmd_install(args: argparse.Namespace) -> None:
    """Connect BrainCell to one explicitly selected project and client."""
    from . import config
    from .install import get_client, resolve_portable_server_command
    from .project_target import ProjectTargetError, validate_project_target

    try:
        target = validate_project_target(
            args.path,
            acknowledge_home=args.acknowledge_home,
            acknowledge_non_git=args.acknowledge_non_git,
            allow_privileged=args.allow_privileged,
            require_git=args.client == "codex",
        )
    except ProjectTargetError as exc:
        raise SystemExit(f"braincell connect: {exc}") from exc
    for warning in target.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    root = target.path
    pid = get_project_id(root)
    env: dict[str, str] = {
        "BRAINCELL_DATA_NAMESPACE": config.DATA_NAMESPACE,
        "BRAINCELL_PROJECT_ID": pid,
        "BRAINCELL_STORE": "sqlite",
    }
    command, cmd_args = resolve_portable_server_command()
    try:
        client = get_client(args.client)
    except ValueError as exc:
        print(f"braincell install: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    try:
        client.mcp_add("braincell", command, cmd_args, env, scope=args.scope, cwd=str(root))
    except RuntimeError as exc:
        print(f"braincell install: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(
        f"✓ Connected BrainCell to {client.name} for project {pid}\n"
        f"  path: {root}\n  configuration: project-local\n  command: {command}"
    )
    if args.client == "codex":
        print("  Codex loads this connection only after this project is trusted.")

    restart = {"claude": "Claude Code", "codex": "Codex", "vscode": "VS Code"}[args.client]
    print("\nNext steps:")
    print(f"  1. Restart {restart} so it loads the new MCP server.")


def cmd_uninstall(args: argparse.Namespace) -> None:
    """Disconnect BrainCell from one project's selected client."""
    from .install import get_client
    from .project_target import ProjectTargetError, validate_project_target

    try:
        target = validate_project_target(
            args.path,
            acknowledge_home=args.acknowledge_home,
            acknowledge_non_git=args.acknowledge_non_git,
            allow_privileged=args.allow_privileged,
            require_git=args.client == "codex",
        )
    except ProjectTargetError as exc:
        raise SystemExit(f"braincell disconnect: {exc}") from exc

    try:
        client = get_client(args.client)
    except ValueError as exc:
        print(f"braincell uninstall: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    try:
        client.mcp_remove("braincell", scope=args.scope, cwd=str(target.path))
    except (RuntimeError, NotImplementedError) as exc:
        print(f"braincell disconnect: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"✓ Disconnected BrainCell from {client.name} for {target.path}")


def cmd_skills(args: argparse.Namespace) -> None:
    """Add or remove BrainCell skills inside one explicitly selected project."""
    from .install import install_project_skills, remove_project_skills
    from .project_target import ProjectTargetError, validate_project_target

    try:
        target = validate_project_target(
            args.path,
            acknowledge_home=args.acknowledge_home,
            acknowledge_non_git=args.acknowledge_non_git,
            allow_privileged=args.allow_privileged,
        )
    except ProjectTargetError as exc:
        raise SystemExit(f"braincell skills: {exc}") from exc

    operation = install_project_skills if args.skills_action == "add" else remove_project_skills
    try:
        results = operation(target.path, args.client)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f"braincell skills: {exc}") from exc
    for name, status, path in results:
        print(f"{status}: {name} ({path})")


def cmd_automatic_pool_recall(args: argparse.Namespace) -> None:
    """Manage or execute Project-local Automatic Pool recall."""
    from .automatic_pool_recall import (
        disable_automatic_pool_recall,
        enable_automatic_pool_recall,
        hook_main,
        status_automatic_pool_recall,
    )

    if args.automatic_recall_action == "run":
        hook_main(args.pool, args.project_id)
        return

    from .project_target import ProjectTargetError, validate_project_target

    try:
        target = validate_project_target(
            args.path,
            acknowledge_home=args.acknowledge_home,
            acknowledge_non_git=args.acknowledge_non_git,
            allow_privileged=args.allow_privileged,
        )
    except ProjectTargetError as exc:
        raise SystemExit(f"braincell automatic-pool-recall: {exc}") from exc
    try:
        if args.automatic_recall_action == "enable":
            result = enable_automatic_pool_recall(
                target.path, scope=args.scope, pool_name=args.pool
            )
        elif args.automatic_recall_action == "disable":
            result = disable_automatic_pool_recall(target.path, scope=args.scope)
        else:
            result = status_automatic_pool_recall(target.path, scope=args.scope)
    except (RuntimeError, ValueError, KeyError) as exc:
        raise SystemExit(f"braincell automatic-pool-recall: {exc}") from exc

    state = "Enabled" if result.get("enabled") else "Disabled"
    print(f"Automatic Pool recall: {state}")
    print(f"  Project: {target.path}")
    print(f"  Claude settings: {result['settings_path']}")
    if result.get("pool"):
        print(f"  Pool: {result['pool']}")
    if result.get("conflict"):
        print("  Conflict: a non-canonical hook was left unchanged")


def cmd_legacy_service(args: argparse.Namespace) -> None:
    """Inspect or remove the retired always-on GUI unit."""
    from . import legacy_service

    if args.legacy_service_cmd == "status":
        result = legacy_service.status()
        state = (
            "active" if result["active"]
            else "enabled" if result["enabled"]
            else "installed" if result["installed"]
            else "absent"
        )
        print(f"Legacy GUI service: {state}")
        print(f"  unit: {result['unit_path']}")
        return

    result = legacy_service.remove()
    if result["removed"]:
        print(f"✓ removed retired GUI service: {result['unit_path']}")
    elif result.get("installed"):
        print(f"✗ retired GUI service was not removed: {result['unit_path']}")
    else:
        print("• no retired GUI service unit found")
    if result["detail"]:
        print(f"  systemctl said: {result['detail']}", file=sys.stderr)


def cmd_legacy_migration(args: argparse.Namespace) -> None:
    """Preview or back up legacy shared data; never migrates or retires it."""
    import json
    from .legacy_migration import (
        backup_legacy_database,
        default_legacy_database,
        inspect_legacy_database,
        write_manifest,
    )

    source = Path(args.source).expanduser() if args.source else default_legacy_database()
    if args.legacy_migration_cmd == "preview":
        result = inspect_legacy_database(source)
        if args.manifest:
            write_manifest(result, Path(args.manifest))
        if args.json:
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        else:
            print(f"Legacy source: {result.source}")
            print(f"Readable: {'yes' if result.readable else 'no'}")
            print(f"Quick check: {result.quick_check}")
            print(f"Projects identified: {len(result.project_ids)}")
            for table, count in sorted(result.counts.items()):
                print(f"  {table}: {count}")
            print(f"  pooled rows: {sum(result.pooled_rows.values())}")
            print(f"  unclassified rows: {sum(result.ambiguous_rows.values())}")
            print(f"  note links: {result.link_rows} ({result.dangling_link_rows} dangling)")
            print(f"  audit operations: {result.operation_rows} / {result.operation_note_rows} note entries")
            for warning in result.warnings:
                print(f"WARNING: {warning}", file=sys.stderr)
        return

    if not args.destination:
        raise SystemExit("legacy-migration backup requires --destination")
    result = backup_legacy_database(source, Path(args.destination))
    if args.manifest:
        write_manifest(result, Path(args.manifest))
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(f"Verified backup: {result.destination}")
        print(f"SHA-256: {result.sha256}")
        print(f"Bytes: {result.bytes}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="braincell", description="Standalone BrainCell memory CLI.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("build", help="Ingest agent transcripts into the project brain.")
    pb.add_argument("path", nargs="?", default=".", help="Project path (default: cwd).")
    pb.add_argument("--skip-transcripts", action="store_true",
                    help="Skip transcript ingestion (register + embed-safety checks only).")
    pb.add_argument("--reembed", action="store_true",
                    help="Wipe embeddings first (required when changing embed provider/dim).")
    pb.add_argument("--no-mint", action="store_true",
                    help="Fail instead of registering/minting a new project ULID if unregistered.")
    pb.add_argument("-v", "--verbose", action="store_true")
    pb.add_argument(
        "--mode", choices=["project", "global"], default=None,
        help=(
            "Brain to build into (default: resolves BRAINCELL_MODE env or 'project'). "
            "Use 'global' to ingest into the shared global brain."
        ),
    )
    pb.set_defaults(func=cmd_build)

    ps = sub.add_parser("sync", help="Incremental refresh (new/changed transcripts).")
    ps.add_argument("path", nargs="?", default=".", help="Project path (default: cwd).")
    ps.add_argument("--skip-transcripts", action="store_true")
    ps.add_argument("-v", "--verbose", action="store_true")
    ps.add_argument(
        "--mode", choices=["project", "global"], default=None,
        help=(
            "Brain to sync into (default: resolves BRAINCELL_MODE env or 'project'). "
            "Use 'global' to sync into the shared global brain."
        ),
    )
    ps.set_defaults(func=cmd_sync)

    pr = sub.add_parser("register", help="Mint/confirm the project ULID (no ingest).")
    pr.add_argument("path", nargs="?", default=".", help="Project path (default: cwd).")
    pr.set_defaults(func=cmd_register)

    pv = sub.add_parser("serve", help="Run the FastMCP stdio server.")
    pv.set_defaults(func=cmd_serve)

    prn = sub.add_parser(
        "reembed-notes",
        help="Backfill embeddings for memory notes with NULL embedding.",
    )
    prn.add_argument("path", nargs="?", default=".", help="Project path (default: cwd).")
    prn.add_argument("-v", "--verbose", action="store_true")
    prn.set_defaults(func=cmd_reembed_notes)

    pc = sub.add_parser(
        "consolidate",
        help="Find near-duplicate notes and (opt-in) merge them.",
    )
    pc.add_argument("path", nargs="?", default=".", help="Project path (default: cwd).")
    pc.add_argument(
        "--threshold", type=float, default=0.9,
        help="Cosine similarity threshold for clustering (default: 0.9).",
    )
    pc.add_argument(
        "--apply", action="store_true",
        help="Apply merges (default: dry-run, no writes).",
    )
    pc.add_argument(
        "--llm", action="store_true",
        help=(
            "Use ollama to synthesise a merged note body (opt-in, best-effort). "
            "Falls back to deterministic keep-newest merge on any failure."
        ),
    )
    pc.add_argument("-v", "--verbose", action="store_true")
    pc.set_defaults(func=cmd_consolidate)

    prf = sub.add_parser(
        "reflect",
        help="Synthesize higher-level notes from clusters of related notes (LLM).",
    )
    prf.add_argument("path", nargs="?", default=".", help="Project path (default: cwd).")
    prf.add_argument(
        "--since", type=int, default=None,
        help="Only reflect clusters whose newest note is within N days.",
    )
    prf.add_argument(
        "--threshold", type=float, default=0.85,
        help="Cosine similarity threshold for clustering (default: 0.85).",
    )
    prf.add_argument(
        "--apply", action="store_true",
        help="Apply synthesis + supersede (default: dry-run, no writes).",
    )
    prf.add_argument(
        "--dry-run", action="store_true",
        help="Explicit dry-run (default behaviour; no writes).",
    )
    prf.add_argument(
        "--model", default=None,
        help="Ollama model for synthesis (default: $BRAINCELL_LLM_MODEL or qwen2.5:7b).",
    )
    prf.add_argument("-v", "--verbose", action="store_true")
    prf.set_defaults(func=cmd_reflect)

    pcx = sub.add_parser(
        "contradictions",
        help="Audit embedding-close active notes for contradictions (read-only, LLM-judged).",
    )
    pcx.add_argument("path", nargs="?", default=".", help="Project path (default: cwd).")
    pcx.add_argument(
        "--threshold", type=float, default=None,
        help="Cosine floor for candidate pairs (default: $BRAINCELL_CONFLICT_COS or 0.85).",
    )
    pcx.add_argument(
        "--limit", type=int, default=50,
        help="Max pairs to judge, highest cosine first (default: 50).",
    )
    pcx.add_argument(
        "--no-llm", action="store_true",
        help="List close pairs without LLM judgment (verdict: unjudged).",
    )
    pcx.add_argument(
        "--model", default=None,
        help="Ollama judge model (default: $BRAINCELL_LLM_MODEL or qwen2.5:7b).",
    )
    pcx.add_argument("-v", "--verbose", action="store_true")
    pcx.set_defaults(func=cmd_contradictions)

    prc = sub.add_parser(
        "recall",
        help="Recall curated memory notes (same engine as the MCP recall tool).",
    )
    prc.add_argument("query", help="Natural-language query text.")
    prc.add_argument(
        "--path", default=".",
        help="Project path for scope/seed resolution (default: cwd; project mode).",
    )
    prc.add_argument(
        "--scope", choices=["self", "family", "all"], default="self",
        help=(
            "self (default) = this project. family/all require global mode; "
            "family also works in project mode with BRAINCELL_FEDERATE=on (fan-out)."
        ),
    )
    prc.add_argument("-k", "--k", type=int, default=5,
                     help="Max notes to return (1-50, default 5).")
    prc.add_argument("--min-cosine", type=float, default=None,
                     help="Cosine floor [0,1] applied to vector-ranked hits.")
    prc.add_argument("--no-dedup", action="store_true",
                     help="Disable near-duplicate (cosine>0.95) suppression.")
    prc.add_argument(
        "--include-superseded", action="store_true",
        help=(
            "Return the historical set instead of current truth: superseded notes "
            "rank on their own merits and are NOT resolved to their replacements."
        ),
    )
    prc.add_argument("--json", action="store_true",
                     help="Emit JSON for machine consumption.")
    prc.add_argument(
        "--mode", choices=["project", "global"], default=None,
        help="Brain to recall from (default: resolves BRAINCELL_MODE env or 'project').",
    )
    prc.set_defaults(func=cmd_recall)

    pse = sub.add_parser(
        "search",
        help="Search ingested documents & transcripts (same engine as the MCP search tool).",
    )
    pse.add_argument("query", help="Natural-language search query.")
    pse.add_argument(
        "--path", default=".",
        help="Project path for scope/seed resolution (default: cwd; project mode).",
    )
    pse.add_argument(
        "--scope", choices=["self", "family", "all"], default="self",
        help=(
            "self (default) = this project. family/all require global mode; "
            "family also works in project mode with BRAINCELL_FEDERATE=on (fan-out)."
        ),
    )
    pse.add_argument("-k", "--k", type=int, default=10,
                     help="Max chunks to return (1-100, default 10).")
    pse.add_argument(
        "--rank", choices=["hybrid", "semantic", "keyword"], default="hybrid",
        help=(
            "Ranking strategy (default hybrid = RRF over vector + FTS5). NOT the "
            "brain selector — that is --mode."
        ),
    )
    pse.add_argument(
        "--project", default=None,
        help="Explicit project ULID to scope to (overrides --scope).",
    )
    pse.add_argument("--json", action="store_true",
                     help="Emit JSON for machine consumption.")
    pse.add_argument(
        "--mode", choices=["project", "global"], default=None,
        help="Brain to search (default: resolves BRAINCELL_MODE env or 'project').",
    )
    pse.set_defaults(func=cmd_search)

    pmem = sub.add_parser(
        "memory",
        help="Inspect and undo recorded merge operations (consolidate/reflect).",
    )
    memsub = pmem.add_subparsers(dest="memory_cmd", required=True)

    pmlog = memsub.add_parser("log", help="List recorded merge operations.")
    pmlog.add_argument("--path", default=".", help="Project path (default: cwd).")
    pmlog.add_argument("--limit", type=int, default=20,
                       help="Max operations to list (default 20).")
    pmlog.add_argument("--json", action="store_true", help="Emit JSON.")
    pmlog.set_defaults(func=cmd_memory_log)

    pmundo = memsub.add_parser(
        "undo", help="Reverse a merge operation by number (see `memory log`).",
    )
    pmundo.add_argument("op_id", type=int, help="Operation number from `memory log`.")
    pmundo.add_argument("--path", default=".", help="Project path (default: cwd).")
    pmundo.set_defaults(func=cmd_memory_undo)

    pi = sub.add_parser(
        "connect", aliases=["install"],
        help="Connect BrainCell to one project in Codex, Claude, or VS Code.",
    )
    pi.add_argument("path", nargs="?", default=".",
                    help="Project path to connect (default: cwd).")
    pi.add_argument("--client", choices=["claude", "codex", "vscode"], default="claude",
                    help="Target client (default: Claude).")
    pi.add_argument("--scope", choices=["local", "project"], default="local",
                    help="Claude scope: local private-project (default) or shareable project .mcp.json.")
    pi.add_argument("--acknowledge-home", action="store_true",
                    help="Confirm intentionally selecting the home directory as a project.")
    pi.add_argument("--acknowledge-non-git", action="store_true",
                    help="Confirm intentionally selecting a non-Git project.")
    pi.add_argument("--allow-privileged", action="store_true",
                    help="Confirm root/sudo ownership of selected project configuration and state.")
    pi.set_defaults(func=cmd_install)

    pu = sub.add_parser("disconnect", aliases=["uninstall"], help="Disconnect BrainCell from one project client.")
    pu.add_argument("path", nargs="?", default=".",
                    help="Project path (default: cwd).")
    pu.add_argument("--client", choices=["claude", "codex", "vscode"], default="claude",
                    help="Client to disconnect (default: Claude).")
    pu.add_argument("--scope", choices=["local", "project"], default="local",
                    help="Claude scope to remove (must match the connection scope).")
    pu.add_argument("--acknowledge-home", action="store_true")
    pu.add_argument("--acknowledge-non-git", action="store_true")
    pu.add_argument("--allow-privileged", action="store_true")
    pu.set_defaults(func=cmd_uninstall)

    pskills = sub.add_parser(
        "skills",
        help="Add or remove BrainCell skills inside one selected project.",
    )
    pskills.add_argument("skills_action", choices=["add", "remove"])
    pskills.add_argument("path", nargs="?", default=".",
                         help="Project path (default: cwd).")
    pskills.add_argument("--client", choices=["claude", "codex"], default="claude",
                         help="Project-local skill format (default: Claude).")
    pskills.add_argument("--acknowledge-home", action="store_true")
    pskills.add_argument("--acknowledge-non-git", action="store_true")
    pskills.add_argument("--allow-privileged", action="store_true")
    pskills.set_defaults(func=cmd_skills)

    pautorecall = sub.add_parser(
        "automatic-pool-recall",
        help="Manage optional Project-local Pool recall for Claude.",
    )
    autorecallsub = pautorecall.add_subparsers(
        dest="automatic_recall_action", required=True
    )
    action_help = {
        "enable": "Enable Automatic Pool recall for one Project and Pool.",
        "disable": "Disable it without changing Pool membership or Project memory.",
        "status": "Show the selected Project's current Automatic Pool recall state.",
    }
    for action, help_text in action_help.items():
        parser = autorecallsub.add_parser(action, help=help_text)
        parser.add_argument("path", nargs="?", default=".",
                            help="Project path (default: cwd).")
        parser.add_argument("--scope", choices=["local", "project"], default="local",
                            help="Private local settings or intentional shareable settings.")
        parser.add_argument("--acknowledge-home", action="store_true")
        parser.add_argument("--acknowledge-non-git", action="store_true")
        parser.add_argument("--allow-privileged", action="store_true")
        if action == "enable":
            parser.add_argument(
                "--pool",
                help="Pool name; optional only when this Project belongs to exactly one Pool.",
            )
        parser.set_defaults(func=cmd_automatic_pool_recall)
    prun = autorecallsub.add_parser(
        "run", help="Internal Claude hook entry point; not for interactive use."
    )
    prun.add_argument("--pool", required=True)
    prun.add_argument("--project-id", required=True)
    prun.set_defaults(func=cmd_automatic_pool_recall)

    pmig = sub.add_parser(
        "legacy-migration",
        help="Preview or back up legacy shared data; never applies or retires it.",
    )
    migsub = pmig.add_subparsers(dest="legacy_migration_cmd", required=True)
    for action, help_text in (
        ("preview", "Read-only inventory of the legacy database."),
        ("backup", "Create and verify a read-consistent SQLite backup."),
    ):
        parser = migsub.add_parser(action, help=help_text)
        parser.add_argument(
            "--source", default=None,
            help="Legacy SQLite path (default: the retired shared database path).",
        )
        parser.add_argument(
            "--manifest", default=None,
            help="Write the machine-readable JSON manifest to this path.",
        )
        parser.add_argument("--json", action="store_true", help="Print JSON output.")
        if action == "backup":
            parser.add_argument("--destination", required=True, help="New backup path; never overwritten.")
        parser.set_defaults(func=cmd_legacy_migration)

    pls = sub.add_parser(
        "legacy-service",
        help="Inspect or remove the retired braincell-map.service unit.",
    )
    pls.add_argument(
        "legacy_service_cmd",
        choices=["status", "remove"],
        help="status=inspect legacy residue; remove=disable, stop, and delete it.",
    )
    pls.set_defaults(func=cmd_legacy_service)

    pst = sub.add_parser(
        "stats",
        help="Show store size + a vector-search p95 benchmark (backend decision).",
    )
    pst.add_argument("path", nargs="?", default=".", help="Project path (default: cwd).")
    pst.add_argument(
        "--mode", choices=["project", "global"], default=None,
        help="Brain to inspect (default: resolves BRAINCELL_MODE env or 'project').",
    )
    pst.add_argument(
        "--iters", type=int, default=20,
        help="Number of vector-search probes for the p95 benchmark (default: 20).",
    )
    pst.set_defaults(func=cmd_stats)

    pbk = sub.add_parser(
        "backup",
        help="Backup the current brain via SQLite VACUUM INTO (read-only, safe while in use).",
    )
    pbk.add_argument(
        "path", nargs="?", default=".",
        help="Project path (default: cwd). Ignored in global mode.",
    )
    pbk.add_argument(
        "--mode", choices=["project", "global"], default=None,
        help=(
            "Brain to back up (default: resolves BRAINCELL_MODE env or 'project'). "
            "Use 'global' to back up the shared global brain."
        ),
    )
    pbk.add_argument(
        "--out",
        help=(
            "Explicit output file path. Default: braincell-backup-<UTC-timestamp>.db "
            "in the same directory as the source DB."
        ),
    )
    pbk.add_argument("-v", "--verbose", action="store_true")
    pbk.set_defaults(func=cmd_backup)

    ppool = sub.add_parser("pool", help="Manage explicit live-query Pools.")
    poolsub = ppool.add_subparsers(dest="pool_cmd", required=True)
    ppool_create = poolsub.add_parser("create", help="Create a named Pool with no copied memory.")
    ppool_create.add_argument("name")
    ppool_create.set_defaults(func=cmd_pool_create)
    ppool_add = poolsub.add_parser("add", help="Add project ULIDs to a Pool.")
    ppool_add.add_argument("name")
    ppool_add.add_argument("project_ids", nargs="+", help="Stable project ULIDs to add.")
    ppool_add.set_defaults(func=cmd_pool_add)
    ppool_decouple = poolsub.add_parser("decouple", help="Decouple one project from one Pool.")
    ppool_decouple.add_argument("name")
    ppool_decouple.add_argument("project_id")
    ppool_decouple.set_defaults(func=cmd_pool_decouple)
    ppool_delete = poolsub.add_parser("delete", help="Delete a Pool membership definition only.")
    ppool_delete.add_argument("name")
    ppool_delete.set_defaults(func=cmd_pool_delete)
    ppool_list = poolsub.add_parser("list", help="List named Pools and project ULIDs.")
    ppool_list.set_defaults(func=cmd_pool_list)
    ppool_search = poolsub.add_parser("search", help="Search one named Pool live and read-only.")
    ppool_search.add_argument("name")
    ppool_search.add_argument("query")
    ppool_search.add_argument("--path", default=".", help="Connected project path (default: cwd).")
    ppool_search.add_argument("-k", type=int, default=10)
    ppool_search.add_argument("--rank", choices=["hybrid", "semantic", "keyword"], default="hybrid")
    ppool_search.add_argument("--json", action="store_true")
    ppool_search.set_defaults(func=cmd_pool_search)
    ppool_recall = poolsub.add_parser("recall", help="Recall from one named Pool live and read-only.")
    ppool_recall.add_argument("name")
    ppool_recall.add_argument("query")
    ppool_recall.add_argument("--path", default=".", help="Connected project path (default: cwd).")
    ppool_recall.add_argument("-k", type=int, default=5)
    ppool_recall.add_argument("--json", action="store_true")
    ppool_recall.set_defaults(func=cmd_pool_recall)

    pstart = sub.add_parser(
        "start",
        help=(
            "Start the native Memory Map for a project folder (writable; "
            "activates an already-running map on the same port)."
        ),
    )
    pstart.add_argument(
        "path", nargs="?", default=".",
        help="Project folder (default: cwd).",
    )
    pstart.add_argument(
        "--port", type=int, default=8765,
        help="TCP port to listen on (default: 8765).",
    )
    pstart.add_argument(
        "--native", action="store_true", default=False,
        help=argparse.SUPPRESS,
    )
    pstart.set_defaults(func=cmd_start)

    pgui = sub.add_parser("gui", help="Launch the native BrainCell GUI.")
    pgui.add_argument(
        "path", nargs="?", default=".",
        help="Project path (default: cwd, project mode only).",
    )
    pgui.add_argument(
        "--port", type=int, default=8765,
        help="TCP port to listen on (default: 8765).",
    )
    pgui.add_argument(
        "--allow-writes", action="store_true", default=False,
        help="Enable write endpoints (forget notes, manage families). Default: read-only.",
    )
    pgui.add_argument(
        "--rotate-token", action="store_true", default=False,
        help=(
            "Delete the persisted GUI token so a fresh one is minted "
            "(invalidates existing renderer sessions)."
        ),
    )
    pgui.add_argument(
        "--install-launcher", action="store_true", default=False,
        help=(
            "Install the desktop icon + .desktop entry (Linux XDG) for the "
            "given project folder (the icon runs `braincell start <path>`) "
            "and exit."
        ),
    )
    pgui.add_argument("-v", "--verbose", action="store_true")
    pgui.set_defaults(func=cmd_gui)

    pf = sub.add_parser("family", help="Manage project families.")
    fsub = pf.add_subparsers(dest="family_cmd", required=True)

    pfa = fsub.add_parser("add", help="Add member paths to a family (create if absent).")
    pfa.add_argument("name", help="Family name.")
    pfa.add_argument("paths", nargs="+", help="Project paths to add.")
    pfa.set_defaults(func=cmd_family_add)

    pfr = fsub.add_parser("rm", help="Remove a family or specific members.")
    pfr.add_argument("name", help="Family name.")
    pfr.add_argument(
        "paths", nargs="*",
        help="Paths to remove (omit all paths to remove the entire family).",
    )
    pfr.set_defaults(func=cmd_family_rm)

    pfl = fsub.add_parser("ls", help="List families and their members (with ULID resolution).")
    pfl.set_defaults(func=cmd_family_ls)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
