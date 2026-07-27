# SPDX-License-Identifier: AGPL-3.0-or-later
"""Explicit, preview-first recovery from a retired shared BrainCell database.

This is the only runtime consumer of legacy pooled rows.  The retired
family/global/materialized-pool implementation remains elsewhere solely so this
one-time migration can read old databases; normal Project and Pool operations do
not import this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import DATA_NAMESPACE, get_db_path
from .project_registry import load_path_registry
from .schema import MEMORY_SCHEMA_VERSION
from .store import SqliteStore


class LegacyRecoveryError(RuntimeError):
    """The requested legacy recovery cannot proceed safely."""

    def __init__(self, message: str, *, completed_projects: Iterable[str] = ()) -> None:
        self.completed_projects = tuple(completed_projects)
        suffix = (
            f" Completed Projects remain recovered: {', '.join(self.completed_projects)}."
            if self.completed_projects else ""
        )
        super().__init__(message + suffix)


@dataclass(frozen=True)
class LegacyDiscovery:
    database: str
    database_exists: bool
    families_catalog: str
    families_catalog_exists: bool
    global_mode_environment: bool


def discover_legacy_configuration() -> LegacyDiscovery:
    """Discover retired artifacts without reading client configuration."""
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    root = data_home / DATA_NAMESPACE
    database = root / "global" / "braincell.db"
    families = root / "families.json"
    return LegacyDiscovery(str(database), database.is_file(), str(families), families.is_file(),
                           os.environ.get("BRAINCELL_MODE", "").strip().lower() == "global")


def _wal_path(path: Path) -> Path:
    return path.with_name(f"{path.name}-wal")


def _shm_path(path: Path) -> Path:
    return path.with_name(f"{path.name}-shm")


def _source_state_digest(path: Path) -> str:
    """Digest the database and committed WAL frames without encoding its path."""
    digest = hashlib.sha256()
    for artifact in (path, _wal_path(path)):
        if artifact.exists():
            digest.update(artifact.name.encode())
            digest.update(artifact.read_bytes())
    return digest.hexdigest()


def _read_only(path: Path, *, purpose: str) -> sqlite3.Connection:
    """Open a stable read-only snapshot without hiding committed WAL frames.

    SQLite needs an existing WAL shared-memory index to read a WAL database
    without writing one.  Refuse rather than create that sidecar during preview,
    or silently omit the WAL by using ``immutable=1``.
    """
    wal, shm = _wal_path(path), _shm_path(path)
    if wal.exists() and not shm.exists():
        raise LegacyRecoveryError(
            f"{purpose} cannot safely read {path}: committed WAL data exists but "
            "the WAL shared-memory index is unavailable. Close/checkpoint the "
            "writer, then retry recovery."
        )
    query = "mode=ro&cache=private" if wal.exists() else "mode=ro&immutable=1"
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?{query}", uri=True, timeout=0
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@contextmanager
def _exclusive_destination(path: Path):
    """Hold SQLite's writer lock while a destination is backed up or changed."""
    connection = sqlite3.connect(path, timeout=0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout=0")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise LegacyRecoveryError(
                f"Destination {path} has an active writer. Stop BrainCell or the "
                "other writer before applying recovery."
            ) from exc
        yield connection
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def _schema_version(connection: sqlite3.Connection) -> int:
    required = {"bc_documents", "bc_chunks", "memory_notes", "schema_version"}
    if missing := sorted(required - _tables(connection)):
        raise LegacyRecoveryError(f"Legacy database is missing required tables: {', '.join(missing)}")
    row = connection.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    version = int(row[0]) if row else 0
    if version > MEMORY_SCHEMA_VERSION:
        raise LegacyRecoveryError(f"Legacy schema v{version} is newer than supported v{MEMORY_SCHEMA_VERSION}.")
    return version


def _route(row: sqlite3.Row, registered: set[str]) -> tuple[str | None, str]:
    project_id = str(row["project_id"] or "").strip()
    pooled_from = str(row["pooled_from"] or "").strip() if "pooled_from" in row.keys() else ""  # noqa: SIM118  # sqlite3.Row membership checks values, not column names.
    if pooled_from:
        if pooled_from != project_id:
            return None, "ambiguous_pooled_from_conflict"
        if pooled_from in registered:
            return pooled_from, "known_pooled_from"
        return None, "ambiguous_or_unattributed"
    if project_id in registered:
        return project_id, "attributable"
    return None, "ambiguous_or_unattributed"


