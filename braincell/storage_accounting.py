# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""Bounded visibility, dry-run retention planning, and explicit retention apply.

Retention discipline (BC-12 / BC-23):
- Nothing is ever expired by default: every axis (backup pruning, operation-
  history expiry, tombstone purge) is disabled until the owner configures it.
- Every cleanup begins as a dry run: ``storage_report`` only plans; only
  ``apply_retention`` deletes, and it re-plans under the destination mutation
  lock so it never executes a stale plan.
- Fail closed: a snapshot referenced by undo/operation history is never
  deleted, a tombstoned note referenced by recorded operation history is never
  purged, and unreadable history refuses the whole operation.
- Curated memory (active/superseded notes, documents, chunks) is never touched.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import config
from .catalog_io import mutation_lock


class RetentionRefusedError(RuntimeError):
    """Fail-closed refusal: retention would touch protected or unprovable state."""


def _category(path: Path) -> str:
    name = path.name
    if name.endswith(".db") and (
        "backup" in name
        or name.startswith((
            "braincell-preconsolidate-",
            "braincell-prereflect-",
            "legacy-",
            "destination-",
        ))
    ):
        return "backups"
    if name == "braincell.db" or name in {"braincell.db-wal", "braincell.db-shm"}:
        return "databases"
    if name.endswith("ledger.json") or name == "gui-schedules.json":
        return "catalogs"
    if name.endswith(".lock"):
        return "locks"
    return "other"


def _readonly_connection(database: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True)


def _row_counts(database: Path) -> dict[str, int]:
    if not database.is_file():
        return {}
    connection = _readonly_connection(database)
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


# WAL-starvation: the `-wal` file growing far past the database it belongs to
# means checkpoints aren't running (a long-lived reader, or nothing ever
# calling PRAGMA wal_checkpoint) — a warning-only signal; this module never
# executes VACUUM or a forced checkpoint (BUGS.md: SQLite compaction/WAL
# diagnostics stays an authorized, unimplemented workflow).
_WAL_STARVATION_RATIO = 2.0
_WAL_STARVATION_MIN_BYTES = 10 * 1024 * 1024  # ignore noise on small/fresh databases


def _db_diagnostics(database: Path, project_id: str) -> dict[str, Any]:
    """Read-only residual-state detail for one project database: SQLite
    freelist pages, embedding-storage footprint, foreign-owned document rows,
    and WAL-starvation. Detection only — never VACUUMs, checkpoints, or deletes.
    """
    diagnostics: dict[str, Any] = {
        "freelist_pages": None,
        "freelist_bytes": None,
        "page_count": None,
        "page_size": None,
        "embedding": {
            "chunks_embedded": 0,
            "chunks_null_embedding": 0,
            "embedding_bytes": 0,
        },
        "foreign_documents": 0,
        "wal": {"wal_bytes": 0, "db_bytes": 0, "starved": False},
    }
    if not database.is_file():
        return diagnostics

    connection = _readonly_connection(database)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        page_count = connection.execute("PRAGMA page_count").fetchone()[0]
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        freelist = connection.execute("PRAGMA freelist_count").fetchone()[0]
        diagnostics["page_count"] = int(page_count)
        diagnostics["page_size"] = int(page_size)
        diagnostics["freelist_pages"] = int(freelist)
        diagnostics["freelist_bytes"] = int(freelist) * int(page_size)

        if "bc_chunks" in tables:
            row = connection.execute(
                "SELECT "
                "SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN embedding IS NULL THEN 1 ELSE 0 END), "
                "SUM(LENGTH(embedding)) "
                "FROM bc_chunks"
            ).fetchone()
            diagnostics["embedding"] = {
                "chunks_embedded": int(row[0] or 0),
                "chunks_null_embedding": int(row[1] or 0),
                "embedding_bytes": int(row[2] or 0),
            }
        if "bc_documents" in tables:
            row = connection.execute(
                "SELECT COUNT(*) FROM bc_documents WHERE project_id != ?",
                (project_id,),
            ).fetchone()
            diagnostics["foreign_documents"] = int(row[0] or 0)
    finally:
        connection.close()

    wal_path = database.with_name(database.name + "-wal")
    wal_bytes = wal_path.stat().st_size if wal_path.is_file() else 0
    db_bytes = database.stat().st_size
    diagnostics["wal"] = {
        "wal_bytes": wal_bytes,
        "db_bytes": db_bytes,
        "starved": bool(
            wal_bytes > _WAL_STARVATION_MIN_BYTES
            and wal_bytes > db_bytes * _WAL_STARVATION_RATIO
        ),
    }
    return diagnostics


