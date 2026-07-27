# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""Read-only inventory and verified backup helpers for legacy shared data.

This module is deliberately outside normal runtime startup.  It does not open a
project store, alter configuration, migrate rows, or retire the legacy database.
It only inventories a legacy SQLite database and can create a verified SQLite
backup for a later, explicitly approved migration.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import get_global_db_path


@dataclass(frozen=True)
class LegacyInventory:
    source: str
    readable: bool
    schema_versions: list[int] = field(default_factory=list)
    fingerprints: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    project_ids: list[str] = field(default_factory=list)
    pooled_rows: dict[str, int] = field(default_factory=dict)
    ambiguous_rows: dict[str, int] = field(default_factory=dict)
    link_rows: int = 0
    dangling_link_rows: int = 0
    operation_rows: int = 0
    operation_note_rows: int = 0
    missing_operation_notes: int = 0
    quick_check: str = "not-run"
    foreign_key_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LegacyBackup:
    source: str
    destination: str
    sha256: str
    bytes: int
    verified: bool
    inventory: LegacyInventory

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MigrationStats:
    project_id: str
    notes_migrated: int = 0
    notes_skipped: int = 0
    documents_migrated: int = 0
    documents_skipped: int = 0
    chunks_migrated: int = 0
    links_migrated: int = 0
    conflicts: int = 0
    preserved_global_native: int = 0
    skipped_legacy_unclassified: int = 0
    audit_rows_preserved_in_source: int = 0
    foreign_key_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _tables(con: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
        )
    }


def _count(con: sqlite3.Connection, table: str) -> int:
    try:
        return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return 0


def inspect_legacy_database(source: Path) -> LegacyInventory:
    """Inventory ``source`` read-only without opening any destination store."""
    source = Path(source).expanduser().resolve()
    base = dict(
        source=str(source), readable=False, schema_versions=[], fingerprints=[],
        counts={}, project_ids=[], pooled_rows={}, ambiguous_rows={}, link_rows=0,
        dangling_link_rows=0, operation_rows=0, operation_note_rows=0,
        missing_operation_notes=0, quick_check="not-run", foreign_key_errors=[],
        warnings=[],
    )
    if not source.exists():
        base["warnings"] = ["legacy database does not exist"]
        return LegacyInventory(**base)

    uri = f"file:{source.as_posix()}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        base["warnings"] = [f"cannot open read-only: {exc}"]
        return LegacyInventory(**base)

    try:
        base["readable"] = True
        tables = _tables(con)
        for table in (
            "bc_documents", "bc_chunks", "memory_notes", "bc_note_links",
            "bc_chunks_fts", "memory_fts", "bc_operations", "bc_operation_notes",
        ):
            if table in tables:
                base["counts"][table] = _count(con, table)

        if "schema_version" in tables:
            base["schema_versions"] = [
                int(row[0]) for row in con.execute("SELECT version FROM schema_version")
                if row[0] is not None
            ]
        if "embed_fingerprint" in tables:
            base["fingerprints"] = [
                str(row[0]) for row in con.execute("SELECT fingerprint FROM embed_fingerprint")
                if row[0] is not None
            ]

        project_ids: set[str] = set()
        for table in ("bc_documents", "memory_notes"):
            if table not in tables:
                continue
            project_ids.update(
                str(row[0]) for row in con.execute(
                    f"SELECT DISTINCT project_id FROM {table} WHERE project_id IS NOT NULL"
                )
            )
            if "pooled_from" in {
                row[1] for row in con.execute(f"PRAGMA table_info({table})")
            }:
                pooled = int(con.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE pooled_from IS NOT NULL"
                ).fetchone()[0])
                ambiguous = int(con.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE pooled_from IS NULL"
                ).fetchone()[0])
            else:
                pooled, ambiguous = 0, _count(con, table)
            base["pooled_rows"][table] = pooled
            base["ambiguous_rows"][table] = ambiguous
        base["project_ids"] = sorted(project_ids)

        if "bc_note_links" in tables:
            base["link_rows"] = _count(con, "bc_note_links")
            if "memory_notes" in tables:
                base["dangling_link_rows"] = int(con.execute(
                    "SELECT COUNT(*) FROM bc_note_links l "
                    "LEFT JOIN memory_notes s ON s.id=l.src_id "
                    "LEFT JOIN memory_notes d ON d.id=l.dst_id "
                    "WHERE s.id IS NULL OR d.id IS NULL"
                ).fetchone()[0])
        base["operation_rows"] = _count(con, "bc_operations")
        base["operation_note_rows"] = _count(con, "bc_operation_notes")
        if "bc_operation_notes" in tables and "memory_notes" in tables:
            base["missing_operation_notes"] = int(con.execute(
                "SELECT COUNT(*) FROM bc_operation_notes o "
                "LEFT JOIN memory_notes n ON n.id=o.note_id WHERE n.id IS NULL"
            ).fetchone()[0])
        try:
            base["quick_check"] = str(con.execute("PRAGMA quick_check").fetchone()[0])
        except sqlite3.Error as exc:
            base["warnings"].append(f"quick_check failed: {exc}")
        try:
            base["foreign_key_errors"] = [
                " | ".join(str(value) for value in row)
                for row in con.execute("PRAGMA foreign_key_check")
            ]
        except sqlite3.Error as exc:
            base["warnings"].append(f"foreign_key_check failed: {exc}")
        if base["ambiguous_rows"]:
            base["warnings"].append(
                "rows without pooled_from provenance require explicit attribution; "
                "they will not be assigned automatically"
            )
        if base["missing_operation_notes"]:
            base["warnings"].append(
                "operation audit entries reference missing notes; preserve the audit trail"
            )
    finally:
        con.close()
    return LegacyInventory(**base)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def backup_legacy_database(source: Path, destination: Path) -> LegacyBackup:
    """Create and verify a read-consistent SQLite backup; refuse overwrite."""
    source = Path(source).expanduser().resolve()
    destination = Path(destination).expanduser().resolve()
    if source == destination:
        raise ValueError("backup destination must differ from the legacy source")
    if destination.exists():
        raise FileExistsError(f"backup destination already exists: {destination}")
    inventory = inspect_legacy_database(source)
    if not inventory.readable:
        raise RuntimeError("legacy source is not readable; no backup was created")
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(str(destination))
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()
    verified_inventory = inspect_legacy_database(destination)
    verified = verified_inventory.readable and verified_inventory.quick_check == "ok"
    if not verified:
        raise RuntimeError("backup was created but failed read/quick_check verification")
    return LegacyBackup(
        source=str(source), destination=str(destination), sha256=_sha256(destination),
        bytes=destination.stat().st_size, verified=True, inventory=verified_inventory,
    )


