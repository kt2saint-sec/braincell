# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
schema.py — Raw DDL for the BrainCell tables.

Single database: one per-project `braincell.db` holds everything —
`bc_documents`, `bc_chunks`, `bc_chunks_fts`, `memory_notes`, `memory_fts`,
`schema_version`, and `embed_fingerprint`. One file gives one connection,
cross-table transactions, atomic backup, and a single whole-store version gate.

Embedding stored as a raw float32 BLOB column on `bc_chunks` (NOT a vec0 virtual
table) — no loadable extension needed; NumPy brute-force cosine for V0.
`sqlite-vec` (vec0) becomes a later optimisation behind the same Store interface.

FTS5 is built into Python's stdlib sqlite3 on this system; we verify it at
store open-time and fall back gracefully if absent.

These DDL strings are applied (idempotent CREATE IF NOT EXISTS + FTS5 gate) by
SqliteStore.assert_schema_version on first open.
"""

# ── bc_* document/chunk tables (braincell-owned) ──────────────────────────────

DOCUMENTS_DDL = """
CREATE TABLE IF NOT EXISTS bc_documents (
    id          INTEGER PRIMARY KEY,
    project_id  TEXT    NOT NULL,
    doc_key     TEXT    NOT NULL,
    title       TEXT    NOT NULL DEFAULT '',
    content_hash BLOB,
    content_type TEXT   NOT NULL DEFAULT 'cell',
    commit_sha  TEXT,
    run_id      TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT,
    metadata    TEXT,
    pooled_from TEXT,   -- v4: source project_id when this row arrived via `pool`; NULL = born here
    UNIQUE(project_id, doc_key)
);
"""

DOCUMENTS_IDX_DDL = """
CREATE INDEX IF NOT EXISTS bc_documents_project_idx ON bc_documents(project_id);
"""

CHUNKS_DDL = """
CREATE TABLE IF NOT EXISTS bc_chunks (
    id            INTEGER PRIMARY KEY,
    document_id   INTEGER NOT NULL REFERENCES bc_documents(id) ON DELETE CASCADE,
    chunk_index   INTEGER NOT NULL DEFAULT 0,
    chunk_text    TEXT    NOT NULL,
    chunk_hash    BLOB,
    embedding     BLOB,        -- float32 array, DIM per embed_spec (1024 ollama / 1536 openai), NumPy .tobytes()
    run_id        TEXT,
    UNIQUE(document_id, chunk_index)
);
"""

CHUNKS_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS bc_chunks_fts
    USING fts5(chunk_text, content='bc_chunks', content_rowid='id');
"""

# ── Graph note-links (also-see recall) ────────────────────────────────────────
# A small directed graph over memory_notes: auto-populated at write time (cosine
# to recent notes above a threshold) and optionally traversed at read time
# (BRAINCELL_LINK_EXPAND). Additive-only; absent behaviour is byte-identical.

# ON DELETE CASCADE (v4): with `PRAGMA foreign_keys=ON` a hard-deleted note takes
# its edges with it, so the graph can no longer accumulate orphan rows. SQLite
# cannot ALTER a constraint onto an existing table — v3 stores are rebuilt
# (create → copy → drop → rename) by the v3→v4 migration in store.py.
NOTE_LINKS_DDL = """
CREATE TABLE IF NOT EXISTS bc_note_links (
    src_id      INTEGER NOT NULL REFERENCES memory_notes(id) ON DELETE CASCADE,
    dst_id      INTEGER NOT NULL REFERENCES memory_notes(id) ON DELETE CASCADE,
    kind        TEXT    NOT NULL DEFAULT 'related'
                CHECK (kind IN ('related','causes','refines')),
    weight      REAL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(src_id, dst_id, kind)
);
"""

NOTE_LINKS_IDX_DDL = """
CREATE INDEX IF NOT EXISTS bc_note_links_src_idx ON bc_note_links(src_id);
"""

# ── v5: merge operation log (undo for consolidate/reflect) ────────────────────
# `consolidate --apply` and `reflect --apply` mutate many notes at once and were
# irreversible without hand-written SQL: both are SOFT (tombstone / supersede), so
# the rows survive, but nothing recorded WHICH rows an operation touched or what
# their prior state was. These two tables record that, so `braincell memory undo`
# can restore the exact pre-merge values instead of guessing.
#
# `bc_operation_notes.note_id` deliberately carries NO foreign key: a CASCADE would
# erase the audit trail at the moment a note is hard-deleted, which is precisely
# when you want the record. Undo tolerates a missing note and reports it.
OPERATIONS_DDL = """
CREATE TABLE IF NOT EXISTS bc_operations (
    id          INTEGER PRIMARY KEY,
    kind        TEXT    NOT NULL CHECK (kind IN ('consolidate','reflect')),
    project_id  TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    note_count  INTEGER NOT NULL DEFAULT 0,
    backup_path TEXT,
    undone_at   TEXT
);
"""

