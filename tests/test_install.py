# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_install.py — turnkey self-wiring (`braincell install` / `uninstall` / `hook`).

Offline: `claude` is never invoked (subprocess.run is monkeypatched to capture argv),
and settings.json is redirected to a temp file via BRAINCELL_CLAUDE_SETTINGS. Asserts
the MCP registration argv, the append-only + idempotent hook merge (a co-resident
iron-law-like hook is preserved), and the arm/disarm flag lifecycle.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from braincell import install as inst
from braincell.cli import main


# ── command resolution ──────────────────────────────────────────────────────────

def test_resolve_server_command_prefers_console_script(monkeypatch):
    monkeypatch.setattr(inst.shutil, "which", lambda n: "/usr/bin/braincell-mcp"
                        if n == "braincell-mcp" else None)
    cmd, args = inst.resolve_server_command()
    assert cmd == "/usr/bin/braincell-mcp" and args == []


def test_resolve_server_command_fallback_to_module(monkeypatch):
    monkeypatch.setattr(inst.shutil, "which", lambda n: None)
    cmd, args = inst.resolve_server_command()
    assert args == ["-m", "braincell.server"]


def test_hook_command_uses_module(monkeypatch):
    assert inst.hook_command("/x/py") == "/x/py -m braincell.family_hook"


# ── hook merge into settings.json ───────────────────────────────────────────────

def _settings(tmp_path, monkeypatch, initial=None):
    path = tmp_path / "settings.json"
    if initial is not None:
        path.write_text(json.dumps(initial), encoding="utf-8")
    monkeypatch.setenv("BRAINCELL_CLAUDE_SETTINGS", str(path))
    return path


_IRONLAW = {"hooks": {"UserPromptSubmit": [
    {"hooks": [{"type": "command", "command": "bash /x/check-iron-law.sh"}]}
]}}


def test_install_hook_appends_preserving_others(tmp_path, monkeypatch):
    path = _settings(tmp_path, monkeypatch, _IRONLAW)
    assert inst.install_hook("py -m braincell.family_hook") is True

    data = json.loads(path.read_text())
    ups = data["hooks"]["UserPromptSubmit"]
    cmds = [h["command"] for e in ups for h in e["hooks"]]
    assert "bash /x/check-iron-law.sh" in cmds, "iron-law hook must be preserved"
    assert any("braincell.family_hook" in c for c in cmds), "braincell hook must be added"
    assert (tmp_path / "settings.json.bak").exists(), "a backup must be written"


def test_install_hook_is_idempotent(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, _IRONLAW)
    assert inst.install_hook("py -m braincell.family_hook") is True
    assert inst.install_hook("py -m braincell.family_hook") is False  # no dup
    data = json.loads((tmp_path / "settings.json").read_text())
    cmds = [h["command"] for e in data["hooks"]["UserPromptSubmit"] for h in e["hooks"]]
    assert sum("braincell.family_hook" in c for c in cmds) == 1


def test_uninstall_hook_removes_only_braincell(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, _IRONLAW)
    inst.install_hook("py -m braincell.family_hook")
    removed = inst.uninstall_hook()
    assert removed == 1
    data = json.loads((tmp_path / "settings.json").read_text())
    cmds = [h["command"] for e in data["hooks"]["UserPromptSubmit"] for h in e["hooks"]]
    assert cmds == ["bash /x/check-iron-law.sh"], "only braincell entry removed"


def test_install_hook_from_empty_settings(tmp_path, monkeypatch):
    path = _settings(tmp_path, monkeypatch)  # no file yet
    assert inst.install_hook("py -m braincell.family_hook") is True
    assert path.exists()


def test_load_json_refuses_corrupt(tmp_path, monkeypatch):
    path = _settings(tmp_path, monkeypatch)
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(RuntimeError):
        inst.install_hook("py -m braincell.family_hook")


# ── MCP registration argv (claude never actually runs) ──────────────────────────

def test_mcp_add_builds_correct_argv(monkeypatch):
    calls = []

    def fake_run(argv, cwd=None, capture_output=True, text=True):
        calls.append((argv, cwd))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(inst.subprocess, "run", fake_run)
    client = inst.ClaudeCodeClient(claude_bin="/fake/claude")
    client.mcp_add("braincell", "/bin/braincell-mcp", [],
                   {"BRAINCELL_PROJECT_ID": "PID1", "BRAINCELL_DATA_NAMESPACE": "braincell"},
                   scope="local", cwd="/repo")

    # remove-then-add → two calls; the add carries name, scope, -e pairs, and the command.
    assert calls[0][0][:3] == ["/fake/claude", "mcp", "remove"]
    add_argv, add_cwd = calls[1]
    assert add_cwd == "/repo"
    assert add_argv[:5] == ["/fake/claude", "mcp", "add", "braincell", "-s"]
    assert "local" in add_argv
    assert "-e" in add_argv and "BRAINCELL_PROJECT_ID=PID1" in add_argv
    assert add_argv[-2:] == ["--", "/bin/braincell-mcp"] or add_argv[-1] == "/bin/braincell-mcp"


