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
from typing import Optional

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import embed_spec
from .embed import embed_query_async
from .gui_template import INDEX_HTML
from .log import get as _get_log
from .mode import resolve_mode
from .project_registry import (
    add_family_members,
    load_families,
    load_path_registry,
    normalize_path,
    remove_family,
)
from .store import SqliteStore

log = _get_log("braincell.gui")


# ── Browser-open helper (A1: schedule after the server is bound) ──────────────

def _schedule_browser_open(url: str, delay: float = 0.2) -> None:
    """Open *url* in a browser shortly after the caller returns.

    Called from the FastAPI lifespan (after the store is ready) so the opened
    tab always lands on a live, listening server — fixing the connection-refused
    race where ``webbrowser.open`` fired before uvicorn had bound the socket.

    When a running event loop is present (the normal serve path) the open is
    deferred with ``call_later`` so it happens just after the loop starts
    accepting connections.  With no running loop (defensive) it opens inline.
    """
    import asyncio
    import webbrowser

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        webbrowser.open(url)
        return
    loop.call_later(delay, webbrowser.open, url)


# ── Write-endpoint Pydantic models (module-level for FastAPI schema gen) ──────

class _ForgetBody(BaseModel):
    note_id: int
    project: str


class _FamilyBody(BaseModel):
    action: str                  # "add" or "rm"
    name: str
    paths: Optional[list[str]] = None  # None / [] → remove entire family (rm)


class _PoolBody(BaseModel):
    family: str


# ── App factory ───────────────────────────────────────────────────────────────

