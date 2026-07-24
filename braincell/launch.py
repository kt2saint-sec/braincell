# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
launch.py — `braincell start` preflight: single-instance probe + pre-launch report.

`braincell start` ≡ `braincell gui <path> --allow-writes` plus the three things
`gui` does not do: reuse an already-running map on the same port (instead of
dying on uvicorn "address already in use"), print a pre-launch report (embedder
health FIRST, then brain state and MCP registration — print-and-continue, never
a gate), and hand the SPA its first-run tour signal (`tour=1`).

Registration stays an explicit user action: preflight only READS client config
(install.registration_status) and never auto-registers or mutates anything —
not even a project id (run_gui mints that at launch).

Stdlib-only probe (urllib) — no new dependency.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import get_db_path, get_global_db_path, resolve_project_id_readonly
from .embed import embedder_status
from .install import registration_status
from .project_registry import load_path_registry


# ── Single-instance probe ─────────────────────────────────────────────────────

def probe_status(port: int, token: str, timeout: float = 1.0) -> Optional[dict]:
    """GET /api/status on 127.0.0.1:<port> with the persisted GUI token.

    Returns the parsed status dict on a 200 JSON-object response; None on ANY
    failure (connection refused, timeout, 401, non-JSON body — a foreign
    process on the port). None means "proceed to bind": uvicorn will error
    clearly if a non-braincell process actually holds the port.
    """
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/status",
        headers={"X-BrainCell-Token": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if getattr(resp, "status", None) != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — any failure = not a matching braincell GUI
        return None
    return data if isinstance(data, dict) else None


# ── Preflight ─────────────────────────────────────────────────────────────────

@dataclass
class Preflight:
    """Outcome of the pre-launch checks — cmd_start decides from ``action``."""

    action: str                          # "launch" | "reuse" | "conflict"
    first_run: bool = False
    report_lines: list[str] = field(default_factory=list)
    reuse_url: Optional[str] = None      # set when action == "reuse"
    conflict_db: Optional[str] = None    # the running server's db (conflict)
    expected_db: Optional[str] = None    # our target db path; None = unbuilt


def _doc_count(db: Path) -> Optional[int]:
    """COUNT(*) of bc_documents via a read-only stdlib sqlite3 open.

    Cheap enough for preflight (no SqliteStore / aiosqlite spin-up). None on
    any failure — preflight reports "?" rather than blocking the launch.
    """
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT COUNT(*) FROM bc_documents").fetchone()
            return int(row[0]) if row else None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — report-only helper, never blocks startup
        return None


def preflight(
    path: Path, *, mode: str = "project", port: int = 8765,
    probe_timeout: float = 1.0,
) -> Preflight:
    """Single-instance probe + pre-launch report for `braincell start`.

    Read-only and best-effort throughout: never mints a project id, never opens
    the store (a read-only sqlite3 count at most), never auto-registers, and a
    failing sub-check becomes a report line, not an abort.
    """
    from .gui import _resolve_gui_token  # lazy: gui.py pulls in fastapi

    resolved = Path(path).resolve()
    if mode == "global":
        pid: Optional[str] = None
        db: Optional[Path] = get_global_db_path()
    else:
        pid = resolve_project_id_readonly(resolved)
        db = get_db_path(pid) if pid else None

    # Probe BEFORE binding: a 200 with our db_path = this brain's GUI is
    # already up → reuse its tab instead of dying on "address already in use".
    # A 200 with a DIFFERENT db_path = another brain owns the port → refuse
    # (never silently open the wrong brain).
    token = _resolve_gui_token()
    running = probe_status(port, token, timeout=probe_timeout)
    if running is not None:
        running_db = str(running.get("db_path") or "")
        if db is not None and running_db == str(db):
            return Preflight(
                action="reuse",
                reuse_url=f"http://127.0.0.1:{port}/?t={token}",
                expected_db=str(db),
            )
        return Preflight(
            action="conflict",
            conflict_db=running_db or "(unknown)",
            expected_db=str(db) if db else None,
        )

    lines: list[str] = []
    # Embedder FIRST — the one prerequisite braincell cannot fix for the user.
    try:
        emb = embedder_status()
        if emb.get("ok"):
            lines.append(f"✓ Embedder ready: {emb['model']} ({emb['provider']})")
        else:
            lines.append(f"✗ Embedder not ready: {emb.get('detail') or 'unknown'}")
    except Exception as exc:  # noqa: BLE001 — print-and-continue
        lines.append(f"✗ Embedder check failed: {exc!r}")

    doc_count: Optional[int] = None
    if mode == "global":
        built = db is not None and db.exists()
        lines.append(f"Global brain: {db}" + ("" if built else " (not built yet)"))
        if built:
            doc_count = _doc_count(db)
    else:
        lines.append(f"Project folder: {resolved}")
        lines.append(
            f"  id: {pid}" if pid else "  id: (new — registered at launch)"
        )
        if db is not None and db.exists():
            doc_count = _doc_count(db)
            docs = "?" if doc_count is None else str(doc_count)
            lines.append(f"  brain: {db} ({docs} docs)")
        else:
            lines.append("  brain: not built yet — use Build in the map")
        # MCP registration — read-only report. NEVER auto-register (client
        # config mutation stays an explicit user action, like the hook).
        try:
            reg = registration_status(resolved)
            registered = [
                f"{name} ({info.get('scope')})"
                for name, info in reg.items() if info.get("registered")
            ]
            if registered:
                lines.append(f"  MCP: registered — {', '.join(registered)}")
            else:
                lines.append(
                    "  MCP: not registered — the map's Register MCP button "
                    "(or `braincell install`) wires it"
                )
        except Exception as exc:  # noqa: BLE001 — print-and-continue
            lines.append(f"  MCP: status unknown ({exc!r})")

    # First run = nothing to show yet: unregistered project / missing db, or an
    # empty brain with no OTHER registered project on this machine (the seed
    # itself is minted at launch, so it never counts against "first run").
    if mode != "global" and pid is None:
        first_run = True
    elif db is None or not db.exists():
        first_run = True
    else:
        others = [u for u in load_path_registry().values() if u != pid]
        first_run = doc_count == 0 and not others

    return Preflight(
        action="launch", first_run=first_run, report_lines=lines,
        expected_db=str(db) if db else None,
    )
