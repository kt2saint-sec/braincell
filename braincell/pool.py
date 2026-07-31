# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
pool.py — merge existing per-project ``braincell.db`` files into the global brain.

Copies ``bc_documents`` + ``bc_chunks`` + ``memory_notes`` from each source
project DB into the global DB WITHOUT re-embedding (the stored float32 vectors
are reused verbatim). This is the cheap alternative to ``build --mode global``,
which re-ingests and re-embeds every repo.

Pooling CONVERGES, it does not merely copy. A re-pool re-synchronises what
already exists instead of skipping it, so the global brain keeps telling the same
story as its sources: a note superseded, retracted, re-tagged or re-worded in its
project brain after the first pool shows up that way globally, and a document whose
content changed has its chunks replaced rather than left frozen at first-copy state.

Guarantees:
  - **Convergent + idempotent.** Notes are keyed by their stable ``note_uid`` and
    upserted; documents are keyed by ``(project_id, doc_key)`` and re-synced when
    their ``content_hash`` changed. Re-running is safe and brings global up to date.
  - **Single owner per note.** Every note belongs to exactly one ``project_id``, so a
    global row is only ever updated from the source that owns it — there is no
    last-writer-wins across sources. A uid appearing under a different project is
    treated as corruption: skipped and logged, never merged.
  - **No vector-space mixing.** Each source's ``embed_fingerprint`` is
    compared to the global DB's; a mismatch raises ``PoolError`` and that source
    is not copied.
  - **Supersede chains preserved.** ``memory_notes.superseded_by`` is remapped to the
    global ids for EVERY synced note (not only newly inserted ones — that was the
    copy-once bug: the pointer set after the first pool never propagated).
  - **Deletions are opt-in.** Rows that vanished from a source are removed from the
    global brain only under ``prune=True``; by default pooling adds and updates.
  - **FTS stays consistent.** The external-content FTS5 indexes are rebuilt once
    at the end from the (now-merged) content tables — which also re-indexes updates.

The merge runs over a single sqlite3 connection to the global DB with each source
ATTACHed in turn; document/chunk/note id remapping is done in Python because the
INTEGER PRIMARY KEYs differ between the source and global DBs — that is precisely
why notes carry a ``note_uid`` that survives the copy.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .log import get as _get_log

log = _get_log("braincell.pool")


class PoolError(RuntimeError):
    """Raised when a source brain cannot be safely pooled (e.g. embedder mismatch)."""


@dataclass
class PoolStats:
    """Per-source sync counters.

    ``*_copied`` counts rows new to the global brain, ``*_updated`` counts rows that
    already existed and were re-synced from the source, ``*_skipped`` counts rows
    that were already identical.
    """
    project_id: str
    docs_copied: int = 0
    docs_skipped: int = 0
    docs_updated: int = 0
    chunks_copied: int = 0
    chunks_replaced: int = 0
    notes_copied: int = 0
    notes_skipped: int = 0
    notes_updated: int = 0
    links_copied: int = 0
    notes_pruned: int = 0
    docs_pruned: int = 0
    conflicts: int = 0


