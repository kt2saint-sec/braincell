# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
gui.py — BrainCell local web viewer (Phase K).

A thin FastAPI read-mostly viewer/manager over the brain.  Reuses
braincell.store, braincell.project_registry, braincell.embed, braincell.config,
and braincell.mode — ZERO new memory logic.

Usage:
    braincell gui [path] [--mode project|global] [--port 8765] [--allow-writes]

create_app() is a pure factory (no host/port knowledge) so tests can drive it
with fastapi.testclient.TestClient without starting a real server.
run_gui() wires uvicorn, always binding to 127.0.0.1 (localhost-only).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from . import embed_spec
from .embed import embed_query_async, embedder_status
from .gui_template import INDEX_HTML
from .install import claude_registered_map, registration_status
from .log import get as _get_log
from .mode import resolve_mode
from .project_registry import (
    add_family_members,
    load_families,
    load_path_registry,
    normalize_path,
    remove_family,
)
from .store import EmbedderMismatchError, SqliteStore

if TYPE_CHECKING:
    from .native_shell import NativeBridge

log = _get_log("braincell.gui")

# ── Write-endpoint Pydantic models (module-level for FastAPI schema gen) ──────

class _ForgetBody(BaseModel):
    note_id: int
    project: str


class _FamilyBody(BaseModel):
    action: str                  # "add" or "rm"
    name: str
    paths: Optional[list[str]] = None  # None / [] → remove entire family (rm)


class _PoolBody(BaseModel):
    family: Optional[str] = None
    all_projects: bool = False
    prune: bool = False


# ── App factory ───────────────────────────────────────────────────────────────

