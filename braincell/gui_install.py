# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
gui_install.py — MCP client install/uninstall + hook management for the Memory-Map GUI.

Write-gated endpoints mounted by gui.create_app(allow_writes=True):

  POST /api/install      connect the BrainCell MCP server to one selected Project
  POST /api/uninstall    disconnect it without changing Project memory
  POST /api/skills       add/remove packaged skills inside one selected project
  POST /api/restart      re-exec the GUI server process (server-recorded argv only)

This is the GUI counterpart of `braincell install`/`uninstall`/`hook` (cli.py
cmd_install/cmd_uninstall/cmd_hook) — it reuses the exact same library functions
(install.py) rather than re-implementing client wiring. The MCP command and env are
always assembled server-side from those functions; the request supplies paths and
closed enums only (never a command/args/env — a security invariant). /api/restart
follows the same posture: the argv it execs is recorded server-side at launch and
never taken from the request.
"""

from __future__ import annotations

import asyncio
import os
from typing import Literal, Protocol

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from . import config
from .config import get_project_id
from .install import (
    get_client,
    install_project_skills,
    remove_project_skills,
)
from .project_target import ProjectTarget, ProjectTargetError, validate_project_target

# Delay before the restart re-exec fires — lets the 200 response flush first.
_RESTART_DELAY_S = 0.5
_BAD_TARGET_CODES = frozenset({"filesystem_root_forbidden", "target_not_directory"})


def _target_http_exception(exc: ProjectTargetError) -> HTTPException:
    """Preserve the difference between invalid input and needed consent."""
    status_code = 400 if exc.code in _BAD_TARGET_CODES else 409
    return HTTPException(
        status_code,
        {"code": exc.code, "message": str(exc)},
    )


class _TargetAcknowledgementBody(Protocol):
    """The closed request-body fields required for Project target validation."""

    path: str
    acknowledge_home: bool
    acknowledge_non_git: bool
    allow_privileged: bool


def _validate_gui_target(
    body: _TargetAcknowledgementBody, *, require_git: bool = False
) -> ProjectTarget:
    """Validate a closed request body and expose structured target failures."""
    try:
        return validate_project_target(
            body.path,
            acknowledge_home=body.acknowledge_home,
            acknowledge_non_git=body.acknowledge_non_git,
            allow_privileged=body.allow_privileged,
            require_git=require_git,
        )
    except ProjectTargetError as exc:
        raise _target_http_exception(exc) from exc


# ── Request bodies (path + closed enums/booleans only — SI-3) ──────────────────

class InstallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    client: Literal["claude", "codex", "vscode", "opencode"] = "claude"
    scope: Literal["local", "project"] = "local"
    acknowledge_home: bool = False
    acknowledge_non_git: bool = False
    allow_privileged: bool = False


class SkillsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    client: Literal["claude", "codex"]
    action: Literal["add", "remove"]
    acknowledge_home: bool = False
    acknowledge_non_git: bool = False
    allow_privileged: bool = False


class AutomaticPoolRecallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    action: Literal["enable", "disable", "status"]
    scope: Literal["local", "project"] = "local"
    pool: str | None = None
    acknowledge_home: bool = False
    acknowledge_non_git: bool = False
    allow_privileged: bool = False


class RestartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UninstallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    client: Literal["claude", "codex", "vscode", "opencode"] = "claude"
    scope: Literal["local", "project"] = "local"
    acknowledge_home: bool = False
    acknowledge_non_git: bool = False
    allow_privileged: bool = False


# ── Route mounting (called by gui.create_app when allow_writes=True) ──────────

def mount_install_api(app: FastAPI, *, restart_argv: list[str] | None = None) -> None:
    """Register the install/uninstall/hook/skills/restart routes on *app*.

    ``restart_argv`` is the server-recorded argv POST /api/restart re-execs
    (assembled by run_gui — never taken from a request). None disables restart
    (409), e.g. embedded/test apps with no restartable process of their own.
    """

    @app.post("/api/install")
    async def api_install(body: InstallBody) -> dict:  # type: ignore[type-arg]
        target = _validate_gui_target(body, require_git=body.client == "codex")
        client = get_client(body.client)
        if not client.available():
            raise HTTPException(
                409, f"`{body.client}` CLI not found on PATH — install it, then retry."
            )

        root = target.path
        pid = get_project_id(root)
        cwd = str(root)
        env = {
            "BRAINCELL_DATA_NAMESPACE": config.DATA_NAMESPACE,
            "BRAINCELL_PROJECT_ID": pid,
            "BRAINCELL_STORE": "sqlite",
        }
        if body.client in {"codex", "vscode", "opencode"} or body.scope == "project":
            from .install import resolve_portable_server_command
            command, cmd_args = resolve_portable_server_command()
        else:
            from .install import resolve_server_command
            command, cmd_args = resolve_server_command()
        import anyio
        try:
            await anyio.to_thread.run_sync(
                lambda: client.mcp_add(
                    "braincell", command, cmd_args, env, scope=body.scope, cwd=cwd
                )
            )
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))

        return {
            "ok": True,
            "project_id": pid,
            "client": body.client,
            "command": command,
            "restart_required": True,
        }

    @app.post("/api/uninstall")
    async def api_uninstall(body: UninstallBody) -> dict:  # type: ignore[type-arg]
        target = _validate_gui_target(body)
        root = target.path
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

        return {"ok": True, "mcp_removed": mcp_removed}

    @app.post("/api/skills")
    async def api_skills(body: SkillsBody) -> dict:  # type: ignore[type-arg]
        """Add or remove packaged skills inside one selected project.

        Existing same-name skills with different content are reported as
        ``conflict`` and left untouched.
        """
        target = _validate_gui_target(body)
        import anyio
        operation = install_project_skills if body.action == "add" else remove_project_skills
        try:
            results = await anyio.to_thread.run_sync(operation, target.path, body.client)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "project": str(target.path),
            "client": body.client,
            "action": body.action,
            "skills": [
                {"name": name, "status": status, "path": str(path)}
                for name, status, path in results
            ]
        }

    @app.post("/api/automatic-pool-recall")
    async def api_automatic_pool_recall(
        body: AutomaticPoolRecallBody,
    ) -> dict:  # type: ignore[type-arg]
        """Manage only the selected Project's Claude hook configuration."""
        import anyio

        from .automatic_pool_recall import (
            disable_automatic_pool_recall,
            enable_automatic_pool_recall,
            status_automatic_pool_recall,
        )
        try:
            target = _validate_gui_target(body)
            if body.action == "enable":
                result = await anyio.to_thread.run_sync(
                    lambda: enable_automatic_pool_recall(
                        target.path, scope=body.scope, pool_name=body.pool
                    )
                )
            elif body.action == "disable":
                result = await anyio.to_thread.run_sync(
                    lambda: disable_automatic_pool_recall(target.path, scope=body.scope)
                )
            else:
                result = await anyio.to_thread.run_sync(
                    lambda: status_automatic_pool_recall(target.path, scope=body.scope)
                )
        except (RuntimeError, ValueError, KeyError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"project": str(target.path), "action": body.action, **result}

    @app.post("/api/restart")
    async def api_restart(request: Request, body: RestartBody) -> dict:  # type: ignore[type-arg]
        """Re-exec the GUI server with its launch argv (recorded server-side).

        Refused while an ingest/ops job runs (an exec would kill it mid-write)
        and when no argv was recorded (create_app without run_gui). The exec is
        deferred so the 200 response reaches the client first; the persisted
        token file means the re-exec'd server accepts the same ``?t=``.
        """
        ingest = getattr(request.app.state, "ingest_manager", None)
        ops = getattr(request.app.state, "ops_manager", None)
        if (ingest is not None and ingest.busy) or (ops is not None and ops.busy):
            raise HTTPException(409, "A job is running — retry once it finishes.")
        if not restart_argv:
            raise HTTPException(
                409, "Restart unavailable — this server was not launched via `braincell gui`."
            )
        asyncio.get_running_loop().call_later(
            _RESTART_DELAY_S, os.execv, restart_argv[0], list(restart_argv)
        )
        return {"ok": True, "restarting": True}
