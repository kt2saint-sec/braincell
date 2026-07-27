# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exclusive preview-first boundary for retired shared BrainCell data."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DATA_NAMESPACE, get_db_path
from .project_registry import load_path_registry
from .schema import MEMORY_SCHEMA_VERSION
from .store import SqliteStore


class LegacyRecoveryError(RuntimeError):
    """The requested legacy recovery cannot proceed safely."""


@dataclass(frozen=True)
class LegacyDiscovery:
    database: str
    database_exists: bool
    families_catalog: str
    families_catalog_exists: bool
    global_mode_environment: bool


def discover_legacy_configuration() -> LegacyDiscovery:
    """Discover retired XDG artifacts without reading client configuration."""
    data_home = Path(
        os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    )
    root = data_home / DATA_NAMESPACE
    database = root / "global" / "braincell.db"
    families = root / "families.json"
    return LegacyDiscovery(
        database=str(database),
        database_exists=database.is_file(),
        families_catalog=str(families),
        families_catalog_exists=families.is_file(),
        global_mode_environment=os.environ.get("BRAINCELL_MODE", "").strip().lower()
        == "global",
    )


def _read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def _schema_version(connection: sqlite3.Connection) -> int:
    tables = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    }
    required = {"bc_documents", "bc_chunks", "memory_notes", "schema_version"}
    if missing := sorted(required - tables):
        raise LegacyRecoveryError(
            f"Legacy database is missing required tables: {', '.join(missing)}"
        )
    row = connection.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    version = int(row[0]) if row else 0
    if version > MEMORY_SCHEMA_VERSION:
        raise LegacyRecoveryError(
            f"Legacy schema v{version} is newer than supported v{MEMORY_SCHEMA_VERSION}."
        )
    return version


def _conflicts(
    source: sqlite3.Connection, project_id: str, destination: Path
) -> list[dict[str, Any]]:
    if not destination.exists():
        return []
    conflicts: list[dict[str, Any]] = []
    with sqlite3.connect(destination) as dest:
        for row in source.execute(
            "SELECT doc_key, content_hash FROM bc_documents WHERE project_id=?",
            (project_id,),
        ):
            existing = dest.execute(
                "SELECT content_hash FROM bc_documents "
                "WHERE project_id=? AND doc_key=?",
                (project_id, row["doc_key"]),
            ).fetchone()
            if existing is not None and existing[0] != row["content_hash"]:
                conflicts.append({"kind": "document", "key": row["doc_key"]})
        if "note_uid" in _columns(source, "memory_notes"):
            for row in source.execute(
                "SELECT id, note_uid, content FROM memory_notes "
                "WHERE project_id=? AND note_uid IS NOT NULL",
                (project_id,),
            ):
                existing = dest.execute(
                    "SELECT content FROM memory_notes WHERE note_uid=?",
                    (row["note_uid"],),
                ).fetchone()
                if existing is not None and existing[0] != row["content"]:
                    conflicts.append(
                        {"kind": "note", "key": row["note_uid"], "source_id": row["id"]}
                    )
    return conflicts


