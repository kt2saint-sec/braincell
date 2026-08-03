# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
gui_ops.py — maintenance-command endpoints for the Memory-Map GUI.

Write-gated endpoints mounted by gui.create_app(allow_writes=True):

  POST /api/ops/consolidate    find + (opt-in) merge near-duplicate notes
  POST /api/ops/reflect        LLM-synthesize higher-level notes from clusters
  POST /api/ops/contradictions read-only LLM audit of embedding-close notes
  POST /api/ops/reembed-notes  backfill NULL note embeddings
  POST /api/ops/hard-prune/plan  evidence-led permanent-cleanup preview
  POST /api/ops/hard-prune/apply digest-gated cleanup + compaction job
  GET  /api/ops/status         poll the current/last maintenance job
  POST /api/backup             VACUUM INTO snapshot of the opened brain
  GET  /api/memory             list recorded merge operations (memory log)
  POST /api/memory/undo        reverse a recorded merge operation

These are the GUI counterparts of `braincell consolidate` / `reflect` /
`contradictions` / `reembed-notes` / `backup` / `memory log|undo` — each
endpoint reuses the exact core function its CLI command calls (never a CLI
subprocess). The long-running / LLM-invoking ops follow the /api/ingest
background-job + status-polling pattern: one job at a time (409 when busy),
run in a worker thread with its OWN SqliteStore on the same db (WAL handles
the concurrent reader), stdout captured as the job log. Destructive applies
keep the CLI's discipline: pre-merge VACUUM INTO backup + one undoable
bc_operations record.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from .embed import embed_texts
from .gui_mutation import GuiMutationCoordinator
from .log import get as _get_log
from .store import SqliteStore

log = _get_log("braincell.gui_ops")

_LOG_TAIL = 200  # lines of job log kept/returned (mirrors gui_ingest)


# ── Request bodies (closed shapes only, extra=forbid like gui_install) ────────

class ConsolidateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    threshold: float = 0.9
    apply: bool = False
    llm: bool = False


class ReflectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    threshold: float = 0.85
    since_days: int | None = None
    apply: bool = False
    model: str | None = None


class ContradictionsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    threshold: float | None = None
    limit: int = 50
    no_llm: bool = False
    model: str | None = None


class ReembedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str


class UndoBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op_id: int
    project_id: str


class HardPrunePlanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    keep_backups: int | None = None
    backup_roots: list[str] = []
    expire_operations_days: int | None = None
    expire_tombstones_days: int | None = None


class HardPruneApplyBody(HardPrunePlanBody):
    approval_digest: str
    confirmation_phrase: str | None = None
    create_local_snapshot: bool = False


# ── Background job manager (one maintenance op at a time) ─────────────────────

@dataclass
class OpsJob:
    name: str
    state: str = "running"            # running | done | error
    log: list[str] = field(default_factory=list)
    result: dict | None = None     # structured outcome (op-specific)
    started: float = field(default_factory=time.time)
    finished: float | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "log": self.log[-_LOG_TAIL:],
            "result": self.result,
            "started": self.started,
            "finished": self.finished,
        }


class _LineWriter:
    """File-like sink appending complete lines to a job's log list."""

    def __init__(self, sink: list[str]) -> None:
        self._sink = sink
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._sink.append(line)
            del self._sink[:-_LOG_TAIL or None]
        return len(s)

    def flush(self) -> None:  # pragma: no cover - protocol completeness
        if self._buf:
            self._sink.append(self._buf)
            self._buf = ""


