# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""Project-local BrainCell client connections and Project skills.

Legacy user-level Claude hook helpers remain isolated here only for explicit
migration cleanup. Ordinary connection, disconnection, CLI, and GUI paths do
not call them.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import tomlkit

from .log import get as _get_log

log = _get_log("braincell.install")

# Marker present in the hook command so install/uninstall can find OUR entry among
# other UserPromptSubmit hooks without matching anything else.
_HOOK_MARKER = "braincell.family_hook"
_HOOK_TIMEOUT_S = 15
_HOOK_STATUS = "braincell family memory…"


# ── Command resolution ────────────────────────────────────────────────────────

def resolve_server_command() -> tuple[str, list[str]]:
    """The command a client runs to start the braincell MCP server.

    Prefer the installed ``braincell-mcp`` console script (absolute path, works
    regardless of the client's PATH); fall back to ``<this python> -m braincell.server``.
    """
    exe = shutil.which("braincell-mcp")
    if exe:
        return exe, []
    return sys.executable, ["-m", "braincell.server"]


def resolve_portable_server_command() -> tuple[str, list[str]]:
    """Return the portable command safe to place in a project configuration.

    A project-level configuration can be committed or moved.  Never place this
    machine's virtualenv path in it: the package console script on ``PATH`` is
    the portable contract.  Refuse rather than silently writing an absolute
    fallback.
    """
    if not shutil.which("braincell-mcp"):
        raise RuntimeError(
            "`braincell-mcp` is not on PATH. Install BrainCell in the environment "
            "the client will use; refusing to write a machine-specific executable "
            "path into a project configuration."
        )
    return "braincell-mcp", []


def hook_command(python: str | None = None) -> str:
    """The UserPromptSubmit hook command string (`<python> -m braincell.family_hook`)."""
    return f"{python or sys.executable} -m braincell.family_hook"


# ── Claude Code settings.json (hook install) ──────────────────────────────────

def claude_settings_path() -> Path:
    """Path to Claude Code's user settings.json (override via env for tests)."""
    override = os.environ.get("BRAINCELL_CLAUDE_SETTINGS")
    if override:
        return Path(override)
    from .platform import get_claude_config_dir
    return get_claude_config_dir() / "settings.json"


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(
            f"Cannot parse {path} — refusing to overwrite a settings file I can't read. "
            f"Fix or move it, then re-run. ({exc})"
        ) from exc


