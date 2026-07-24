# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
gui_install.py — MCP client install/uninstall + hook management for the Memory-Map GUI.

Write-gated endpoints mounted by gui.create_app(allow_writes=True):

  POST /api/install     register the braincell MCP server for a client (+ optional
                         --federate-equivalent env stamp + family-recall hook)
  POST /api/uninstall    reverse the above (VS Code has no remove-MCP CLI → 409 with
                         manual instructions, mirroring `braincell uninstall`)
  POST /api/hook         arm / disarm / report the proactive family-recall hook flag

This is the GUI counterpart of `braincell install`/`uninstall`/`hook` (cli.py
cmd_install/cmd_uninstall/cmd_hook) — it reuses the exact same library functions
(install.py) rather than re-implementing client wiring. The MCP command and env are
always assembled server-side from those functions; the request supplies paths and
closed enums only (never a command/args/env — a security invariant).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from . import config
from .config import get_project_id
from .install import (
    get_client,
    hook_command,
    install_hook,
    resolve_server_command,
    uninstall_hook,
)


# ── Request bodies (path + closed enums/booleans only — SI-3) ──────────────────

class InstallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    client: Literal["claude", "codex", "vscode"] = "claude"
    scope: Literal["local", "user", "project"] = "local"
    no_hook: bool = False
    federate: bool = False


class UninstallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    client: Literal["claude", "codex", "vscode"] = "claude"
    scope: Literal["local", "user", "project"] = "local"
    disarm: bool = False


class HookBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["on", "off", "status"]


# ── Path validation (SI-4: mirror gui_ingest.py:299-303) ───────────────────────

def _resolve_dir(raw: str) -> Path:
    p = Path(raw).expanduser()
    if not p.is_dir():
        raise HTTPException(400, f"Not a directory: {raw}")
    return p.resolve()


# ── Hook flag path (mirrors cli.py:783-786 _family_hook_flag) ──────────────────

def _hook_flag_path() -> Path:
    from .family_hook import default_flag_path
    return Path(os.environ.get("BRAINCELL_FAMILY_HOOK_FLAG", str(default_flag_path())))


def _arm_flag(flag: Path) -> None:
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()


# ── Route mounting (called by gui.create_app when allow_writes=True) ──────────

def mount_install_api(app: FastAPI) -> None:
    """Register the install/uninstall/hook routes on *app*."""

    @app.post("/api/install")
    async def api_install(body: InstallBody) -> dict:  # type: ignore[type-arg]
        root = _resolve_dir(body.path)
        client = get_client(body.client)
        if not client.available():
            raise HTTPException(
                409, f"`{body.client}` CLI not found on PATH — install it, then retry."
            )

        pid = get_project_id(root)  # mints + registers if absent, mirrors cli.py:812

        env: dict[str, str] = {
            "BRAINCELL_DATA_NAMESPACE": config.DATA_NAMESPACE,
            "BRAINCELL_PROJECT_ID": pid,
        }
        if body.federate:
            env["BRAINCELL_FEDERATE"] = "on"

        command, cmd_args = resolve_server_command()
        import anyio
        try:
            await anyio.to_thread.run_sync(
                lambda: client.mcp_add(
                    "braincell", command, cmd_args, env, scope=body.scope, cwd=str(root)
                )
            )
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))

        hook_installed = False
        if body.client == "claude" and not body.no_hook:
            hook_installed = await anyio.to_thread.run_sync(
                lambda: install_hook(hook_command())
            )

        return {
            "ok": True,
            "project_id": pid,
            "client": body.client,
            "command": command,
            "hook_installed": bool(hook_installed),
            "restart_required": True,
        }

    @app.post("/api/uninstall")
    async def api_uninstall(body: UninstallBody) -> dict:  # type: ignore[type-arg]
        root = _resolve_dir(body.path)
        client = get_client(body.client)

        import anyio
        mcp_removed = False
        if client.available():
            try:
                await anyio.to_thread.run_sync(
                    lambda: client.mcp_remove("braincell", scope=body.scope, cwd=str(root))
                )
                mcp_removed = True
            except NotImplementedError as exc:
                raise HTTPException(409, str(exc))

        hook_removed = 0
        if body.client == "claude":
            hook_removed = await anyio.to_thread.run_sync(uninstall_hook)
            if body.disarm:
                flag = _hook_flag_path()
                await anyio.to_thread.run_sync(lambda: flag.unlink(missing_ok=True))

        return {"ok": True, "mcp_removed": mcp_removed, "hook_removed": hook_removed}

    @app.post("/api/hook")
    async def api_hook(body: HookBody) -> dict:  # type: ignore[type-arg]
        flag = _hook_flag_path()
        import anyio
        if body.action == "on":
            await anyio.to_thread.run_sync(_arm_flag, flag)
            armed = True
        elif body.action == "off":
            await anyio.to_thread.run_sync(lambda: flag.unlink(missing_ok=True))
            armed = False
        else:
            armed = flag.is_file()
        return {"armed": armed, "flag": str(flag)}