class OpsJobManager:
    """Runs one maintenance worker at a time in a thread; keeps the last job."""

    def __init__(self, coordinator: GuiMutationCoordinator | None = None) -> None:
        self.job: OpsJob | None = None
        self._task: asyncio.Task | None = None
        self._coordinator = coordinator or GuiMutationCoordinator()

    @property
    def busy(self) -> bool:
        return self.job is not None and self.job.state == "running"

    async def start(self, name: str, worker: Callable[[], dict | None]) -> OpsJob:
        if self.busy:
            raise RuntimeError("A maintenance job is already running.")
        self._coordinator.claim(f"maintenance:{name}")
        job = OpsJob(name=name)
        self.job = job
        self._task = asyncio.ensure_future(self._run(job, worker))
        return job

    async def _run(self, job: OpsJob, worker: Callable[[], dict | None]) -> None:
        try:
            job.result = await asyncio.to_thread(self._captured, job, worker)
            job.state = "done"
        except Exception as exc:  # noqa: BLE001 — worker failure — never crash the GUI
            job.log.append(f"{job.name} failed: {exc!r}")
            job.state = "error"
        finally:
            job.finished = time.time()
            self._coordinator.release(f"maintenance:{job.name}")

    @staticmethod
    def _captured(job: OpsJob, worker: Callable[[], dict | None]) -> dict | None:
        """Run *worker* with stdout captured into the job log.

        redirect_stdout swaps the process-wide sys.stdout, but only one job runs
        at a time and the server itself logs via `logging` (stderr), so the only
        prints during the window are the worker's own — the same lines the CLI
        command shows.
        """
        w = _LineWriter(job.log)
        with redirect_stdout(w):  # type: ignore[arg-type]
            out = worker()
        w.flush()
        return out


# ── Workers — each reuses the SAME core function its CLI command calls ────────
#
# Every worker opens its OWN store (fresh asyncio.run loop in the thread; the
# app's aiosqlite store must never be driven from a second loop) and closes it.

def run_consolidate(
    db_path: Path, project_id: str, threshold: float, apply: bool, llm: bool,
) -> dict | None:
    """`braincell consolidate` core (cli._consolidate_async + auto-backup)."""
    from .cli import _consolidate_async, _required_auto_backup

    backup: str | None = None

    def create_backup() -> str:
        nonlocal backup
        backup = _required_auto_backup(db_path, "consolidate")
        return backup

    store = SqliteStore(db_path)
    store.assert_schema_version()
    try:
        asyncio.run(_consolidate_async(
            store, project_id, threshold=threshold, apply=apply,
            use_llm=llm, verbose=False,
            backup_factory=create_backup if apply else None,
        ))
    finally:
        store.close()
    return {"backup": backup, "applied": apply}


def run_reflect(
    db_path: Path, project_id: str, threshold: float,
    since_days: int | None, apply: bool, model: str | None,
) -> dict | None:
    """`braincell reflect` core (reflect.reflect + auto-backup)."""
    from .cli import _required_auto_backup
    from .embed import embed_query_async
    from .reflect import reflect

    backup: str | None = None

    def create_backup() -> str:
        nonlocal backup
        backup = _required_auto_backup(db_path, "reflect")
        return backup

    store = SqliteStore(db_path)
    store.assert_schema_version()
    try:
        result = asyncio.run(reflect(
            store, project_id, threshold=threshold, since_days=since_days,
            apply=apply, model=model,
            embed_fn=embed_query_async if apply else None,
            verbose=False,
            backup_factory=create_backup if apply else None,
        ))
    finally:
        store.close()
    return {
        "backup": backup,
        "applied": apply,
        "clusters_considered": result.clusters_considered,
        "synthesized": result.synthesized,
        "skipped": result.skipped,
    }


def run_contradictions(
    db_path: Path, project_id: str, threshold: float | None,
    limit: int, no_llm: bool, model: str | None,
) -> dict | None:
    """`braincell contradictions` core — READ-ONLY by design (no apply exists)."""
    from .contradictions import find_contradictions, ollama_judge, print_report

    judge = None
    if not no_llm:
        def judge(a: str, b: str) -> bool:
            return ollama_judge(a, b, model=model)

    store = SqliteStore(db_path)
    store.assert_schema_version()
    try:
        report = asyncio.run(find_contradictions(
            store, project_id, threshold=threshold, limit=limit, judge_fn=judge,
        ))
    finally:
        store.close()
    print_report(report, verbose=False)
    return {
        "notes_scanned": report.notes_scanned,
        "pairs_over_threshold": report.pairs_over_threshold,
        "pairs_judged": report.pairs_judged,
        "pairs": [
            {
                "id_a": p.id_a, "id_b": p.id_b,
                "cosine": round(p.cosine, 4), "verdict": p.verdict,
            }
            for p in report.pairs
        ],
    }


