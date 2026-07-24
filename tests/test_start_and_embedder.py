# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_start_and_embedder.py — regression tests for `braincell start`
(braincell/launch.py + cli.cmd_start), embed.embedder_status, and
install.registration_status / claude_registered_map.

Hermetic: the single-instance probe stubs urllib (no sockets), the embedder
probe stubs ollama.Client (no live daemon), and registration detection reads
tmp config files via the BRAINCELL_CLAUDE_JSON / BRAINCELL_CODEX_CONFIG env
overrides — the real ~/.claude.json / ~/.codex/config.toml are never read, and
no client CLI is ever invoked (`start` never auto-registers).
"""

from __future__ import annotations

import argparse
import io
import json
import types
import urllib.error
from pathlib import Path

import pytest

from braincell import embed_spec, launch
from braincell.install import claude_registered_map, registration_status


# ── shared stubs ──────────────────────────────────────────────────────────────

def _stub_embedder(monkeypatch, ok: bool = True, detail: str = ""):
    """Replace launch's embedder probe (no live Ollama in tests)."""
    monkeypatch.setattr(
        launch, "embedder_status",
        lambda *a, **k: {
            "provider": "ollama", "model": "stub-model", "dim": 4,
            "reachable": ok, "model_present": ok, "ok": ok, "detail": detail,
        },
    )


def _isolate_client_configs(monkeypatch, tmp_path: Path) -> None:
    """Point registration detection at (absent) tmp configs — never real ones."""
    monkeypatch.setenv("BRAINCELL_CLAUDE_JSON", str(tmp_path / "no-claude.json"))
    monkeypatch.setenv("BRAINCELL_CODEX_CONFIG", str(tmp_path / "no-codex.toml"))


class _FakeResp:
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ── probe_status (stubbed urllib — no sockets) ───────────────────────────────

class TestProbeStatus:
    def test_returns_parsed_status_with_token_header(self, monkeypatch):
        payload = json.dumps({"db_path": "/x/braincell.db"}).encode()
        seen: dict = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["headers"] = dict(req.headers)
            seen["timeout"] = timeout
            return _FakeResp(payload)

        monkeypatch.setattr(launch.urllib.request, "urlopen", fake_urlopen)
        out = launch.probe_status(8765, "tok")
        assert out == {"db_path": "/x/braincell.db"}
        assert seen["url"] == "http://127.0.0.1:8765/api/status"
        assert "tok" in seen["headers"].values()
        assert seen["timeout"] == 1.0

    def test_none_on_connection_refused(self, monkeypatch):
        def fake_urlopen(req, timeout=None):
            raise OSError("connection refused")

        monkeypatch.setattr(launch.urllib.request, "urlopen", fake_urlopen)
        assert launch.probe_status(8765, "tok") is None

    def test_none_on_401(self, monkeypatch):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 401, "unauthorized", None, io.BytesIO(b"")
            )

        monkeypatch.setattr(launch.urllib.request, "urlopen", fake_urlopen)
        assert launch.probe_status(8765, "tok") is None

    def test_none_on_non_json_foreign_process(self, monkeypatch):
        monkeypatch.setattr(
            launch.urllib.request, "urlopen",
            lambda req, timeout=None: _FakeResp(b"<html>not braincell</html>"),
        )
        assert launch.probe_status(8765, "tok") is None


# ── preflight ─────────────────────────────────────────────────────────────────