def _manifest(connection: sqlite3.Connection, registered: set[str]) -> tuple[dict[str, dict[str, list[int]]], dict[str, dict[str, int]]]:
    manifest: dict[str, dict[str, list[int]]] = {}
    categories: dict[str, dict[str, int]] = {"known_pooled_from": {}, "attributable": {}, "ambiguous_pooled_from_conflict": {}, "ambiguous_or_unattributed": {}}
    for table in ("bc_documents", "memory_notes"):
        pooled = "pooled_from" if "pooled_from" in _columns(connection, table) else "NULL"
        for row in connection.execute(f"SELECT id, project_id, {pooled} AS pooled_from FROM {table} ORDER BY id"):
            project_id, category = _route(row, registered)
            key = project_id or str(row["pooled_from"] or row["project_id"] or "<unattributed>")
            categories[category][key] = categories[category].get(key, 0) + 1
            if project_id:
                manifest.setdefault(project_id, {"documents": [], "notes": []})["documents" if table == "bc_documents" else "notes"].append(int(row["id"]))
    return manifest, categories


def _placeholders(values: list[int]) -> str:
    return ",".join("?" for _ in values) or "NULL"


def _destination_conflicts(source: sqlite3.Connection, destination: Path, project_id: str, selected_docs: list[int], selected_notes: list[int]) -> list[dict[str, Any]]:
    if not destination.is_file():
        return []
    conflicts: list[dict[str, Any]] = []
    with _read_only(destination, purpose="Preview") as dest:
        for row in source.execute(f"SELECT doc_key, content_hash FROM bc_documents WHERE id IN ({_placeholders(selected_docs)})", selected_docs):
            existing = dest.execute("SELECT content_hash FROM bc_documents WHERE project_id=? AND doc_key=?", (project_id, row["doc_key"])).fetchone()
            if existing is not None and existing[0] != row["content_hash"]:
                conflicts.append({"kind": "document", "key": row["doc_key"]})
        for row in source.execute(f"SELECT * FROM memory_notes WHERE id IN ({_placeholders(selected_notes)})", selected_notes):
            uid = _note_uid(row)
            existing = dest.execute("SELECT content FROM memory_notes WHERE note_uid=?", (uid,)).fetchone()
            if existing is not None and existing[0] != row["content"]:
                conflicts.append({"kind": "note", "key": uid, "source_id": row["id"]})
    return conflicts