def run_reembed_notes(db_path: Path, project_id: str) -> dict | None:
    """`braincell reembed-notes` core (store.reembed_notes + embed_texts)."""
    store = SqliteStore(db_path)
    store.assert_schema_version()
    try:
        count = asyncio.run(store.reembed_notes(project_id, embed_texts))
    finally:
        store.close()
    print(f"Re-embedded {count} notes.")
    return {"reembedded": count}


def run_hard_prune(body: HardPruneApplyBody) -> dict[str, object]:
    """Run the digest-gated core workflow with the GUI's 15-second reader wait."""
    from .storage_accounting import execute_hard_prune

    result = execute_hard_prune(
        body.project_id,
        approval_digest=body.approval_digest,
        confirmation_phrase=body.confirmation_phrase,
        create_local_snapshot=body.create_local_snapshot,
        keep_backups=body.keep_backups,
        backup_roots=[Path(item) for item in body.backup_roots],
        expire_operations_days=body.expire_operations_days,
        expire_tombstones_days=body.expire_tombstones_days,
        wait_for_readers_seconds=15.0,
        allow_trusted_bypass=True,
    )
    print(
        "Hard-prune complete: "
        f"{result['retention']['notes_purged']} tombstoned notes, "  # type: ignore[index]
        f"{result['retention']['operations_expired']} operation rows, "  # type: ignore[index]
        f"{len(result['retention']['removed_backups'])} backup files."  # type: ignore[index]
    )
    compaction = result["compaction"]  # type: ignore[assignment]
    if compaction["status"] == "reader-blocked":  # type: ignore[index]
        print(
            "Compaction is pending: close Memory Map/MCP clients and retry a "
            "future hard-prune when no live reader holds the WAL."
        )
    elif compaction["status"] != "compacted":  # type: ignore[index]
        print(f"Compaction did not complete: {compaction.get('detail', compaction['status'])}")  # type: ignore[union-attr,index]
    return result


def _run_locked(
    db_path: Path, operation: str, worker: Callable[[], dict | None],
) -> dict | None:
    """Run a maintenance worker under the cross-process destination lock."""
    from .catalog_io import mutation_lock

    with mutation_lock(db_path, operation=operation):
        return worker()


# ── Route mounting (called by gui.create_app when allow_writes=True) ──────────