def preview(source_path: Path | None = None) -> dict[str, Any]:
    """Classify rows and return the digest required for an explicit apply."""
    discovery = discover_legacy_configuration()
    source = Path(source_path or discovery.database).expanduser().resolve()
    if not source.is_file():
        raise LegacyRecoveryError(f"Legacy database does not exist: {source}")
    registered = set(load_path_registry().values())
    categories: dict[str, dict[str, int]] = {
        "known_pooled_from": {},
        "attributable": {},
        "ambiguous_or_unattributed": {},
    }
    with _read_only(source) as connection:
        version = _schema_version(connection)
        for table in ("bc_documents", "memory_notes"):
            pooled_expr = (
                "pooled_from" if "pooled_from" in _columns(connection, table) else "NULL"
            )
            rows = connection.execute(
                f"SELECT project_id, {pooled_expr} AS pooled_from FROM {table}"
            )
            for row in rows:
                project_id = str(row["project_id"] or "").strip()
                if row["pooled_from"]:
                    category = "known_pooled_from"
                elif project_id and project_id in registered:
                    category = "attributable"
                else:
                    category = "ambiguous_or_unattributed"
                key = project_id or "<unattributed>"
                categories[category][key] = categories[category].get(key, 0) + 1
        attributable = sorted(
            {
                project_id
                for category in ("known_pooled_from", "attributable")
                for project_id in categories[category]
                if project_id in registered
            }
        )
        projects = {}
        for project_id in attributable:
            destination = get_db_path(project_id)
            projects[project_id] = {
                "destination": str(destination),
                "documents": connection.execute(
                    "SELECT COUNT(*) FROM bc_documents WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0],
                "notes": connection.execute(
                    "SELECT COUNT(*) FROM memory_notes WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0],
                "conflicts": _conflicts(connection, project_id, destination),
            }
    report: dict[str, Any] = {
        "source": str(source),
        "source_schema_version": version,
        "discovery": asdict(discovery),
        "classifications": categories,
        "projects": projects,
    }
    payload = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    report["approval_digest"] = hashlib.sha256(payload).hexdigest()
    return report


def create_backup(source: Path, backup_dir: Path | None = None) -> Path:
    """Create and retain a transactionally consistent copy of the original."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = (backup_dir or source.parent).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{source.stem}.legacy-backup-{timestamp}.db"
    serial = 1
    while destination.exists():
        destination = directory / (
            f"{source.stem}.legacy-backup-{timestamp}-{serial}.db"
        )
        serial += 1
    with _read_only(source) as legacy, sqlite3.connect(destination) as backup:
        legacy.backup(backup)
    return destination


def _value(row: sqlite3.Row, name: str, default: Any = None) -> Any:
    return row[name] if name in row.keys() else default


def _copy_project(
    source: sqlite3.Connection, destination: Path, project_id: str
) -> dict[str, int]:
    store = SqliteStore(destination)
    store.assert_schema_version()
    store.close()
    copied = {"documents": 0, "chunks": 0, "notes": 0, "links": 0}
    source_marker = hashlib.sha256(
        source.execute("PRAGMA database_list").fetchone()[2].encode()
    ).hexdigest()[:16]
    with sqlite3.connect(destination) as dest:
        dest.row_factory = sqlite3.Row
        dest.execute("PRAGMA foreign_keys=ON")
        dest.execute("BEGIN IMMEDIATE")
        for document in source.execute(
            "SELECT * FROM bc_documents WHERE project_id=? ORDER BY id", (project_id,)
        ):
            existing = dest.execute(
                "SELECT id, content_hash FROM bc_documents "
                "WHERE project_id=? AND doc_key=?",
                (project_id, document["doc_key"]),
            ).fetchone()
            if existing is not None:
                if existing["content_hash"] != document["content_hash"]:
                    raise LegacyRecoveryError(
                        f"Destination conflict for document {document['doc_key']!r}."
                    )
                continue
            cursor = dest.execute(
                "INSERT INTO bc_documents "
                "(project_id,doc_key,title,content_hash,content_type,commit_sha,"
                "run_id,created_at,updated_at,metadata,pooled_from) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    project_id, document["doc_key"], document["title"],
                    document["content_hash"], document["content_type"],
                    _value(document, "commit_sha"), _value(document, "run_id"),
                    document["created_at"], _value(document, "updated_at"),
                    _value(document, "metadata"),
                ),
            )
            copied["documents"] += 1
            for chunk in source.execute(
                "SELECT * FROM bc_chunks WHERE document_id=? ORDER BY chunk_index",
                (document["id"],),
            ):
                dest.execute(
                    "INSERT INTO bc_chunks "
                    "(document_id,chunk_index,chunk_text,chunk_hash,embedding,run_id) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        cursor.lastrowid, chunk["chunk_index"], chunk["chunk_text"],
                        chunk["chunk_hash"], chunk["embedding"], _value(chunk, "run_id"),
                    ),
                )
                copied["chunks"] += 1

        note_ids: dict[int, int] = {}
        replacements: list[tuple[int, int | None]] = []
        for note in source.execute(
            "SELECT * FROM memory_notes WHERE project_id=? ORDER BY id", (project_id,)
        ):
            uid = _value(note, "note_uid") or (
                f"legacy-{source_marker}-{project_id}-{note['id']}"
            )
            existing = dest.execute(
                "SELECT id, content FROM memory_notes WHERE note_uid=?", (uid,)
            ).fetchone()
            if existing is not None:
                if existing["content"] != note["content"]:
                    raise LegacyRecoveryError(f"Destination conflict for note {uid!r}.")
                note_ids[note["id"]] = existing["id"]
                replacements.append((existing["id"], _value(note, "superseded_by")))
                continue
            cursor = dest.execute(
                "INSERT INTO memory_notes "
                "(project_id,scope,kind,content,tags,confidence,source_hint,"
                "superseded_by,created_at,embedding,deleted_at,note_uid,revision,"
                "pooled_from,status) VALUES (?,?,?,?,?,?,?,NULL,?,?,?,?,?,NULL,?)",
                (
                    project_id, note["scope"], note["kind"], note["content"],
                    _value(note, "tags"), _value(note, "confidence"),
                    _value(note, "source_hint"), note["created_at"],
                    _value(note, "embedding"), _value(note, "deleted_at"), uid,
                    _value(note, "revision", 1), _value(note, "status", "active"),
                ),
            )
            note_ids[note["id"]] = int(cursor.lastrowid)
            replacements.append(
                (int(cursor.lastrowid), _value(note, "superseded_by"))
            )
            copied["notes"] += 1
        for destination_id, source_replacement in replacements:
            dest.execute(
                "UPDATE memory_notes SET superseded_by=? WHERE id=?",
                (
                    note_ids.get(source_replacement)
                    if source_replacement is not None else None,
                    destination_id,
                ),
            )
        tables = {
            row[0]
            for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "bc_note_links" in tables:
            for link in source.execute("SELECT * FROM bc_note_links"):
                src_id, dst_id = note_ids.get(link["src_id"]), note_ids.get(link["dst_id"])
                if src_id is None or dst_id is None:
                    continue
                cursor = dest.execute(
                    "INSERT OR IGNORE INTO bc_note_links "
                    "(src_id,dst_id,kind,weight,created_at) VALUES (?,?,?,?,?)",
                    (src_id, dst_id, link["kind"], link["weight"], link["created_at"]),
                )
                copied["links"] += max(cursor.rowcount, 0)
        for table in ("bc_chunks_fts", "memory_fts"):
            dest.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")
        dest.commit()
    return copied


def _verify(
    source: sqlite3.Connection, destination: Path, project_id: str
) -> dict[str, Any]:
    with sqlite3.connect(destination) as dest:
        source_docs = {
            row[0] for row in source.execute(
                "SELECT doc_key FROM bc_documents WHERE project_id=?", (project_id,)
            )
        }
        destination_docs = {
            row[0] for row in dest.execute(
                "SELECT doc_key FROM bc_documents WHERE project_id=?", (project_id,)
            )
        }
        source_notes = source.execute(
            "SELECT COUNT(*) FROM memory_notes WHERE project_id=?", (project_id,)
        ).fetchone()[0]
        source_chunks = source.execute(
            "SELECT COUNT(*) FROM bc_chunks c JOIN bc_documents d "
            "ON d.id=c.document_id WHERE d.project_id=?",
            (project_id,),
        ).fetchone()[0]
        destination_notes = dest.execute(
            "SELECT COUNT(*) FROM memory_notes WHERE project_id=?", (project_id,)
        ).fetchone()[0]
        destination_chunks = dest.execute(
            "SELECT COUNT(*) FROM bc_chunks c JOIN bc_documents d "
            "ON d.id=c.document_id WHERE d.project_id=?",
            (project_id,),
        ).fetchone()[0]
        foreign_keys = dest.execute("PRAGMA foreign_key_check").fetchall()
        fts = {}
        for table in ("bc_chunks_fts", "memory_fts"):
            try:
                dest.execute(f"INSERT INTO {table}({table}) VALUES('integrity-check')")
                fts[table] = "ok"
            except sqlite3.Error as exc:
                fts[table] = f"failed: {exc}"
        source_links = 0
        if "bc_note_links" in {
            row[0] for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }:
            source_links = source.execute(
                "SELECT COUNT(*) FROM bc_note_links l "
                "JOIN memory_notes s ON s.id=l.src_id "
                "JOIN memory_notes d ON d.id=l.dst_id "
                "WHERE s.project_id=? AND d.project_id=?",
                (project_id, project_id),
            ).fetchone()[0]
        destination_links = dest.execute(
            "SELECT COUNT(*) FROM bc_note_links l "
            "JOIN memory_notes s ON s.id=l.src_id "
            "JOIN memory_notes d ON d.id=l.dst_id "
            "WHERE s.project_id=? AND d.project_id=?",
            (project_id, project_id),
        ).fetchone()[0]
        result = {
            "document_keys_verified": source_docs <= destination_docs,
            "source_notes": source_notes,
            "destination_notes": destination_notes,
            "source_chunks": source_chunks,
            "destination_chunks": destination_chunks,
            "note_count_verified": destination_notes >= source_notes,
            "chunk_count_verified": destination_chunks >= source_chunks,
            "foreign_key_violations": len(foreign_keys),
            "fts": fts,
            "source_links": source_links,
            "destination_links": destination_links,
            "note_links_verified": destination_links >= source_links,
        }
        result["ok"] = (
            result["document_keys_verified"]
            and result["note_count_verified"]
            and result["chunk_count_verified"]
            and result["foreign_key_violations"] == 0
            and all(value == "ok" for value in fts.values())
            and result["note_links_verified"]
        )
        return result


def apply(
    *,
    source_path: Path | None,
    project_ids: list[str],
    approval_digest: str,
    backup_dir: Path | None = None,
) -> dict[str, Any]:
    """Apply the exact approved preview to selected matching Project databases."""
    if not project_ids:
        raise LegacyRecoveryError("Apply requires at least one selected Project.")
    report = preview(source_path)
    if approval_digest != report["approval_digest"]:
        raise LegacyRecoveryError(
            "Approval digest does not match the current preview; preview again."
        )
    selected = sorted(set(project_ids))
    if unknown := [pid for pid in selected if pid not in report["projects"]]:
        raise LegacyRecoveryError(
            f"Selected Projects are not attributable: {', '.join(unknown)}"
        )
    conflicts = {
        pid: report["projects"][pid]["conflicts"]
        for pid in selected if report["projects"][pid]["conflicts"]
    }
    if conflicts:
        raise LegacyRecoveryError(
            f"Destination conflicts must be resolved before apply: {conflicts}"
        )
    source = Path(report["source"])
    backup = create_backup(source, backup_dir)
    results = {}
    with _read_only(source) as connection:
        for project_id in selected:
            destination = get_db_path(project_id)
            copied = _copy_project(connection, destination, project_id)
            verification = _verify(connection, destination, project_id)
            if not verification["ok"]:
                raise LegacyRecoveryError(
                    f"Post-copy verification failed for {project_id}: {verification}"
                )
            results[project_id] = {
                "destination": str(destination),
                "copied": copied,
                "verification": verification,
            }
    return {
        "source": str(source),
        "backup": str(backup),
        "backup_retained": True,
        "projects": results,
    }