def _storage_impact(
    database: Path, diagnostics: dict[str, Any]
) -> dict[str, Any]:
    """Report conservative local disk needs without creating a snapshot.

    Snapshot size includes the live database and its WAL.  Compaction is
    intentionally budgeted at twice that source size so the future workflow
    has room for a retained same-host copy and temporary SQLite work.  These
    are disk figures, not a misleading prediction of RAM usage.
    """
    wal = diagnostics["wal"]
    source_bytes = int(wal["db_bytes"]) + int(wal["wal_bytes"])
    try:
        usage = shutil.disk_usage(database.parent)
        filesystem: dict[str, int | None] = {
            "total_bytes": int(usage.total),
            "used_bytes": int(usage.used),
            "free_bytes": int(usage.free),
        }
        free_bytes: int | None = int(usage.free)
    except OSError:
        filesystem = {"total_bytes": None, "used_bytes": None, "free_bytes": None}
        free_bytes = None

    snapshot_bytes = source_bytes
    compaction_bytes = source_bytes * 2
    return {
        "filesystem": filesystem,
        "local_snapshot": {
            "estimated_retained_bytes": snapshot_bytes,
            "fits_available_space": (
                None if free_bytes is None else snapshot_bytes <= free_bytes
            ),
        },
        "compaction": {
            "conservative_temporary_bytes": compaction_bytes,
            "fits_available_space": (
                None if free_bytes is None else compaction_bytes <= free_bytes
            ),
            "estimated_reclaimable_bytes": diagnostics["freelist_bytes"],
        },
        "memory_estimate_bytes": None,
        "memory_notice": (
            "RAM use cannot be reliably estimated from stored bytes; local "
            "snapshots consume disk space, while SQLite and the operating "
            "system decide runtime memory use."
        ),
    }


def referenced_backup_paths(database: Path) -> frozenset[str]:
    """Snapshot paths referenced by undo/operation history, as resolved strings.

    Fail closed: when the database exists but its operation history cannot be
    read, raise ``RetentionRefusedError`` — without the history there is no way
    to prove which snapshots are protected, so none may be deleted.
    """
    if not database.is_file():
        return frozenset()
    try:
        connection = _readonly_connection(database)
        try:
            rows = connection.execute(
                "SELECT backup_path FROM bc_operations WHERE backup_path IS NOT NULL"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RetentionRefusedError(
            f"retention refused: undo history in {database} is unreadable "
            f"({exc}); cannot prove which snapshots are protected"
        ) from exc
    return frozenset(
        str(Path(str(row[0])).expanduser().resolve()) for row in rows if row[0]
    )


def _history_expiry_plan(
    database: Path,
    *,
    expire_operations_days: int | None,
    expire_tombstones_days: int | None,
) -> dict[str, Any]:
    """Plan (never execute) row-level history expiry for one project database.

    Tombstone protection is deliberately conservative: a tombstoned note
    referenced by ANY recorded operation is protected until that operation's
    history row has itself been expired in an EARLIER apply — expiring both in
    one run never unprotects mid-run.
    """
    plan: dict[str, Any] = {
        "expire_operations_days": expire_operations_days,
        "expire_tombstones_days": expire_tombstones_days,
        "operations": [],
        "tombstoned_notes": [],
        "protected_notes": [],
    }
    if expire_operations_days is None and expire_tombstones_days is None:
        return plan
    if not database.is_file():
        return plan
    try:
        connection = _readonly_connection(database)
        try:
            if expire_operations_days is not None:
                plan["operations"] = [
                    {"op_id": int(row[0]), "created_at": row[1], "backup_path": row[2]}
                    for row in connection.execute(
                        "SELECT id, created_at, backup_path FROM bc_operations "
                        "WHERE datetime(created_at) < datetime('now', ?) "
                        "ORDER BY id",
                        (f"-{int(expire_operations_days)} days",),
                    )
                ]
            if expire_tombstones_days is not None:
                referenced_notes = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT DISTINCT note_id FROM bc_operation_notes"
                    )
                }
                for row in connection.execute(
                    "SELECT id FROM memory_notes "
                    "WHERE status='tombstoned' AND deleted_at IS NOT NULL "
                    "AND datetime(deleted_at) < datetime('now', ?) "
                    "ORDER BY id",
                    (f"-{int(expire_tombstones_days)} days",),
                ):
                    note_id = int(row[0])
                    if note_id in referenced_notes:
                        plan["protected_notes"].append(note_id)
                    else:
                        plan["tombstoned_notes"].append(note_id)
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RetentionRefusedError(
            f"retention refused: history in {database} is unreadable ({exc}); "
            "refusing to plan expiry against unprovable state"
        ) from exc
    return plan


