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


def write_manifest(value: LegacyInventory | LegacyBackup, destination: Path) -> Path:
    """Write a human-readable JSON manifest without changing source data."""
    destination = Path(destination).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def default_legacy_database() -> Path:
    return get_global_db_path()
