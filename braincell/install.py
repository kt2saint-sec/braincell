# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
install.py — turnkey self-wiring of braincell into an MCP client.

`braincell install` registers the per-project MCP server AND installs the proactive
family-recall hook so nobody hand-edits client config. This module holds the
client-specific mechanics behind a small adapter seam; Claude Code is implemented
here (a local-model Claude Code session is covered by the
same mechanism). Codex / Antigravity / VS Code / Cursor adapters slot in later.

Two things wire two different ways:
  - MCP server  → the client's MCP config, via the official `claude mcp add` CLI
    (never hand-edit ~/.claude.json — the CLI is secrets-aware and format-stable).
  - Hook        → merged into ~/.claude/settings.json hooks.UserPromptSubmit
    (there is no `claude hooks` CLI). APPEND-ONLY + backup so a co-resident hook
    (e.g. iron-law) is never clobbered. Atomic tmp+rename (project_registry idiom).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

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


def hook_command(python: str | None = None) -> str:
    """The UserPromptSubmit hook command string (`<python> -m braincell.family_hook`)."""
    return f"{python or sys.executable} -m braincell.family_hook"


# ── Claude Code settings.json (hook install) ──────────────────────────────────

def claude_settings_path() -> Path:
    """Path to Claude Code's user settings.json (override via env for tests)."""
    override = os.environ.get("BRAINCELL_CLAUDE_SETTINGS")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "settings.json"


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


# ── Claude Code skills (packaged SKILL.md → ~/.claude/skills/) ────────────────