def _atomic_write_json(path: Path, data: dict) -> None:
    """Backup (if present) then atomically write pretty JSON (tmp + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _is_braincell_inner(hook: dict) -> bool:
    return _HOOK_MARKER in str(hook.get("command", ""))


def install_hook(command: str) -> bool:
    """Append the braincell UserPromptSubmit hook to Claude Code settings.

    Idempotent (no duplicate if an entry with the marker already exists) and
    append-only (never disturbs other hooks). Returns True if it wrote a new entry,
    False if it was already present.
    """
    path = claude_settings_path()
    settings = _load_json(path)
    ups = settings.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
    for entry in ups:
        if any(_is_braincell_inner(h) for h in entry.get("hooks", [])):
            return False  # already installed
    ups.append({
        "hooks": [{
            "type": "command",
            "command": command,
            "timeout": _HOOK_TIMEOUT_S,
            "statusMessage": _HOOK_STATUS,
        }],
    })
    _atomic_write_json(path, settings)
    return True


def uninstall_hook() -> int:
    """Remove ONLY braincell's UserPromptSubmit hook(s). Returns count removed.

    Drops braincell inner hooks; an outer entry left with no inner hooks is dropped
    too. Other hooks (iron-law, etc.) are preserved untouched.
    """
    path = claude_settings_path()
    settings = _load_json(path)
    ups = settings.get("hooks", {}).get("UserPromptSubmit")
    if not ups:
        return 0
    removed = 0
    new_ups: list = []
    for entry in ups:
        inner = entry.get("hooks", [])
        kept = [h for h in inner if not _is_braincell_inner(h)]
        removed += len(inner) - len(kept)
        if kept:
            entry = {**entry, "hooks": kept}
            new_ups.append(entry)
        elif "hooks" not in entry:
            new_ups.append(entry)  # entry had no inner hooks list — leave as-is
    if removed:
        settings["hooks"]["UserPromptSubmit"] = new_ups
        _atomic_write_json(path, settings)
    return removed


# ── Project skills ────────────────────────────────────────────────────────────

_PROJECT_SKILL_DIRS = {
    "claude": Path(".claude") / "skills",
    "codex": Path(".agents") / "skills",
    "opencode": Path(".opencode") / "skills",
}

_SKILL_CLIENT_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex",
    "opencode": "OpenCode",
}


def packaged_skills() -> list[str]:
    """Names of the skills shipped inside the wheel (``braincell/skills/<name>/``)."""
    from importlib.resources import files
    root = files("braincell").joinpath("skills")
    if not root.is_dir():
        return []
    # Resource packages can contain Python's ``__pycache__`` alongside the
    # data directories in a source checkout.  A skill is defined by its
    # SKILL.md contract, never merely by being a directory.
    return sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir() and entry.joinpath("SKILL.md").is_file()
    )


def project_skills_dir(project_root: str | Path, client: str) -> Path:
    """Return the supported project-local skills directory for one client."""
    try:
        relative = _PROJECT_SKILL_DIRS[client]
    except KeyError:
        raise ValueError(
            "Project skills are supported only for Claude, Codex, or OpenCode."
        ) from None
    root = Path(project_root).expanduser().resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise RuntimeError(
            f"{root / relative} resolves outside the selected project; BrainCell refused it."
        ) from None
    return target


def _packaged_skill_payloads(client: str) -> dict[str, str]:
    """Return skill content rendered for one supported project-local client."""
    from importlib.resources import files

    try:
        client_label = _SKILL_CLIENT_LABELS[client]
    except KeyError:
        raise ValueError(
            "Project skills are supported only for Claude, Codex, or OpenCode."
        ) from None

    root = files("braincell").joinpath("skills")
    payloads = {
        name: root.joinpath(name, "SKILL.md").read_text(encoding="utf-8")
        for name in packaged_skills()
    }
    payloads["braincell-init"] = (
        payloads["braincell-init"]
        .replace("__BRAINCELL_CLIENT_LABEL__", client_label)
        .replace("__BRAINCELL_CLIENT_KEY__", client)
    )
    return payloads


# SHA-256 digests of every skill body BrainCell itself shipped in EARLIER
# releases (raw git blobs; none carried client placeholders, so installed bytes
# equal blob bytes). A destination matching one of these is BrainCell-authored,
# not user-edited: install may update it in place and remove may delete it.
# Anything else stays user-owned and untouchable. Append-only: never remove a
# digest — an installed copy of that version exists somewhere.
_HISTORICAL_SKILL_SHA256: dict[str, frozenset[str]] = {
    "braincell-init": frozenset({
        "993e51bde9966c2f3a8b1ad2fe916efddd4ea11f11443f8fdce18ff9ed7e20d5",
        "6ca400ac7ea2f3fbe2470930719d7e75ee8664fc019d3665153e04e99506b3f5",
    }),
    "braincell-sync": frozenset({
        "aec538761928febfe166bd6a364cf900ce14e4cda5faa1d08d5199585e583837",
        "e26fb9de6836ef8fdf502520631d00dac3cb280f9f982f4d7a52f8b4a79b0563",
    }),
}


def _is_braincell_authored_skill_body(name: str, content: str) -> bool:
    """True when *content* is a skill body an earlier BrainCell release wrote."""
    import hashlib

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return digest in _HISTORICAL_SKILL_SHA256.get(name, frozenset())


def project_skills_status(
    project_root: str | Path, client: str
) -> list[tuple[str, str, Path]]:
    """Inspect one project's packaged skills without changing its files.

    Each result is ``(name, status, path)`` where status is ``not_installed``,
    ``current``, ``outdated``, or ``modified``. An outdated skill is a body an
    earlier BrainCell release wrote — updating it loses no user work. A
    modified skill is user-owned and must not be overwritten or removed
    automatically.
    """
    dest_root = project_skills_dir(project_root, client)
    results: list[tuple[str, str, Path]] = []
    for name, payload in _packaged_skill_payloads(client).items():
        dest = dest_root / name / "SKILL.md"
        if not dest.exists():
            results.append((name, "not_installed", dest))
            continue
        try:
            current = dest.read_text(encoding="utf-8")
        except OSError:
            current = None
        if current == payload:
            status = "current"
        elif current is not None and _is_braincell_authored_skill_body(name, current):
            status = "outdated"
        else:
            status = "modified"
        results.append((name, status, dest))
    return results


def install_project_skills(
    project_root: str | Path, client: str
) -> list[tuple[str, str, Path]]:
    """Copy packaged skills into one selected project's client directory.

    Returns one ``(name, status, path)`` per skill, where status is:
      ``installed``  — written (destination did not exist)
      ``current``    — destination already byte-identical; nothing written
      ``updated``    — destination was an EARLIER BrainCell release's body;
                       replaced with the current one (no user work involved)
      ``conflict``   — destination exists with content BrainCell never shipped;
                       left untouched

    Never clobbers user work: a person may have their own skill of the same
    name (or an edited copy of ours), and silently overwriting it would destroy
    work no backup covers. Only bodies BrainCell itself wrote are replaced.
    Conflicts are reported so the caller can print a manual step — the same
    posture as the VS Code adapter, which refuses to guess when it cannot act
    safely.
    """
    dest_root = project_skills_dir(project_root, client)
    results: list[tuple[str, str, Path]] = []

    for name, payload in _packaged_skill_payloads(client).items():
        dest = dest_root / name / "SKILL.md"
        status = "installed"
        if dest.exists():
            try:
                current = dest.read_text(encoding="utf-8")
            except OSError:
                current = None
            if current == payload:
                results.append((name, "current", dest))
                continue
            if current is None or not _is_braincell_authored_skill_body(name, current):
                results.append((name, "conflict", dest))
                continue
            status = "updated"
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(dest)                      # atomic, mirrors _atomic_write_json
        results.append((name, status, dest))

    return results


def remove_project_skills(
    project_root: str | Path, client: str
) -> list[tuple[str, str, Path]]:
    """Remove only BrainCell-authored packaged skills from one selected project.

    A body BrainCell shipped — the current one or any earlier release's — is
    removable; an edited same-name skill is user-managed and remains untouched.
    Missing files are idempotent no-ops.
    """
    dest_root = project_skills_dir(project_root, client)
    results: list[tuple[str, str, Path]] = []
    for name, payload in _packaged_skill_payloads(client).items():
        dest = dest_root / name / "SKILL.md"
        if not dest.exists():
            results.append((name, "absent", dest))
            continue
        try:
            current = dest.read_text(encoding="utf-8")
        except OSError:
            current = None
        if current != payload and (
            current is None or not _is_braincell_authored_skill_body(name, current)
        ):
            results.append((name, "conflict", dest))
            continue
        dest.unlink()
        try:
            dest.parent.rmdir()
        except OSError:
            pass
        results.append((name, "removed", dest))
    return results


# ── Claude Code MCP registration (via the official CLI) ───────────────────────

class ClaudeCodeClient:
    """Wire braincell into Claude Code via `claude mcp …` + settings.json."""

    name = "claude-code"

    def __init__(self, claude_bin: str | None = None):
        self._claude = claude_bin or shutil.which("claude")

    def available(self) -> bool:
        return bool(self._claude)

    def _run(self, args: list[str], cwd: str | None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self._claude, *args], cwd=cwd, capture_output=True, text=True, check=False,
        )

    def mcp_remove(self, name: str, scope: str, cwd: str | None = None) -> None:
        if scope not in {"local", "project"}:
            raise RuntimeError("Claude user-scope BrainCell connections are not supported.")
        if name != _MCP_SERVER_NAME or not cwd:
            raise RuntimeError("Claude project disconnection requires a selected project.")
        from .config import DATA_NAMESPACE, get_project_id

        project_id = get_project_id(Path(cwd), create=False)
        expected = _canonical_claude_entry(
            "braincell-mcp",
            [],
            {
                "BRAINCELL_DATA_NAMESPACE": DATA_NAMESPACE,
                "BRAINCELL_PROJECT_ID": project_id,
                "BRAINCELL_STORE": "sqlite",
            },
        )
        existing = _claude_entry_for_scope(Path(cwd), scope)
        if existing is None:
            return
        if existing != expected:
            raise RuntimeError(
                "Claude has a different user-managed BrainCell entry; it was not removed."
            )
        if not self.available():
            raise RuntimeError("`claude` CLI not found on PATH; cannot disconnect the local project entry.")
        result = self._run(["mcp", "remove", name, "-s", scope], cwd)
        if result.returncode != 0:
            raise RuntimeError(
                f"`claude mcp remove {name}` failed (exit {result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

    def mcp_add(
        self,
        name: str,
        command: str,
        args: list[str],
        env: dict[str, str],
        scope: str,
        cwd: str | None = None,
    ) -> None:
        """Create one local/project entry and refuse user-managed conflicts."""
        if scope not in {"local", "project"}:
            raise RuntimeError("Claude user-scope BrainCell connections are not supported.")
        if not self.available():
            raise RuntimeError(
                "`claude` CLI not found on PATH — is Claude Code installed? "
                "(the MCP registration uses `claude mcp add`)."
            )
        if name != _MCP_SERVER_NAME or not cwd:
            raise RuntimeError("Claude project connection requires a selected project.")
        existing = _claude_entry_for_scope(Path(cwd), scope)
        canonical = _canonical_claude_entry(command, args, env)
        if existing is not None:
            if existing == canonical:
                return
            raise RuntimeError(
                "Claude has a different user-managed BrainCell entry; it was not overwritten."
            )
        argv = ["mcp", "add", name, "-s", scope]
        for key, val in env.items():
            argv += ["-e", f"{key}={val}"]
        argv += ["--", command, *args]
        res = self._run(argv, cwd)
        if res.returncode != 0:
            raise RuntimeError(
                f"`claude mcp add {name}` failed (exit {res.returncode}): "
                f"{res.stderr.strip() or res.stdout.strip()}"
            )


def _canonical_claude_entry(command: str, args: list[str], env: dict[str, str]) -> dict[str, Any]:
    return {"command": command, "args": list(args), "env": dict(env)}


def _claude_entry_for_scope(path: Path, scope: str) -> dict[str, Any] | None:
    """Read one supported Claude project-bounded scope without writing it."""
    resolved = Path(path).resolve()
    if scope == "local":
        cfg_file = claude_config_path()
        if not cfg_file.exists():
            return None
        cfg = json.loads(cfg_file.read_text(encoding="utf-8") or "{}")
        entry = (((cfg.get("projects") or {}).get(str(resolved)) or {}).get("mcpServers") or {}).get(
            _MCP_SERVER_NAME
        )
    elif scope == "project":
        cfg_file = resolved / ".mcp.json"
        if not cfg_file.exists():
            return None
        cfg = json.loads(cfg_file.read_text(encoding="utf-8") or "{}")
        entry = (cfg.get("mcpServers") or {}).get(_MCP_SERVER_NAME)
    else:
        raise RuntimeError(f"Unsupported Claude scope: {scope}")
    return entry if isinstance(entry, dict) else None


# ── Project-local Codex / VS Code configuration ─────────────────────────────

_MCP_SERVER_NAME = "braincell"
_CODEX_MANAGED_COMMENT = "Managed by BrainCell project connection."


def _plain(value: Any) -> Any:
    """Convert tomlkit values to ordinary containers for exact comparison."""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value.unwrap() if hasattr(value, "unwrap") else value


def _backup_path(path: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    return path.with_name(f"{path.name}.braincell.bak.{stamp}")


def _atomic_write_text(path: Path, text: str, mode: int | None) -> Path | None:
    """Back up an existing file then atomically replace it in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if path.exists():
        backup = _backup_path(path)
        shutil.copy2(path, backup)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    try:
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8", newline="")
        except Exception:
            os.close(fd)
            raise
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        # Permissions are applied by path after the descriptor is closed:
        # os.fchmod is absent on Windows before 3.13, and a live descriptor
        # would block the unlink below on Windows.
        if mode is not None:
            os.chmod(temp_path, stat.S_IMODE(mode))
        os.replace(temp_path, path)
    except Exception:
        # Cleanup must never replace the exception that caused it.
        with suppress(OSError):
            temp_path.unlink(missing_ok=True)
        raise
    return backup