def test_mcp_add_raises_on_failure(monkeypatch):
    def fake_run(argv, cwd=None, capture_output=True, text=True):
        rc = 0 if argv[2] == "remove" else 1
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="boom")

    monkeypatch.setattr(inst.subprocess, "run", fake_run)
    client = inst.ClaudeCodeClient(claude_bin="/fake/claude")
    with pytest.raises(RuntimeError, match="boom"):
        client.mcp_add("braincell", "cmd", [], {}, scope="local")


def test_mcp_add_no_claude_binary_raises(monkeypatch):
    monkeypatch.setattr(inst.shutil, "which", lambda n: None)  # no claude on PATH
    client = inst.ClaudeCodeClient(claude_bin=None)
    assert client.available() is False
    with pytest.raises(RuntimeError, match="claude"):
        client.mcp_add("braincell", "cmd", [], {}, scope="local")


# ── Codex adapter ───────────────────────────────────────────────────────────────

def test_codex_mcp_add_builds_argv(monkeypatch):
    calls = []

    def fake_run(argv, cwd=None, capture_output=True, text=True):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(inst.subprocess, "run", fake_run)
    client = inst.CodexClient(codex_bin="/fake/codex")
    client.mcp_add("braincell", "/bin/braincell-mcp", [],
                   {"BRAINCELL_PROJECT_ID": "PID1"}, cwd="/repo")

    # remove-then-add; codex uses `--env K=V` (not `-e`) and has no `-s` scope.
    assert calls[0][:4] == ["/fake/codex", "mcp", "remove", "braincell"]
    add = calls[1]
    assert add[:4] == ["/fake/codex", "mcp", "add", "braincell"]
    assert "--env" in add and "BRAINCELL_PROJECT_ID=PID1" in add
    assert "-s" not in add  # codex is global-scope, no scope flag
    assert add[-2:] == ["--", "/bin/braincell-mcp"]


def test_codex_mcp_add_raises_on_failure(monkeypatch):
    def fake_run(argv, cwd=None, capture_output=True, text=True):
        rc = 0 if argv[2] == "remove" else 1
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="nope")

    monkeypatch.setattr(inst.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="nope"):
        inst.CodexClient(codex_bin="/fake/codex").mcp_add("braincell", "cmd", [], {})


# ── VS Code adapter ─────────────────────────────────────────────────────────────

def test_vscode_mcp_add_builds_json(monkeypatch):
    calls = []

    def fake_run(argv, cwd=None, capture_output=True, text=True):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(inst.subprocess, "run", fake_run)
    client = inst.VSCodeClient(code_bin="/fake/code")
    client.mcp_add("braincell", "/bin/bc-mcp", ["-m", "x"],
                   {"BRAINCELL_PROJECT_ID": "PID1"})

    assert calls[0][:2] == ["/fake/code", "--add-mcp"]
    payload = json.loads(calls[0][2])
    assert payload["name"] == "braincell"
    assert payload["command"] == "/bin/bc-mcp"
    assert payload["args"] == ["-m", "x"]
    assert payload["env"] == {"BRAINCELL_PROJECT_ID": "PID1"}


def test_vscode_mcp_remove_is_manual(monkeypatch):
    with pytest.raises(NotImplementedError, match="manually"):
        inst.VSCodeClient(code_bin="/fake/code").mcp_remove("braincell")


# ── client registry ─────────────────────────────────────────────────────────────

def test_get_client_returns_right_types():
    assert isinstance(inst.get_client("claude"), inst.ClaudeCodeClient)
    assert isinstance(inst.get_client("codex"), inst.CodexClient)
    assert isinstance(inst.get_client("vscode"), inst.VSCodeClient)


def test_get_client_unknown_raises():
    with pytest.raises(ValueError, match="Unknown client"):
        inst.get_client("emacs")


def test_cmd_install_codex_mcp_only_no_hook(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch)  # redirect settings; must stay untouched for codex
    monkeypatch.setattr(inst.shutil, "which",
                        lambda n: f"/fake/{n}")  # codex + braincell-mcp resolvable
    monkeypatch.setattr(inst.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""))
    repo = tmp_path / "repoC"
    repo.mkdir()
    main(["install", str(repo), "--client", "codex"])
    out = capsys.readouterr().out
    assert "registered braincell MCP with codex" in out
    assert "Claude Code-only" in out  # hook explicitly skipped
    assert "Restart Codex" in out
    # no Claude settings hook written
    assert not (tmp_path / "settings.json").exists() or \
        "UserPromptSubmit" not in (tmp_path / "settings.json").read_text()


