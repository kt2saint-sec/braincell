# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
gui.py — FastAPI application behind the native Memory Map.

Not a browser product: the app is served on 127.0.0.1 exclusively to the
native Memory Map shell (``native_shell.py``). The historical "local web
viewer (Phase K)" framing is retired.

A thin FastAPI read-mostly viewer/manager over the brain.  Reuses
braincell.store, braincell.project_registry, braincell.embed, braincell.config,
and braincell.mode — ZERO new memory logic.

Usage:
    braincell gui [path] [--port 8765] [--allow-writes]

create_app() is a pure factory (no host/port knowledge) so tests can drive it
with fastapi.testclient.TestClient without starting a real server.
run_gui() wires uvicorn, always binding to 127.0.0.1 (localhost-only).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict

from . import embed_spec
from .embed import embed_query_async, embedder_status
from .gui_template import INDEX_HTML
from .install import claude_registered_map, registration_status
from .log import get as _get_log
from .mode import resolve_mode
from .platform import install_launcher  # noqa: F401 — re-exported for tests
from .project_registry import (
    add_to_pool,
    create_pool,
    decouple_from_pool,
    delete_pool,
    load_path_registry,
    load_pools,
    pools_for_project,
    reassociate_project_path,
)
from .store import EmbedderMismatchError, SqliteStore

if TYPE_CHECKING:
    from .native_shell import NativeBridge

log = _get_log("braincell.gui")

# ── Write-endpoint Pydantic models (module-level for FastAPI schema gen) ──────

class _ForgetBody(BaseModel):
    note_id: int
    project: str


class _LivePoolQueryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pool: str
    query: str = ""
    k: int = 10
    rank: str = "hybrid"


class _PoolMembershipBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    name: str
    project_ids: list[str] | None = None
    project_id: str | None = None


class _ProjectReassociateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    new_path: str
    acknowledge_home: bool = False
    acknowledge_non_git: bool = False
    allow_privileged: bool = False


class _MaintenancePreferencesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bypass_delete_confirmation: bool
    acknowledgement: str | None = None


# ── App factory ───────────────────────────────────────────────────────────────