def _read_toml_document(path: Path) -> tuple[Any, str | None, bool]:
    if not path.exists():
        return tomlkit.document(), None, True
    try:
        raw = path.read_text(encoding="utf-8")
        return tomlkit.parse(raw), raw, raw.endswith("\n")
    except Exception as exc:  # tomlkit exposes several parser exception types
        raise RuntimeError(
            f"Cannot parse {path}; BrainCell left it unchanged. Fix the TOML and retry. ({exc})"
        ) from exc


def _render_toml(document: Any, raw: str | None, had_final_newline: bool) -> str:
    rendered = tomlkit.dumps(document)
    return rendered if raw is None or had_final_newline else rendered.rstrip("\n")


def codex_project_config_path(project_root: str | Path) -> Path:
    """Return the only Codex file BrainCell is allowed to manage.

    Registration is deliberately limited to a selected Git project root.  This
    prevents a nested ``.codex`` file from overriding a parent project's MCP
    entry with an unrelated project's project ULID.
    """
    root = Path(project_root).resolve()
    if not (root / ".git").exists():
        raise RuntimeError(
            f"Codex project configuration requires the selected Git project root: {root}"
        )
    return root / ".codex" / "config.toml"


def _canonical_codex_entry(
    command: str, args: list[str], env: dict[str, str], cwd: str
) -> dict[str, Any]:
    return {"command": command, "args": list(args), "env": dict(env), "cwd": cwd}


