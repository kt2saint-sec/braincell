# SPDX-License-Identifier: AGPL-3.0-or-later
"""Project-local, explicit Automatic Pool recall for Claude Code."""

from __future__ import annotations

import asyncio
import json
import shlex
import stat
import sys
from pathlib import Path
from typing import Any

from .config import resolve_project_id_readonly
from .install import _atomic_write_text
from .project_registry import (
    load_path_registry,
    pools_for_project,
    resolve_pool,
)

_MARKER = "braincell automatic-pool-recall run"
_TIMEOUT_SECONDS = 20
_STATUS_MESSAGE = "BrainCell Automatic Pool recall…"


def project_hook_settings_path(project_root: str | Path, scope: str) -> Path:
    """Return one Project-local Claude settings path."""
    root = Path(project_root).expanduser().resolve()
    if scope == "local":
        relative = Path(".claude") / "settings.local.json"
    elif scope == "project":
        relative = Path(".claude") / "settings.json"
    else:
        raise ValueError("Automatic Pool recall scope must be 'local' or 'project'.")
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise RuntimeError(
            f"{root / relative} resolves outside the selected Project; BrainCell refused it."
        ) from None
    return target


def _load_settings(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.exists():
        return {}, None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw or "{}")
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot parse {path}; BrainCell left it unchanged. ({exc})") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must contain a JSON object; BrainCell left it unchanged.")
    return data, raw


def _canonical_command(pool_name: str, project_id: str) -> str:
    return (
        f"{_MARKER} --pool {shlex.quote(pool_name)} "
        f"--project-id {shlex.quote(project_id)}"
    )


def _canonical_inner(pool_name: str, project_id: str) -> dict[str, Any]:
    return {
        "type": "command",
        "command": _canonical_command(pool_name, project_id),
        "timeout": _TIMEOUT_SECONDS,
        "statusMessage": _STATUS_MESSAGE,
    }


def _managed_identity(hook: Any) -> tuple[str, str] | None:
    if not isinstance(hook, dict) or _MARKER not in str(hook.get("command", "")):
        return None
    try:
        argv = shlex.split(str(hook["command"]))
    except ValueError:
        return None
    if argv[:3] != ["braincell", "automatic-pool-recall", "run"]:
        return None
    if len(argv) != 7 or argv[3] != "--pool" or argv[5] != "--project-id":
        return None
    expected = _canonical_inner(argv[4], argv[6])
    return (argv[4], argv[6]) if hook == expected else None


def _iter_inner_hooks(settings: dict[str, Any]):
    hooks = settings.get("hooks")
    if hooks is None:
        return
    if not isinstance(hooks, dict):
        raise RuntimeError("Claude hooks must be a JSON object.")
    entries = hooks.get("UserPromptSubmit") or []
    if not isinstance(entries, list):
        raise RuntimeError("Claude UserPromptSubmit hooks must be a list.")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        hooks = entry.get("hooks", [])
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            yield entry, hook


def _resolve_pool_choice(project_id: str, pool_name: str | None) -> str:
    memberships = pools_for_project(project_id)
    if pool_name is not None:
        display, members = resolve_pool(pool_name)
        if project_id not in members:
            raise ValueError(f"Project {project_id} is not a member of Pool {display!r}.")
        return display
    if len(memberships) == 1:
        return memberships[0]
    if not memberships:
        raise ValueError("This Project does not belong to a Pool.")
    raise ValueError(
        "This Project belongs to multiple Pools; choose one explicitly: "
        + ", ".join(memberships)
    )


def enable_automatic_pool_recall(
    project_root: str | Path,
    *,
    scope: str = "local",
    pool_name: str | None = None,
) -> dict[str, Any]:
    """Enable one canonical Project-local hook, preserving unrelated settings."""
    root = Path(project_root).expanduser().resolve()
    project_id = resolve_project_id_readonly(root)
    if project_id is None:
        raise ValueError("This Project has no BrainCell memory yet. Run `braincell build` first.")
    pool = _resolve_pool_choice(project_id, pool_name)
    path = project_hook_settings_path(root, scope)
    settings, raw = _load_settings(path)
    canonical = _canonical_inner(pool, project_id)
    marked = [hook for _entry, hook in _iter_inner_hooks(settings) if _MARKER in str(hook)]
    if marked:
        if len(marked) == 1 and marked[0] == canonical:
            return {
                "changed": False, "enabled": True, "pool": pool, "project_id": project_id,
                "settings_path": str(path), "backup_path": None,
            }
        raise RuntimeError(
            f"{path} has a conflicting Automatic Pool recall hook; BrainCell left it unchanged."
        )

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise RuntimeError(f"{path} has a non-object hooks value; BrainCell left it unchanged.")
    entries = hooks.setdefault("UserPromptSubmit", [])
    if not isinstance(entries, list):
        raise RuntimeError(f"{path} has invalid UserPromptSubmit hooks; BrainCell left it unchanged.")
    entries.append({"hooks": [canonical]})
    rendered = json.dumps(settings, indent=2, ensure_ascii=False)
    if raw is None or raw.endswith("\n"):
        rendered += "\n"
    mode = path.stat().st_mode if path.exists() else None
    backup = _atomic_write_text(path, rendered, mode)
    return {
        "changed": True, "enabled": True, "pool": pool, "project_id": project_id,
        "settings_path": str(path), "backup_path": str(backup) if backup else None,
    }