def create_app(
    *,
    db_path: Path,
    allow_writes: bool = False,
    auth_token: str | None = None,
    cookie_name: str = "bc_gui_token",
    seed_project_id: str | None = None,
    restart_argv: list[str] | None = None,
    native_bridge: NativeBridge | None = None,
) -> FastAPI:
    """Build and return a FastAPI application backed by a SqliteStore on db_path.

    The store is opened (and schema-verified) in the FastAPI lifespan and closed
    on shutdown.  The application is host/port-agnostic — bind/serve is the
    caller's responsibility (run_gui uses uvicorn on 127.0.0.1).

    Args:
        db_path:      Absolute path to the braincell.db file to open.
        allow_writes: When False (default), write endpoints are not registered
                      and write-only Pool membership endpoints return 404.
                      When True, the write endpoints are mounted.
        auth_token:   When set (A4), all ``/api/*`` requests must present a
                      matching ``t`` query param or ``X-BrainCell-Token`` header
                      or get a 401.  Default None = no auth (behaviour unchanged).
    Returns:
        A configured FastAPI app ready for use with TestClient or uvicorn.
    """

    @asynccontextmanager
    async def _lifespan(app: FastAPI):  # type: ignore[type-arg]
        import asyncio

        store = SqliteStore(db_path)
        try:
            store.assert_schema_version()
        except EmbedderMismatchError as exc:
            # Permanent config-level failure: log one clean, actionable line
            # before Starlette's traceback noise.
            log.error("FATAL: %s", exc)
            raise
        log.info("BrainCell GUI store opened: %s", db_path)
        app.state.store = store
        # Scheduled ingestion runs only while the GUI server is up (local tool).
        sched_task: asyncio.Task | None = None
        if allow_writes:
            from .gui_ingest import scheduler_loop
            sched_task = asyncio.ensure_future(scheduler_loop(app.state.ingest_manager))
        try:
            yield
        finally:
            if sched_task is not None:
                sched_task.cancel()
            # A dying GUI must not leave a build subprocess running (it holds
            # the SQLite write lock). Graceful path here; hard parent death is
            # covered by the child's PR_SET_PDEATHSIG (gui_ingest).
            ingest = getattr(app.state, "ingest_manager", None)
            if ingest is not None:
                await ingest.shutdown()
            await store.aclose()
            log.info("BrainCell GUI store closed")

    app = FastAPI(title="BrainCell GUI", lifespan=_lifespan)
    app.state.seed_project_id = seed_project_id

    # ── A4: optional shared-secret guard on all /api/* routes ─────────────────
    if auth_token:
        import secrets as _secrets

        @app.middleware("http")
        async def _require_token(request: Request, call_next):  # type: ignore[no-untyped-def]
            if request.url.path.startswith("/api/"):
                # Accept the token from (in precedence order) an explicit ?t=,
                # the X-BrainCell-Token header, or the durable auth COOKIE that
                # GET / sets. The cookie lets a restarted embedded renderer
                # authenticate with no URL state instead of stranding the SPA on
                # a 401 (which used to render as an empty "wiped" map).
                # Explicit ?t=/header still win so env-token curl
                # scripting and a correct ?t= recovering from a stale cookie both
                # keep working. Constant-time compare avoids a timing oracle.
                supplied = (
                    request.query_params.get("t")
                    or request.headers.get("X-BrainCell-Token")
                    or request.cookies.get(cookie_name)
                )
                if not (supplied and _secrets.compare_digest(supplied, auth_token)):
                    return JSONResponse(
                        {"detail": "Unauthorized (bad or missing BrainCell token)."},
                        status_code=401,
                    )
            return await call_next(request)

    # ── Helper ────────────────────────────────────────────────────────────────

    def _store(request: Request) -> SqliteStore:
        return request.app.state.store  # type: ignore[return-value]

    def _split_projects(projects: str) -> list[str] | None:
        """Split a comma-separated projects query param into a list or None."""
        parts = [p.strip() for p in projects.split(",") if p.strip()]
        return parts if parts else None

    def _normal_project_filter(request: Request, projects: str, operation: str) -> list[str] | None:
        """Return the connected Project filter for an ordinary GUI operation.

        A Memory Map owns one already-open Project store. Cross-Project reads
        are available only through the explicit named-Pool endpoints below.
        """
        selected = _split_projects(projects)
        connected = getattr(request.app.state, "seed_project_id", None)
        if connected is None:  # isolated unit-test factory compatibility
            return selected
        if selected and selected != [connected]:
            raise HTTPException(
                400,
                f"{operation} reads only the connected Project. Use an explicit named Pool for cross-project reads.",
            )
        return [connected]

    def _reject_retired_cross_project_query(
        *, federate: bool, seed: str, operation: str
    ) -> None:
        if federate or seed.strip():
            raise HTTPException(
                400,
                f"{operation} is project-only. Use Search Pool or Recall from Pool with an explicit Pool name.",
            )

    # ── Read endpoints ────────────────────────────────────────────────────────

    @app.get("/")
    async def index(request: Request):  # type: ignore[no-untyped-def]
        # Durable-cookie auth survives an embedded-renderer restart. GET / hands
        # the renderer the server's own token as an HttpOnly, SameSite=Strict
        # cookie, so subsequent /api/* calls authenticate with no token in the
        # visible URL.
        #
        # Posture (user-approved): the token still guards /api/*; only the
        # localhost PAGE navigation self-heals. Safe because the server binds
        # 127.0.0.1 only and the token is a same-user 0600 on-disk secret already
        # readable by this user's processes. HttpOnly keeps it out of SPA JS
        # (no XSS exfil); SameSite=Strict blocks cross-site sends; no Secure flag
        # because Chromium does not send Secure cookies over plain HTTP loopback.
        if not auth_token:
            return HTMLResponse(content=INDEX_HTML)
        # If a ?t= rode in (first launch, an old bookmark, a stale link), strip it
        # to a clean URL — the cookie carries auth now, so the token no longer
        # needs to live in the visible address or screenshots.
        if request.query_params.get("t") is not None:
            from urllib.parse import urlencode
            params = dict(request.query_params)
            params.pop("t", None)
            clean = "/?" + urlencode(params) if params else "/"
            resp: Response = RedirectResponse(url=clean, status_code=302)
        else:
            resp = HTMLResponse(content=INDEX_HTML)
        resp.set_cookie(
            key=cookie_name, value=auth_token, max_age=30 * 24 * 3600, path="/",
            httponly=True, samesite="strict",  # no secure= on http loopback
        )
        return resp

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        """Serve the packaged icon requested by the embedded renderer.

        Single source of truth: the same ``braincell/assets`` the desktop
        launcher installs from.
        """
        from importlib.resources import files
        try:
            ico = files("braincell").joinpath("assets", "braincell.ico").read_bytes()
        except FileNotFoundError:  # pragma: no cover — partial install
            raise HTTPException(404, "icon asset missing")
        return Response(content=ico, media_type="image/x-icon")

    @app.get("/api/status")
    async def api_status(request: Request) -> dict:  # type: ignore[type-arg]
        """Aggregate ingest status + active mode/db path + embedder/MCP health.

        ``embedder`` is the read-only probe (embed.embedder_status — never loads
        a model into VRAM); ``mcp`` is the read-only registration detection for
        the launch *seed* project. Both are best-effort: a down embedder or an
        unreadable client config yields a failure-shaped field, never a 5xx.
        """
        import anyio
        store = _store(request)
        status = await store.ingest_status(None)
        try:
            embedder = await anyio.to_thread.run_sync(embedder_status)
        except Exception as exc:  # noqa: BLE001  # Status must remain available when an external probe fails.
            embedder = {
                "provider": embed_spec.PROVIDER, "model": embed_spec.MODEL,
                "dim": embed_spec.DIM, "reachable": False,
                "model_present": False, "ok": False,
                "detail": f"Embedder check failed: {exc!r}",
            }
        # MCP registration for the connected Project.
        mcp: dict = {"path": None, "clients": []}
        if seed_project_id is not None:
            registry = load_path_registry()
            seed_path = next(
                (p for p, u in registry.items() if u == seed_project_id), None
            )
            if seed_path is not None:
                mcp["path"] = seed_path
                try:
                    reg = await anyio.to_thread.run_sync(
                        lambda: registration_status(Path(seed_path))
                    )
                    mcp["clients"] = [
                        {"client": name, "scope": str(info.get("scope") or "")}
                        for name, info in reg.items()
                        if info.get("registered")
                    ]
                except Exception:  # noqa: BLE001, S110  # Optional client-config detection must not break status.
                    pass
        return {
            "indexed": status.indexed,
            "doc_count": status.doc_count,
            "chunk_count": status.chunk_count,
            "last_ingest_ts": status.last_ingest_ts,
            "head_sha": status.head_sha,
            "stale": status.stale,
            "mode": resolve_mode(),
            "db_path": str(db_path),
            "allow_writes": allow_writes,
            "embedder": embedder,
            "mcp": mcp,
        }

    @app.post("/api/activate")
    async def api_activate() -> dict:  # type: ignore[type-arg]
        """Raise and focus the existing native window."""
        if native_bridge is None or not native_bridge.activate():
            raise HTTPException(409, "Native window is not ready.")
        return {"ok": True}

    @app.get("/api/config")
    async def api_config(request: Request) -> dict:  # type: ignore[type-arg]
        """SPA bootstrap config for the connected Project.

        ``suggest_tour`` is the server-side first-run signal (same predicate as
        `braincell start`'s ``tour=1`` handoff): an empty launch brain and no
        OTHER registered project — the seed itself is minted at launch, so it
        never counts against "first run". Lets a direct lower-level GUI launch
        (no ``?tour=1``) still get the SPA's own first-run prompt.
        """
        suggest_tour = False
        try:
            status = await _store(request).ingest_status(None)
            others = [
                u for u in load_path_registry().values() if u != seed_project_id
            ]
            suggest_tour = status.doc_count == 0 and not others
        except Exception:  # noqa: BLE001  # Best-effort bootstrap data must never break the native map.
            suggest_tour = False
        from .config import get_tour_seen_path
        return {
            "seed_project_id": seed_project_id,
            "connected_project_id": seed_project_id,
            "mode": resolve_mode(),
            "suggest_tour": suggest_tour,
            # Server-persisted first-run signal (POST /api/tour-seen sets it).
            # localStorage alone cannot carry this: the native window's renderer
            # profile is non-persistent, so a profile-local flag re-ambushes on
            # every native launch.
            "tour_seen": get_tour_seen_path().exists(),
        }

    @app.get("/api/maintenance/overview")
    async def api_maintenance_overview(request: Request) -> dict:  # type: ignore[type-arg]
        """Read Connected-Project storage health for the Memory Map.

        This route intentionally exposes no candidate selection or execution.
        It is available in read-only launches because understanding local disk
        impact must not require permission to change memory.
        """
        import sqlite3

        from .maintenance_preferences import (
            MaintenancePreferencesError,
            load_preferences,
        )
        from .storage_accounting import RetentionRefusedError, storage_report

        project_id = _connected_pool_project(request)
        try:
            report = storage_report(project_id)
            preferences = load_preferences(project_id)
        except (
            MaintenancePreferencesError,
            RetentionRefusedError,
            OSError,
            sqlite3.Error,
        ) as exc:
            raise HTTPException(409, f"Maintenance health is unavailable: {exc}") from exc
        return {
            "connected_project_id": project_id,
            "database_diagnostics": report["database_diagnostics"],
            "storage_impact": report["storage_impact"],
            "preferences": preferences,
        }

    @app.post("/api/tour-seen")
    async def api_tour_seen() -> dict:  # type: ignore[type-arg]
        """Mark the guided tour as seen (completed OR skipped) — machine-level.

        Mounted unconditionally like /api/feed: it is a UX flag, not a memory
        write, and it is token-gated by the /api/* middleware like everything
        else. Idempotent (touch)."""
        from .config import get_tour_seen_path
        path = get_tour_seen_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        return {"ok": True, "tour_seen": True}

    @app.get("/api/projects")
    async def api_projects(request: Request) -> list:  # type: ignore[type-arg]
        """Registered Project metadata with counts from the connected store only.

        The path registry may list known Projects for intentional Pool
        membership management, but this normal map route never opens a sibling
        database.

        ``mcp_registered`` is the Claude-client registration summary per path,
        computed from ONE ``~/.claude.json`` read for all cells
        (install.claude_registered_map) — detection failure degrades to False,
        never a 500.
        """
        import anyio
        registry = load_path_registry()
        store = _store(request)
        counts = await store.project_counts()
        reg_paths = list(registry.keys())
        try:
            reg_map = await anyio.to_thread.run_sync(
                lambda: claude_registered_map(reg_paths)
            )
        except Exception:  # noqa: BLE001  # Optional client-config discovery must not break the map.
            reg_map = {}
        return sorted(
            [
                {
                    "project_id": ulid,
                    "path": path,
                    "docs": counts.get(ulid, {}).get("docs", 0),
                    "chunks": counts.get(ulid, {}).get("chunks", 0),
                    "notes": counts.get(ulid, {}).get("notes", 0),
                    "mcp_registered": bool(reg_map.get(path, False)),
                }
                for path, ulid in registry.items()
            ],
            key=lambda x: x["path"],
        )

    @app.get("/api/notes")
    async def api_notes(
        request: Request,
        q: str = "",
        projects: str = "",
        k: int = 20,
        federate: bool = False,
        seed: str = "",
    ) -> dict:  # type: ignore[type-arg]
        """Recall memory notes from the connected Project only.

        The embedder is never required: if it is unavailable the endpoint falls
        back to keyword/recency recall and includes a ``warning`` field.  It
        never returns 5xx because Ollama is down.

        A named Pool is the only cross-Project recall surface. Legacy selector
        flags are rejected rather than inferred.
        """
        _reject_retired_cross_project_query(
            federate=federate, seed=seed, operation="Recall"
        )
        proj_filter = _normal_project_filter(request, projects, "Recall")

        qvec = None
        warning: str | None = None

        if q.strip():
            try:
                qvec = await embed_query_async(q)
            except Exception as exc:  # noqa: BLE001  # Embedder failures deliberately use keyword fallback.
                warning = (
                    f"Embedder unavailable; using keyword/recency fallback. ({exc!r})"
                )

        notes = await _store(request).recall(
            qvec, proj_filter, k, qtext=q, include_superseded=True,
        )
        return {
            "notes": [
                {
                    "id": n.id,
                    "project_id": n.project_id,
                    "scope": n.scope,
                    "kind": n.kind,
                    "content": n.content,
                    "tags": n.tags,
                    "confidence": n.confidence,
                    "source_hint": n.source_hint,
                    "superseded_by": n.superseded_by,
                    "created_at": n.created_at,
                    "retrieval_origin": n.retrieval_origin,
                    "resolved_from": n.resolved_from,
                }
                for n in notes
            ],
            "warning": warning,
        }

    @app.get("/api/search")
    async def api_search(
        request: Request,
        q: str,
        projects: str = "",
        k: int = 10,
        mode: str = "hybrid",
        federate: bool = False,
        seed: str = "",
    ) -> dict:  # type: ignore[type-arg]
        """Hybrid search over ingested document chunks.

        Best-effort embed: when the embedder is down the endpoint falls back to
        keyword mode and includes a ``warning`` field in the 200 response rather
        than raising a 5xx.

        A named Pool is the only cross-Project search surface. Legacy selector
        flags are rejected rather than inferred.
        """
        if mode not in ("hybrid", "semantic", "keyword"):
            raise HTTPException(status_code=400, detail=f"Invalid mode {mode!r}.")
        if k < 1 or k > 100:
            raise HTTPException(status_code=400, detail="k must be 1–100.")

        _reject_retired_cross_project_query(
            federate=federate, seed=seed, operation="Search"
        )
        proj_filter = _normal_project_filter(request, projects, "Search")

        warning: str | None = None
        effective_mode = mode

        try:
            qvec = await embed_query_async(q)
        except Exception as exc:  # noqa: BLE001  # Embedder failures deliberately use keyword fallback.
            # Embedder down: fall back to keyword-only.  A zero vector is safe
            # because _vector_search is never called in keyword mode.
            qvec = np.zeros(embed_spec.DIM, dtype=np.float32)
            effective_mode = "keyword"
            warning = (
                f"Embedder unavailable; using keyword fallback. ({exc!r})"
            )

        hits = await _store(request).search(
            qvec, q, proj_filter, k, effective_mode
        )
        return {
            "hits": [
                {
                    "chunk_id": h.chunk_id,
                    "doc_key": h.doc_key,
                    "title": h.title,
                    "snippet": h.snippet,
                    "score": round(h.score, 6),
                    "cosine": round(h.cosine, 4) if h.cosine is not None else None,
                    "fts_matched": h.fts_matched,
                    "source_path": h.source_path,
                }
                for h in hits
            ],
            "warning": warning,
        }

    @app.get("/api/feed")
    async def api_feed(
        request: Request,
        after_note: int = 0,
        after_doc: int = 0,
        k: int = 30,
        projects: str = "",
    ) -> dict:  # type: ignore[type-arg]
        """Incremental activity feed: notes/documents past an id cursor + build job.

        Mounted unconditionally (read view; the token middleware still gates it
        under /api/*). ``job`` reflects the ingest manager when one exists —
        read-only launches (no manager) always report ``null``.
        """
        store = _store(request)
        k = min(max(k, 1), 50)
        data = await store.tail_since(
            note_after=after_note,
            doc_after=after_doc,
            projects=_normal_project_filter(request, projects, "Feed"),
            limit=k,
        )
        job = None
        manager = getattr(request.app.state, "ingest_manager", None)
        if manager is not None and manager.job is not None:
            j = manager.job
            job = {
                "state": j.state,
                "path": j.path,
                # The build subprocess emits no machine-readable progress yet;
                # 0/0 = indeterminate. getattr keeps the contract keys stable
                # if IngestJob later grows real counters.
                "done": int(getattr(j, "done", 0) or 0),
                "total": int(getattr(j, "total", 0) or 0),
            }
        return {**data, "job": job}

    @app.get("/api/pools")
    async def api_pools(request: Request) -> dict:  # type: ignore[type-arg]
        """Return passive Pool membership metadata, never memory rows."""
        pools = load_pools()
        connected = getattr(request.app.state, "seed_project_id", None)
        return {
            "pools": [
                {
                    "name": name,
                    "project_ids": list(members),
                    "connected": bool(connected and name in pools_for_project(connected)),
                }
                for name, members in pools.items()
            ],
            "connected_project_id": connected,
        }

    def _connected_pool_project(request: Request) -> str:
        project_id = getattr(request.app.state, "seed_project_id", None)
        if not project_id:
            raise HTTPException(409, "Pool actions require a connected Project session.")
        return project_id

    def _member_statuses(plan) -> list[dict[str, str]]:  # type: ignore[no-untyped-def]
        return [
            {"project_id": item.project_id, "status": item.status, "detail": item.detail}
            for item in plan.member_status
        ]

    @app.post("/api/pools/search")
    async def api_pool_search(
        request: Request, body: _LivePoolQueryBody
    ) -> dict:  # type: ignore[type-arg]
        """Search exactly one named Pool through read-only member stores."""
        from .federate import federated_search, plan_for_pool

        connected = _connected_pool_project(request)
        try:
            plan = plan_for_pool(body.pool, connected)
        except (KeyError, ValueError) as exc:
            raise HTTPException(403, str(exc)) from exc
        k = min(max(body.k, 1), 100)
        try:
            qvec = await embed_query_async(body.query)
            mode = body.rank if body.rank in {"hybrid", "semantic", "keyword"} else "hybrid"
        except Exception as exc:  # noqa: BLE001  # Pool search uses keyword fallback when embeddings are unavailable.
            qvec = np.zeros(embed_spec.DIM, dtype=np.float32)
            mode = "keyword"
            warning = f"Embedder unavailable; using keyword fallback. ({exc!r})"
        else:
            warning = None
        hits = await federated_search(None, plan, qvec, body.query, k, mode)
        return {
            "pool": body.pool,
            "connected_project_id": connected,
            "warning": warning,
            "member_status": _member_statuses(plan),
            "hits": [
                {
                    "chunk_id": hit.chunk_id,
                    "doc_key": hit.doc_key,
                    "title": hit.title,
                    "snippet": hit.snippet,
                    "score": round(hit.score, 6),
                    "source_path": hit.source_path,
                }
                for hit in hits
            ],
        }

    @app.post("/api/pools/recall")
    async def api_pool_recall(
        request: Request, body: _LivePoolQueryBody
    ) -> dict:  # type: ignore[type-arg]
        """Recall exactly one named Pool through read-only member stores."""
        from .federate import federated_recall, plan_for_pool

        connected = _connected_pool_project(request)
        try:
            plan = plan_for_pool(body.pool, connected)
        except (KeyError, ValueError) as exc:
            raise HTTPException(403, str(exc)) from exc
        try:
            qvec = await embed_query_async(body.query) if body.query.strip() else None
        except Exception:  # noqa: BLE001  # Pool recall deliberately falls back to lexical/recency results.
            qvec = None
        notes = await federated_recall(
            None, plan, qvec, min(max(body.k, 1), 100), qtext=body.query
        )
        return {
            "pool": body.pool,
            "connected_project_id": connected,
            "member_status": _member_statuses(plan),
            "notes": [
                {"id": note.id, "project_id": note.project_id, "kind": note.kind, "content": note.content}
                for note in notes
            ],
        }

    # ── Write endpoints (only mounted when allow_writes=True) ─────────────────

    # The skills catalog is safe in read-only mode. It resolves only this
    # launched window's Connected Project and never opens a memory database.
    from .gui_install import mount_skill_status_api
    mount_skill_status_api(app)

    if allow_writes:

        @app.get("/api/preferences/maintenance")
        async def api_maintenance_preferences(request: Request) -> dict:  # type: ignore[type-arg]
            """Read destructive-maintenance confirmation settings for this Project."""
            from .maintenance_preferences import (
                MaintenancePreferencesError,
                load_preferences,
            )

            project_id = _connected_pool_project(request)
            try:
                return load_preferences(project_id)
            except MaintenancePreferencesError as exc:
                raise HTTPException(409, str(exc)) from exc

        @app.put("/api/preferences/maintenance")
        async def api_set_maintenance_preferences(
            request: Request, body: _MaintenancePreferencesBody
        ) -> dict:  # type: ignore[type-arg]
            """Change only the Connected Project's typed-delete bypass setting."""
            from .maintenance_preferences import (
                MaintenancePreferencesError,
                set_bypass_delete_confirmation,
            )

            project_id = _connected_pool_project(request)
            try:
                return set_bypass_delete_confirmation(
                    project_id,
                    body.bypass_delete_confirmation,
                    acknowledgement=body.acknowledgement,
                )
            except MaintenancePreferencesError as exc:
                raise HTTPException(409, str(exc)) from exc

        @app.post("/api/pools")
        async def api_pool_membership(
            request: Request, body: _PoolMembershipBody
        ) -> dict:  # type: ignore[type-arg]
            """Change Pool membership metadata only; never open a memory database."""
            _connected_pool_project(request)
            try:
                if body.action == "create":
                    pools = create_pool(body.name)
                elif body.action == "add":
                    if not body.project_ids:
                        raise HTTPException(400, "Add to Pool requires project_ids.")
                    add_to_pool(body.name, body.project_ids)
                    pools = load_pools()
                elif body.action == "decouple":
                    if not body.project_id:
                        raise HTTPException(400, "Decouple from Pool requires project_id.")
                    decouple_from_pool(body.name, body.project_id)
                    pools = load_pools()
                elif body.action == "delete":
                    delete_pool(body.name)
                    pools = load_pools()
                else:
                    raise HTTPException(400, "Use create, add, decouple, or delete.")
            except (KeyError, ValueError, RuntimeError) as exc:
                raise HTTPException(409, str(exc)) from exc
            return {"ok": True, "pools": pools}

        @app.post("/api/projects/reassociate")
        async def api_project_reassociate(
            body: _ProjectReassociateBody,
        ) -> dict:  # type: ignore[type-arg]
            """Update one stable Project's current path; never touch its database."""
            from .project_target import ProjectTargetError, validate_project_target

            try:
                target = validate_project_target(
                    body.new_path,
                    acknowledge_home=body.acknowledge_home,
                    acknowledge_non_git=body.acknowledge_non_git,
                    allow_privileged=body.allow_privileged,
                )
                old_path, new_path = reassociate_project_path(
                    body.project_id, target.path
                )
            except (ProjectTargetError, KeyError, ValueError) as exc:
                raise HTTPException(409, str(exc)) from exc
            return {
                "ok": True,
                "project_id": body.project_id,
                "old_path": str(old_path),
                "new_path": str(new_path),
                "memory_unchanged": True,
                "pool_memberships_unchanged": True,
            }

        @app.post("/api/forget")
        async def api_forget(request: Request, body: _ForgetBody) -> dict:  # type: ignore[type-arg]
            """Soft-delete a memory note in the connected Project only."""
            connected = getattr(request.app.state, "seed_project_id", None)
            if connected is not None and body.project != connected:
                raise HTTPException(409, "Forget writes only to the connected Project.")
            deleted = await _store(request).forget(body.note_id, body.project)
            return {"deleted": deleted}

        # Ingestion management (folder navigation / build jobs / clear / schedules).
        from .gui_ingest import IngestManager, mount_ingest_api
        from .gui_mutation import GuiMutationCoordinator
        app.state.mutation_coordinator = GuiMutationCoordinator()
        app.state.ingest_manager = IngestManager(app.state.mutation_coordinator)
        mount_ingest_api(
            app,
            db_path=db_path,
            manager=app.state.ingest_manager,
            connected_project_id=seed_project_id or "",
            coordinator=app.state.mutation_coordinator,
            pick_folder=(native_bridge.pick_folder if native_bridge is not None else None),
        )

        # Project-local client connection and Connected Project skill management.
        from .gui_install import mount_install_api
        mount_install_api(app, restart_argv=restart_argv)

        # Maintenance commands (consolidate/reflect/contradictions/reembed/
        # backup/memory log+undo) — the GUI counterparts of the remaining CLI.
        from .gui_ops import OpsJobManager, mount_ops_api
        app.state.ops_manager = OpsJobManager(app.state.mutation_coordinator)
        mount_ops_api(
            app,
            db_path=db_path,
            manager=app.state.ops_manager,
            connected_project_id=seed_project_id or "",
            coordinator=app.state.mutation_coordinator,
        )

    return app