def _fingerprint(con: sqlite3.Connection, schema: str = "main") -> str | None:
    """Return the embedding-space fingerprint stored in *schema*, or None."""
    row = con.execute(
        f"SELECT fingerprint FROM {schema}.embed_fingerprint LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def _verify_fingerprint(con: sqlite3.Connection, project_id: str, global_fp: str | None) -> None:
    """Refuse to pool a source whose embedding space differs from the global DB."""
    src_fp = _fingerprint(con, "src")
    if global_fp is not None and src_fp is not None and src_fp != global_fp:
        raise PoolError(
            f"BrainCell pool refused for project {project_id}: source brain was built "
            f"with embedder {src_fp!r} but the global brain uses {global_fp!r}. Pooling "
            f"would mix vector spaces and corrupt search. Rebuild the source under the "
            f"global embedder (`braincell build --reembed`) before pooling."
        )


def _copy_chunks(con: sqlite3.Connection, src_doc_id: int, dst_doc_id: int) -> int:
    """Copy one source document's chunks (embeddings reused verbatim). Returns count."""
    chunk_rows = con.execute(
        "SELECT chunk_index, chunk_text, chunk_hash, embedding, run_id "
        "FROM src.bc_chunks WHERE document_id = ?",
        (src_doc_id,),
    ).fetchall()
    for c in chunk_rows:
        con.execute(
            "INSERT INTO bc_chunks (document_id, chunk_index, chunk_text, "
            "chunk_hash, embedding, run_id) VALUES (?, ?, ?, ?, ?, ?)",
            (dst_doc_id, *c),
        )
    return len(chunk_rows)


def _sync_documents_and_chunks(con: sqlite3.Connection, stats: PoolStats) -> None:
    """Bring the global copy of src.bc_documents (+ chunks) up to date.

    New documents are copied. Documents already present are compared by
    ``content_hash``: unchanged ones are skipped, changed ones have their row
    refreshed and their chunks REPLACED (delete + re-copy) — the copy-once
    behaviour left a stale document indexed forever under its original text.
    """
    doc_rows = con.execute(
        "SELECT id, project_id, doc_key, title, content_hash, content_type, "
        "commit_sha, run_id, created_at, updated_at, metadata FROM src.bc_documents"
    ).fetchall()
    for d in doc_rows:
        (src_doc_id, p_id, doc_key, title, content_hash, content_type,
         commit_sha, run_id, created_at, updated_at, metadata) = d
        existing = con.execute(
            "SELECT id, content_hash FROM bc_documents WHERE project_id = ? AND doc_key = ?",
            (p_id, doc_key),
        ).fetchone()
        if existing is not None:
            dst_doc_id, dst_hash = existing
            if dst_hash == content_hash:
                stats.docs_skipped += 1
                continue
            con.execute(
                "UPDATE bc_documents SET title = ?, content_hash = ?, content_type = ?, "
                "commit_sha = ?, run_id = ?, updated_at = ?, metadata = ?, "
                "pooled_from = ? WHERE id = ?",
                (title, content_hash, content_type, commit_sha, run_id,
                 updated_at, metadata, stats.project_id, dst_doc_id),
            )
            con.execute("DELETE FROM bc_chunks WHERE document_id = ?", (dst_doc_id,))
            stats.chunks_replaced += _copy_chunks(con, src_doc_id, dst_doc_id)
            stats.docs_updated += 1
            continue
        cur = con.execute(
            "INSERT INTO bc_documents (project_id, doc_key, title, content_hash, "
            "content_type, commit_sha, run_id, created_at, updated_at, metadata, "
            "pooled_from) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (p_id, doc_key, title, content_hash, content_type,
             commit_sha, run_id, created_at, updated_at, metadata, stats.project_id),
        )
        stats.docs_copied += 1
        stats.chunks_copied += _copy_chunks(con, src_doc_id, cur.lastrowid)