def manage_codex_project_registration(
    project_root: str | Path,
    command: str,
    args: list[str],
    env: dict[str, str],
) -> dict[str, Any]:
    """Create BrainCell's canonical Codex project entry or refuse a conflict."""
    cfg = codex_project_config_path(project_root)
    document, raw, had_final_newline = _read_toml_document(cfg)
    servers = document.get("mcp_servers")
    if servers is None:
        servers = tomlkit.table()
        document.add("mcp_servers", servers)
    elif not isinstance(servers, dict):
        raise RuntimeError(f"{cfg} has a non-table [mcp_servers]; BrainCell left it unchanged.")

    canonical = _canonical_codex_entry(command, args, env, str(Path(project_root).resolve()))
    existing = servers.get(_MCP_SERVER_NAME)
    if existing is not None:
        if _plain(existing) != canonical:
            raise RuntimeError(
                f"{cfg} already has a different [mcp_servers.braincell] entry. "
                "It is user-managed and was not overwritten."
            )
        return {"changed": False, "config_path": str(cfg), "backup_path": None}

    entry = tomlkit.table()
    entry.add("command", command)
    entry.add("args", list(args))
    entry.add("env", dict(env))
    entry.add("cwd", str(Path(project_root).resolve()))
    entry.comment(_CODEX_MANAGED_COMMENT)
    servers.add(_MCP_SERVER_NAME, entry)
    mode = cfg.stat().st_mode if cfg.exists() else None
    backup = _atomic_write_text(cfg, _render_toml(document, raw, had_final_newline), mode)
    return {"changed": True, "config_path": str(cfg), "backup_path": str(backup) if backup else None}