def claude_skills_dir() -> Path:
    """Path to Claude Code's user skills dir (override via env for tests)."""
    override = os.environ.get("BRAINCELL_CLAUDE_SKILLS_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "skills"


def packaged_skills() -> list[str]:
    """Names of the skills shipped inside the wheel (``braincell/skills/<name>/``)."""
    from importlib.resources import files
    root = files("braincell").joinpath("skills")
    if not root.is_dir():
        return []
    return sorted(e.name for e in root.iterdir() if e.is_dir())


def install_skills(target_dir: Path | None = None) -> list[tuple[str, str, Path]]:
    """Copy packaged skills into Claude Code's skills dir.

    Returns one ``(name, status, path)`` per skill, where status is:
      ``installed``  — written (destination did not exist)
      ``current``    — destination already byte-identical; nothing written
      ``conflict``   — destination exists with DIFFERENT content; left untouched

    Never clobbers: a user may have their own skill of the same name, and silently
    overwriting it would destroy work no backup covers. Conflicts are reported so
    the caller can print a manual step — the same posture as the VS Code adapter,
    which refuses to guess when it cannot act safely.
    """
    from importlib.resources import files

    dest_root = target_dir or claude_skills_dir()
    src_root = files("braincell").joinpath("skills")
    results: list[tuple[str, str, Path]] = []

    for name in packaged_skills():
        payload = src_root.joinpath(name, "SKILL.md").read_text(encoding="utf-8")
        dest = dest_root / name / "SKILL.md"
        if dest.exists():
            try:
                same = dest.read_text(encoding="utf-8") == payload
            except OSError:
                same = False
            results.append((name, "current" if same else "conflict", dest))
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(dest)                      # atomic, mirrors _atomic_write_json
        results.append((name, "installed", dest))

    return results


# ── systemd --user service (opt-in always-on Memory-Map) ──────────────────────
#
# Fable-advised: opt-in ONLY, never a default — an always-on daemon is
# footprint-surprise for a local desktop tool, and the probe-then-start desktop
# icon already makes "server down" a one-click fix. This installs a `--user`
# unit for the user who WANTS the Map to survive logout/reboot and restart on
# failure. All systemctl calls funnel through `_run_systemctl` (one seam for
# tests to fake — never touching the real user manager) and degrade to
# "unit written, enable it yourself" when systemd is absent, rather than raising.

_SERVICE_UNIT = "braincell-map.service"


def systemd_user_dir() -> Path:
    """The systemd --user unit directory (override via env for tests)."""
    override = os.environ.get("BRAINCELL_SYSTEMD_USER_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "systemd" / "user"


def _service_unit_text(port: int, namespace: str) -> str:
    """Render the braincell-map.service unit — global, writable, no browser.

    ExecStart uses the ABSOLUTE interpreter + ``-m braincell.cli gui`` so it runs
    under systemd's minimal --user PATH, and always BINDS and BLOCKS (uvicorn) so
    ``Restart=on-failure`` genuinely keeps it up (unlike the icon's braincell-map,
    which reuses-and-exits when the port is already served).
    """
    exec_start = (
        f"{sys.executable} -m braincell.cli gui --mode global "
        f"--allow-writes --no-browser --port {port}"
    )
    return (
        "[Unit]\n"
        "Description=BrainCell Memory Map (local, always-on)\n"
        "After=default.target\n"
        # Rate-limit restarts so a PERMANENT config-level failure (e.g. an
        # embedder-fingerprint mismatch) lands the unit in `failed` instead of
        # crash-looping forever. systemd's defaults (10 s interval, burst 5)
        # can NEVER trip with RestartSec=3 — five 3s-apart restarts span ~25 s,
        # outside any 10 s window — which is exactly how this unit once burned
        # ~2 s CPU every 3 s across 800+ restarts. 5 failures inside 2 minutes
        # → give up (transient one-off crashes still restart fine); recover
        # with `systemctl --user restart braincell-map` once the cause is
        # fixed. These directives belong in [Unit] (systemd ≥ 229).
        "StartLimitIntervalSec=120\n"
        "StartLimitBurst=5\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"Environment=BRAINCELL_DATA_NAMESPACE={namespace}\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=3\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _run_systemctl(args: list[str]) -> tuple[int, str]:
    """Run ``systemctl --user <args>`` → (returncode, combined output).

    Returns ``(127, <msg>)`` when systemctl is absent (headless / no systemd), so
    callers degrade gracefully. Single function on purpose: tests monkeypatch it.
    """
    exe = shutil.which("systemctl")
    if not exe:
        return 127, "systemctl not found (no systemd on this host)"
    proc = subprocess.run([exe, "--user", *args], capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _svc_active() -> bool:
    return _run_systemctl(["is-active", _SERVICE_UNIT])[0] == 0


def _svc_enabled() -> bool:
    return _run_systemctl(["is-enabled", _SERVICE_UNIT])[0] == 0


def install_service(port: int = 8765) -> dict:
    """Write + enable + start the always-on Map service unit (opt-in, idempotent).

    Rewrites the unit, ``daemon-reload``, then ``enable --now``. systemctl
    failures land in ``detail`` (not raised) — the unit file is still written so
    the user can enable it once a manager is available.
    """
    from . import config

    unit_dir = systemd_user_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / _SERVICE_UNIT
    tmp = unit_path.with_name(unit_path.name + ".tmp")
    tmp.write_text(_service_unit_text(port, config.DATA_NAMESPACE), encoding="utf-8")
    tmp.replace(unit_path)                       # atomic (mirrors install_skills)

    details: list[str] = []
    for step in (["daemon-reload"], ["enable", "--now", _SERVICE_UNIT]):
        rc, out = _run_systemctl(step)
        if rc != 0:
            details.append(f"{' '.join(step)}: {out}")
    return {
        "unit_path": str(unit_path),
        "installed": True,
        "active": _svc_active(),
        "enabled": _svc_enabled(),
        "detail": "; ".join(details),
    }


def uninstall_service() -> dict:
    """Disable + stop + remove the Map service unit (best-effort, idempotent)."""
    unit_path = systemd_user_dir() / _SERVICE_UNIT
    details: list[str] = []
    rc, out = _run_systemctl(["disable", "--now", _SERVICE_UNIT])
    low = out.lower()
    if rc != 0 and "not loaded" not in low and "no such file" not in low:
        details.append(f"disable --now: {out}")
    removed = unit_path.exists()
    unit_path.unlink(missing_ok=True)
    _run_systemctl(["daemon-reload"])
    return {
        "unit_path": str(unit_path),
        "installed": unit_path.exists(),
        "removed": removed,
        "active": _svc_active(),
        "enabled": _svc_enabled(),
        "detail": "; ".join(details),
    }


def service_status() -> dict:
    """Report the Map service state: unit present? active? enabled? failing?

    ``state``/``substate``/``result``/``restarts`` come from ``systemctl show``
    so a crash-looping or start-limit-hit unit is VISIBLE (not just
    ``active: False`` with no explanation). ``failure`` carries the last
    journal error lines when the unit is failing, so the GUI can tell the user
    WHY (e.g. an embedder-fingerprint mismatch) instead of dying silently.
    All best-effort: a faked/absent systemctl degrades to the original shape.
    """
    unit_path = systemd_user_dir() / _SERVICE_UNIT
    status: dict = {
        "unit_path": str(unit_path),
        "installed": unit_path.exists(),
        "active": _svc_active(),
        "enabled": _svc_enabled(),
    }
    rc, out = _run_systemctl([
        "show", _SERVICE_UNIT,
        "-p", "ActiveState,SubState,Result,NRestarts",
    ])
    if rc == 0 and out:
        props = dict(
            line.split("=", 1) for line in out.splitlines() if "=" in line
        )
        status["state"] = props.get("ActiveState", "")
        status["substate"] = props.get("SubState", "")
        status["result"] = props.get("Result", "")
        try:
            status["restarts"] = int(props.get("NRestarts", "0"))
        except ValueError:
            status["restarts"] = 0
        status["failing"] = (
            status["state"] == "failed"
            or status["substate"] == "auto-restart"
            or status["result"] not in ("", "success")
        )
        if status["failing"]:
            failure = _service_failure_reason()
            if failure:
                status["failure"] = failure
    return status


def _service_failure_reason() -> str:
    """Best-effort: the most recent actionable error line from the unit's journal.

    Prefers the ``FATAL:`` line the GUI logs for permanent config-level
    failures (e.g. embedder-fingerprint mismatch — gui.py logs it clean, no
    traceback); falls back to the journal's last line. Empty string when
    journalctl is unavailable or has nothing.
    """
    journalctl = shutil.which("journalctl")
    if not journalctl:
        return ""
    proc = subprocess.run(
        [journalctl, "--user", "-u", _SERVICE_UNIT,
         "-n", "50", "-o", "cat", "--no-pager"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return ""
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    for line in reversed(lines):
        if "FATAL:" in line:
            return line[line.index("FATAL:"):]
    return lines[-1] if lines else ""


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
            [self._claude, *args], cwd=cwd, capture_output=True, text=True,
        )

    def mcp_remove(self, name: str, scope: str, cwd: str | None = None) -> None:
        """Best-effort remove (ignore 'not found') so add is idempotent."""
        self._run(["mcp", "remove", name, "-s", scope], cwd)

    def mcp_add(
        self,
        name: str,
        command: str,
        args: list[str],
        env: dict[str, str],
        scope: str,
        cwd: str | None = None,
    ) -> None:
        """Register the stdio MCP server (remove-then-add = idempotent). Raises on failure."""
        if not self.available():
            raise RuntimeError(
                "`claude` CLI not found on PATH — is Claude Code installed? "
                "(the MCP registration uses `claude mcp add`)."
            )
        self.mcp_remove(name, scope, cwd)
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


class CodexClient:
    """Wire braincell into Codex via `codex mcp add`. MCP-only (Codex has no
    UserPromptSubmit-style hook). Config is global (`~/.codex/config.toml`)."""

    name = "codex"

    def __init__(self, codex_bin: str | None = None):
        self._codex = codex_bin or shutil.which("codex")

    def available(self) -> bool:
        return bool(self._codex)

    def _run(self, args: list[str], cwd: str | None) -> subprocess.CompletedProcess:
        return subprocess.run([self._codex, *args], cwd=cwd, capture_output=True, text=True)

    def mcp_remove(self, name: str, scope: str | None = None, cwd: str | None = None) -> None:
        """Best-effort remove (ignore 'not found') so add is idempotent."""
        self._run(["mcp", "remove", name], cwd)

    def mcp_add(self, name, command, args, env, scope=None, cwd=None) -> None:
        """Register the stdio MCP server (remove-then-add = idempotent). Raises on failure.
        ``scope`` is accepted for a uniform call site but ignored — Codex config is global."""
        if not self.available():
            raise RuntimeError(
                "`codex` CLI not found on PATH — is Codex installed? "
                "(the MCP registration uses `codex mcp add`)."
            )
        self.mcp_remove(name, cwd=cwd)
        argv = ["mcp", "add", name]
        for key, val in env.items():
            argv += ["--env", f"{key}={val}"]
        argv += ["--", command, *args]
        res = self._run(argv, cwd)
        if res.returncode != 0:
            raise RuntimeError(
                f"`codex mcp add {name}` failed (exit {res.returncode}): "
                f"{res.stderr.strip() or res.stdout.strip()}"
            )


class VSCodeClient:
    """Wire braincell into VS Code via `code --add-mcp`. MCP-only. Writes the user
    MCP config (`User/mcp.json`, keyed by name → re-adding updates in place).

    NOTE: VS Code exposes no remove-MCP CLI (only `--add-mcp`), so uninstall is a
    documented MANUAL step — mcp_remove raises with instructions rather than pretending."""

    name = "vscode"

    def __init__(self, code_bin: str | None = None):
        self._code = code_bin or shutil.which("code")

    def available(self) -> bool:
        return bool(self._code)

    def mcp_add(self, name, command, args, env, scope=None, cwd=None) -> None:
        if not self.available():
            raise RuntimeError(
                "`code` CLI not found on PATH — install VS Code's shell command "
                "(Command Palette → 'Shell Command: Install code command in PATH')."
            )
        payload: dict = {"name": name, "command": command, "args": list(args)}
        if env:
            payload["env"] = dict(env)
        res = subprocess.run(
            [self._code, "--add-mcp", json.dumps(payload)],
            cwd=cwd, capture_output=True, text=True,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"`code --add-mcp` failed (exit {res.returncode}): "
                f"{res.stderr.strip() or res.stdout.strip()}"
            )

    def mcp_remove(self, name: str, scope: str | None = None, cwd: str | None = None) -> None:
        raise NotImplementedError(
            "VS Code has no remove-MCP CLI. Remove the 'braincell' entry manually: "
            "Command Palette → 'MCP: Open User Configuration', or delete it from "
            "User/mcp.json."
        )


# ── Read-only registration detection (never shells to a client CLI) ───────────
# The read-only counterpart of mcp_add: reading client config does not violate
# the "never hand-edit ~/.claude.json" rule (that rule is about WRITES, which
# stay on the client CLIs). NEVER shell to `claude mcp list` — it is documented
# to time out on at least one machine; detection reads config files only.

_MCP_SERVER_NAME = "braincell"


def claude_config_path() -> Path:
    """Path to Claude Code's ``~/.claude.json`` (override via env for tests).

    READ-ONLY here — detection parses this file; writes stay on `claude mcp add`.
    """
    override = os.environ.get("BRAINCELL_CLAUDE_JSON")
    if override:
        return Path(override)
    return Path.home() / ".claude.json"


def codex_config_path() -> Path:
    """Path to Codex's global ``config.toml`` (override via env for tests)."""
    override = os.environ.get("BRAINCELL_CODEX_CONFIG")
    if override:
        return Path(override)
    return Path.home() / ".codex" / "config.toml"


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


def _codex_registration() -> dict:
    """Codex registration (global config — path-independent). Same shapes as
    _claude_registration; scope is always "global"."""
    cfg_file = codex_config_path()
    try:
        if not cfg_file.exists():
            return {"registered": False}
        import tomllib  # noqa: PLC0415 — stdlib on the required python>=3.11
        data = tomllib.loads(cfg_file.read_text(encoding="utf-8"))
        entry = (data.get("mcp_servers") or {}).get(_MCP_SERVER_NAME)
        if isinstance(entry, dict):
            return {
                "registered": True, "scope": "global",
                "command": str(entry.get("command", "")),
            }
        return {"registered": False}
    except Exception:  # noqa: BLE001 — unknown, never an exception into a caller
        return {"registered": None}


def registration_status(path: Path) -> dict:
    """Read-only MCP-registration detection for *path*, per client.

    ``{"claude": {...}, "codex": {...}, "vscode": {"registered": None}}`` with
    the per-client shapes documented on _claude_registration. VS Code's
    ``User/mcp.json`` location is platform/variant-dependent → honest
    ``None`` ("cannot determine") rather than a guess. Detection must never
    raise into a status endpoint — any parse/IO failure is ``None``.
    """
    return {
        "claude": _claude_registration(path),
        "codex": _codex_registration(),
        "vscode": {"registered": None},
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


# Client registry — maps the CLI --client key to an adapter.
CLIENTS = {
    "claude": ClaudeCodeClient,
    "codex": CodexClient,
    "vscode": VSCodeClient,
}


def get_client(key: str):
    """Instantiate the adapter for a --client key. Raises ValueError on an unknown key."""
    try:
        return CLIENTS[key]()
    except KeyError:
        raise ValueError(
            f"Unknown client {key!r}. Choose from: {', '.join(CLIENTS)}."
        ) from None