def _sync_notes(con: sqlite3.Connection, stats: PoolStats) -> None:
    """Upsert src.memory_notes into the global brain, keyed by stable ``note_uid``.

    Every note is either inserted or updated in place, then ``superseded_by`` is
    remapped for ALL of them in a second pass. That second pass is what makes the
    global brain converge: under the old copy-once scheme a note pooled while it
    was current kept ``superseded_by = NULL`` forever, so the global brain went on
    answering with a decision the project had already retracted.
    """
    note_rows = con.execute(
        "SELECT id, project_id, scope, kind, content, tags, confidence, source_hint, "
        "superseded_by, created_at, embedding, deleted_at, note_uid, revision, status "
        "FROM src.memory_notes ORDER BY id"
    ).fetchall()
    idmap: dict[int, int] = {}                    # src note id -> global note id
    supersessions: list[tuple[int, int | None]] = []  # (global id, src superseded_by)
    for r in note_rows:
        (src_id, p_id, scope, kind, content, tags, conf, src_hint,
         superseded_by, created_at, embedding, deleted_at, note_uid, revision,
         status) = r
        # Status rides in the upsert tuple — a status-only change (e.g. a
        # survivor flipped back to 'active' when its replacement was purged at
        # source, which bumps no revision) must still converge on re-pool.
        incoming = (scope, kind, content, tags, conf, src_hint, created_at,
                    embedding, deleted_at, revision, status)
        existing = con.execute(
            "SELECT id, project_id, scope, kind, content, tags, confidence, source_hint, "
            "created_at, embedding, deleted_at, revision, status FROM memory_notes "
            "WHERE note_uid = ?",
            (note_uid,),
        ).fetchone()
        if existing is not None and existing[1] != p_id:
            # One note, one owning project — never merge a uid across owners.
            # The ORDINARY cause is not note corruption: a project directory that
            # was copied, or moved and re-registered under a new ULID, presents
            # its old note uids under a new project_id.
            log.warning(
                "pool: note_uid %s is owned by project %s in the global brain, but "
                "source %s presents it — skipped, never merged (one note, one owning "
                "project). This usually means the source directory is a copy of, or a "
                "moved-and-re-registered version of, project %s. If the directory "
                "moved, re-register it under its original project id; if it is a copy "
                "meant to live as a separate project, its notes need fresh uids "
                "before it can pool.",
                note_uid, existing[1], p_id, existing[1],
            )
            stats.conflicts += 1
            continue
        if existing is not None:
            dst_id = existing[0]
            # Byte-identical rows are left alone (and counted as skipped), but they
            # still join the id map: their superseded_by pointer is re-derived in the
            # second pass, which is exactly the case the copy-once version missed.
            if tuple(existing[2:]) == incoming:
                stats.notes_skipped += 1
            else:
                con.execute(
                    "UPDATE memory_notes SET scope = ?, kind = ?, content = ?, tags = ?, "
                    "confidence = ?, source_hint = ?, created_at = ?, embedding = ?, "
                    "deleted_at = ?, revision = ?, status = ?, pooled_from = ? "
                    "WHERE id = ?",
                    (*incoming, stats.project_id, dst_id),
                )
                stats.notes_updated += 1
            idmap[src_id] = dst_id
            supersessions.append((dst_id, superseded_by))
            continue
        cur = con.execute(
            "INSERT INTO memory_notes (project_id, scope, kind, content, tags, "
            "confidence, source_hint, superseded_by, created_at, embedding, deleted_at, "
            "note_uid, revision, status, pooled_from) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
            (p_id, scope, kind, content, tags, conf, src_hint, created_at, embedding,
             deleted_at, note_uid, revision, status, stats.project_id),
        )
        new_id = cur.lastrowid
        idmap[src_id] = new_id
        supersessions.append((new_id, superseded_by))
        stats.notes_copied += 1
    # Second pass: now that every source id has a global id, point each note at its
    # replacement — including notes that were pooled BEFORE they were superseded,
    # and clearing the pointer again if the source cleared it.
    for dst_id, src_sup in supersessions:
        con.execute(
            "UPDATE memory_notes SET superseded_by = ? WHERE id = ?",
            (idmap.get(src_sup) if src_sup is not None else None, dst_id),
        )
    # Copy the note-link graph, remapping src/dst to the new global ids.
    _copy_note_links(con, idmap, stats)