class TestPreflight:
    def test_reuse_when_running_db_matches(self, tmp_path, monkeypatch):
        from braincell.config import get_db_path, get_project_id
        repo = tmp_path / "repo"
        repo.mkdir()
        pid = get_project_id(repo)
        db = get_db_path(pid)
        monkeypatch.setattr("braincell.gui._resolve_gui_token", lambda: "tok")
        monkeypatch.setattr(
            launch, "probe_status", lambda *a, **k: {"db_path": str(db)}
        )
        pre = launch.preflight(repo, mode="project", port=8765)
        assert pre.action == "reuse"
        assert pre.reuse_url == "http://127.0.0.1:8765/?t=tok"
        assert pre.expected_db == str(db)

    def test_conflict_when_running_db_differs(self, tmp_path, monkeypatch):
        from braincell.config import get_db_path, get_project_id
        repo = tmp_path / "repo"
        repo.mkdir()
        pid = get_project_id(repo)
        monkeypatch.setattr("braincell.gui._resolve_gui_token", lambda: "tok")
        monkeypatch.setattr(
            launch, "probe_status",
            lambda *a, **k: {"db_path": "/other/brain/braincell.db"},
        )
        pre = launch.preflight(repo, mode="project", port=8765)
        assert pre.action == "conflict"
        assert pre.conflict_db == "/other/brain/braincell.db"
        assert pre.expected_db == str(get_db_path(pid))

    def test_conflict_for_unregistered_project_on_busy_port(
        self, tmp_path, monkeypatch
    ):
        """A responding GUI can never match an UNREGISTERED target — refuse,
        never silently open the wrong brain."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr("braincell.gui._resolve_gui_token", lambda: "tok")
        monkeypatch.setattr(
            launch, "probe_status", lambda *a, **k: {"db_path": "/other.db"}
        )
        pre = launch.preflight(repo, mode="project", port=8765)
        assert pre.action == "conflict"
        assert pre.expected_db is None

    def test_launch_first_run_for_unregistered_project(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr("braincell.gui._resolve_gui_token", lambda: "tok")
        monkeypatch.setattr(launch, "probe_status", lambda *a, **k: None)
        _stub_embedder(monkeypatch, ok=True)
        _isolate_client_configs(monkeypatch, tmp_path)
        pre = launch.preflight(repo, mode="project", port=8765)
        assert pre.action == "launch"
        assert pre.first_run is True
        # Report contract: embedder line FIRST, then project/brain/MCP lines.
        assert "Embedder" in pre.report_lines[0]
        joined = "\n".join(pre.report_lines)
        assert "not registered" in joined      # MCP line, print-and-continue
        assert "not built yet" in joined       # brain line

    def test_embedder_failure_is_report_line_not_abort(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr("braincell.gui._resolve_gui_token", lambda: "tok")
        monkeypatch.setattr(launch, "probe_status", lambda *a, **k: None)
        _stub_embedder(monkeypatch, ok=False, detail="Ollama unreachable — fix it")
        _isolate_client_configs(monkeypatch, tmp_path)
        pre = launch.preflight(repo, mode="project", port=8765)
        assert pre.action == "launch"
        assert pre.report_lines[0].startswith("✗ Embedder not ready")
        assert "Ollama unreachable" in pre.report_lines[0]

    def test_not_first_run_when_other_projects_exist(self, tmp_path, monkeypatch):
        """Empty brain but ANOTHER registered project → not a first run."""
        from braincell.config import get_db_path, get_project_id
        from braincell.store import SqliteStore
        repo = tmp_path / "repo"
        repo.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        pid = get_project_id(repo)
        get_project_id(other)  # the "other" project that defeats first-run
        db = get_db_path(pid)
        store = SqliteStore(db)
        store.assert_schema_version()
        store.close()
        monkeypatch.setattr("braincell.gui._resolve_gui_token", lambda: "tok")
        monkeypatch.setattr(launch, "probe_status", lambda *a, **k: None)
        _stub_embedder(monkeypatch)
        _isolate_client_configs(monkeypatch, tmp_path)
        pre = launch.preflight(repo, mode="project", port=8765)
        assert pre.first_run is False

    def test_first_run_when_empty_brain_and_no_others(self, tmp_path, monkeypatch):
        from braincell.config import get_db_path, get_project_id
        from braincell.store import SqliteStore
        repo = tmp_path / "repo"
        repo.mkdir()
        pid = get_project_id(repo)
        store = SqliteStore(get_db_path(pid))
        store.assert_schema_version()
        store.close()
        monkeypatch.setattr("braincell.gui._resolve_gui_token", lambda: "tok")
        monkeypatch.setattr(launch, "probe_status", lambda *a, **k: None)
        _stub_embedder(monkeypatch)
        _isolate_client_configs(monkeypatch, tmp_path)
        pre = launch.preflight(repo, mode="project", port=8765)
        assert pre.first_run is True

    def test_registered_mcp_shows_in_report(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        cfg = {
            "projects": {
                str(repo.resolve()): {
                    "mcpServers": {"braincell": {"command": "/x/braincell-mcp"}}
                }
            }
        }
        claude_json = tmp_path / "claude.json"
        claude_json.write_text(json.dumps(cfg), encoding="utf-8")
        monkeypatch.setenv("BRAINCELL_CLAUDE_JSON", str(claude_json))
        monkeypatch.setenv(
            "BRAINCELL_CODEX_CONFIG", str(tmp_path / "no-codex.toml")
        )
        monkeypatch.setattr("braincell.gui._resolve_gui_token", lambda: "tok")
        monkeypatch.setattr(launch, "probe_status", lambda *a, **k: None)
        _stub_embedder(monkeypatch)
        pre = launch.preflight(repo, mode="project", port=8765)
        joined = "\n".join(pre.report_lines)
        assert "MCP: registered — claude (local)" in joined


def test_doc_count_reads_real_store(tmp_path):
    """_doc_count counts bc_documents via a read-only sqlite3 open."""
    import asyncio
    from tests.conftest import _insert_doc_and_chunk, make_store

    store = make_store(tmp_path)
    db = tmp_path / "braincell.db"
    assert launch._doc_count(db) == 0
    asyncio.run(
        _insert_doc_and_chunk(store, project="P1", doc_key="d1", text="hello")
    )
    assert launch._doc_count(db) == 1
    assert launch._doc_count(tmp_path / "missing.db") is None


# ── cmd_start (thin CLI wiring) ───────────────────────────────────────────────

class TestCmdStart:
    def _args(self, path, **kw):
        defaults = dict(
            path=str(path), port=8765, no_browser=True, global_brain=False
        )
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    def test_launch_passes_tour_and_writes(self, tmp_path, monkeypatch):
        from braincell.cli import cmd_start
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr("braincell.gui._resolve_gui_token", lambda: "tok")
        monkeypatch.setattr(launch, "probe_status", lambda *a, **k: None)
        _stub_embedder(monkeypatch)
        _isolate_client_configs(monkeypatch, tmp_path)
        captured: dict = {}
        monkeypatch.setattr(
            "braincell.gui.run_gui", lambda **kw: captured.update(kw)
        )
        cmd_start(self._args(repo))
        assert captured["mode"] == "project"
        assert captured["allow_writes"] is True
        assert captured["open_browser"] is False
        assert captured["url_extra_query"] == "tour=1"

    def test_no_tour_when_not_first_run(self, tmp_path, monkeypatch):
        from braincell.cli import cmd_start
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr("braincell.gui._resolve_gui_token", lambda: "tok")
        monkeypatch.setattr(launch, "probe_status", lambda *a, **k: None)
        _stub_embedder(monkeypatch)
        _isolate_client_configs(monkeypatch, tmp_path)
        # Force the not-first-run outcome via the preflight seam.
        monkeypatch.setattr(
            launch, "preflight",
            lambda *a, **k: launch.Preflight(action="launch", first_run=False),
        )
        captured: dict = {}
        monkeypatch.setattr(
            "braincell.gui.run_gui", lambda **kw: captured.update(kw)
        )
        cmd_start(self._args(repo))
        assert captured["url_extra_query"] is None

    def test_reuse_opens_browser_and_skips_run_gui(self, tmp_path, monkeypatch):
        from braincell.cli import cmd_start
        from braincell.config import get_db_path, get_project_id
        repo = tmp_path / "repo"
        repo.mkdir()
        pid = get_project_id(repo)
        db = get_db_path(pid)
        monkeypatch.setattr("braincell.gui._resolve_gui_token", lambda: "tok")
        monkeypatch.setattr(
            launch, "probe_status", lambda *a, **k: {"db_path": str(db)}
        )
        opened: list = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
        ran: list = []
        monkeypatch.setattr(
            "braincell.gui.run_gui", lambda **kw: ran.append(kw)
        )
        cmd_start(self._args(repo))
        assert opened == ["http://127.0.0.1:8765/?t=tok"]
        assert ran == []

    def test_conflict_exits_1_without_run_gui(self, tmp_path, monkeypatch):
        from braincell.cli import cmd_start
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr("braincell.gui._resolve_gui_token", lambda: "tok")
        monkeypatch.setattr(
            launch, "probe_status", lambda *a, **k: {"db_path": "/other.db"}
        )
        ran: list = []
        monkeypatch.setattr(
            "braincell.gui.run_gui", lambda **kw: ran.append(kw)
        )
        with pytest.raises(SystemExit) as exc:
            cmd_start(self._args(repo))
        assert exc.value.code == 1
        assert ran == []

    def test_never_auto_registers(self, tmp_path, monkeypatch):
        """`start` must never invoke a client adapter (no mcp_add path)."""
        from braincell import install as inst
        from braincell.cli import cmd_start

        def _boom(*a, **k):
            raise AssertionError("start must never instantiate a client adapter")

        monkeypatch.setattr(inst, "get_client", _boom)
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr("braincell.gui._resolve_gui_token", lambda: "tok")
        monkeypatch.setattr(launch, "probe_status", lambda *a, **k: None)
        _stub_embedder(monkeypatch)
        _isolate_client_configs(monkeypatch, tmp_path)
        monkeypatch.setattr("braincell.gui.run_gui", lambda **kw: None)
        cmd_start(self._args(repo))  # completes without touching get_client

    def test_global_flag_targets_global_brain(self, tmp_path, monkeypatch):
        from braincell.cli import cmd_start
        monkeypatch.setattr("braincell.gui._resolve_gui_token", lambda: "tok")
        monkeypatch.setattr(launch, "probe_status", lambda *a, **k: None)
        _stub_embedder(monkeypatch)
        captured: dict = {}
        monkeypatch.setattr(
            "braincell.gui.run_gui", lambda **kw: captured.update(kw)
        )
        cmd_start(self._args(tmp_path, global_brain=True))
        assert captured["mode"] == "global"
        assert captured["allow_writes"] is True


# ── run_gui url_extra_query ───────────────────────────────────────────────────

class TestRunGuiExtraQuery:
    def _run(self, tmp_path, monkeypatch, **kw):
        import uvicorn
        from braincell import gui
        captured: dict = {}
        monkeypatch.setattr(
            gui, "create_app", lambda **k: captured.update(k) or object()
        )
        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
        monkeypatch.setenv("BRAINCELL_GUI_TOKEN", "tok")
        gui.run_gui(
            mode="project", port=8123, allow_writes=True, open_browser=True,
            path=str(tmp_path), **kw,
        )
        return captured

    def test_appends_extra_query_to_opened_url(self, tmp_path, monkeypatch):
        captured = self._run(tmp_path, monkeypatch, url_extra_query="tour=1")
        assert captured["open_browser_url"] == "http://127.0.0.1:8123/?t=tok&tour=1"
        # restart_argv must NOT carry the tour param — a GUI restart must not
        # re-trigger the tour.
        assert not any("tour" in a for a in captured["restart_argv"])

    def test_default_has_no_extra_query(self, tmp_path, monkeypatch):
        captured = self._run(tmp_path, monkeypatch)
        assert captured["open_browser_url"] == "http://127.0.0.1:8123/?t=tok"


# ── embed.embedder_status (stubbed ollama client) ────────────────────────────

class TestEmbedderStatus:
    def _fake_ollama(self, monkeypatch, *, models=None, exc=None):
        import ollama

        class _C:
            def __init__(self, *a, **k):
                pass

            def list(self):
                if exc is not None:
                    raise exc
                return types.SimpleNamespace(
                    models=[
                        types.SimpleNamespace(model=m) for m in (models or [])
                    ]
                )

        monkeypatch.setattr(ollama, "Client", _C)

    def test_ok_when_reachable_and_model_present(self, monkeypatch):
        from braincell.embed import embedder_status
        self._fake_ollama(monkeypatch, models=[embed_spec.MODEL, "other:1b"])
        st = embedder_status()
        assert st["reachable"] is True
        assert st["model_present"] is True
        assert st["ok"] is True
        assert st["detail"] == ""
        assert st["model"] == embed_spec.MODEL
        assert st["provider"] == "ollama"

    def test_model_missing_gives_pull_hint(self, monkeypatch):
        from braincell.embed import embedder_status
        self._fake_ollama(monkeypatch, models=["other:1b"])
        st = embedder_status()
        assert st["reachable"] is True
        assert st["ok"] is False
        assert f"ollama pull {embed_spec.MODEL}" in st["detail"]

    def test_unreachable_gives_install_hint(self, monkeypatch):
        from braincell.embed import embedder_status
        self._fake_ollama(monkeypatch, exc=ConnectionError("refused"))
        st = embedder_status()
        assert st["reachable"] is False
        assert st["ok"] is False
        assert "https://ollama.com" in st["detail"]
        assert embed_spec.MODEL in st["detail"]

    def test_tagless_model_matches_latest(self, monkeypatch):
        from braincell.embed import embedder_status
        monkeypatch.setattr(embed_spec, "MODEL", "foo-embed")
        self._fake_ollama(monkeypatch, models=["foo-embed:latest"])
        assert embedder_status()["ok"] is True

    def test_openai_branch_key_present(self, monkeypatch):
        from braincell.embed import embedder_status
        monkeypatch.setattr(embed_spec, "PROVIDER", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        st = embedder_status()
        assert st["ok"] is True and st["detail"] == ""

    def test_openai_branch_key_missing(self, monkeypatch):
        from braincell.embed import embedder_status
        monkeypatch.setattr(embed_spec, "PROVIDER", "openai")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        st = embedder_status()
        assert st["ok"] is False
        assert "OPENAI_API_KEY" in st["detail"]


# ── install.registration_status (tmp config fixtures) ────────────────────────

class TestRegistrationStatus:
    @pytest.fixture(autouse=True)
    def _cfg(self, tmp_path, monkeypatch):
        self.claude_json = tmp_path / "claude.json"
        self.codex_toml = tmp_path / "codex-config.toml"
        monkeypatch.setenv("BRAINCELL_CLAUDE_JSON", str(self.claude_json))
        monkeypatch.setenv("BRAINCELL_CODEX_CONFIG", str(self.codex_toml))
        self.repo = tmp_path / "repo"
        self.repo.mkdir()

    def _write_claude(self, cfg: dict) -> None:
        self.claude_json.write_text(json.dumps(cfg), encoding="utf-8")

    def test_absent_files_not_registered(self):
        st = registration_status(self.repo)
        assert st["claude"] == {"registered": False}
        assert st["codex"] == {"registered": False}
        assert st["vscode"]["registered"] is None

    def test_claude_local_scope(self):
        self._write_claude({
            "projects": {
                str(self.repo.resolve()): {
                    "mcpServers": {
                        "braincell": {
                            "type": "stdio", "command": "/x/braincell-mcp",
                            "args": [], "env": {},
                        }
                    }
                }
            }
        })
        st = registration_status(self.repo)["claude"]
        assert st["registered"] is True
        assert st["scope"] == "local"
        assert st["command"] == "/x/braincell-mcp"

    def test_claude_user_scope(self):
        self._write_claude(
            {"mcpServers": {"braincell": {"command": "/x/braincell-mcp"}}}
        )
        st = registration_status(self.repo)["claude"]
        assert st["registered"] is True and st["scope"] == "user"

    def test_claude_project_scope_mcp_json(self):
        self._write_claude({})
        (self.repo / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"braincell": {"command": "/x"}}}),
            encoding="utf-8",
        )
        st = registration_status(self.repo)["claude"]
        assert st["registered"] is True and st["scope"] == "project"

    def test_local_scope_wins_over_user(self):
        self._write_claude({
            "mcpServers": {"braincell": {"command": "/user-scope"}},
            "projects": {
                str(self.repo.resolve()): {
                    "mcpServers": {"braincell": {"command": "/local-scope"}}
                }
            },
        })
        st = registration_status(self.repo)["claude"]
        assert st["scope"] == "local" and st["command"] == "/local-scope"

    def test_malformed_claude_json_is_unknown(self):
        self.claude_json.write_text("{not json", encoding="utf-8")
        assert registration_status(self.repo)["claude"] == {"registered": None}

    def test_codex_registered(self):
        self.codex_toml.write_text(
            '[mcp_servers.braincell]\ncommand = "/x/braincell-mcp"\n',
            encoding="utf-8",
        )
        st = registration_status(self.repo)["codex"]
        assert st["registered"] is True
        assert st["scope"] == "global"
        assert st["command"] == "/x/braincell-mcp"

    def test_malformed_codex_toml_is_unknown(self):
        self.codex_toml.write_text("not [[ valid toml", encoding="utf-8")
        assert registration_status(self.repo)["codex"] == {"registered": None}


class TestClaudeRegisteredMap:
    def test_one_read_per_call_with_per_path_lookups(self, tmp_path, monkeypatch):
        claude_json = tmp_path / "claude.json"
        monkeypatch.setenv("BRAINCELL_CLAUDE_JSON", str(claude_json))
        p1 = tmp_path / "p1"
        p1.mkdir()
        p2 = tmp_path / "p2"
        p2.mkdir()
        p3 = tmp_path / "p3"
        p3.mkdir()
        claude_json.write_text(json.dumps({
            "projects": {
                str(p1): {"mcpServers": {"braincell": {"command": "/x"}}}
            }
        }), encoding="utf-8")
        (p3 / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"braincell": {"command": "/x"}}}),
            encoding="utf-8",
        )
        out = claude_registered_map([str(p1), str(p2), str(p3)])
        assert out == {str(p1): True, str(p2): False, str(p3): True}

    def test_user_scope_marks_every_path(self, tmp_path, monkeypatch):
        claude_json = tmp_path / "claude.json"
        monkeypatch.setenv("BRAINCELL_CLAUDE_JSON", str(claude_json))
        claude_json.write_text(
            json.dumps({"mcpServers": {"braincell": {"command": "/x"}}}),
            encoding="utf-8",
        )
        out = claude_registered_map(["/a", "/b"])
        assert out == {"/a": True, "/b": True}

    def test_missing_file_all_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "BRAINCELL_CLAUDE_JSON", str(tmp_path / "absent.json")
        )
        assert claude_registered_map(["/a"]) == {"/a": False}

    def test_malformed_file_degrades_to_false(self, tmp_path, monkeypatch):
        claude_json = tmp_path / "claude.json"
        claude_json.write_text("{oops", encoding="utf-8")
        monkeypatch.setenv("BRAINCELL_CLAUDE_JSON", str(claude_json))
        assert claude_registered_map(["/a"]) == {"/a": False}