# ── Server launcher (localhost-only) ──────────────────────────────────────────

def _windows_restrict_token_acl(path: Path) -> None:
    """Restrict *path* to the current user only via ``icacls``.

    Windows has no POSIX chmod semantics: ``os.chmod(path, 0o600)`` there only
    toggles the FILE_ATTRIBUTE_READONLY flag, never actual ACL permissions, so
    it does nothing to stop another account on the same machine from reading
    the token. ``icacls`` (stdlib-only — a subprocess call, not a new
    dependency) removes inherited permissions and grants full control to only
    the current user.

    Fail-closed: any failure here — a missing ``USERNAME``, a non-NTFS volume
    (FAT32/exFAT have no ACLs at all), ``icacls`` itself erroring — is
    surfaced as a loud warning rather than silently leaving the token at
    whatever ACL it inherited. This does not delete the token or block the
    GUI from starting: the token already lives under the user's own config
    directory, which carries a reasonably-scoped default ACL on Windows (real
    exposure is a custom or relocated data root — see BUGS.md), so a failed
    hardening attempt is a defense-in-depth gap to report, not a reason to
    refuse to serve the GUI at all.
    """
    import os as _os
    import subprocess

    domain = _os.environ.get("USERDOMAIN", "")
    username = _os.environ.get("USERNAME", "")
    if not username:
        log.warning(
            "Cannot restrict the GUI token's ACL at %s: USERNAME is not set "
            "in the environment — it may be readable by other accounts on "
            "this machine.", path,
        )
        return
    account = f"{domain}\\{username}" if domain else username
    result = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"{account}:F"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        log.warning(
            "icacls failed to restrict the GUI token at %s to %s (exit %s): "
            "%s — it may be readable by other accounts on this machine.",
            path, account, result.returncode, result.stderr.strip(),
        )