def status_automatic_pool_recall(
    project_root: str | Path, *, scope: str = "local"
) -> dict[str, Any]:
    path = project_hook_settings_path(project_root, scope)
    settings, _raw = _load_settings(path)
    marked = [hook for _entry, hook in _iter_inner_hooks(settings) if _MARKER in str(hook)]
    if not marked:
        return {"enabled": False, "conflict": False, "settings_path": str(path)}
    identities = [_managed_identity(hook) for hook in marked]
    if len(identities) != 1 or identities[0] is None:
        return {"enabled": False, "conflict": True, "settings_path": str(path)}
    pool, project_id = identities[0]
    return {
        "enabled": True, "conflict": False, "pool": pool,
        "project_id": project_id, "settings_path": str(path),
    }


def disable_automatic_pool_recall(
    project_root: str | Path, *, scope: str = "local"
) -> dict[str, Any]:
    path = project_hook_settings_path(project_root, scope)
    if not path.exists():
        return {"changed": False, "enabled": False, "settings_path": str(path)}
    settings, raw = _load_settings(path)
    marked = [hook for _entry, hook in _iter_inner_hooks(settings) if _MARKER in str(hook)]
    if any(_managed_identity(hook) is None for hook in marked):
        raise RuntimeError(
            f"{path} has a conflicting Automatic Pool recall hook; BrainCell left it unchanged."
        )
    if not marked:
        return {"changed": False, "enabled": False, "settings_path": str(path)}

    entries = settings["hooks"]["UserPromptSubmit"]
    new_entries = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            new_entries.append(entry)
            continue
        kept = [hook for hook in entry["hooks"] if _managed_identity(hook) is None]
        if kept:
            new_entries.append({**entry, "hooks": kept})
    settings["hooks"]["UserPromptSubmit"] = new_entries
    rendered = json.dumps(settings, indent=2, ensure_ascii=False)
    if raw is None or raw.endswith("\n"):
        rendered += "\n"
    backup = _atomic_write_text(path, rendered, stat.S_IMODE(path.stat().st_mode))
    return {
        "changed": True, "enabled": False, "settings_path": str(path),
        "backup_path": str(backup) if backup else None,
    }


def _connected_root(cwd: str | Path, expected_project_id: str) -> Path | None:
    current = Path(cwd).expanduser().resolve()
    candidates = []
    for raw_path, project_id in load_path_registry().items():
        if project_id != expected_project_id:
            continue
        root = Path(raw_path).expanduser().resolve()
        try:
            current.relative_to(root)
        except ValueError:
            continue
        candidates.append(root)
    return max(candidates, key=lambda path: len(path.parts)) if candidates else None


def _hook_is_enabled(root: Path, pool_name: str, project_id: str) -> bool:
    expected = _canonical_inner(pool_name, project_id)
    for scope in ("local", "project"):
        try:
            settings, _raw = _load_settings(project_hook_settings_path(root, scope))
            if any(hook == expected for _entry, hook in _iter_inner_hooks(settings)):
                return True
        except RuntimeError:
            continue
    return False


def _recall_from_pool(pool_name: str, root: Path, query: str, k: int) -> list[dict[str, Any]]:
    from .embed import embed_query_async
    from .federate import federated_recall, plan_for_pool

    project_id = resolve_project_id_readonly(root)
    if project_id is None:
        return []
    plan = plan_for_pool(pool_name, project_id)
    try:
        vector = asyncio.run(embed_query_async(query)) if query.strip() else None
    except Exception:
        vector = None
    notes = asyncio.run(federated_recall(None, plan, vector, k, qtext=query))
    return [
        {
            "kind": note.kind,
            "content": note.content,
            "project_id": note.project_id,
        }
        for note in notes
    ]


def run_hook(
    payload: dict[str, Any],
    *,
    pool_name: str,
    project_id: str,
    k: int = 5,
    maxchars: int = 500,
) -> dict[str, Any]:
    """Return Claude hook output, or an empty fail-quiet result."""
    try:
        prompt = str(payload.get("prompt") or "").strip()
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not cwd.strip():
            return {}
        root = _connected_root(cwd, project_id)
        if not prompt or root is None or not _hook_is_enabled(root, pool_name, project_id):
            return {}
        _display, members = resolve_pool(pool_name)
        if project_id not in members:
            return {}
        notes = _recall_from_pool(pool_name, root, prompt, k)
        if not notes:
            return {}
        lines = [f"Automatic Pool recall from {pool_name}:"]
        for note in notes:
            content = " ".join(str(note.get("content") or "").split())
            if len(content) > maxchars:
                content = content[: maxchars - 3] + "..."
            lines.append(f"- [{note.get('kind', 'note')}] {content}")
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "\n".join(lines),
            }
        }
    except Exception:
        return {}


def hook_main(pool_name: str, project_id: str) -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    print(json.dumps(run_hook(payload, pool_name=pool_name, project_id=project_id)))
