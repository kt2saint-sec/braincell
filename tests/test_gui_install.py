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
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

from braincell import install as inst


def _app(tmp_path, *, allow_writes: bool = True, auth_token=None, restart_argv=None,
         seed_project_id=None):
    from braincell.gui import create_app
    return create_app(
        db_path=tmp_path / "braincell.db", allow_writes=allow_writes,
        auth_token=auth_token, restart_argv=restart_argv,
        seed_project_id=seed_project_id,
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
    # Regression: without BRAINCELL_STORE=sqlite the server's lifespan open_store()
    # exit(1)s at startup and the MCP never loads (project mode).
    assert env["BRAINCELL_STORE"] == "sqlite"
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


def test_install_global_brain(tmp_path, monkeypatch):
    """(t8b) global_brain=true mirrors `braincell install --global`: env carries
    MODE=global only (no PROJECT_ID/STORE, federate ignored), cwd None,
    project_id None — and the path is never resolved (a bogus one still 200s)."""
    _settings(tmp_path, monkeypatch)
    fake_cls, calls = _fake_client()
    monkeypatch.setitem(inst.CLIENTS, "claude", fake_cls)

    with TestClient(_app(tmp_path)) as client:
        r = client.post("/api/install", json={
            "path": str(tmp_path / "does-not-exist"),
            "global_brain": True,
            "federate": True,
        })

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["project_id"] is None
    env = calls[0]["env"]
    assert env["BRAINCELL_MODE"] == "global"
    assert "BRAINCELL_DATA_NAMESPACE" in env
    assert "BRAINCELL_PROJECT_ID" not in env
    assert "BRAINCELL_STORE" not in env
    assert "BRAINCELL_FEDERATE" not in env
    assert calls[0]["cwd"] is None


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


# ── /api/skills ───────────────────────────────────────────────────────────────

def _skills_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "claude-skills"
    monkeypatch.setenv("BRAINCELL_CLAUDE_SKILLS_DIR", str(d))
    return d


class TestSkillsEndpoint:
    def test_places_packaged_skills_then_current(self, tmp_path, monkeypatch):
        """(t11) First call installs both packaged skills; a rerun is 'current'."""
        _skills_dir(tmp_path, monkeypatch)

        with TestClient(_app(tmp_path)) as client:
            r = client.post("/api/skills", json={})
            assert r.status_code == 200
            skills = r.json()["skills"]
            names = {s["name"] for s in skills}
            assert {"braincell-init", "braincell-sync"} <= names
            assert all(s["status"] == "installed" for s in skills)
            for s in skills:
                assert Path(s["path"]).is_file()

            r2 = client.post("/api/skills", json={})
            assert all(s["status"] == "current" for s in r2.json()["skills"])

    def test_conflict_never_clobbers(self, tmp_path, monkeypatch):
        """(t12) A user-authored same-name skill is reported as conflict and its
        content left byte-identical; the other skill still resolves normally."""
        _skills_dir(tmp_path, monkeypatch)

        with TestClient(_app(tmp_path)) as client:
            first = client.post("/api/skills", json={}).json()["skills"]
            init = next(s for s in first if s["name"] == "braincell-init")
            Path(init["path"]).write_text("MY OWN SKILL\n", encoding="utf-8")

            second = client.post("/api/skills", json={}).json()["skills"]

        by_name = {s["name"]: s["status"] for s in second}
        assert by_name["braincell-init"] == "conflict"
        assert by_name["braincell-sync"] == "current"
        assert Path(init["path"]).read_text(encoding="utf-8") == "MY OWN SKILL\n"

    def test_extra_field_422(self, tmp_path, monkeypatch):
        _skills_dir(tmp_path, monkeypatch)
        with TestClient(_app(tmp_path)) as client:
            r = client.post("/api/skills", json={"target": "/etc"})
        assert r.status_code == 422

    def test_absent_in_read_only_mode(self, tmp_path):
        with TestClient(_app(tmp_path, allow_writes=False)) as client:
            assert client.post("/api/skills", json={}).status_code in (404, 405)

    def test_401_without_token(self, tmp_path):
        with TestClient(_app(tmp_path, auth_token="secret")) as client:
            assert client.post("/api/skills", json={}).status_code == 401


# ── /api/status mcp+embedder, /api/config suggest_tour, /api/projects badge ───

def _stub_embedder_route(monkeypatch, ok: bool = True):
    """Replace gui's embedder probe (no live Ollama in route tests)."""
    monkeypatch.setattr(
        "braincell.gui.embedder_status",
        lambda *a, **k: {
            "provider": "ollama", "model": "stub-model", "dim": 4,
            "reachable": ok, "model_present": ok, "ok": ok,
            "detail": "" if ok else "Ollama unreachable — fix it",
        },
    )


def _isolate_client_configs(tmp_path, monkeypatch):
    """Point registration detection at (absent) tmp configs — never real ones."""
    monkeypatch.setenv("BRAINCELL_CLAUDE_JSON", str(tmp_path / "no-claude.json"))
    monkeypatch.setenv("BRAINCELL_CODEX_CONFIG", str(tmp_path / "no-codex.toml"))


class TestStatusEmbedderAndMcp:
    def test_embedder_contract_keys(self, tmp_path, monkeypatch):
        """(t14) /api/status.embedder carries the frozen contract keys."""
        _stub_embedder_route(monkeypatch)
        _isolate_client_configs(tmp_path, monkeypatch)
        with TestClient(_app(tmp_path)) as client:
            emb = client.get("/api/status").json()["embedder"]
        for key in ("reachable", "model", "ok", "detail"):
            assert key in emb, f"Missing embedder key: {key}"
        assert emb["ok"] is True

    def test_embedder_down_still_200(self, tmp_path, monkeypatch):
        """(t15) A down embedder is a failure-shaped field, never a 5xx."""
        _stub_embedder_route(monkeypatch, ok=False)
        _isolate_client_configs(tmp_path, monkeypatch)
        with TestClient(_app(tmp_path)) as client:
            r = client.get("/api/status")
        assert r.status_code == 200
        emb = r.json()["embedder"]
        assert emb["ok"] is False
        assert "unreachable" in emb["detail"]

    def test_mcp_global_mode_shape(self, tmp_path, monkeypatch):
        """(t16) No seed (global-mode launch) → path null, no clients — the
        object shape stays stable for the SPA."""
        _stub_embedder_route(monkeypatch)
        _isolate_client_configs(tmp_path, monkeypatch)
        with TestClient(_app(tmp_path)) as client:
            mcp = client.get("/api/status").json()["mcp"]
        assert mcp == {"path": None, "clients": []}

    def test_mcp_seed_project_registered(self, tmp_path, monkeypatch):
        """(t17) Seeded launch + a local-scope claude registration → path is the
        seed's registry path and clients lists {claude, local}."""
        from braincell.config import get_project_id
        repo = tmp_path / "repo"
        repo.mkdir()
        pid = get_project_id(repo)
        claude_json = tmp_path / "claude.json"
        claude_json.write_text(json.dumps({
            "projects": {
                str(repo.resolve()): {
                    "mcpServers": {"braincell": {"command": "/x/braincell-mcp"}}
                }
            }
        }), encoding="utf-8")
        monkeypatch.setenv("BRAINCELL_CLAUDE_JSON", str(claude_json))
        monkeypatch.setenv("BRAINCELL_CODEX_CONFIG", str(tmp_path / "no.toml"))
        _stub_embedder_route(monkeypatch)
        with TestClient(_app(tmp_path, seed_project_id=pid)) as client:
            mcp = client.get("/api/status").json()["mcp"]
        assert mcp["path"] == str(repo.resolve())
        assert {"client": "claude", "scope": "local"} in mcp["clients"]

    def test_mcp_seed_project_not_registered(self, tmp_path, monkeypatch):
        """(t18) Seeded launch, no registration anywhere → empty clients."""
        from braincell.config import get_project_id
        repo = tmp_path / "repo"
        repo.mkdir()
        pid = get_project_id(repo)
        _stub_embedder_route(monkeypatch)
        _isolate_client_configs(tmp_path, monkeypatch)
        with TestClient(_app(tmp_path, seed_project_id=pid)) as client:
            mcp = client.get("/api/status").json()["mcp"]
        assert mcp["path"] == str(repo.resolve())
        assert mcp["clients"] == []


class TestProjectsMcpRegistered:
    def test_entries_carry_mcp_registered(self, tmp_path, monkeypatch):
        """(t19) /api/projects entries gain mcp_registered from ONE claude.json
        read: registered path True, unregistered sibling False."""
        from braincell.config import get_project_id
        wired = tmp_path / "wired"
        wired.mkdir()
        bare = tmp_path / "bare"
        bare.mkdir()
        get_project_id(wired)
        get_project_id(bare)
        claude_json = tmp_path / "claude.json"
        claude_json.write_text(json.dumps({
            "projects": {
                str(wired.resolve()): {
                    "mcpServers": {"braincell": {"command": "/x"}}
                }
            }
        }), encoding="utf-8")
        monkeypatch.setenv("BRAINCELL_CLAUDE_JSON", str(claude_json))
        _stub_embedder_route(monkeypatch)
        with TestClient(_app(tmp_path)) as client:
            rows = client.get("/api/projects").json()
        by_path = {r["path"]: r["mcp_registered"] for r in rows}
        assert by_path[str(wired.resolve())] is True
        assert by_path[str(bare.resolve())] is False

    def test_detection_failure_degrades_to_false(self, tmp_path, monkeypatch):
        """(t20) A malformed claude.json never 500s the map — every cell False."""
        from braincell.config import get_project_id
        repo = tmp_path / "repo"
        repo.mkdir()
        get_project_id(repo)
        claude_json = tmp_path / "claude.json"
        claude_json.write_text("{oops", encoding="utf-8")
        monkeypatch.setenv("BRAINCELL_CLAUDE_JSON", str(claude_json))
        _stub_embedder_route(monkeypatch)
        with TestClient(_app(tmp_path)) as client:
            r = client.get("/api/projects")
        assert r.status_code == 200
        assert all(row["mcp_registered"] is False for row in r.json())


class TestConfigSuggestTour:
    def test_true_on_empty_brain_no_other_projects(self, tmp_path, monkeypatch):
        """(t21) Empty launch brain + empty registry → first-run signal on."""
        _isolate_client_configs(tmp_path, monkeypatch)
        with TestClient(_app(tmp_path)) as client:
            data = client.get("/api/config").json()
        assert data["suggest_tour"] is True

    def test_false_when_another_project_exists(self, tmp_path, monkeypatch):
        """(t22) An OTHER registered project defeats first-run (the seed itself
        is minted at launch and never counts)."""
        from braincell.config import get_project_id
        other = tmp_path / "other"
        other.mkdir()
        get_project_id(other)
        _isolate_client_configs(tmp_path, monkeypatch)
        with TestClient(_app(tmp_path, seed_project_id="SEEDPID00001")) as client:
            data = client.get("/api/config").json()
        assert data["suggest_tour"] is False

    def test_seed_project_alone_still_first_run(self, tmp_path, monkeypatch):
        """(t23) Registry holding ONLY the seed (just minted by start) is still
        a first run when the brain is empty."""
        from braincell.config import get_project_id
        repo = tmp_path / "repo"
        repo.mkdir()
        pid = get_project_id(repo)
        _isolate_client_configs(tmp_path, monkeypatch)
        with TestClient(_app(tmp_path, seed_project_id=pid)) as client:
            data = client.get("/api/config").json()
        assert data["suggest_tour"] is True


# ── /api/restart ──────────────────────────────────────────────────────────────

_ARGV = [sys.executable, "-m", "braincell.cli", "gui", "/some/proj",
         "--mode", "project", "--port", "8765", "--no-browser", "--allow-writes"]


class TestRestartEndpoint:
    def _patch_execv(self, monkeypatch, delay: float = 0.05) -> list:
        import braincell.gui_install as gi
        calls: list = []
        monkeypatch.setattr(gi, "_RESTART_DELAY_S", delay)
        monkeypatch.setattr(gi.os, "execv", lambda prog, argv: calls.append((prog, argv)))
        return calls

    def test_schedules_execv_with_server_argv(self, tmp_path, monkeypatch):
        """(t13) 200 returns first, then the deferred exec fires with the argv
        recorded server-side at launch (never from the request)."""
        calls = self._patch_execv(monkeypatch)

        with TestClient(_app(tmp_path, restart_argv=list(_ARGV))) as client:
            r = client.post("/api/restart", json={})
            assert r.status_code == 200
            assert r.json() == {"ok": True, "restarting": True}
            deadline = time.time() + 5.0
            while not calls and time.time() < deadline:
                time.sleep(0.02)

        assert calls == [(_ARGV[0], _ARGV)]

    def test_409_while_job_runs(self, tmp_path, monkeypatch):
        from braincell.gui_ingest import IngestJob
        calls = self._patch_execv(monkeypatch)
        app = _app(tmp_path, restart_argv=list(_ARGV))
        with TestClient(app) as client:
            app.state.ingest_manager.job = IngestJob(path="/x")  # state=running
            r = client.post("/api/restart", json={})
        assert r.status_code == 409
        assert calls == []

    def test_409_without_restart_argv(self, tmp_path, monkeypatch):
        calls = self._patch_execv(monkeypatch)
        with TestClient(_app(tmp_path)) as client:  # restart_argv=None
            r = client.post("/api/restart", json={})
        assert r.status_code == 409
        assert calls == []

    def test_extra_field_422(self, tmp_path, monkeypatch):
        calls = self._patch_execv(monkeypatch)
        with TestClient(_app(tmp_path, restart_argv=list(_ARGV))) as client:
            r = client.post("/api/restart", json={"argv": ["evil"]})
        assert r.status_code == 422
        assert calls == []

    def test_absent_in_read_only_mode(self, tmp_path):
        with TestClient(_app(tmp_path, allow_writes=False)) as client:
            assert client.post("/api/restart", json={}).status_code in (404, 405)

    def test_401_without_token(self, tmp_path):
        with TestClient(_app(tmp_path, auth_token="secret",
                              restart_argv=list(_ARGV))) as client:
            assert client.post("/api/restart", json={}).status_code == 401