def _resolve_gui_token() -> str:
    """Return the GUI auth token — durable across launches.

    Precedence: explicit ``BRAINCELL_GUI_TOKEN`` env override (ephemeral, NEVER
    written) > persisted per-namespace file > mint + persist (atomic
    tmp-then-replace, restricted to the current user only — POSIX/macOS via
    ``os.chmod(0o600)``, Windows via ``icacls`` in `_windows_restrict_token_acl`
    since ``os.chmod`` there cannot express real ACL permissions). Persisting
    means a GUI restart reuses the same token, so the embedded renderer can
    reauthenticate instead of being stranded on 401. Rotate via
    ``braincell gui --rotate-token``.
    """
    import os
    import secrets
    import sys

    from .config import get_gui_token_path

    env = os.environ.get("BRAINCELL_GUI_TOKEN")
    if env:
        return env
    path = get_gui_token_path()
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(token, encoding="utf-8")
    if sys.platform == "win32":
        _windows_restrict_token_acl(tmp)
    else:
        os.chmod(tmp, 0o600)
    tmp.replace(path)
    return token


def run_gui(
    *,
    mode: str | None,
    port: int,
    allow_writes: bool,
    path: str = ".",
    url_extra_query: str | None = None,
    restart_command: str = "gui",
    acknowledge_home: bool = False,
    acknowledge_non_git: bool = False,
    allow_privileged: bool = False,
) -> None:
    """Resolve the brain, build the app, and run the native GUI.

    FastAPI remains bound to 127.0.0.1 as the private transport between the
    QtWebEngine renderer and the application API.

    Args:
        mode:         ``project`` or None. Other modes are rejected.
        port:         TCP port to listen on (e.g. 8765).
        allow_writes: Mount connected-Project write endpoints and Pool membership controls.
        path:         Project root for project-mode db resolution (default cwd).
        url_extra_query: Extra query string appended to the window URL only
                      (e.g. ``"tour=1"`` — `braincell start`'s first-run
                      handoff). Never part of restart_argv, so a GUI restart
                      does not re-trigger it.
        restart_command: ``"start"`` for the full interactive launcher or
                      ``"gui"`` for the lower-level command.
    """
    import sys

    from . import native_shell
    from .config import get_db_path, get_project_id

    unavailable = native_shell.native_unavailable_reason()
    if unavailable:
        raise RuntimeError(unavailable)

    resolve_mode(mode)
    project_root = Path(path).resolve()
    project_id = get_project_id(project_root)
    db_path = get_db_path(project_id)

    # Gate the namespace-wide API even though it is localhost-only. The first
    # QtWebEngine navigation carries the token; GET / stores it as an HttpOnly
    # same-origin cookie and strips it from the visible URL.
    auth_token = _resolve_gui_token()

    url = f"http://127.0.0.1:{port}"
    if auth_token:
        url += f"/?t={auth_token}"
    open_url = url
    if url_extra_query:
        open_url += ("&" if "?" in open_url else "/?") + url_extra_query

    # POST /api/restart replaces the complete process, including the Qt window.
    if restart_command == "start":
        restart_argv = [
            sys.executable, "-m", "braincell.cli", "start",
            str(project_root), "--port", str(port),
        ]
    else:
        restart_argv = [
            sys.executable, "-m", "braincell.cli", "gui", str(project_root),
            "--port", str(port),
        ]
        if allow_writes:
            restart_argv.append("--allow-writes")
    if acknowledge_home:
        restart_argv.append("--acknowledge-home")
    if acknowledge_non_git:
        restart_argv.append("--acknowledge-non-git")
    if allow_privileged:
        restart_argv.append("--allow-privileged")

    native_bridge = native_shell.NativeBridge()
    app = create_app(
        db_path=db_path,
        allow_writes=allow_writes,
        auth_token=auth_token,
        # Cookies are host-scoped, not port-scoped. Key by port so concurrent
        # native instances do not share credentials.
        cookie_name=f"bc_gui_{port}",
        seed_project_id=project_id,
        restart_argv=restart_argv,
        native_bridge=native_bridge,
    )

    # Never log the tokened URL — anyone with the log sink could replay the
    # bearer token against token-gated routes, including writes.
    log.info(
        "BrainCell GUI starting at http://127.0.0.1:%s  allow_writes=%s  auth=%s  db=%s",
        port, allow_writes, bool(auth_token), db_path,
    )
    native_shell.serve_native(
        app, port=port, url=open_url, bridge=native_bridge
    )