def create_app(
    *,
    db_path: Path,
    allow_writes: bool = False,
    auth_token: Optional[str] = None,
    cookie_name: str = "bc_gui_token",
    seed_project_id: Optional[str] = None,
    restart_argv: Optional[list[str]] = None,
    native_bridge: Optional["NativeBridge"] = None,
) -> FastAPI:
    """Build and return a FastAPI application backed by a SqliteStore on db_path.

    The store is opened (and schema-verified) in the FastAPI lifespan and closed
    on shutdown.  The application is host/port-agnostic — bind/serve is the
    caller's responsibility (run_gui uses uvicorn on 127.0.0.1).

    Args:
        db_path:      Absolute path to the braincell.db file to open.
        allow_writes: When False (default), write endpoints are not registered
                      and POST /api/forget and POST /api/family return 404.
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
        sched_task: Optional[asyncio.Task] = None
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

    def _split_projects(projects: str) -> Optional[list[str]]:
        """Split a comma-separated projects query param into a list or None."""
        parts = [p.strip() for p in projects.split(",") if p.strip()]
        return parts if parts else None

    def _open_for_view(
        request: Request, proj_filter: Optional[list[str]]
    ) -> tuple[SqliteStore, bool]:
        """Resolve which store serves this read → ``(store, close_after)``.

        The opened (launch) store, close_after=False, when: no filter; no launch
        seed (global-mode launch — the global db carries every project, the
        in-db ``projects=`` filter is correct); a multi-ULID filter (today's
        filter-the-opened-db behavior — the map never sends one); or the filter
        names the launch project itself.

        A single OTHER registered ULID in project mode → that sibling's own db,
        opened READ-ONLY (federation's recipe: ``mode=ro`` + ``query_only`` —
        the sibling is never written), close_after=True so the caller closes it
        per-request in a try/finally.  Unregistered, or registered with no db
        file yet → 404 whose detail says "not built" (the SPA maps it to the
        build-me empty state).
        """
        store = _store(request)
        if not proj_filter or seed_project_id is None or len(proj_filter) != 1:
            return store, False
        pid = proj_filter[0]
        if pid == seed_project_id:
            return store, False
        from .config import get_db_path
        if pid not in set(load_path_registry().values()):
            raise HTTPException(
                404, f"Project {pid!r} not built — not a registered project."
            )
        db = get_db_path(pid)
        if not db.exists():
            raise HTTPException(
                404, f"Project {pid!r} not built — no per-project brain exists yet."
            )
        return SqliteStore(db, read_only=True), True

    def _resolve_seed_param(seed: str) -> Optional[str]:
        """Validate an optional ``?seed=`` ULID against the path registry."""
        seed = seed.strip()
        if not seed:
            return None
        if seed != seed_project_id and seed not in set(load_path_registry().values()):
            raise HTTPException(
                404, f"Unknown seed project {seed!r} — not in the registry."
            )
        return seed

    def _open_seed_store(
        request: Request, view_seed: str
    ) -> tuple[SqliteStore, bool]:
        """Self-store for a federated query seeded at ``view_seed``.

        ``federated_recall``/``federated_search`` treat the target whose
        ``project_id == plan.seed_pid`` as *self* and query the PASSED store —
        so a non-launch seed must come with an RO-opened store of the seed's
        OWN db (reads only — safe), or the merge would silently read the launch
        db as the seed member.  close_after=True for that RO store; the launch
        seed reuses the opened store (close_after=False).
        """
        if view_seed == seed_project_id:
            return _store(request), False
        from .config import get_db_path
        db = get_db_path(view_seed)
        if not db.exists():
            raise HTTPException(
                404,
                f"Project {view_seed!r} not built — no per-project brain exists yet.",
            )
        return SqliteStore(db, read_only=True), True

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
        from .config import get_global_db_path
        store = _store(request)
        status = await store.ingest_status(None)
        global_db = get_global_db_path()
        try:
            embedder = await anyio.to_thread.run_sync(embedder_status)
        except Exception as exc:  # defensive — the probe itself never raises
            embedder = {
                "provider": embed_spec.PROVIDER, "model": embed_spec.MODEL,
                "dim": embed_spec.DIM, "reachable": False,
                "model_present": False, "ok": False,
                "detail": f"Embedder check failed: {exc!r}",
            }
        # MCP registration for the seed project. Global-mode launches have no
        # single project to check → path None, no clients (shape stays stable).
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
                except Exception:  # detection must never break /api/status
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
            "global_brain": {
                "exists": global_db.exists(),
                "path": str(global_db),
            },
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
        """SPA bootstrap config for the scope toggle.

        Exposes the launch *seed* project (if this GUI was started in project
        mode) so the SPA can enable the per-project + family scope views. Without
        a seed (global-mode launch) both are meaningless: ``federate_available``
        is False and the SPA keeps the scope on the namespace-wide "All" view.

        ``launch_project_id`` is the NAMINGS.md-canon alias of
        ``seed_project_id`` (the project the GUI was launched on — the one that
        owns the opened, write-capable store); ``seed_project_id`` is kept for
        compat.

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
        except Exception:  # best-effort signal — never break SPA bootstrap
            suggest_tour = False
        from .config import get_tour_seen_path
        return {
            "seed_project_id": seed_project_id,
            "launch_project_id": seed_project_id,
            "federate_available": seed_project_id is not None,
            "mode": resolve_mode(),
            "suggest_tour": suggest_tour,
            # Server-persisted first-run signal (POST /api/tour-seen sets it).
            # localStorage alone cannot carry this: the native window's renderer
            # profile is non-persistent, so a profile-local flag re-ambushes on
            # every native launch.
            "tour_seen": get_tour_seen_path().exists(),
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
        """All registered projects sorted by path, enriched with doc/chunk/note counts.

        Project-mode launches (a seed is set) enrich each SIBLING row from the
        sibling's own db, opened READ-ONLY per request — the opened store holds
        only the launch project, so its counts for siblings would always read
        zero.  A missing / corrupt sibling contributes zeros, never a 500
        (per-member isolation, mirroring ``resolve_federation_targets``).

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
        except Exception:  # detection must never break the map
            reg_map = {}
        if seed_project_id is not None:
            from .config import get_db_path
            for ulid in set(registry.values()):
                if ulid == seed_project_id:
                    continue
                db = get_db_path(ulid)
                if not db.exists():
                    continue  # not built yet — honest zeros
                try:
                    sibling = SqliteStore(db, read_only=True)
                    try:
                        counts[ulid] = (await sibling.project_counts()).get(ulid, {})
                    finally:
                        await sibling.aclose()
                except Exception as exc:  # corrupt sibling → zeros, never a 500
                    log.warning(
                        "projects: sibling %s counts skipped (%r)", ulid, exc
                    )
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

    @app.get("/api/families")
    async def api_families() -> list:  # type: ignore[type-arg]
        """All project families with per-member ULID resolution."""
        families = load_families()
        registry = load_path_registry()
        result = []
        for fname, members in sorted(families.items()):
            result.append({
                "name": fname,
                "members": [
                    {
                        "path": m,
                        "project_id": registry.get(normalize_path(m)),
                    }
                    for m in members
                ],
            })
        return result

    @app.get("/api/notes")
    async def api_notes(
        request: Request,
        q: str = "",
        projects: str = "",
        k: int = 20,
        federate: bool = False,
        seed: str = "",
    ) -> dict:  # type: ignore[type-arg]
        """Recall memory notes (recency/keyword when q empty; best-effort embed when q set).

        The embedder is never required: if it is unavailable the endpoint falls
        back to keyword/recency recall and includes a ``warning`` field.  It
        never returns 5xx because Ollama is down.

        ``projects=<single-other-registered-ulid>`` in project mode serves the
        SIBLING's real rows from its own db, opened read-only per request
        (``_open_for_view``).  ``seed=<ulid>`` (optional, registry-validated)
        re-seeds the federated family branch at the ACTIVE project instead of
        the launch project; ignored on the non-federated path.
        """
        proj_filter = _split_projects(projects)
        view_seed = _resolve_seed_param(seed)

        qvec = None
        warning: Optional[str] = None

        if q.strip():
            try:
                qvec = await embed_query_async(q)
            except Exception as exc:
                warning = (
                    f"Embedder unavailable; using keyword/recency fallback. ({exc!r})"
                )

        # Opt-in federated family recall: when the toggle is on, the flag is set,
        # and a seed is known (the launch project, or an explicit ?seed= active
        # project), fan out across that seed's family instead of querying only
        # the opened store.
        # The Memory Map is a HISTORY viewer, not an answer engine: it shows what a
        # project believed as well as what it believes now, so it opts into the
        # historical set and renders supersession as state rather than hiding it.
        from .federate import federated_recall, federation_enabled, plan_for_seed
        effective_seed = view_seed or seed_project_id
        if federate and federation_enabled() and effective_seed:
            plan = plan_for_seed(effective_seed)
            self_store, close_self = _open_seed_store(request, effective_seed)
            try:
                notes = await federated_recall(
                    self_store, plan, qvec, k, qtext=q, include_superseded=True,
                )
            finally:
                if close_self:
                    await self_store.aclose()
        else:
            view_store, close_view = _open_for_view(request, proj_filter)
            try:
                notes = await view_store.recall(
                    qvec, proj_filter, k, qtext=q, include_superseded=True,
                )
            finally:
                if close_view:
                    await view_store.aclose()
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

        ``projects=`` / ``seed=`` follow the same active-project contract as
        ``/api/notes``: a single OTHER registered ULID serves the sibling's own
        db read-only, and ``seed=<ulid>`` re-seeds the federated family branch.
        """
        if mode not in ("hybrid", "semantic", "keyword"):
            raise HTTPException(status_code=400, detail=f"Invalid mode {mode!r}.")
        if k < 1 or k > 100:
            raise HTTPException(status_code=400, detail="k must be 1–100.")

        proj_filter = _split_projects(projects)
        view_seed = _resolve_seed_param(seed)

        warning: Optional[str] = None
        effective_mode = mode

        try:
            qvec = await embed_query_async(q)
        except Exception as exc:
            # Embedder down: fall back to keyword-only.  A zero vector is safe
            # because _vector_search is never called in keyword mode.
            qvec = np.zeros(embed_spec.DIM, dtype=np.float32)
            effective_mode = "keyword"
            warning = (
                f"Embedder unavailable; using keyword fallback. ({exc!r})"
            )

        from .federate import federated_search, federation_enabled, plan_for_seed
        effective_seed = view_seed or seed_project_id
        if federate and federation_enabled() and effective_seed:
            plan = plan_for_seed(effective_seed)
            self_store, close_self = _open_seed_store(request, effective_seed)
            try:
                hits = await federated_search(
                    self_store, plan, qvec, q, k, effective_mode
                )
            finally:
                if close_self:
                    await self_store.aclose()
        else:
            view_store, close_view = _open_for_view(request, proj_filter)
            try:
                hits = await view_store.search(
                    qvec, q, proj_filter, k, effective_mode
                )
            finally:
                if close_view:
                    await view_store.aclose()
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
            projects=_split_projects(projects),
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

    # ── Write endpoints (only mounted when allow_writes=True) ─────────────────

    if allow_writes:

        @app.post("/api/forget")
        async def api_forget(request: Request, body: _ForgetBody) -> dict:  # type: ignore[type-arg]
            """Soft-delete a memory note by id + project."""
            store = _store(request)
            deleted = await store.forget(body.note_id, body.project)
            return {"deleted": deleted}

        @app.post("/api/family")
        async def api_family(body: _FamilyBody) -> dict:  # type: ignore[type-arg]
            """Add members to or remove a project family."""
            if body.action == "add":
                paths = body.paths or []
                if not paths:
                    raise HTTPException(
                        status_code=400,
                        detail="paths must be non-empty for action='add'.",
                    )
                add_family_members(body.name, paths)
                return {"ok": True, "action": "add", "name": body.name}
            elif body.action == "rm":
                # paths=None or [] → remove entire family
                paths_arg = body.paths if body.paths else None
                changed = remove_family(body.name, paths_arg)
                return {"ok": changed, "action": "rm", "name": body.name}
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown action {body.action!r}. Use 'add' or 'rm'.",
                )

        @app.post("/api/pool")
        async def api_pool(body: _PoolBody) -> dict:  # type: ignore[type-arg]
            """Pool per-project brains (a family, or all) into the global brain (no re-embed)."""
            from .config import get_global_db_path
            from .pool import PoolError, pool_into_global, resolve_pool_sources
            global_db = get_global_db_path()
            if not global_db.exists():
                raise HTTPException(
                    409, "No global brain yet. Run `braincell build --mode global` first."
                )
            if not body.family and not body.all_projects:
                raise HTTPException(
                    400, "Provide a family name or set all_projects=true."
                )
            try:
                sources, skipped = resolve_pool_sources(
                    family=body.family, include_all=body.all_projects
                )
            except KeyError:
                raise HTTPException(404, f"Family {body.family!r} not found.")
            if not sources:
                return {"pooled": [], "skipped": skipped}
            try:
                import anyio
                stats = await anyio.to_thread.run_sync(
                    lambda: pool_into_global(sources, global_db, prune=body.prune)
                )
            except PoolError as e:
                raise HTTPException(409, str(e))
            return {"pooled": [s.__dict__ for s in stats], "skipped": skipped}

        # Ingestion management (folder navigation / build jobs / clear / schedules).
        from .gui_ingest import IngestManager, mount_ingest_api
        app.state.ingest_manager = IngestManager()
        mount_ingest_api(
            app,
            db_path=db_path,
            manager=app.state.ingest_manager,
            pick_folder=(native_bridge.pick_folder if native_bridge is not None else None),
        )

        # MCP client install/uninstall + family-recall hook management.
        from .gui_install import mount_install_api
        mount_install_api(app, restart_argv=restart_argv)

        # Maintenance commands (consolidate/reflect/contradictions/reembed/
        # backup/memory log+undo) — the GUI counterparts of the remaining CLI.
        from .gui_ops import OpsJobManager, mount_ops_api
        app.state.ops_manager = OpsJobManager()
        mount_ops_api(app, db_path=db_path, manager=app.state.ops_manager)

    return app