def remove_codex_project_registration(
    project_root: str | Path,
    command: str,
    args: list[str],
    env: dict[str, str],
) -> dict[str, Any]:
    """Remove only a still-canonical project BrainCell entry."""
    cfg = codex_project_config_path(project_root)
    if not cfg.exists():
        return {"changed": False, "config_path": str(cfg), "backup_path": None}
    document, raw, had_final_newline = _read_toml_document(cfg)
    servers = document.get("mcp_servers")
    entry = servers.get(_MCP_SERVER_NAME) if isinstance(servers, dict) else None
    if entry is None:
        return {"changed": False, "config_path": str(cfg), "backup_path": None}
    canonical = _canonical_codex_entry(command, args, env, str(Path(project_root).resolve()))
    if _plain(entry) != canonical:
        raise RuntimeError(
            f"{cfg} has a user-managed BrainCell entry with different settings; it was not removed."
        )
    del servers[_MCP_SERVER_NAME]
    backup = _atomic_write_text(
        cfg, _render_toml(document, raw, had_final_newline), cfg.stat().st_mode
    )
    return {"changed": True, "config_path": str(cfg), "backup_path": str(backup) if backup else None}


def vscode_workspace_config_path(project_root: str | Path) -> Path:
    return Path(project_root).resolve() / ".vscode" / "mcp.json"