def mount_ops_api(
    app: FastAPI, *, db_path: Path, manager: OpsJobManager,
    connected_project_id: str,
    coordinator: GuiMutationCoordinator | None = None,
) -> None:
    """Register the maintenance-command routes on *app*."""

    mutation_coordinator = coordinator or manager._coordinator

    def _require_project(project_id: str) -> None:
        if not connected_project_id:  # isolated factory compatibility for unit tests
            from .project_registry import load_path_registry
            if project_id not in set(load_path_registry().values()):
                raise HTTPException(404, f"Unknown project {project_id!r}.")
            return
        if project_id != connected_project_id:
            raise HTTPException(409, "This operation is limited to the connected Project.")

    async def _start(name: str, worker: Callable[[], dict | None]) -> dict:
        try:
            job = await manager.start(name, worker)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return {"started": True, "job": job.as_dict()}

    @app.post("/api/ops/consolidate")
    async def api_ops_consolidate(body: ConsolidateBody) -> dict:  # type: ignore[type-arg]
        _require_project(body.project_id)
        return await _start(
            "consolidate",
            lambda: _run_locked(
                db_path,
                "maintenance:consolidate",
                lambda: run_consolidate(
                    db_path, body.project_id, body.threshold, body.apply, body.llm,
                ),
            ),
        )

    @app.post("/api/ops/reflect")
    async def api_ops_reflect(body: ReflectBody) -> dict:  # type: ignore[type-arg]
        _require_project(body.project_id)
        return await _start(
            "reflect",
            lambda: _run_locked(
                db_path,
                "maintenance:reflect",
                lambda: run_reflect(
                    db_path, body.project_id, body.threshold, body.since_days,
                    body.apply, body.model,
                ),
            ),
        )

    @app.post("/api/ops/contradictions")
    async def api_ops_contradictions(body: ContradictionsBody) -> dict:  # type: ignore[type-arg]
        _require_project(body.project_id)
        return await _start(
            "contradictions",
            lambda: _run_locked(
                db_path,
                "maintenance:contradictions",
                lambda: run_contradictions(
                    db_path, body.project_id, body.threshold, body.limit,
                    body.no_llm, body.model,
                ),
            ),
        )

    @app.post("/api/ops/reembed-notes")
    async def api_ops_reembed(body: ReembedBody) -> dict:  # type: ignore[type-arg]
        _require_project(body.project_id)
        return await _start(
            "reembed-notes",
            lambda: _run_locked(
                db_path,
                "maintenance:reembed-notes",
                lambda: run_reembed_notes(db_path, body.project_id),
            ),
        )

    @app.post("/api/ops/hard-prune/plan")
    async def api_hard_prune_plan(body: HardPrunePlanBody) -> dict:  # type: ignore[type-arg]
        """Preview only the connected Project's evidence-backed cleanup plan."""
        from .maintenance_preferences import load_preferences
        from .storage_accounting import RetentionRefusedError, hard_prune_plan

        _require_project(body.project_id)
        try:
            plan = hard_prune_plan(
                body.project_id,
                keep_backups=body.keep_backups,
                backup_roots=[Path(item) for item in body.backup_roots],
                expire_operations_days=body.expire_operations_days,
                expire_tombstones_days=body.expire_tombstones_days,
            )
            plan["preferences"] = load_preferences(body.project_id)
            return plan
        except (RetentionRefusedError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/ops/hard-prune/apply")
    async def api_hard_prune_apply(body: HardPruneApplyBody) -> dict:  # type: ignore[type-arg]
        """Start one digest-gated hard-prune under the shared GUI mutation gate."""
        _require_project(body.project_id)
        return await _start("hard-prune", lambda: run_hard_prune(body))

    @app.get("/api/ops/status")
    async def api_ops_status() -> dict:  # type: ignore[type-arg]
        return {"job": manager.job.as_dict() if manager.job else None}

    @app.post("/api/backup")
    async def api_backup() -> dict:  # type: ignore[type-arg]
        """VACUUM INTO snapshot of the opened brain — same as `braincell backup`."""
        from .cli import _vacuum_into
        if not db_path.exists():
            raise HTTPException(409, "No brain built yet — nothing to back up.")
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        dest = db_path.parent / f"braincell-backup-{ts}-{uuid.uuid4().hex[:8]}.db"
        import anyio
        try:
            await anyio.to_thread.run_sync(_vacuum_into, db_path, dest)
        except Exception as exc:  # noqa: BLE001 — disk full / locked — surface, never 500-trace
            raise HTTPException(409, f"Backup failed: {exc}")
        return {"ok": True, "path": str(dest)}

    @app.get("/api/memory")
    async def api_memory_log(
        request: Request, project_id: str, limit: int = 20,
    ) -> dict:  # type: ignore[type-arg]
        """List recorded merge operations — same as `braincell memory log`."""
        _require_project(project_id)
        store: SqliteStore = request.app.state.store
        ops = await store.list_operations(project_id, limit=limit)
        return {"operations": ops}

    @app.post("/api/memory/undo")
    async def api_memory_undo(request: Request, body: UndoBody) -> dict:  # type: ignore[type-arg]
        """Reverse a recorded merge operation — same as `braincell memory undo`."""
        _require_project(body.project_id)
        store: SqliteStore = request.app.state.store
        from .catalog_io import mutation_lock

        try:
            mutation_coordinator.claim("memory-undo")
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        try:
            with mutation_lock(db_path, operation="memory-undo"):
                try:
                    result = await store.undo_operation(body.op_id, body.project_id)
                except ValueError as exc:
                    raise HTTPException(409, str(exc)) from exc
        finally:
            mutation_coordinator.release("memory-undo")
        return {"ok": True, **result}