# action: what this operation DID to the note, which tells undo how to reverse it.
#   'tombstoned' → consolidate soft-deleted a non-representative note
#   'superseded' → reflect pointed a source note at its synthesis (+ tombstoned it)
#   'created'    → reflect's synthesized note; undo tombstones it, or undoing a
#                  reflect would resurrect the sources AND keep their replacement.
OPERATION_NOTES_DDL = """
CREATE TABLE IF NOT EXISTS bc_operation_notes (
    op_id              INTEGER NOT NULL REFERENCES bc_operations(id) ON DELETE CASCADE,
    note_id            INTEGER NOT NULL,
    action             TEXT    NOT NULL
                       CHECK (action IN ('tombstoned','superseded','created')),
    prev_deleted_at    TEXT,
    prev_superseded_by INTEGER,
    UNIQUE(op_id, note_id, action)
);
"""

OPERATION_NOTES_IDX_DDL = """
CREATE INDEX IF NOT EXISTS bc_operation_notes_op_idx ON bc_operation_notes(op_id);
"""

# ── whole-store version + embedding-space fingerprint ─────────────────────────

MEMORY_SCHEMA_VERSION: int = 6

SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL,
    applied_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

EMBED_FINGERPRINT_DDL = """
CREATE TABLE IF NOT EXISTS embed_fingerprint (
    fingerprint TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

# `status` (v6) is the SINGLE liveness authority: 'active' | 'superseded' |
# 'tombstoned'. It REPLACES the old two-column predicate (`deleted_at IS NULL AND
# superseded_by IS NULL`) — `deleted_at` (when) and `superseded_by` (what replaced
# it) are demoted to pure provenance, kept in sync by every write path but never
# consulted for liveness. One authority means a future lifecycle state is a new
# enum value + `_live_note_predicate()` (store.py), not a schema-wide predicate
# hunt. Declared last so a v5→v6-migrated table (ALTER ADD COLUMN appends) and a
# fresh one have identical column order — same convention as the v4 columns.
#
# `note_uid` (v4) is the note's STABLE identity: a ULID minted once at write time
# that survives copying into the global brain, so pooling can upsert instead of
# copy-once (the INTEGER PRIMARY KEY is local to each database file). It is
# declared nullable + covered by a separate UNIQUE INDEX rather than
# `TEXT NOT NULL UNIQUE`, because SQLite cannot ADD a UNIQUE column to an existing
# table — this way a migrated v3 store and a fresh one end up with identical
# schema. Every write path supplies one; a NULL uid is a bug (regression-tested).
MEMORY_NOTES_DDL = """
CREATE TABLE IF NOT EXISTS memory_notes (
    id              INTEGER PRIMARY KEY,
    project_id      TEXT    NOT NULL,
    scope           TEXT    NOT NULL DEFAULT 'project',
    kind            TEXT    NOT NULL CHECK (kind IN ('decision','bug_lesson','note','observation')),
    content         TEXT    NOT NULL,
    tags            TEXT,                   -- JSON array
    confidence      REAL,
    source_hint     TEXT,
    superseded_by   INTEGER REFERENCES memory_notes(id),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    embedding       BLOB,
    deleted_at      TEXT,
    note_uid        TEXT,                   -- v4: stable cross-database identity (ULID)
    revision        INTEGER NOT NULL DEFAULT 1,  -- v4: bumped by every supersede (optimistic concurrency)
    pooled_from     TEXT,                   -- v4: source project_id when this row arrived via `pool`; NULL = born here
    status          TEXT    NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','superseded','tombstoned'))  -- v6: liveness authority
);
"""

MEMORY_NOTES_IDX_DDL = """
CREATE INDEX IF NOT EXISTS memory_notes_project_idx ON memory_notes(project_id);
"""

# Applied by SqliteStore.assert_schema_version AFTER the migration ladder, never
# from BRAINCELL_INIT_STMTS: on a v3 store the `note_uid` column does not exist
# until the v3→v4 ALTER has run, and indexing a missing column is an error.
MEMORY_NOTES_UID_IDX_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS memory_notes_uid_idx ON memory_notes(note_uid);
"""

MEMORY_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts
    USING fts5(content, content='memory_notes', content_rowid='id');
"""

# ── Ordered bootstrap for the single braincell.db ─────────────────────────────
# Applied by SqliteStore.assert_schema_version on first open (idempotent
# CREATE IF NOT EXISTS + FTS5 availability gate). Order matters: content tables
# before their external-content FTS5 virtual tables.

BRAINCELL_INIT_STMTS: list[str] = [
    SCHEMA_VERSION_DDL,
    EMBED_FINGERPRINT_DDL,
    DOCUMENTS_DDL,
    DOCUMENTS_IDX_DDL,
    CHUNKS_DDL,
    CHUNKS_FTS_DDL,
    MEMORY_NOTES_DDL,
    MEMORY_NOTES_IDX_DDL,
    MEMORY_FTS_DDL,
    NOTE_LINKS_DDL,
    NOTE_LINKS_IDX_DDL,
    OPERATIONS_DDL,
    OPERATION_NOTES_DDL,
    OPERATION_NOTES_IDX_DDL,
]