def _prune_removed(con: sqlite3.Connection, stats: PoolStats) -> None:
    """Delete global rows for this project that no longer exist in its source brain.

    Opt-in (``pool --prune``): only rows owned by the project being pooled are
    considered, so pruning one source can never touch another project's memory.
    Inbound supersession pointers are cleared first — with foreign keys enforced a
    referenced note cannot be deleted — while ``bc_note_links`` edges and
    ``bc_chunks`` go automatically via ON DELETE CASCADE.

    Ownership scoping alone is NOT enough, and getting this wrong destroys data: a note
    written directly into the global brain (a global-mode ``remember``) carries the same
    project_id as that project's pooled notes but has no counterpart in the project's
    own brain. Pruning on ownership alone therefore deletes notes that were born here
    and never came from anywhere. So prune is scoped by PROVENANCE: only rows stamped
    ``pooled_from`` (set by this module on every pooled insert/update) are candidates.

    Rows pooled before ``pooled_from`` existed have it NULL and are skipped — the
    fail-safe direction (a stale copy lingers) rather than the destructive one. One
    re-pool stamps them and prune starts collecting them.
    """
    stale_notes = [r[0] for r in con.execute(
        "SELECT id FROM memory_notes WHERE project_id = ? AND pooled_from IS NOT NULL "
        "AND note_uid IS NOT NULL "
        "AND note_uid NOT IN (SELECT note_uid FROM src.memory_notes "
        "                     WHERE note_uid IS NOT NULL)",
        (stats.project_id,),
    ).fetchall()]
    for note_id in stale_notes:
        con.execute("DELETE FROM memory_fts WHERE rowid = ?", (note_id,))
        con.execute(
            "UPDATE memory_notes SET superseded_by = NULL WHERE superseded_by = ?",
            (note_id,),
        )
        con.execute("DELETE FROM memory_notes WHERE id = ?", (note_id,))
    stats.notes_pruned = len(stale_notes)

    cur = con.execute(
        "DELETE FROM bc_documents WHERE project_id = ? AND pooled_from IS NOT NULL "
        "AND doc_key NOT IN "
        "(SELECT doc_key FROM src.bc_documents WHERE project_id = ?)",
        (stats.project_id, stats.project_id),
    )
    stats.docs_pruned = cur.rowcount or 0

    # Make the fail-safe VISIBLE: rows with no source counterpart that prune left
    # alone because pooled_from is NULL. Silent inertness on a legacy global brain
    # would look like prune working while stale copies live forever.
    skipped_notes = con.execute(
        "SELECT COUNT(*) FROM memory_notes WHERE project_id = ? AND pooled_from IS NULL "
        "AND note_uid IS NOT NULL "
        "AND note_uid NOT IN (SELECT note_uid FROM src.memory_notes "
        "                     WHERE note_uid IS NOT NULL)",
        (stats.project_id,),
    ).fetchone()[0]
    skipped_docs = con.execute(
        "SELECT COUNT(*) FROM bc_documents WHERE project_id = ? AND pooled_from IS NULL "
        "AND doc_key NOT IN (SELECT doc_key FROM src.bc_documents WHERE project_id = ?)",
        (stats.project_id, stats.project_id),
    ).fetchone()[0]
    if skipped_notes or skipped_docs:
        log.warning(
            "pool --prune: left %d note(s) and %d document(s) of project %s untouched — "
            "no counterpart in the source brain, but no pooled_from provenance either "
            "(born in the global brain, or pooled before provenance stamping). If they "
            "are legacy pooled copies, a plain `braincell pool` re-run stamps them and "
            "the next --prune collects them.",
            skipped_notes, skipped_docs, stats.project_id,
        )


def _copy_note_links(con: sqlite3.Connection, idmap: dict[int, int], stats: PoolStats) -> None:
    """Copy src.bc_note_links, remapping src/dst ids via *idmap* (idempotent).

    Skips silently when the source has no bc_note_links table (pre-link-graph schema) or when a
    link references a note that was not copied. INSERT OR IGNORE against the
    UNIQUE(src,dst,kind) constraint makes re-pooling a no-op for existing links.
    """
    try:
        rows = con.execute(
            "SELECT src_id, dst_id, kind, weight, created_at FROM src.bc_note_links"
        ).fetchall()
    except sqlite3.OperationalError:
        return  # source brain predates the note-link graph
    for src_id, dst_id, kind, weight, created_at in rows:
        g_src = idmap.get(src_id)
        g_dst = idmap.get(dst_id)
        if g_src is None or g_dst is None:
            continue
        cur = con.execute(
            "INSERT OR IGNORE INTO bc_note_links "
            "(src_id, dst_id, kind, weight, created_at) VALUES (?, ?, ?, ?, ?)",
            (g_src, g_dst, kind, weight, created_at),
        )
        if cur.rowcount > 0:
            stats.links_copied += 1


def _rebuild_fts(con: sqlite3.Connection) -> None:
    """Rebuild the external-content FTS5 indexes from the merged content tables."""
    for tbl in ("bc_chunks_fts", "memory_fts"):
        try:
            con.execute(f"INSERT INTO {tbl}({tbl}) VALUES('rebuild')")
        except sqlite3.OperationalError as exc:
            log.warning("FTS rebuild skipped for %s (FTS5 unavailable?): %s", tbl, exc)