def preview(source_path: Path | None = None) -> dict[str, Any]:
    """Classify rows using read-only source *and destination* connections only."""
    discovery = discover_legacy_configuration()
    source = Path(source_path or discovery.database).expanduser().resolve()
    if not source.is_file():
        raise LegacyRecoveryError(f"Legacy database does not exist: {source}")
    with _read_only(source, purpose="Preview") as connection:
        version = _schema_version(connection)
        manifest, categories = _manifest(connection, set(load_path_registry().values()))
        projects = {}
        for project_id, selected in sorted(manifest.items()):
            destination = get_db_path(project_id)
            projects[project_id] = {"destination": str(destination), "documents": len(selected["documents"]), "notes": len(selected["notes"]), "conflicts": _destination_conflicts(connection, destination, project_id, selected["documents"], selected["notes"])}
    report: dict[str, Any] = {"source": str(source), "source_state_digest": _source_state_digest(source), "source_schema_version": version, "discovery": asdict(discovery), "classifications": categories, "projects": projects}
    report["approval_digest"] = hashlib.sha256(json.dumps(report, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return report


def _backup_database(source: Path, kind: str, backup_dir: Path | None = None) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    directory = (backup_dir or source.parent).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{source.stem}.{kind}-backup-{timestamp}.db"
    serial = 1
    while destination.exists():
        destination = directory / f"{source.stem}.{kind}-backup-{timestamp}-{serial}.db"
        serial += 1
    with _read_only(source, purpose="Backup") as original, sqlite3.connect(destination) as backup:
        original.backup(backup)
    return destination


def create_backup(source: Path, backup_dir: Path | None = None) -> Path:
    """Retain a transactionally consistent copy of the legacy source."""
    return _backup_database(source, "legacy", backup_dir)


def _value(row: sqlite3.Row, name: str, default: Any = None) -> Any:
    return row[name] if name in row.keys() else default  # noqa: SIM118  # sqlite3.Row membership checks values and has no mapping get().


def _note_uid(note: sqlite3.Row) -> str:
    """Stable fallback identity: source row content, never a machine-specific path."""
    if _value(note, "note_uid"):
        return str(note["note_uid"])
    fields = ("id", "project_id", "scope", "kind", "content", "tags", "confidence", "source_hint", "created_at", "revision", "deleted_at", "status")
    payload = {field: _value(note, field) for field in fields}
    return "legacy-" + hashlib.sha256(json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _copy_project(
    source: sqlite3.Connection,
    dest: sqlite3.Connection,
    project_id: str,
    selected: dict[str, list[int]],
) -> dict[str, int]:
    """Copy one selected manifest inside the caller's destination transaction."""
    copied = {"documents": 0, "chunks": 0, "notes": 0, "links": 0}
    document_map: dict[int, int] = {}
    for document in source.execute(f"SELECT * FROM bc_documents WHERE id IN ({_placeholders(selected['documents'])}) ORDER BY id", selected["documents"]):
        existing = dest.execute("SELECT id, content_hash FROM bc_documents WHERE project_id=? AND doc_key=?", (project_id, document["doc_key"])).fetchone()
        if existing is not None:
            if existing["content_hash"] != document["content_hash"]:
                raise LegacyRecoveryError(f"Destination conflict for document {document['doc_key']!r}.")
            document_map[int(document["id"])] = int(existing["id"])
            continue
        cursor = dest.execute("INSERT INTO bc_documents (project_id,doc_key,title,content_hash,content_type,commit_sha,run_id,created_at,updated_at,metadata,pooled_from) VALUES (?,?,?,?,?,?,?,?,?,?,NULL)", (project_id, document["doc_key"], document["title"], document["content_hash"], document["content_type"], _value(document, "commit_sha"), _value(document, "run_id"), document["created_at"], _value(document, "updated_at"), _value(document, "metadata")))
        document_map[int(document["id"])] = int(cursor.lastrowid)
        copied["documents"] += 1
    for source_document_id, destination_document_id in document_map.items():
        for chunk in source.execute("SELECT * FROM bc_chunks WHERE document_id=? ORDER BY chunk_index", (source_document_id,)):
            existing = dest.execute("SELECT id FROM bc_chunks WHERE document_id=? AND chunk_index=?", (destination_document_id, chunk["chunk_index"])).fetchone()
            if existing is None:
                dest.execute("INSERT INTO bc_chunks (document_id,chunk_index,chunk_text,chunk_hash,embedding,run_id) VALUES (?,?,?,?,?,?)", (destination_document_id, chunk["chunk_index"], chunk["chunk_text"], chunk["chunk_hash"], chunk["embedding"], _value(chunk, "run_id")))
                copied["chunks"] += 1
    note_map: dict[int, int] = {}
    replacements: list[tuple[int, int | None]] = []
    for note in source.execute(f"SELECT * FROM memory_notes WHERE id IN ({_placeholders(selected['notes'])}) ORDER BY id", selected["notes"]):
        uid = _note_uid(note)
        existing = dest.execute("SELECT id, content FROM memory_notes WHERE note_uid=?", (uid,)).fetchone()
        if existing is not None:
            if existing["content"] != note["content"]:
                raise LegacyRecoveryError(f"Destination conflict for note {uid!r}.")
            destination_id = int(existing["id"])
        else:
            cursor = dest.execute("INSERT INTO memory_notes (project_id,scope,kind,content,tags,confidence,source_hint,superseded_by,created_at,embedding,deleted_at,note_uid,revision,pooled_from,status) VALUES (?,?,?,?,?,?,?,NULL,?,?,?,?,?,NULL,?)", (project_id, note["scope"], note["kind"], note["content"], _value(note, "tags"), _value(note, "confidence"), _value(note, "source_hint"), note["created_at"], _value(note, "embedding"), _value(note, "deleted_at"), uid, _value(note, "revision", 1), _value(note, "status", "active")))
            destination_id = int(cursor.lastrowid)
            copied["notes"] += 1
        note_map[int(note["id"])] = destination_id
        replacements.append((destination_id, _value(note, "superseded_by")))
    for destination_id, source_replacement in replacements:
        dest.execute("UPDATE memory_notes SET superseded_by=? WHERE id=?", (note_map.get(source_replacement) if source_replacement is not None else None, destination_id))
    if "bc_note_links" in _tables(source):
        for link in source.execute("SELECT * FROM bc_note_links"):
            src_id, dst_id = note_map.get(link["src_id"]), note_map.get(link["dst_id"])
            if src_id is not None and dst_id is not None:
                cursor = dest.execute("INSERT OR IGNORE INTO bc_note_links (src_id,dst_id,kind,weight,created_at) VALUES (?,?,?,?,?)", (src_id, dst_id, link["kind"], link["weight"], link["created_at"]))
                copied["links"] += max(cursor.rowcount, 0)
    for table in ("bc_chunks_fts", "memory_fts"):
        dest.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")
    return copied


def _verify(
    source: sqlite3.Connection,
    destination: Path | sqlite3.Connection,
    project_id: str,
    selected: dict[str, list[int]],
) -> dict[str, Any]:
    """Compare every selected entity and its indexes, not aggregate destination counts."""
    failures: list[str] = []
    # This runs only after apply has copied rows. FTS5's integrity command is an
    # INSERT-form pragma, so it cannot run through preview's strict read-only
    # connection; it does not alter indexed content.
    destination_context = (
        nullcontext(destination)
        if isinstance(destination, sqlite3.Connection)
        else sqlite3.connect(destination)
    )
    with destination_context as dest:
        dest.row_factory = sqlite3.Row
        dest.execute("PRAGMA foreign_keys=ON")
        source_docs = list(source.execute(f"SELECT * FROM bc_documents WHERE id IN ({_placeholders(selected['documents'])})", selected["documents"]))
        document_map: dict[int, int] = {}
        for row in source_docs:
            actual = dest.execute("SELECT * FROM bc_documents WHERE project_id=? AND doc_key=?", (project_id, row["doc_key"])).fetchone()
            fields = ("doc_key", "title", "content_hash", "content_type", "commit_sha", "run_id", "created_at", "updated_at", "metadata")
            if actual is None or any(actual[field] != _value(row, field) for field in fields):
                failures.append(f"document:{row['doc_key']}")
            else:
                document_map[int(row["id"])] = int(actual["id"])
        chunk_count = 0
        for source_document_id, destination_document_id in document_map.items():
            for chunk in source.execute("SELECT * FROM bc_chunks WHERE document_id=?", (source_document_id,)):
                chunk_count += 1
                actual = dest.execute("SELECT * FROM bc_chunks WHERE document_id=? AND chunk_index=?", (destination_document_id, chunk["chunk_index"])).fetchone()
                fields = ("chunk_index", "chunk_text", "chunk_hash", "embedding", "run_id")
                if actual is None or any(actual[field] != _value(chunk, field) for field in fields):
                    failures.append(f"chunk:{source_document_id}:{chunk['chunk_index']}")
                elif (fts_row := dest.execute("SELECT chunk_text FROM bc_chunks_fts WHERE rowid=?", (actual["id"],)).fetchone()) is None or fts_row["chunk_text"] != chunk["chunk_text"]:
                    failures.append(f"chunk_fts:{source_document_id}:{chunk['chunk_index']}")
        note_map: dict[int, int] = {}
        for note in source.execute(f"SELECT * FROM memory_notes WHERE id IN ({_placeholders(selected['notes'])})", selected["notes"]):
            actual = dest.execute("SELECT * FROM memory_notes WHERE note_uid=?", (_note_uid(note),)).fetchone()
            fields = ("project_id", "scope", "kind", "content", "tags", "confidence", "source_hint", "created_at", "embedding", "deleted_at", "note_uid", "revision", "status")
            expected = {field: (_note_uid(note) if field == "note_uid" else (project_id if field == "project_id" else _value(note, field))) for field in fields}
            if actual is None or any(actual[field] != expected[field] for field in fields):
                failures.append(f"note:{note['id']}")
            else:
                note_map[int(note["id"])] = int(actual["id"])
                fts_row = dest.execute("SELECT content FROM memory_fts WHERE rowid=?", (actual["id"],)).fetchone()
                if fts_row is None or fts_row["content"] != note["content"]:
                    failures.append(f"note_fts:{note['id']}")
        for source_id, destination_id in note_map.items():
            source_superseded = source.execute("SELECT superseded_by FROM memory_notes WHERE id=?", (source_id,)).fetchone()[0]
            actual = dest.execute("SELECT superseded_by FROM memory_notes WHERE id=?", (destination_id,)).fetchone()[0]
            if actual != note_map.get(source_superseded):
                failures.append(f"note_foreign_key:{source_id}")
        expected_links = set()
        if "bc_note_links" in _tables(source):
            for link in source.execute("SELECT * FROM bc_note_links"):
                if link["src_id"] in note_map and link["dst_id"] in note_map:
                    expected_links.add((note_map[link["src_id"]], note_map[link["dst_id"]], link["kind"], link["weight"], link["created_at"]))
        actual_links = {tuple(row) for row in dest.execute("SELECT src_id,dst_id,kind,weight,created_at FROM bc_note_links WHERE src_id IN ({0}) AND dst_id IN ({0})".format(_placeholders(list(note_map.values()))), list(note_map.values()) * 2)} if note_map else set()
        if expected_links != actual_links:
            failures.append("note_links")
        foreign_keys = dest.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            failures.append("foreign_keys")
        fts: dict[str, str] = {}
        for table in ("bc_chunks_fts", "memory_fts"):
            try:
                dest.execute(f"INSERT INTO {table}({table}) VALUES('integrity-check')")
                fts[table] = "ok"
            except sqlite3.Error as exc:
                fts[table] = f"failed: {exc}"
                failures.append(table)
    return {"ok": not failures, "documents_verified": len(document_map), "chunks_verified": chunk_count, "notes_verified": len(note_map), "links_verified": len(expected_links), "foreign_key_violations": len(foreign_keys), "fts": fts, "failures": failures}


def _prepare_destination(destination: Path) -> bool:
    """Refuse incompatible existing databases before taking the writer lock."""
    existed = destination.is_file()
    if existed:
        with _read_only(destination, purpose="Apply") as connection:
            if _schema_version(connection) != MEMORY_SCHEMA_VERSION:
                raise LegacyRecoveryError(
                    f"Destination {destination} is not at supported schema "
                    f"v{MEMORY_SCHEMA_VERSION}; upgrade it before recovery."
                )
        return True
    destination.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(destination)
    store.assert_schema_version()
    store.close()
    return False


def apply(*, source_path: Path | None, project_ids: list[str], approval_digest: str, backup_dir: Path | None = None) -> dict[str, Any]:
    """Apply an approved manifest, restoring the failed destination on every error."""
    if not project_ids:
        raise LegacyRecoveryError("Apply requires at least one selected Project.")
    report = preview(source_path)
    if approval_digest != report["approval_digest"]:
        raise LegacyRecoveryError("Approval digest does not match the current preview; preview again.")
    selected = sorted(set(project_ids))
    if unknown := [pid for pid in selected if pid not in report["projects"]]:
        raise LegacyRecoveryError(f"Selected Projects are not attributable: {', '.join(unknown)}")
    conflicts = {pid: report["projects"][pid]["conflicts"] for pid in selected if report["projects"][pid]["conflicts"]}
    if conflicts:
        raise LegacyRecoveryError(f"Destination conflicts must be resolved before apply: {conflicts}")
    source = Path(report["source"])
    source_backup = create_backup(source, backup_dir)
    results: dict[str, Any] = {}
    with _read_only(source, purpose="Apply") as connection:
        connection.execute("BEGIN")
        manifest, _ = _manifest(connection, set(load_path_registry().values()))
        for project_id in selected:
            destination = get_db_path(project_id)
            existed = _prepare_destination(destination)
            destination_backup = None
            try:
                with _exclusive_destination(destination) as destination_connection:
                    if existed:
                        destination_backup = _backup_database(
                            destination, "destination", backup_dir
                        )
                    copied = _copy_project(
                        connection, destination_connection, project_id, manifest[project_id]
                    )
                    verification = _verify(
                        connection, destination_connection, project_id, manifest[project_id]
                    )
                    if not verification["ok"]:
                        raise LegacyRecoveryError(
                            f"Post-copy verification failed for {project_id}: {verification}"
                        )
                    destination_connection.commit()
            except Exception as exc:
                # The guarded transaction is never committed before exact
                # verification, so its rollback restores the affected database.
                if not existed:
                    for artifact in (destination, _wal_path(destination), _shm_path(destination)):
                        if artifact.exists():
                            artifact.unlink()
                raise LegacyRecoveryError(
                    f"Recovery failed for {project_id}; its destination was restored "
                    f"from its guarded transaction. {exc}",
                    completed_projects=results,
                ) from exc
            results[project_id] = {"destination": str(destination), "destination_backup": str(destination_backup) if destination_backup else None, "destination_existed": existed, "copied": copied, "verification": verification}
    return {"source": str(source), "backup": str(source_backup), "backup_retained": True, "projects": results}