# ── CLI: install end-to-end (claude + subprocess faked) ─────────────────────────

def test_cmd_install_registers_and_installs_hook(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch)
    monkeypatch.setattr(inst.shutil, "which",
                        lambda n: "/fake/claude" if n == "claude" else None)
    monkeypatch.setattr(inst.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""))

    repo = tmp_path / "repoI"
    repo.mkdir()
    main(["install", str(repo)])
    out = capsys.readouterr().out
    assert "registered braincell MCP" in out
    assert "installed family-recall hook (DISARMED)" in out
    # the hook really landed in the redirected settings.json
    data = json.loads((tmp_path / "settings.json").read_text())
    cmds = [h["command"] for e in data["hooks"]["UserPromptSubmit"] for h in e["hooks"]]
    assert any("braincell.family_hook" in c for c in cmds)


def test_cmd_install_default_no_federate_key(tmp_path, monkeypatch, capsys):
    """Regression: a vanilla install must be byte-identical — no BRAINCELL_FEDERATE key."""
    _settings(tmp_path, monkeypatch)
    monkeypatch.setattr(inst.shutil, "which",
                        lambda n: "/fake/claude" if n == "claude" else None)
    calls = []

    def fake_run(argv, cwd=None, capture_output=True, text=True):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(inst.subprocess, "run", fake_run)
    repo = tmp_path / "repoDefault"
    repo.mkdir()
    main(["install", str(repo)])
    add_argv = calls[1]  # remove-then-add
    assert not any(a.startswith("BRAINCELL_FEDERATE=") for a in add_argv)
    out = capsys.readouterr().out
    assert "federation" not in out.lower()


def test_cmd_install_federate_stamps_env(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch)
    monkeypatch.setattr(inst.shutil, "which",
                        lambda n: "/fake/claude" if n == "claude" else None)
    calls = []

    def fake_run(argv, cwd=None, capture_output=True, text=True):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(inst.subprocess, "run", fake_run)
    repo = tmp_path / "repoFederate"
    repo.mkdir()
    main(["install", str(repo), "--federate"])
    add_argv = calls[1]
    assert "BRAINCELL_FEDERATE=on" in add_argv
    assert any(a.startswith("BRAINCELL_PROJECT_ID=") for a in add_argv)
    assert any(a.startswith("BRAINCELL_DATA_NAMESPACE=") for a in add_argv)
    out = capsys.readouterr().out
    assert "federation: on" in out.lower()


def test_cmd_install_federate_with_global_warns_and_skips(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch)
    monkeypatch.setattr(inst.shutil, "which",
                        lambda n: "/fake/claude" if n == "claude" else None)
    calls = []

    def fake_run(argv, cwd=None, capture_output=True, text=True):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(inst.subprocess, "run", fake_run)
    main(["install", "--global", "--federate"])
    add_argv = calls[1]
    assert "BRAINCELL_MODE=global" in add_argv
    assert not any(a.startswith("BRAINCELL_FEDERATE=") for a in add_argv)
    err = capsys.readouterr().err
    assert "--federate ignored with --global" in err


def test_cmd_install_no_hook_flag(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch)
    monkeypatch.setattr(inst.shutil, "which",
                        lambda n: "/fake/claude" if n == "claude" else None)
    monkeypatch.setattr(inst.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""))
    repo = tmp_path / "repoN"
    repo.mkdir()
    main(["install", str(repo), "--no-hook"])
    assert not (tmp_path / "settings.json").exists() or \
        "UserPromptSubmit" not in (tmp_path / "settings.json").read_text()


def test_cmd_install_errors_without_claude(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch)
    monkeypatch.setattr(inst.shutil, "which", lambda n: None)  # no claude, no braincell-mcp
    repo = tmp_path / "repoX"
    repo.mkdir()
    with pytest.raises(SystemExit) as exc:
        main(["install", str(repo)])
    assert exc.value.code == 1
    assert "claude" in capsys.readouterr().err.lower()


# ── CLI: hook arm/disarm/status ─────────────────────────────────────────────────

def test_cmd_hook_lifecycle(tmp_path, monkeypatch, capsys):
    flag = tmp_path / "state" / "flag.txt"
    monkeypatch.setenv("BRAINCELL_FAMILY_HOOK_FLAG", str(flag))

    main(["hook", "status"])
    assert "disarmed" in capsys.readouterr().out

    main(["hook", "on"])
    assert flag.is_file()
    assert "ARMED" in capsys.readouterr().out

    main(["hook", "status"])
    assert "ARMED" in capsys.readouterr().out

    main(["hook", "off"])
    assert not flag.exists()
    assert "DISARMED" in capsys.readouterr().out