def _read_json_object(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.exists():
        return {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot parse {path}; BrainCell left it unchanged. ({exc})") from exc
    if not isinstance(data, dict):
        # Suppression rationale: this is a user-facing configuration error, not a
        # programming type error. Matches automatic_pool_recall.py:55; raising
        # TypeError here would break that contract.
        raise RuntimeError(  # noqa: TRY004
            f"{path} must contain a JSON object; BrainCell left it unchanged."
        )
    return data, path.read_text(encoding="utf-8")


def _canonical_vscode_entry(command: str, args: list[str], env: dict[str, str]) -> dict[str, Any]:
    return {
        "type": "stdio",
        "command": command,
        "args": list(args),
        "env": dict(env),
        "cwd": "${workspaceFolder}",
    }


def manage_vscode_workspace_registration(
    project_root: str | Path, command: str, args: list[str], env: dict[str, str]
) -> dict[str, Any]:
    """Manage only ``servers.braincell`` in workspace ``.vscode/mcp.json``."""
    cfg = vscode_workspace_config_path(project_root)
    data, raw = _read_json_object(cfg)
    servers = data.get("servers")
    if servers is None:
        servers = {}
        data["servers"] = servers
    elif not isinstance(servers, dict):
        raise RuntimeError(f"{cfg} has a non-object 'servers' field; BrainCell left it unchanged.")
    canonical = _canonical_vscode_entry(command, args, env)
    existing = servers.get(_MCP_SERVER_NAME)
    if existing is not None:
        if existing != canonical:
            raise RuntimeError(
                f"{cfg} already has a different servers.braincell entry. "
                "It is user-managed and was not overwritten."
            )
        return {"changed": False, "config_path": str(cfg), "backup_path": None}
    servers[_MCP_SERVER_NAME] = canonical
    mode = cfg.stat().st_mode if cfg.exists() else None
    rendered = json.dumps(data, indent=2, ensure_ascii=False) + ("\n" if raw is None or raw.endswith("\n") else "")
    backup = _atomic_write_text(cfg, rendered, mode)
    return {"changed": True, "config_path": str(cfg), "backup_path": str(backup) if backup else None}


def remove_vscode_workspace_registration(
    project_root: str | Path, command: str, args: list[str], env: dict[str, str]
) -> dict[str, Any]:
    """Remove only BrainCell's canonical workspace entry."""
    cfg = vscode_workspace_config_path(project_root)
    if not cfg.exists():
        return {"changed": False, "config_path": str(cfg), "backup_path": None}
    data, raw = _read_json_object(cfg)
    servers = data.get("servers")
    entry = servers.get(_MCP_SERVER_NAME) if isinstance(servers, dict) else None
    if entry is None:
        return {"changed": False, "config_path": str(cfg), "backup_path": None}
    if entry != _canonical_vscode_entry(command, args, env):
        raise RuntimeError(
            f"{cfg} has a user-managed BrainCell entry with different settings; it was not removed."
        )
    del servers[_MCP_SERVER_NAME]
    rendered = json.dumps(data, indent=2, ensure_ascii=False) + ("\n" if raw is None or raw.endswith("\n") else "")
    backup = _atomic_write_text(cfg, rendered, cfg.stat().st_mode)
    return {"changed": True, "config_path": str(cfg), "backup_path": str(backup) if backup else None}


class CodexClient:
    """Wire BrainCell only into a trusted project's ``.codex/config.toml``."""

    name = "codex"

    def __init__(self, codex_bin: str | None = None):
        self._codex = codex_bin or shutil.which("codex")

    def available(self) -> bool:
        return bool(self._codex)

    def mcp_remove(self, name: str, scope: str | None = None, cwd: str | None = None) -> None:
        if name != _MCP_SERVER_NAME or not cwd:
            raise RuntimeError("Codex project disconnection requires BrainCell's selected project.")
        from .config import DATA_NAMESPACE, get_project_id

        command, args = resolve_portable_server_command()
        project_id = get_project_id(Path(cwd), create=False)
        remove_codex_project_registration(
            cwd,
            command,
            args,
            {
                "BRAINCELL_DATA_NAMESPACE": DATA_NAMESPACE,
                "BRAINCELL_PROJECT_ID": project_id,
                "BRAINCELL_STORE": "sqlite",
            },
        )

    def mcp_add(self, name, command, args, env, scope=None, cwd=None) -> None:
        """Register only in the selected trusted project's configuration."""
        if not self.available():
            raise RuntimeError(
                "`codex` CLI not found on PATH — is Codex installed? "
                "(Codex loads project configuration only for trusted projects.)"
            )
        if name != _MCP_SERVER_NAME or not cwd:
            raise RuntimeError("Codex project connection requires a selected project.")
        manage_codex_project_registration(cwd, command, args, env)


class VSCodeClient:
    """Wire BrainCell only into workspace ``.vscode/mcp.json`` files."""

    name = "vscode"

    def __init__(self, code_bin: str | None = None):
        self._code = code_bin or shutil.which("code")

    def available(self) -> bool:
        return bool(self._code)

    def mcp_add(self, name, command, args, env, scope=None, cwd=None) -> None:
        if name != _MCP_SERVER_NAME or not cwd:
            raise RuntimeError("VS Code workspace connection requires a selected project.")
        manage_vscode_workspace_registration(cwd, command, args, env)

    def mcp_remove(self, name: str, scope: str | None = None, cwd: str | None = None) -> None:
        if name != _MCP_SERVER_NAME or not cwd:
            raise RuntimeError("VS Code workspace disconnection requires BrainCell's selected project.")
        from .config import DATA_NAMESPACE, get_project_id

        command, args = resolve_portable_server_command()
        project_id = get_project_id(Path(cwd), create=False)
        remove_vscode_workspace_registration(
            cwd,
            command,
            args,
            {
                "BRAINCELL_DATA_NAMESPACE": DATA_NAMESPACE,
                "BRAINCELL_PROJECT_ID": project_id,
                "BRAINCELL_STORE": "sqlite",
            },
        )


# ── Read-only registration detection (never shells to a client CLI) ───────────
# The read-only counterpart of mcp_add: reading client config does not violate
# the "never hand-edit ~/.claude.json" rule (that rule is about WRITES, which
# stay on the client CLIs). NEVER shell to `claude mcp list` — it is documented
# to time out on at least one machine; detection reads config files only.

def claude_config_path() -> Path:
    """Path to Claude Code's ``~/.claude.json`` (override via env for tests).

    READ-ONLY here — detection parses this file; writes stay on `claude mcp add`.
    """
    override = os.environ.get("BRAINCELL_CLAUDE_JSON")
    if override:
        return Path(override)
    from .platform import get_claude_config_dir
    return get_claude_config_dir() / ".claude.json"


def codex_config_path() -> Path:
    """Path to Codex's global ``config.toml`` (override via env for tests)."""
    override = os.environ.get("BRAINCELL_CODEX_CONFIG")
    if override:
        return Path(override)
    from .platform import get_codex_config_dir
    return get_codex_config_dir() / "config.toml"


def _claude_registration(path: Path) -> dict:
    """Claude Code registration for *path*: local > user > project scope.

    Shapes (also the per-client contract of registration_status):
      {"registered": True, "scope": "local"|"user"|"project", "command": "..."}
      {"registered": False}          — configs readable, no braincell entry
      {"registered": None}           — parse/IO failure: cannot determine
    """
    resolved = str(Path(path).resolve())
    cfg_file = claude_config_path()
    try:
        cfg: dict = {}
        if cfg_file.exists():
            cfg = json.loads(cfg_file.read_text(encoding="utf-8") or "{}")
        # -s local → projects["<abs path>"].mcpServers.braincell
        entry = (
            ((cfg.get("projects") or {}).get(resolved) or {}).get("mcpServers") or {}
        ).get(_MCP_SERVER_NAME)
        if entry is not None:
            return {
                "registered": True, "scope": "local",
                "command": str(entry.get("command", "")),
            }
        # -s user → top-level mcpServers.braincell
        entry = (cfg.get("mcpServers") or {}).get(_MCP_SERVER_NAME)
        if entry is not None:
            return {
                "registered": True, "scope": "user",
                "command": str(entry.get("command", "")),
            }
        # -s project → <path>/.mcp.json mcpServers.braincell
        mcp_json = Path(resolved) / ".mcp.json"
        if mcp_json.exists():
            proj = json.loads(mcp_json.read_text(encoding="utf-8") or "{}")
            entry = (proj.get("mcpServers") or {}).get(_MCP_SERVER_NAME)
            if entry is not None:
                return {
                    "registered": True, "scope": "project",
                    "command": str(entry.get("command", "")),
                }
        return {"registered": False}
    except Exception:  # noqa: BLE001 — unknown, never an exception into a caller
        return {"registered": None}


def _toml_braincell_entry(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    document, _raw, _newline = _read_toml_document(path)
    servers = document.get("mcp_servers")
    entry = servers.get(_MCP_SERVER_NAME) if isinstance(servers, dict) else None
    return _plain(entry) if entry is not None else None


def _codex_project_registration(path: Path) -> dict[str, Any]:
    """Describe the selected project's entry without consulting global config."""
    resolved = Path(path).resolve()
    result: dict[str, Any] = {
        "config_path": str(resolved / ".codex" / "config.toml"),
        "trust_required": True,
    }
    if not (resolved / ".git").exists():
        return {**result, "registered": False, "target_error": "Codex requires a Git project root."}
    try:
        entry = _toml_braincell_entry(resolved / ".codex" / "config.toml")
        if entry is None:
            return {**result, "registered": False, "conflict": False}
        from .config import DATA_NAMESPACE, resolve_project_id_readonly

        project_id = resolve_project_id_readonly(resolved)
        canonical = (
            _canonical_codex_entry(
                "braincell-mcp",
                [],
                {
                    "BRAINCELL_DATA_NAMESPACE": DATA_NAMESPACE,
                    "BRAINCELL_PROJECT_ID": project_id,
                    "BRAINCELL_STORE": "sqlite",
                },
                str(resolved),
            )
            if project_id
            else None
        )
        return {
            **result,
            "registered": True,
            "command": str(entry.get("command", "")),
            "conflict": canonical is None or entry != canonical,
        }
    except Exception as exc:  # noqa: BLE001 — status must never crash callers
        return {**result, "registered": None, "error": str(exc)}


def _legacy_codex_registration() -> dict[str, Any]:
    """Detect, but never remove, a legacy user-global Codex entry."""
    cfg_file = codex_config_path()
    try:
        entry = _toml_braincell_entry(cfg_file)
        if entry is not None:
            return {
                "registered": True, "scope": "global",
                "command": str(entry.get("command", "")),
            }
        return {"registered": False}
    except Exception:  # noqa: BLE001 — unknown, never an exception into a caller
        return {"registered": None}


def remove_legacy_codex_global_registration(*, confirm: bool = False) -> dict[str, Any]:
    """Explicitly remove only legacy global BrainCell config after confirmation."""
    if not confirm:
        raise RuntimeError(
            "Legacy Codex cleanup is preview-first. Re-run with explicit confirmation to remove only "
            "the global BrainCell entry."
        )
    cfg = codex_config_path()
    if not cfg.exists():
        return {"changed": False, "config_path": str(cfg), "backup_path": None}
    document, raw, had_final_newline = _read_toml_document(cfg)
    servers = document.get("mcp_servers")
    entry = servers.get(_MCP_SERVER_NAME) if isinstance(servers, dict) else None
    if entry is None:
        return {"changed": False, "config_path": str(cfg), "backup_path": None}
    del servers[_MCP_SERVER_NAME]
    backup = _atomic_write_text(
        cfg, _render_toml(document, raw, had_final_newline), cfg.stat().st_mode
    )
    return {"changed": True, "config_path": str(cfg), "backup_path": str(backup) if backup else None}


def registration_status(path: Path) -> dict:
    """Read-only MCP-registration detection for *path*, per client.

    Project and legacy-global states stay distinct so a global entry never
    masquerades as an isolated project connection.
    """
    return {
        "claude": _claude_registration(path),
        "codex": {
            "project": _codex_project_registration(path),
            "legacy_global": _legacy_codex_registration(),
        },
        "vscode": {"registered": None, "scope": "workspace"},
    }


def claude_registered_map(paths: list[str]) -> dict[str, bool]:
    """Claude-client registration summary for MANY paths → ``{path: bool}``.

    The Memory Map calls this once for every registered project, so
    ``~/.claude.json`` is parsed ONCE and each path is a dict lookup (plus a
    cheap per-path ``<path>/.mcp.json`` check for project-scope
    registrations). Unknown/parse failure degrades to False — the map's badge
    is a summary, not an authority (the inspector uses registration_status).
    """
    cfg_file = claude_config_path()
    try:
        cfg: dict = {}
        if cfg_file.exists():
            cfg = json.loads(cfg_file.read_text(encoding="utf-8") or "{}")
        user_scope = _MCP_SERVER_NAME in (cfg.get("mcpServers") or {})
        projects = cfg.get("projects") or {}
    except Exception:  # noqa: BLE001 — summary degrades to False, never raises
        return dict.fromkeys(paths, False)
    out: dict[str, bool] = {}
    for p in paths:
        if user_scope:
            out[p] = True
            continue
        registered = _MCP_SERVER_NAME in (
            (projects.get(p) or {}).get("mcpServers") or {}
        )
        if not registered:
            try:
                mcp_json = Path(p) / ".mcp.json"
                if mcp_json.exists():
                    proj = json.loads(mcp_json.read_text(encoding="utf-8") or "{}")
                    registered = _MCP_SERVER_NAME in (proj.get("mcpServers") or {})
            except Exception:  # noqa: BLE001
                registered = False
        out[p] = registered
    return out


# ── OpenCode client adapter ────────────────────────────────────────────────

class OpenCodeClient:
    """Wire BrainCell only into project ``opencode.json`` MCP servers."""

    name = "opencode"

    def __init__(self, opencode_bin: str | None = None):
        self._opencode = opencode_bin or shutil.which("opencode")

    def available(self) -> bool:
        return bool(self._opencode)

    def mcp_add(self, name, command, args, env, scope=None, cwd=None) -> None:
        if name != _MCP_SERVER_NAME or not cwd:
            raise RuntimeError("OpenCode connection requires a selected project.")
        manage_opencode_project_registration(cwd, command, args, env)

    def mcp_remove(self, name: str, scope: str | None = None, cwd: str | None = None) -> None:
        if name != _MCP_SERVER_NAME or not cwd:
            raise RuntimeError("OpenCode disconnection requires BrainCell's selected project.")
        from .config import DATA_NAMESPACE, get_project_id

        command, args = resolve_portable_server_command()
        project_id = get_project_id(Path(cwd), create=False)
        remove_opencode_project_registration(
            cwd,
            command,
            args,
            {
                "BRAINCELL_DATA_NAMESPACE": DATA_NAMESPACE,
                "BRAINCELL_PROJECT_ID": project_id,
                "BRAINCELL_STORE": "sqlite",
            },
        )


def opencode_project_config_path(project_root: str | Path) -> Path:
    """Return the project-local OpenCode config path."""
    from .platform import get_opencode_project_config_path

    return get_opencode_project_config_path(Path(project_root))


def _canonical_opencode_entry(
    command: str, args: list[str], env: dict[str, str]
) -> dict[str, Any]:
    # OpenCode's local-server schema takes ``command`` as an argv array; the
    # server arguments ride in the same array, not a separate ``args`` field.
    return {
        "type": "local",
        "command": [command, *args],
        "enabled": True,
        "environment": dict(env),
    }


def manage_opencode_project_registration(
    project_root: str | Path, command: str, args: list[str], env: dict[str, str]
) -> dict[str, Any]:
    """Write ``mcp.braincell`` into ``opencode.json`` (idempotent, conflict-safe)."""
    cfg = opencode_project_config_path(project_root)
    data, raw = _read_json_object(cfg)
    mcp = data.get("mcp")
    if mcp is None:
        mcp = {}
        data["mcp"] = mcp
    elif not isinstance(mcp, dict):
        raise RuntimeError(f"{cfg} has a non-object 'mcp' field; BrainCell left it unchanged.")
    canonical = _canonical_opencode_entry(command, args, env)
    existing = mcp.get(_MCP_SERVER_NAME)
    if existing is not None:
        if existing != canonical:
            raise RuntimeError(
                f"{cfg} already has a different mcp.braincell entry. "
                "It is user-managed and was not overwritten."
            )
        return {"changed": False, "config_path": str(cfg), "backup_path": None}
    mcp[_MCP_SERVER_NAME] = canonical
    mode = cfg.stat().st_mode if cfg.exists() else None
    rendered = json.dumps(data, indent=2, ensure_ascii=False) + (
        "\n" if raw is None or raw.endswith("\n") else ""
    )
    backup = _atomic_write_text(cfg, rendered, mode)
    return {"changed": True, "config_path": str(cfg), "backup_path": str(backup) if backup else None}


def remove_opencode_project_registration(
    project_root: str | Path, command: str, args: list[str], env: dict[str, str]
) -> dict[str, Any]:
    """Remove ``mcp.braincell`` from ``opencode.json`` (conflict-safe)."""
    cfg = opencode_project_config_path(project_root)
    if not cfg.exists():
        return {"changed": False, "config_path": str(cfg), "backup_path": None}
    data, raw = _read_json_object(cfg)
    mcp = data.get("mcp")
    entry = mcp.get(_MCP_SERVER_NAME) if isinstance(mcp, dict) else None
    if entry is None:
        return {"changed": False, "config_path": str(cfg), "backup_path": None}
    if entry != _canonical_opencode_entry(command, args, env):
        raise RuntimeError(
            f"{cfg} has a user-managed BrainCell entry with different settings; it was not removed."
        )
    del mcp[_MCP_SERVER_NAME]
    rendered = json.dumps(data, indent=2, ensure_ascii=False) + (
        "\n" if raw is None or raw.endswith("\n") else ""
    )
    backup = _atomic_write_text(cfg, rendered, cfg.stat().st_mode)
    return {"changed": True, "config_path": str(cfg), "backup_path": str(backup) if backup else None}


# Client registry — maps the CLI --client key to an adapter.
CLIENTS = {
    "claude": ClaudeCodeClient,
    "codex": CodexClient,
    "vscode": VSCodeClient,
    "opencode": OpenCodeClient,
}


def get_client(key: str):
    """Instantiate the adapter for a --client key. Raises ValueError on an unknown key."""
    try:
        return CLIENTS[key]()
    except KeyError:
        raise ValueError(
            f"Unknown client {key!r}. Choose from: {', '.join(CLIENTS)}."
        ) from None