def resolve_pool_sources(
    *,
    family: str | None = None,
    paths: list[str] | None = None,
    include_all: bool = False,
) -> tuple[list[tuple[str, Path]], list[str]]:
    """Return (sources, skipped). Pure — no argparse, no printing, no SystemExit.

    sources: deduped [(project_id, per_project_db_path)] with a built brain,
             excluding the global DB.
    skipped: human-readable notes ('skip (unregistered member): ...',
             'skip (unregistered path): ...', 'skip (no brain built): ...').

    Raises:
        KeyError: When a named family does not exist.
    """
    from .config import get_db_path, get_global_db_path
    from .project_registry import load_families, load_path_registry, normalize_path

    registry = load_path_registry()
    global_db = get_global_db_path().resolve()
    chosen: dict[str, None] = {}  # ordered set of project_ids
    skipped: list[str] = []

    if include_all:
        for pid in registry.values():
            chosen.setdefault(pid, None)

    if family is not None:
        families = load_families()
        members = families.get(family)
        if members is None:
            raise KeyError(family)
        for m in members:
            pid = registry.get(normalize_path(m))
            if pid:
                chosen.setdefault(pid, None)
            else:
                skipped.append(f"skip (unregistered member): {m}")

    for p in paths or []:
        root = Path(p).resolve()
        pid = registry.get(normalize_path(str(root)))
        if pid:
            chosen.setdefault(pid, None)
        else:
            skipped.append(f"skip (unregistered path): {root}")

    sources: list[tuple[str, Path]] = []
    for pid in chosen:
        db = get_db_path(pid)
        if not db.exists():
            skipped.append(f"skip (no brain built): {pid} ({db})")
            continue
        if db.resolve() == global_db:
            continue  # never pool the global DB into itself
        sources.append((pid, db))
    return sources, skipped


def _migrate_source(src_db: Path) -> None:
    """Bring a source brain up to the current schema before it is ATTACHed.

    Pooling reads the source's ``note_uid`` column directly over ATTACH, which
    bypasses ``SqliteStore`` entirely — so a source that has not been opened since
    the v4 upgrade would have no uids to key the merge on. Opening it once here
    runs the same forward migration every other entry point relies on.
    """
    from .store import SqliteStore  # lazy: avoids a module-level store<->pool edge

    try:
        store = SqliteStore(src_db)
        store.assert_schema_version()
        store.close()
    except RuntimeError as exc:
        # A source built under a DIFFERENT embedder cannot be opened by this process
        # at all (the fingerprint gate fires first). That is not this function's call
        # to make: leave it, and let _verify_fingerprint raise the PoolError that
        # explains the mismatch and names the fix.
        log.debug("pool: source %s not migrated (%s) — deferring to fingerprint check", src_db, exc)


def pool_into_global(
    sources: list[tuple[str, Path]],
    global_db: Path,
    *,
    prune: bool = False,
) -> list[PoolStats]:
    """Synchronise each source project's brain into the global DB.

    Args:
        sources:   List of ``(project_id, src_db_path)`` to merge. The global DB
                   must already be schema-initialised (caller runs
                   ``SqliteStore(global_db).assert_schema_version()`` first).
        global_db: Path to the global ``braincell.db``.
        prune:     When True, also DELETE global rows owned by a pooled project that
                   no longer exist in that project's brain (a true mirror). Default
                   False: pooling only adds and updates, so the global brain keeps
                   anything hard-deleted at the source.

    Returns:
        One ``PoolStats`` per source, in input order.

    Raises:
        PoolError: If a source's embedding fingerprint differs from the global DB.
    """
    con = sqlite3.connect(str(global_db))
    con.isolation_level = None  # autocommit; BEGIN/COMMIT managed explicitly
    try:
        con.execute("PRAGMA busy_timeout=30000")
        # Enforce foreign keys so bc_note_links / bc_chunks cascade on prune.
        con.execute("PRAGMA foreign_keys=ON")
        global_fp = _fingerprint(con)
        all_stats: list[PoolStats] = []
        for project_id, src_db in sources:
            _migrate_source(src_db)
            con.execute("ATTACH DATABASE ? AS src", (str(src_db),))
            try:
                _verify_fingerprint(con, project_id, global_fp)
                con.execute("BEGIN")
                stats = PoolStats(project_id=project_id)
                _sync_documents_and_chunks(con, stats)
                _sync_notes(con, stats)
                if prune:
                    _prune_removed(con, stats)
                con.execute("COMMIT")
                all_stats.append(stats)
            except Exception:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
            finally:
                con.execute("DETACH DATABASE src")
        # Rebuild FTS once, after every source is merged.
        con.execute("BEGIN")
        _rebuild_fts(con)
        con.execute("COMMIT")
        return all_stats
    finally:
        con.close()