def create_app(
    *,
    db_path: Path,
    allow_writes: bool = False,
    auth_token: Optional[str] = None,
    open_browser_url: Optional[str] = None,
    seed_project_id: Optional[str] = None,
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
        open_browser_url: When set (A1), the lifespan schedules a single
                      ``webbrowser.open`` once the server is bound.  Default None
                      opens nothing (e.g. ``--no-browser`` / test / TestClient).

    Returns:
        A configured FastAPI app ready for use with TestClient or uvicorn.
    """

    @asynccontextmanager
    async def _lifespan(app: FastAPI):  # type: ignore[type-arg]
        import asyncio

        store = SqliteStore(db_path)
        store.assert_schema_version()
        log.info("BrainCell GUI store opened: %s", db_path)
        app.state.store = store
        # Scheduled ingestion runs only while the GUI server is up (local tool).
        sched_task: Optional[asyncio.Task] = None
        if allow_writes:
            from .gui_ingest import scheduler_loop
            sched_task = asyncio.ensure_future(scheduler_loop(app.state.ingest_manager))
        # A1: open the browser only now that the app is ready and (imminently)
        # bound — never before uvicorn is listening.
        if open_browser_url:
            _schedule_browser_open(open_browser_url)
        try:
            yield
        finally:
            if sched_task is not None:
                sched_task.cancel()
            await store.aclose()
            log.info("BrainCell GUI store closed")

    app = FastAPI(title="BrainCell GUI", lifespan=_lifespan)

    # ── A4: optional shared-secret guard on all /api/* routes ─────────────────
    if auth_token:

        @app.middleware("http")
        async def _require_token(request: Request, call_next):  # type: ignore[no-untyped-def]
            if request.url.path.startswith("/api/"):
                supplied = (
                    request.query_params.get("t")
                    or request.headers.get("X-BrainCell-Token")
                )
                if supplied != auth_token:
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

    # ── Read endpoints ────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(content=INDEX_HTML)

    @app.get("/api/status")
    async def api_status(request: Request) -> dict:  # type: ignore[type-arg]
        """Aggregate ingest status + active mode/db path."""
        from .config import get_global_db_path
        store = _store(request)
        status = await store.ingest_status(None)
        global_db = get_global_db_path()
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
        }

    @app.get("/api/config")
    async def api_config() -> dict:  # type: ignore[type-arg]
        """SPA bootstrap config for the scope toggle.

        Exposes the launch *seed* project (if this GUI was started in project
        mode) so the SPA can enable the per-project + family scope views. Without
        a seed (global-mode launch) both are meaningless: ``federate_available``
        is False and the SPA keeps the scope on the namespace-wide "All" view.
        """
        return {
            "seed_project_id": seed_project_id,
            "federate_available": seed_project_id is not None,
            "mode": resolve_mode(),
        }

    @app.get("/api/projects")
    async def api_projects(request: Request) -> list:  # type: ignore[type-arg]
        """All registered projects sorted by path, enriched with doc/chunk/note counts."""
        registry = load_path_registry()
        store = _store(request)
        counts = await store.project_counts()
        return sorted(
            [
                {
                    "project_id": ulid,
                    "path": path,
                    "docs": counts.get(ulid, {}).get("docs", 0),
                    "chunks": counts.get(ulid, {}).get("chunks", 0),
                    "notes": counts.get(ulid, {}).get("notes", 0),
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
    ) -> dict:  # type: ignore[type-arg]
        """Recall memory notes (recency/keyword when q empty; best-effort embed when q set).

        The embedder is never required: if it is unavailable the endpoint falls
        back to keyword/recency recall and includes a ``warning`` field.  It
        never returns 5xx because Ollama is down.
        """
        store = _store(request)
        proj_filter = _split_projects(projects)

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
        # and this GUI knows its seed project (project-mode launch), fan out across
        # the family's brains instead of querying only the opened store.
        # The Memory Map is a HISTORY viewer, not an answer engine: it shows what a
        # project believed as well as what it believes now, so it opts into the
        # historical set and renders supersession as state rather than hiding it.
        from .federate import federated_recall, federation_enabled, plan_for_seed
        if federate and federation_enabled() and seed_project_id:
            plan = plan_for_seed(seed_project_id)
            notes = await federated_recall(
                store, plan, qvec, k, qtext=q, include_superseded=True,
            )
        else:
            notes = await store.recall(
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
    ) -> dict:  # type: ignore[type-arg]
        """Hybrid search over ingested document chunks.

        Best-effort embed: when the embedder is down the endpoint falls back to
        keyword mode and includes a ``warning`` field in the 200 response rather
        than raising a 5xx.
        """
        if mode not in ("hybrid", "semantic", "keyword"):
            raise HTTPException(status_code=400, detail=f"Invalid mode {mode!r}.")
        if k < 1 or k > 100:
            raise HTTPException(status_code=400, detail="k must be 1–100.")

        store = _store(request)
        proj_filter = _split_projects(projects)

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
        if federate and federation_enabled() and seed_project_id:
            plan = plan_for_seed(seed_project_id)
            hits = await federated_search(store, plan, qvec, q, k, effective_mode)
        else:
            hits = await store.search(qvec, q, proj_filter, k, effective_mode)
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
            """Pool a family of per-project brains into the global brain (no re-embed)."""
            from .config import get_global_db_path
            from .pool import PoolError, pool_into_global, resolve_pool_sources
            global_db = get_global_db_path()
            if not global_db.exists():
                raise HTTPException(
                    409, "No global brain yet. Run `braincell build --mode global` first."
                )
            try:
                sources, skipped = resolve_pool_sources(family=body.family)
            except KeyError:
                raise HTTPException(404, f"Family {body.family!r} not found.")
            if not sources:
                return {"pooled": [], "skipped": skipped}
            try:
                import anyio
                stats = await anyio.to_thread.run_sync(pool_into_global, sources, global_db)
            except PoolError as e:
                raise HTTPException(409, str(e))
            return {"pooled": [s.__dict__ for s in stats], "skipped": skipped}

        # Ingestion management (folder browse / ingest jobs / clear / schedules).
        from .gui_ingest import IngestManager, mount_ingest_api
        app.state.ingest_manager = IngestManager()
        mount_ingest_api(app, db_path=db_path, manager=app.state.ingest_manager)

        # MCP client install/uninstall + family-recall hook management.
        from .gui_install import mount_install_api
        mount_install_api(app)

        # Maintenance commands (consolidate/reflect/contradictions/reembed/
        # backup/memory log+undo) — the GUI counterparts of the remaining CLI.
        from .gui_ops import OpsJobManager, mount_ops_api
        app.state.ops_manager = OpsJobManager()
        mount_ops_api(app, db_path=db_path, manager=app.state.ops_manager)

    return app


# ── Server launcher (localhost-only) ──────────────────────────────────────────

def run_gui(
    *,
    mode: Optional[str],
    port: int,
    allow_writes: bool,
    open_browser: bool,
    path: str = ".",
) -> None:
    """Resolve db_path from mode/path, build the app, and serve on 127.0.0.1.

    The host is hardcoded to 127.0.0.1 — the GUI is a local tool and must
    never be exposed on a public interface.

    Args:
        mode:         "project" | "global" | None (resolved from env / default).
        port:         TCP port to listen on (e.g. 8765).
        allow_writes: Mount write endpoints (POST /api/forget, /api/family, /api/pool).
        open_browser: Call webbrowser.open() after starting the server.
        path:         Project root for project-mode db resolution (default cwd).
    """
    # Lazy imports: only needed when actually launching.
    import os
    import secrets

    from .config import get_db_path, get_global_db_path, get_project_id

    m = resolve_mode(mode)
    if m == "global":
        db_path = get_global_db_path()
        project_id = None  # global brain has no single seed → GUI federation off
    else:
        project_id = get_project_id(Path(path).resolve())
        db_path = get_db_path(project_id)

    # A4 + edge-#1 hardening: use an explicit BRAINCELL_GUI_TOKEN when set;
    # otherwise ALWAYS mint a per-launch token — read-only launches included. The
    # read endpoints enumerate every registered project (ULID + absolute path)
    # from the namespace-wide registry (/api/projects, /api/families), so even the
    # read-only API is gated to the launched tab rather than open to any local
    # process/tab. The opened URL carries ?t= and the SPA attaches it on each
    # call, so the one-click flow is unaffected; a manually-opened tab must copy
    # the token from the logged URL.
    auth_token = os.environ.get("BRAINCELL_GUI_TOKEN") or secrets.token_urlsafe(16)

    url = f"http://127.0.0.1:{port}"
    if auth_token:
        url += f"/?t={auth_token}"

    app = create_app(
        db_path=db_path,
        allow_writes=allow_writes,
        auth_token=auth_token,
        open_browser_url=(url if open_browser else None),
        seed_project_id=project_id,
    )

    log.info(
        "BrainCell GUI starting at %s  allow_writes=%s  auth=%s  db=%s",
        url, allow_writes, bool(auth_token), db_path,
    )

    import uvicorn  # lazy: not a hard dep for tests
    uvicorn.run(app, host="127.0.0.1", port=port)


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


def _resolve_map_exec() -> str:
    """Absolute path to the ``braincell-map`` console script for the .desktop Exec.

    A desktop environment launches ``.desktop`` entries with the *login/session*
    PATH, which almost never includes a project virtualenv's ``bin`` dir. A bare
    ``Exec=braincell-map`` therefore fails silently (icon does nothing) whenever
    braincell is installed in a venv. Resolve an absolute path so the launcher
    works regardless of the session PATH; fall back to the bare name only if the
    script cannot be located.
    """
    import shutil
    import sys

    found = shutil.which("braincell-map")
    if found:
        return found
    sibling = Path(sys.executable).with_name("braincell-map")
    if sibling.exists():
        return str(sibling)
    return "braincell-map"  # last resort — bare name (relies on session PATH)


def _xdg_data_home() -> Path:
    """Resolve $XDG_DATA_HOME (falling back to ~/.local/share)."""
    import os
    raw = os.environ.get("XDG_DATA_HOME")
    return Path(raw) if raw else Path.home() / ".local" / "share"


_ICON_PNG_SIZES = (48, 128, 256, 512)


def install_launcher() -> tuple[Path, Path]:
    """Install the desktop icon + .desktop entry (idempotent). Returns (icon, desktop).

    Icons go into the XDG *hicolor* theme tree — the location GNOME/KDE actually
    resolve ``Icon=braincell`` from:
      ``$XDG_DATA_HOME/icons/hicolor/scalable/apps/braincell.svg``
      ``$XDG_DATA_HOME/icons/hicolor/<S>x<S>/apps/braincell.png`` (48/128/256/512)
    A legacy loose copy at ``$XDG_DATA_HOME/icons/braincell.svg`` is kept for
    DEs that scan that directory. Writes
    ``$XDG_DATA_HOME/applications/braincell-map.desktop``, then best-effort runs
    ``update-desktop-database`` + ``gtk-update-icon-cache`` so the entry and
    icon show up without a re-login.  Safe to re-run: files are overwritten
    with identical content, no duplicates.
    """
    from importlib.resources import files

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
    desktop_dst.write_text(
        _DESKTOP_ENTRY_TEMPLATE.format(exec=_resolve_map_exec()), encoding="utf-8"
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