def _verify_backup_matches(source: Path, backup: Path) -> None:
    source_report = inspect_legacy_database(source)
    backup_report = inspect_legacy_database(backup)
    if not backup_report.readable or backup_report.quick_check != "ok":
        raise RuntimeError("migration backup is unreadable or failed quick_check")
    if source_report.counts != backup_report.counts:
        raise RuntimeError("migration backup counts do not match the legacy source")


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def _rebuild_fts(con: sqlite3.Connection) -> None:
    for table in ("bc_chunks_fts", "memory_fts"):
        try:
            con.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")
        except sqlite3.OperationalError:
            # Older stores may not have FTS5; the FK and row checks still run.
            pass


def apply_legacy_migration(
    source: Path,
    backup: Path,
    project_ids: list[str],
    *,
    failure_after: int | None = None,
) -> list[MigrationStats]:
    """Migrate only positively-proven pooled rows into approved Project stores.

    The source and backup are never modified.  Rows with ``pooled_from IS NULL``
    are reported as unclassified/global-native and remain untouched.  Each
    destination has its own transaction and rolls back on any failure.
    ``failure_after`` exists solely for rollback regression tests.
    """
    source = Path(source).expanduser().resolve()
    backup = Path(backup).expanduser().resolve()
    selected = sorted({pid.strip() for pid in project_ids if isinstance(pid, str) and pid.strip()})
    if not selected:
        raise ValueError("migration requires at least one explicit Project ULID")
    if not source.exists() or not backup.exists():
        raise FileNotFoundError("migration source and verified backup are both required")
    _verify_backup_matches(source, backup)

    source_inventory = inspect_legacy_database(source)
    unknown = set(selected) - set(source_inventory.project_ids)
    if unknown:
        raise ValueError(f"selected Project ULIDs are not present in the legacy source: {sorted(unknown)}")

    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    results: list[MigrationStats] = []
    try:
        note_columns = _table_columns(src, "memory_notes")
        doc_columns = _table_columns(src, "bc_documents")
        for project_id in selected:
            from .config import get_db_path
            destination = get_db_path(project_id)
            destination.parent.mkdir(parents=True, exist_ok=True)
            from .store import SqliteStore
            dest_store = SqliteStore(destination)
            dest_store.assert_schema_version()
            dest_store.close()
            con = sqlite3.connect(destination)
            con.execute("PRAGMA foreign_keys=ON")
            note_map: dict[int, int] = {}
            doc_map: dict[int, int] = {}
            notes = 0
            skipped_notes = 0
            docs = 0
            skipped_docs = 0
            chunks = 0
            links = 0
            conflicts = 0
            try:
                con.execute("BEGIN")
                doc_rows = src.execute(
                    "SELECT * FROM bc_documents WHERE project_id=? AND pooled_from=?",
                    (project_id, project_id),
                ).fetchall()
                for row in doc_rows:
                    existing = con.execute(
                        "SELECT id, content_hash FROM bc_documents WHERE project_id=? AND doc_key=?",
                        (project_id, row["doc_key"]),
                    ).fetchone()
                    if existing:
                        if existing[1] != row["content_hash"]:
                            conflicts += 1
                        doc_map[int(row["id"])] = int(existing[0])
                        skipped_docs += 1
                        continue
                    values = {
                        key: row[key] for key in doc_columns
                        if key not in {"id", "pooled_from"} and key in row.keys()
                    }
                    values["project_id"] = project_id
                    keys = list(values)
                    cur = con.execute(
                        f"INSERT INTO bc_documents ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                        [values[key] for key in keys],
                    )
                    doc_map[int(row["id"])] = int(cur.lastrowid)
                    docs += 1
                    for chunk in src.execute(
                        "SELECT chunk_index, chunk_text, chunk_hash, embedding, run_id "
                        "FROM bc_chunks WHERE document_id=? ORDER BY chunk_index",
                        (row["id"],),
                    ):
                        con.execute(
                            "INSERT OR IGNORE INTO bc_chunks "
                            "(document_id, chunk_index, chunk_text, chunk_hash, embedding, run_id) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (doc_map[int(row["id"])], chunk[0], chunk[1], chunk[2], chunk[3], chunk[4]),
                        )
                        chunks += 1
                    if failure_after is not None and docs >= failure_after:
                        raise RuntimeError("injected migration failure")

                note_rows = src.execute(
                    "SELECT * FROM memory_notes WHERE project_id=? AND pooled_from=?",
                    (project_id, project_id),
                ).fetchall()
                for row in note_rows:
                    existing = con.execute(
                        "SELECT id, content FROM memory_notes WHERE note_uid=?", (row["note_uid"],)
                    ).fetchone() if row["note_uid"] else None
                    if existing:
                        note_map[int(row["id"])] = int(existing[0])
                        skipped_notes += 1
                        if existing[1] != row["content"]:
                            conflicts += 1
                        continue
                    values = {
                        key: row[key] for key in note_columns
                        if key not in {"id", "superseded_by", "pooled_from"} and key in row.keys()
                    }
                    values["project_id"] = project_id
                    values["superseded_by"] = None
                    keys = list(values)
                    cur = con.execute(
                        f"INSERT INTO memory_notes ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                        [values[key] for key in keys],
                    )
                    note_map[int(row["id"])] = int(cur.lastrowid)
                    notes += 1
                for old_id, new_id in note_map.items():
                    old = src.execute("SELECT superseded_by FROM memory_notes WHERE id=?", (old_id,)).fetchone()
                    if old and old[0] in note_map:
                        con.execute("UPDATE memory_notes SET superseded_by=? WHERE id=?", (note_map[old[0]], new_id))
                if "bc_note_links" in _tables(src):
                    for link in src.execute(
                        "SELECT src_id, dst_id, kind, weight, created_at FROM bc_note_links"
                    ):
                        if link[0] in note_map and link[1] in note_map:
                            con.execute(
                                "INSERT OR IGNORE INTO bc_note_links "
                                "(src_id, dst_id, kind, weight, created_at) VALUES (?, ?, ?, ?, ?)",
                                (note_map[link[0]], note_map[link[1]], link[2], link[3], link[4]),
                            )
                            links += 1
                _rebuild_fts(con)
                fk_errors = [" | ".join(str(v) for v in row) for row in con.execute("PRAGMA foreign_key_check")]
                if fk_errors:
                    raise RuntimeError(f"destination foreign-key check failed: {fk_errors}")
                con.execute("COMMIT")
            except Exception:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
            finally:
                con.close()
            legacy_notes = source_inventory.ambiguous_rows.get("memory_notes", 0)
            legacy_docs = source_inventory.ambiguous_rows.get("bc_documents", 0)
            results.append(MigrationStats(
                project_id=project_id, notes_migrated=notes, notes_skipped=skipped_notes,
                documents_migrated=docs, documents_skipped=skipped_docs, chunks_migrated=chunks,
                links_migrated=links, conflicts=conflicts,
                preserved_global_native=legacy_notes + legacy_docs,
                skipped_legacy_unclassified=legacy_notes + legacy_docs,
                audit_rows_preserved_in_source=source_inventory.operation_rows + source_inventory.operation_note_rows,
            ))
    finally:
        src.close()
    return results


def write_manifest(value: LegacyInventory | LegacyBackup, destination: Path) -> Path:
    """Write a human-readable JSON manifest without changing source data."""
    destination = Path(destination).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def default_legacy_database() -> Path:
    return get_global_db_path()
