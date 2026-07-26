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
import stat
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

def test_claude_mcp_add_builds_one_project_scoped_argv(tmp_path, monkeypatch):
    calls = []

    def fake_run(argv, cwd=None, capture_output=True, text=True):
        calls.append((argv, cwd))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(inst.subprocess, "run", fake_run)
    monkeypatch.setenv("BRAINCELL_CLAUDE_JSON", str(tmp_path / "claude.json"))
    client = inst.ClaudeCodeClient(claude_bin="/fake/claude")
    project = tmp_path / "project"
    project.mkdir()
    client.mcp_add("braincell", "braincell-mcp", [],
                   {"BRAINCELL_PROJECT_ID": "PID1", "BRAINCELL_DATA_NAMESPACE": "braincell"},
                   scope="local", cwd=str(project))

    assert len(calls) == 1
    add_argv, add_cwd = calls[0]
    assert add_cwd == str(project)
    assert add_argv[:5] == ["/fake/claude", "mcp", "add", "braincell", "-s"]
    assert "local" in add_argv
    assert "-e" in add_argv and "BRAINCELL_PROJECT_ID=PID1" in add_argv
    assert add_argv[-2:] == ["--", "braincell-mcp"] or add_argv[-1] == "braincell-mcp"


def test_claude_mcp_add_raises_on_failure(tmp_path, monkeypatch):
    def fake_run(argv, cwd=None, capture_output=True, text=True):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

    monkeypatch.setattr(inst.subprocess, "run", fake_run)
    monkeypatch.setenv("BRAINCELL_CLAUDE_JSON", str(tmp_path / "claude.json"))
    client = inst.ClaudeCodeClient(claude_bin="/fake/claude")
    with pytest.raises(RuntimeError, match="boom"):
        client.mcp_add("braincell", "cmd", [], {}, scope="local", cwd=str(tmp_path))


def test_mcp_add_no_claude_binary_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(inst.shutil, "which", lambda n: None)  # no claude on PATH
    monkeypatch.setenv("BRAINCELL_CLAUDE_JSON", str(tmp_path / "claude.json"))
    client = inst.ClaudeCodeClient(claude_bin=None)
    assert client.available() is False
    with pytest.raises(RuntimeError, match="claude"):
        client.mcp_add("braincell", "cmd", [], {}, scope="local", cwd=str(tmp_path))


# ── project-local Codex / VS Code configuration ───────────────────────────────


def _codex_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


def _env(project_id="01PROJECT00000000000000001"):
    return {
        "BRAINCELL_DATA_NAMESPACE": "braincell_test",
        "BRAINCELL_PROJECT_ID": project_id,
        "BRAINCELL_STORE": "sqlite",
    }


def test_portable_command_refuses_absolute_fallback(monkeypatch):
    monkeypatch.setattr(inst.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="machine-specific"):
        inst.resolve_portable_server_command()


def test_codex_config_preserves_unrelated_content_permissions_and_final_newline(tmp_path):
    repo = _codex_repo(tmp_path)
    cfg = repo / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text("# keep\nmodel = 'x'\n[features]\nfast_mode = true", encoding="utf-8")
    cfg.chmod(0o640)

    result = inst.manage_codex_project_registration(repo, "braincell-mcp", [], _env())

    text = cfg.read_text(encoding="utf-8")
    assert result["changed"] is True
    assert "# keep" in text and "fast_mode = true" in text
    assert "[mcp_servers.braincell]" in text
    assert 'cwd = "' + str(repo.resolve()) + '"' in text
    assert not text.endswith("\n")
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o640
    assert result["backup_path"]


def test_codex_config_refuses_malformed_and_conflicting_entries_without_changes(tmp_path):
    repo = _codex_repo(tmp_path)
    cfg = repo / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text("[broken\n", encoding="utf-8")
    before = cfg.read_bytes()
    with pytest.raises(RuntimeError, match="Cannot parse"):
        inst.manage_codex_project_registration(repo, "braincell-mcp", [], _env())
    assert cfg.read_bytes() == before

    cfg.write_text("[mcp_servers.braincell]\ncommand = 'other'\n", encoding="utf-8")
    before = cfg.read_bytes()
    with pytest.raises(RuntimeError, match="user-managed"):
        inst.manage_codex_project_registration(repo, "braincell-mcp", [], _env())
    assert cfg.read_bytes() == before