def storage_report(
    project_id: str,
    *,
    keep_backups: int | None = None,
    backup_roots: Sequence[Path] = (),
    expire_operations_days: int | None = None,
    expire_tombstones_days: int | None = None,
) -> dict[str, Any]:
    """Account for namespace files and optionally plan retention.

    The plan is informational only — nothing is deleted here. Curated notes,
    documents, and files are never planned for deletion; backup candidates
    referenced by undo/operation history are listed as protected, not as
    candidates.
    """
    if keep_backups is not None and keep_backups < 0:
        raise ValueError("keep_backups must be >= 0")
    for name, days in (
        ("expire_operations_days", expire_operations_days),
        ("expire_tombstones_days", expire_tombstones_days),
    ):
        if days is not None and days < 0:
            raise ValueError(f"{name} must be >= 0")
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

    database = config.get_db_path(project_id)
    removal_candidates: list[dict[str, Any]] = []
    protected: list[dict[str, Any]] = []
    if keep_backups is not None:
        referenced = referenced_backup_paths(database)
        by_directory: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            if entry["category"] == "backups":
                by_directory.setdefault(str(Path(entry["path"]).parent), []).append(entry)
        for candidates in by_directory.values():
            candidates.sort(
                key=lambda item: (item["mtime_ns"], item["path"]), reverse=True
            )
            for item in candidates[keep_backups:]:
                if item["path"] in referenced:
                    protected.append(item)
                else:
                    removal_candidates.append(item)

    history_plan = _history_expiry_plan(
        database,
        expire_operations_days=expire_operations_days,
        expire_tombstones_days=expire_tombstones_days,
    )

    from .project_registry import find_orphans

    diagnostics = _db_diagnostics(database, project_id)
    return {
        "namespace_root": str(root),
        "scanned_roots": [str(item) for item in roots],
        "project_id": project_id,
        "project_database": str(database),
        "project_rows": _row_counts(database),
        "database_diagnostics": diagnostics,
        "storage_impact": _storage_impact(database, diagnostics),
        "orphans": find_orphans(),
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
            "protected": [
                {
                    "path": item["path"],
                    "bytes": item["bytes"],
                    "reason": "referenced-by-undo-history",
                }
                for item in protected
            ],
            "reclaimable_bytes": sum(item["bytes"] for item in removal_candidates),
            "history": history_plan,
        },
    }


