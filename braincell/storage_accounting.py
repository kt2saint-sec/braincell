# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""Bounded visibility and dry-run retention planning for BrainCell state."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import config


def _category(path: Path) -> str:
    name = path.name
    if name.endswith(".db") and (
        "backup" in name
        or name.startswith("braincell-preconsolidate-")
        or name.startswith("braincell-prereflect-")
        or name.startswith("legacy-")
        or name.startswith("destination-")
    ):
        return "backups"
    if name == "braincell.db" or name in {"braincell.db-wal", "braincell.db-shm"}:
        return "databases"
    if name.endswith("ledger.json") or name == "gui-schedules.json":
        return "catalogs"
    if name.endswith(".lock"):
        return "locks"
    return "other"


def _row_counts(database: Path) -> dict[str, int]:
    if not database.is_file():
        return {}
    connection = sqlite3.connect(
        f"file:{database.resolve().as_posix()}?mode=ro", uri=True
    )
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        counts = {}
        for table in ("bc_documents", "bc_chunks", "memory_notes", "bc_operations"):
            if table in tables:
                counts[table] = int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
        return counts
    finally:
        connection.close()


def storage_report(
    project_id: str,
    *,
    keep_backups: int | None = None,
    backup_roots: Sequence[Path] = (),
) -> dict[str, Any]:
    """Account for namespace files and optionally plan backup pruning.

    The plan is informational only. Curated notes, documents, and files are
    never deleted by this function.
    """
    if keep_backups is not None and keep_backups < 0:
        raise ValueError("keep_backups must be >= 0")
    root = config._xdg_data_home() / config.DATA_NAMESPACE
    roots = [root, *(Path(item).expanduser().resolve() for item in backup_roots)]
    files = sorted({
        path.resolve()
        for scan_root in roots
        if scan_root.exists()
        for path in scan_root.rglob("*")
        if path.is_file()
    })
    categories: dict[str, dict[str, int]] = {}
    entries = []
    for path in files:
        category = _category(path)
        size = path.stat().st_size
        bucket = categories.setdefault(category, {"files": 0, "bytes": 0})
        bucket["files"] += 1
        bucket["bytes"] += size
        entries.append(
            {
                "path": str(path),
                "category": category,
                "bytes": size,
                "mtime_ns": path.stat().st_mtime_ns,
            }
        )

    removal_candidates: list[dict[str, Any]] = []
    if keep_backups is not None:
        by_directory: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            if entry["category"] == "backups":
                by_directory.setdefault(str(Path(entry["path"]).parent), []).append(entry)
        for candidates in by_directory.values():
            candidates.sort(
                key=lambda item: (item["mtime_ns"], item["path"]), reverse=True
            )
            removal_candidates.extend(candidates[keep_backups:])

    database = config.get_db_path(project_id)
    return {
        "namespace_root": str(root),
        "scanned_roots": [str(item) for item in roots],
        "project_id": project_id,
        "project_database": str(database),
        "project_rows": _row_counts(database),
        "totals": {
            "files": len(entries),
            "bytes": sum(entry["bytes"] for entry in entries),
        },
        "categories": categories,
        "retention_plan": {
            "dry_run": True,
            "keep_backups_per_directory": keep_backups,
            "candidates": [
                {"path": item["path"], "bytes": item["bytes"]}
                for item in removal_candidates
            ],
            "reclaimable_bytes": sum(item["bytes"] for item in removal_candidates),
        },
    }