def test_codex_install_is_idempotent_and_disconnect_removes_only_canonical_entry(tmp_path):
    repo = _codex_repo(tmp_path)
    cfg = repo / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text("[mcp_servers.other]\ncommand = 'keep'\n", encoding="utf-8")
    first = inst.manage_codex_project_registration(repo, "braincell-mcp", [], _env())
    before = cfg.read_text(encoding="utf-8")
    second = inst.manage_codex_project_registration(repo, "braincell-mcp", [], _env())
    removed = inst.remove_codex_project_registration(repo, "braincell-mcp", [], _env())

    assert first["changed"] is True and second["changed"] is False and removed["changed"] is True
    assert cfg.read_text(encoding="utf-8") != before
    assert "[mcp_servers.other]" in cfg.read_text(encoding="utf-8")
    assert "[mcp_servers.braincell]" not in cfg.read_text(encoding="utf-8")


def test_codex_atomic_replace_failure_leaves_original_intact(tmp_path, monkeypatch):
    repo = _codex_repo(tmp_path)
    cfg = repo / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text("model = 'keep'\n", encoding="utf-8")
    before = cfg.read_bytes()
    monkeypatch.setattr(inst.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("boom")))

    with pytest.raises(OSError, match="boom"):
        inst.manage_codex_project_registration(repo, "braincell-mcp", [], _env())
    assert cfg.read_bytes() == before


def test_legacy_global_codex_is_detected_retained_then_explicitly_cleaned(tmp_path, monkeypatch):
    global_cfg = tmp_path / "global.toml"
    global_cfg.write_text("# keep\nmodel = 'x'\n[mcp_servers.braincell]\ncommand = 'old'\n", encoding="utf-8")
    monkeypatch.setenv("BRAINCELL_CODEX_CONFIG", str(global_cfg))

    assert inst._legacy_codex_registration()["registered"] is True
    with pytest.raises(RuntimeError, match="preview-first"):
        inst.remove_legacy_codex_global_registration()
    result = inst.remove_legacy_codex_global_registration(confirm=True)
    assert result["changed"] is True
    text = global_cfg.read_text(encoding="utf-8")
    assert "model = 'x'" in text and "braincell" not in text


def test_vscode_workspace_config_never_uses_user_global_configuration(tmp_path):
    repo = tmp_path / "workspace"
    repo.mkdir()
    cfg = repo / ".vscode" / "mcp.json"
    cfg.parent.mkdir()
    cfg.write_text('{\n  "servers": {"other": {"command": "keep"}}\n}\n', encoding="utf-8")

    result = inst.manage_vscode_workspace_registration(repo, "braincell-mcp", [], _env())
    payload = json.loads(cfg.read_text(encoding="utf-8"))

    assert result["changed"] is True
    assert payload["servers"]["other"]["command"] == "keep"
    assert payload["servers"]["braincell"]["cwd"] == "${workspaceFolder}"
    assert payload["servers"]["braincell"]["command"] == "braincell-mcp"


# ── client registry ─────────────────────────────────────────────────────────────

def test_get_client_returns_right_types():
    assert isinstance(inst.get_client("claude"), inst.ClaudeCodeClient)
    assert isinstance(inst.get_client("codex"), inst.CodexClient)
    assert isinstance(inst.get_client("vscode"), inst.VSCodeClient)


def test_get_client_unknown_raises():
    with pytest.raises(ValueError, match="Unknown client"):
        inst.get_client("emacs")


def test_connect_to_codex_writes_only_selected_project_config(tmp_path, monkeypatch, capsys):
    repo = _codex_repo(tmp_path)
    monkeypatch.setattr(inst.shutil, "which", lambda _name: "/fake/bin")
    main(["connect", str(repo), "--client", "codex"])

    cfg = repo / ".codex" / "config.toml"
    assert cfg.exists()
    assert "braincell-mcp" in cfg.read_text(encoding="utf-8")
    assert "Connected BrainCell" in capsys.readouterr().out


def test_connect_rejects_global_and_user_scope_options(tmp_path):
    repo = _codex_repo(tmp_path)
    with pytest.raises(SystemExit) as global_option:
        main(["connect", str(repo), "--client", "codex", "--global"])
    assert global_option.value.code == 2
    with pytest.raises(SystemExit) as user_scope:
        main(["connect", str(repo), "--client", "claude", "--scope", "user"])
    assert user_scope.value.code == 2


def test_connect_non_git_project_requires_acknowledgement(tmp_path, monkeypatch):
    project = tmp_path / "plain"
    project.mkdir()
    monkeypatch.setattr(inst.shutil, "which", lambda _name: "/fake/braincell-mcp")
    with pytest.raises(SystemExit, match="acknowledge-non-git"):
        main(["connect", str(project), "--client", "vscode"])


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