def _apply_history_expiry(database: Path, history_plan: dict[str, Any]) -> dict[str, int]:
    """Delete the planned history rows in one immediate transaction.

    Mirrors ``SqliteStore.forget(hard=True)`` for each purged note: FTS entry
    first (external-content FTS5 re-reads the live row to un-index), dangling
    ``superseded_by`` pointers cleared, then the row; ``bc_operation_notes``
    and ``bc_note_links`` follow their parents via ON DELETE CASCADE.
    """
    op_ids = [item["op_id"] for item in history_plan["operations"]]
    note_ids = list(history_plan["tombstoned_notes"])
    if not op_ids and not note_ids:
        return {"operations_expired": 0, "notes_purged": 0}
    connection = sqlite3.connect(str(database))
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        for note_id in note_ids:
            try:
                connection.execute(
                    "DELETE FROM memory_fts WHERE rowid=?", (note_id,)
                )
            except sqlite3.OperationalError:
                pass  # FTS5 absent in this sqlite3 build — non-fatal
            connection.execute(
                "UPDATE memory_notes SET superseded_by=NULL, "
                "status=CASE WHEN status='superseded' THEN 'active' ELSE status END "
                "WHERE superseded_by=?",
                (note_id,),
            )
            connection.execute(
                "DELETE FROM memory_notes WHERE id=? AND status='tombstoned'",
                (note_id,),
            )
        for op_id in op_ids:
            connection.execute("DELETE FROM bc_operations WHERE id=?", (op_id,))
        connection.commit()
    except sqlite3.Error as exc:
        connection.rollback()
        raise RetentionRefusedError(
            f"retention apply failed mid-history-expiry and rolled back: {exc}"
        ) from exc
    finally:
        connection.close()
    return {"operations_expired": len(op_ids), "notes_purged": len(note_ids)}


def apply_retention(
    project_id: str,
    *,
    keep_backups: int | None = None,
    backup_roots: Sequence[Path] = (),
    expire_operations_days: int | None = None,
    expire_tombstones_days: int | None = None,
) -> dict[str, Any]:
    """Execute an explicitly configured retention plan for one project.

    Opt-in by construction: every axis defaults to disabled and a call with no
    axis configured is refused — there is no default retention age. The plan is
    recomputed fresh under the destination mutation lock and is the only source
    of deletions. Snapshots referenced by undo/operation history are never
    deleted (fail closed — a reference appearing between plan and delete aborts
    the apply), and curated memory is never touched. Row-level history expiry
    commits before any file is unlinked, so a snapshot unreferenced by THIS
    run's expiry only becomes a deletion candidate on a later run.
    """
    if (
        keep_backups is None
        and expire_operations_days is None
        and expire_tombstones_days is None
    ):
        raise RetentionRefusedError(
            "retention apply refused: no retention axis was configured; "
            "nothing is ever expired by default"
        )
    database = config.get_db_path(project_id)
    with mutation_lock(database, operation="storage-retention"):
        report = storage_report(
            project_id,
            keep_backups=keep_backups,
            backup_roots=backup_roots,
            expire_operations_days=expire_operations_days,
            expire_tombstones_days=expire_tombstones_days,
        )
        plan = report["retention_plan"]
        history_result = _apply_history_expiry(database, plan["history"])

        referenced = referenced_backup_paths(database)
        removed_files: list[str] = []
        removed_bytes = 0
        for item in plan["candidates"]:
            path = Path(item["path"])
            if str(path) in referenced or _category(path) != "backups":
                raise RetentionRefusedError(
                    f"retention apply refused at {path}: the file is protected "
                    "or is not a backup snapshot; no further deletion was "
                    "performed"
                )
            if path.is_file():
                path.unlink()
                removed_files.append(str(path))
                removed_bytes += int(item["bytes"])

    return {
        "applied": True,
        "project_id": project_id,
        "removed_backups": removed_files,
        "removed_bytes": removed_bytes,
        "operations_expired": history_result["operations_expired"],
        "notes_purged": history_result["notes_purged"],
        "protected": plan["protected"],
        "protected_notes": plan["history"]["protected_notes"],
        "plan": report,
    }