# ── Server launcher (localhost-only) ──────────────────────────────────────────

def _resolve_gui_token() -> str:
    """Return the GUI auth token — durable across launches.

    Precedence: explicit ``BRAINCELL_GUI_TOKEN`` env override (ephemeral, NEVER
    written) > persisted per-namespace file > mint + persist (0600, atomic
    tmp-then-replace). Persisting means a GUI restart reuses the same token, so
    the embedded renderer can reauthenticate instead of being stranded on 401.
    Rotate via ``braincell gui --rotate-token``.
    """
    import os
    import secrets

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
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return token


def run_gui(
    *,
    mode: Optional[str],
    port: int,
    allow_writes: bool,
    path: str = ".",
    url_extra_query: Optional[str] = None,
    restart_command: str = "gui",
) -> None:
    """Resolve the brain, build the app, and run the native GUI.

    FastAPI remains bound to 127.0.0.1 as the private transport between the
    QtWebEngine renderer and the application API.

    Args:
        mode:         "project" | "global" | None (resolved from env / default).
        port:         TCP port to listen on (e.g. 8765).
        allow_writes: Mount write endpoints (POST /api/forget, /api/family, /api/pool).
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
    from .config import get_db_path, get_global_db_path, get_project_id

    if not native_shell.native_available():
        raise RuntimeError(
            "PySide6/QtWebEngine cannot open a native window in this session. "
            "Run BrainCell from a graphical desktop session."
        )

    m = resolve_mode(mode)
    if m == "global":
        db_path = get_global_db_path()
        project_id = None  # global brain has no single seed → GUI federation off
    else:
        project_id = get_project_id(Path(path).resolve())
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
            str(Path(path).resolve()), "--port", str(port),
        ]
        if m == "global":
            restart_argv.append("--global")
    else:
        restart_argv = [
            sys.executable, "-m", "braincell.cli", "gui", str(Path(path).resolve()),
            "--mode", m, "--port", str(port),
        ]
        if allow_writes:
            restart_argv.append("--allow-writes")

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

    log.info(
        "BrainCell GUI starting at %s  allow_writes=%s  auth=%s  db=%s",
        url, allow_writes, bool(auth_token), db_path,
    )
    native_shell.serve_native(
        app, port=port, url=open_url, bridge=native_bridge
    )


# ── Desktop launcher installer (A3, Linux XDG) ────────────────────────────────

_DESKTOP_ENTRY_TEMPLATE = """\
[Desktop Entry]
Type=Application
Name=BrainCell Map
Comment=Local memory map — projects, pools, and the global brain
Exec={exec}
Icon=braincell
Terminal=false
Categories=Development;Utility;
StartupNotify=true
"""


def _resolve_cli_exec() -> str:
    """Absolute path to the ``braincell`` console script for the .desktop Exec.

    A desktop environment launches ``.desktop`` entries with the *login/session*
    PATH, which almost never includes a project virtualenv's ``bin`` dir. A bare
    ``Exec=braincell …`` therefore fails silently (icon does nothing) whenever
    braincell is installed in a venv. Resolve an absolute path so the launcher
    works regardless of the session PATH; fall back to the bare name only if the
    script cannot be located.
    """
    import shutil
    import sys

    found = shutil.which("braincell")
    if found:
        return found
    sibling = Path(sys.executable).with_name("braincell")
    if sibling.exists():
        return str(sibling)
    return "braincell"  # last resort — bare name (relies on session PATH)


def _xdg_data_home() -> Path:
    """Resolve $XDG_DATA_HOME (falling back to ~/.local/share)."""
    import os
    raw = os.environ.get("XDG_DATA_HOME")
    return Path(raw) if raw else Path.home() / ".local" / "share"


_ICON_PNG_SIZES = (48, 128, 256, 512)


def install_launcher(project_path: Optional[Path] = None) -> tuple[Path, Path]:
    """Install the desktop icon + .desktop entry (idempotent). Returns (icon, desktop).

    ``project_path`` is the project folder the icon launches (default: cwd).
    The Exec line is ``braincell start "<project_path>"`` — the full one-command
    launcher (single-instance reuse, preflight, per-project GUI). It was
    ``braincell-map`` (the global-only viewer) before 2026-07-25; that opened an
    EMPTY map on machines with only per-project brains, which read as "the icon
    doesn't launch the GUI".

    Icons go into the XDG *hicolor* theme tree — the location GNOME/KDE actually
    resolve ``Icon=braincell`` from:
      ``$XDG_DATA_HOME/icons/hicolor/scalable/apps/braincell.svg``
      ``$XDG_DATA_HOME/icons/hicolor/<S>x<S>/apps/braincell.png`` (48/128/256/512)
    A legacy loose copy at ``$XDG_DATA_HOME/icons/braincell.svg`` is kept for
    DEs that scan that directory. Writes
    ``$XDG_DATA_HOME/applications/braincell-map.desktop`` — the FILENAME stays
    ``braincell-map.desktop`` on purpose: GNOME favorites pin the desktop-file
    id, so renaming it would silently unpin the icon. Then best-effort runs
    ``update-desktop-database`` + ``gtk-update-icon-cache`` so the entry and
    icon show up without a re-login.  Safe to re-run: files are overwritten,
    no duplicates.
    """
    from importlib.resources import files

    root = (project_path or Path.cwd()).resolve()

    data_home = _xdg_data_home()
    icons_dir = data_home / "icons"
    hicolor = icons_dir / "hicolor"
    apps_dir = data_home / "applications"
    apps_dir.mkdir(parents=True, exist_ok=True)

    assets = files("braincell").joinpath("assets")
    svg_bytes = assets.joinpath("braincell.svg").read_bytes()

    # hicolor theme tree (the reliable lookup path for Icon=braincell)
    scalable = hicolor / "scalable" / "apps"
    scalable.mkdir(parents=True, exist_ok=True)
    (scalable / "braincell.svg").write_bytes(svg_bytes)
    for size in _ICON_PNG_SIZES:
        png = assets.joinpath(f"braincell-{size}.png")
        try:
            png_bytes = png.read_bytes()
        except FileNotFoundError:  # pragma: no cover - defensive (partial install)
            continue
        size_dir = hicolor / f"{size}x{size}" / "apps"
        size_dir.mkdir(parents=True, exist_ok=True)
        (size_dir / "braincell.png").write_bytes(png_bytes)

    # legacy loose copy (some DEs scan $XDG_DATA_HOME/icons directly)
    icons_dir.mkdir(parents=True, exist_ok=True)
    icon_dst = icons_dir / "braincell.svg"
    icon_dst.write_bytes(svg_bytes)

    desktop_dst = apps_dir / "braincell-map.desktop"
    # Quote both parts (Desktop Entry spec quoting) so venv/project paths with
    # spaces survive Desktop Entry argument splitting.
    exec_line = f'"{_resolve_cli_exec()}" start "{root}"'
    desktop_dst.write_text(
        _DESKTOP_ENTRY_TEMPLATE.format(exec=exec_line), encoding="utf-8"
    )

    # Refresh menu + icon caches — best effort; absent on minimal distros.
    import shutil
    import subprocess
    for cmd in (
        ["update-desktop-database", str(apps_dir)],
        ["gtk-update-icon-cache", "-f", "-t", str(hicolor)],
    ):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, check=False, capture_output=True)
            except OSError as exc:  # pragma: no cover - environment dependent
                log.warning("%s failed (non-fatal): %s", cmd[0], exc)
        else:
            log.warning(
                "%s not found — launcher installed; the app menu/icon may need "
                "a manual refresh or re-login.", cmd[0],
            )
    return icon_dst, desktop_dst
