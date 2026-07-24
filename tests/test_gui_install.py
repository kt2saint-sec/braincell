# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_gui_install.py — regression tests for braincell/gui_install.py
(POST /api/install, /api/uninstall, /api/hook).

Hermetic: the real `claude`/`codex`/`vscode` CLIs are NEVER invoked and
~/.claude/settings.json is never touched. braincell.install.CLIENTS is
monkeypatched with a recording fake per test (mirrors the get_client/CLIENTS
seam used by test_install.py), and BRAINCELL_CLAUDE_SETTINGS redirects the
hook merge to a tmp file (same idiom as test_install.py's `_settings` helper).
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from braincell import install as inst


def _app(tmp_path, *, allow_writes: bool = True, auth_token=None):
    from braincell.gui import create_app
    return create_app(
        db_path=tmp_path / "braincell.db", allow_writes=allow_writes, auth_token=auth_token,
    )


def _settings(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setenv("BRAINCELL_CLAUDE_SETTINGS", str(path))
    return path


def _fake_client(*, available: bool = True, add_error=None, remove_error=None):
    """A recording fake CLIENT CLASS (mirrors the shape of install.py's adapters)
    + the shared list its instances append calls to."""
    calls: list[dict] = []

    class _Fake:
        name = "fake"

        def available(self) -> bool:
            return available

        def mcp_add(self, name, command, args, env, scope, cwd=None) -> None:
            if add_error:
                raise add_error
            calls.append({
                "op": "add", "name": name, "command": command, "args": list(args),
                "env": dict(env), "scope": scope, "cwd": cwd,
            })

        def mcp_remove(self, name, scope=None, cwd=None) -> None:
            if remove_error:
                raise remove_error
            calls.append({"op": "remove", "name": name, "scope": scope, "cwd": cwd})

    return _Fake, calls


# ── /api/install ────────────────────────────────────────────────────────────────

def test_install_happy_path(tmp_path, monkeypatch):
    """(t1) Default install: real command from resolve_server_command(), env has
    namespace + project id, NO federate key, hook installed by default."""
    _settings(tmp_path, monkeypatch)
    fake_cls, calls = _fake_client()
    monkeypatch.setitem(inst.CLIENTS, "claude", fake_cls)
    repo = tmp_path / "repo"
    repo.mkdir()

    with TestClient(_app(tmp_path)) as client:
        r = client.post("/api/install", json={"path": str(repo)})

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["hook_installed"] is True
    expected_command, _ = inst.resolve_server_command()
    assert len(calls) == 1 and calls[0]["op"] == "add"
    assert calls[0]["command"] == expected_command == body["command"]
    env = calls[0]["env"]
    assert "BRAINCELL_DATA_NAMESPACE" in env
    assert "BRAINCELL_PROJECT_ID" in env
    assert "BRAINCELL_FEDERATE" not in env


def test_install_federate_stamps_env(tmp_path, monkeypatch):
    """(t2) federate=true → captured env carries BRAINCELL_FEDERATE=on."""
    _settings(tmp_path, monkeypatch)
    fake_cls, calls = _fake_client()
    monkeypatch.setitem(inst.CLIENTS, "claude", fake_cls)
    repo = tmp_path / "repo"
    repo.mkdir()

    with TestClient(_app(tmp_path)) as client:
        r = client.post("/api/install", json={"path": str(repo), "federate": True})

    assert r.status_code == 200
    assert calls[0]["env"]["BRAINCELL_FEDERATE"] == "on"


def test_install_missing_client_409_no_mcp_add(tmp_path, monkeypatch):
    """(t3) Client CLI unavailable → 409, mcp_add never called."""
    _settings(tmp_path, monkeypatch)
    fake_cls, calls = _fake_client(available=False)
    monkeypatch.setitem(inst.CLIENTS, "claude", fake_cls)
    repo = tmp_path / "repo"
    repo.mkdir()

    with TestClient(_app(tmp_path)) as client:
        r = client.post("/api/install", json={"path": str(repo)})

    assert r.status_code == 409
    assert calls == []


def test_install_non_dir_400(tmp_path, monkeypatch):
    """(t4) Non-directory path → 400."""
    _settings(tmp_path, monkeypatch)
    fake_cls, _calls = _fake_client()
    monkeypatch.setitem(inst.CLIENTS, "claude", fake_cls)

    with TestClient(_app(tmp_path)) as client:
        r = client.post("/api/install", json={"path": str(tmp_path / "nope")})

    assert r.status_code == 400


def test_install_body_smuggling_422(tmp_path, monkeypatch):
    """(t5) SI-3: a smuggled `command` or `env` field is rejected (extra=forbid)."""
    _settings(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    with TestClient(_app(tmp_path)) as client:
        r_command = client.post(
            "/api/install", json={"path": str(repo), "command": "evil"}
        )
        r_env = client.post(
            "/api/install", json={"path": str(repo), "env": {"X": "1"}}
        )

    assert r_command.status_code == 422
    assert r_env.status_code == 422


def test_install_absent_in_read_only_mode(tmp_path, monkeypatch):
    """(t6) SI-1: a read-only launch (allow_writes=False) does not expose the route."""
    repo = tmp_path / "repo"
    repo.mkdir()
    with TestClient(_app(tmp_path, allow_writes=False)) as client:
        r = client.post("/api/install", json={"path": str(repo)})
    assert r.status_code in (404, 405)


def test_install_401_without_token(tmp_path, monkeypatch):
    """(t7) SI-2: a token-guarded app rejects a request with no token."""
    repo = tmp_path / "repo"
    repo.mkdir()
    with TestClient(_app(tmp_path, auth_token="secret")) as client:
        r = client.post("/api/install", json={"path": str(repo)})
    assert r.status_code == 401


def test_install_hook_flag_behavior(tmp_path, monkeypatch):
    """(t8) no_hook=true skips install_hook; the claude default installs it."""
    settings_path = _settings(tmp_path, monkeypatch)
    fake_cls, _calls = _fake_client()
    monkeypatch.setitem(inst.CLIENTS, "claude", fake_cls)
    repo_a = tmp_path / "repoA"
    repo_a.mkdir()
    repo_b = tmp_path / "repoB"
    repo_b.mkdir()

    with TestClient(_app(tmp_path)) as client:
        r = client.post("/api/install", json={"path": str(repo_a), "no_hook": True})
        assert r.json()["hook_installed"] is False
        assert not settings_path.exists() or \
            "UserPromptSubmit" not in settings_path.read_text()

        r = client.post("/api/install", json={"path": str(repo_b)})
        assert r.json()["hook_installed"] is True

    data = json.loads(settings_path.read_text())
    cmds = [h["command"] for e in data["hooks"]["UserPromptSubmit"] for h in e["hooks"]]
    assert any("braincell.family_hook" in c for c in cmds)


# ── /api/uninstall ───────────────────────────────────────────────────────────────

def test_uninstall_vscode_409_manual_instructions(tmp_path, monkeypatch):
    """(t9) VS Code has no remove-MCP CLI — 409 surfaces the manual instructions."""
    fake_cls, _calls = _fake_client(
        remove_error=NotImplementedError(
            "VS Code has no remove-MCP CLI. Remove the 'braincell' entry manually."
        )
    )
    monkeypatch.setitem(inst.CLIENTS, "vscode", fake_cls)
    repo = tmp_path / "repo"
    repo.mkdir()

    with TestClient(_app(tmp_path)) as client:
        r = client.post("/api/uninstall", json={"path": str(repo), "client": "vscode"})

    assert r.status_code == 409
    assert "manually" in r.json()["detail"]


def test_uninstall_happy_path_claude(tmp_path, monkeypatch):
    """(t9b) Claude uninstall: MCP removed via the client adapter, hook entry
    stripped from settings, disarm=true clears the flag file. Added before the
    frontend wiring — the endpoint existed with only the vscode-409 test."""
    settings_path = _settings(tmp_path, monkeypatch)
    flag = tmp_path / "state" / "flag.txt"
    flag.parent.mkdir(parents=True)
    flag.touch()
    monkeypatch.setenv("BRAINCELL_FAMILY_HOOK_FLAG", str(flag))
    fake_cls, calls = _fake_client()
    monkeypatch.setitem(inst.CLIENTS, "claude", fake_cls)
    repo = tmp_path / "repo"
    repo.mkdir()

    with TestClient(_app(tmp_path)) as client:
        # install first so a hook entry exists to remove
        assert client.post("/api/install", json={"path": str(repo)}).status_code == 200
        r = client.post("/api/uninstall",
                        json={"path": str(repo), "disarm": True})

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["mcp_removed"] is True
    assert body["hook_removed"] == 1
    assert calls[-1]["op"] == "remove" and calls[-1]["name"] == "braincell"
    assert "braincell.family_hook" not in settings_path.read_text()
    assert not flag.exists(), "disarm=true must clear the hook flag file"


# ── /api/hook ─────────────────────────────────────────────────────────────────

def test_hook_on_off_status_roundtrip(tmp_path, monkeypatch):
    """(t10) /api/hook arm/disarm/status round-trip against a tmp flag path."""
    flag = tmp_path / "state" / "flag.txt"
    monkeypatch.setenv("BRAINCELL_FAMILY_HOOK_FLAG", str(flag))

    with TestClient(_app(tmp_path)) as client:
        r = client.post("/api/hook", json={"action": "status"})
        assert r.json()["armed"] is False

        r = client.post("/api/hook", json={"action": "on"})
        assert r.json()["armed"] is True
        assert flag.is_file()

        r = client.post("/api/hook", json={"action": "status"})
        assert r.json()["armed"] is True

        r = client.post("/api/hook", json={"action": "off"})
        assert r.json()["armed"] is False
        assert not flag.exists()
