# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_native_shell.py — regression tests for the optional PySide6 native shell
(braincell/native_shell.py + the `braincell start --native` wiring).

Hermetic: NO real Qt, NO real uvicorn server, NO sockets. Qt-touching
functions (open_window / show_error) and the uvicorn seam (_make_server) are
monkeypatched; PySide6 need not be installed for this file to pass — the one
availability test that wants the real import skips when it is absent.

Pinned invariants:
  - `start --native` reaches run_gui(native=True); the flag is additive
    (default False, old argparse namespaces keep working via getattr).
  - run_gui native path: serve_native() replaces uvicorn.run, no browser tab
    is scheduled, and restart_argv re-execs `start --native` (the exec kills
    the window, so the relaunch must recreate it).
  - PySide6 missing => graceful fallback to the browser path, never an abort.
  - Reuse => front the RUNNING server with a window (no second server).
    Conflict => a visible error dialog (Terminal=false icons must never
    dead-click).
  - serve_native: window opens only after the server binds; window close
    always shuts the server down (finally).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from braincell import launch, native_shell


def _stub_embedder(monkeypatch):
    monkeypatch.setattr(
        launch, "embedder_status",
        lambda *a, **k: {
            "provider": "ollama", "model": "stub", "dim": 4,
            "reachable": True, "model_present": True, "ok": True, "detail": "",
        },
    )


def _isolate_client_configs(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BRAINCELL_CLAUDE_JSON", str(tmp_path / "no-claude.json"))
    monkeypatch.setenv("BRAINCELL_CODEX_CONFIG", str(tmp_path / "no-codex.toml"))


def _start_args(path, **kw):
    defaults = dict(
        path=str(path), port=8765, no_browser=True, global_brain=False,
        native=False,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


# ── native_available ──────────────────────────────────────────────────────────

class TestNativeAvailable:
    def test_false_without_display(self, monkeypatch):
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
        assert native_shell.native_available() is False

    def test_explicit_platform_counts_as_display(self, monkeypatch):
        """QT_QPA_PLATFORM=offscreen (tests/CI) satisfies the display check —
        the remaining verdict is the real PySide6 import."""
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
        try:
            import PySide6.QtWebEngineWidgets  # noqa: F401
        except Exception:
            assert native_shell.native_available() is False
        else:
            assert native_shell.native_available() is True

    def test_never_raises(self, monkeypatch):
        """A broken PySide6 install must yield False, not an exception."""
        import builtins
        real_import = builtins.__import__

        def _boom(name, *a, **k):
            if name.startswith("PySide6"):
                raise RuntimeError("broken Qt install")
            return real_import(name, *a, **k)

        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setattr(builtins, "__import__", _boom)
        assert native_shell.native_available() is False


# ── cmd_start wiring ──────────────────────────────────────────────────────────

class TestStartNativeWiring:
    def test_native_flag_reaches_run_gui(self, tmp_path, monkeypatch):
        from braincell.cli import cmd_start
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr("braincell.gui._resolve_gui_token", lambda: "tok")
        monkeypatch.setattr(launch, "probe_status", lambda *a, **k: None)
        _stub_embedder(monkeypatch)
        _isolate_client_configs(monkeypatch, tmp_path)
        captured: dict = {}
        monkeypatch.setattr("braincell.gui.run_gui", lambda **kw: captured.update(kw))
        cmd_start(_start_args(repo, native=True))
        assert captured["native"] is True

    def test_old_namespace_without_native_still_works(self, tmp_path, monkeypatch):
        """The flag is additive — a namespace lacking .native means False."""
        from braincell.cli import cmd_start
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr("braincell.gui._resolve_gui_token", lambda: "tok")
        monkeypatch.setattr(launch, "probe_status", lambda *a, **k: None)
        _stub_embedder(monkeypatch)
        _isolate_client_configs(monkeypatch, tmp_path)
        captured: dict = {}
        monkeypatch.setattr("braincell.gui.run_gui", lambda **kw: captured.update(kw))
        ns = _start_args(repo)
        del ns.native
        cmd_start(ns)
        assert captured["native"] is False

    def test_reuse_native_opens_window_not_browser(self, tmp_path, monkeypatch):
        from braincell.cli import cmd_start
        from braincell.config import get_db_path, get_project_id
        repo = tmp_path / "repo"
        repo.mkdir()
        pid = get_project_id(repo)
        db = get_db_path(pid)
        monkeypatch.setattr("braincell.gui._resolve_gui_token", lambda: "tok")
        monkeypatch.setattr(launch, "probe_status", lambda *a, **k: {"db_path": str(db)})
        monkeypatch.setattr(native_shell, "native_available", lambda: True)
        windows: list = []
        monkeypatch.setattr(native_shell, "open_window", lambda url, **k: windows.append(url))
        opened: list = []
        monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u))
        ran: list = []
        monkeypatch.setattr("braincell.gui.run_gui", lambda **kw: ran.append(kw))
        cmd_start(_start_args(repo, native=True))
        assert windows == ["http://127.0.0.1:8765/?t=tok"]
        assert opened == []
        assert ran == []

    def test_reuse_falls_back_to_browser_when_unavailable(self, tmp_path, monkeypatch):
        from braincell.cli import cmd_start
        from braincell.config import get_db_path, get_project_id
        repo = tmp_path / "repo"
        repo.mkdir()
        db = get_db_path(get_project_id(repo))
        monkeypatch.setattr("braincell.gui._resolve_gui_token", lambda: "tok")
        monkeypatch.setattr(launch, "probe_status", lambda *a, **k: {"db_path": str(db)})
        monkeypatch.setattr(native_shell, "native_available", lambda: False)
        opened: list = []
        monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u))
        cmd_start(_start_args(repo, native=True))
        assert opened == ["http://127.0.0.1:8765/?t=tok"]

    def test_conflict_native_shows_dialog_and_exits_1(self, tmp_path, monkeypatch):
        from braincell.cli import cmd_start
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr("braincell.gui._resolve_gui_token", lambda: "tok")
        monkeypatch.setattr(launch, "probe_status", lambda *a, **k: {"db_path": "/other.db"})
        monkeypatch.setattr(native_shell, "native_available", lambda: True)
        dialogs: list = []
        monkeypatch.setattr(native_shell, "show_error", lambda msg, **k: dialogs.append(msg))
        with pytest.raises(SystemExit) as exc:
            cmd_start(_start_args(repo, native=True))
        assert exc.value.code == 1
        assert len(dialogs) == 1 and "DIFFERENT brain" in dialogs[0]


# ── run_gui native path ───────────────────────────────────────────────────────

class TestRunGuiNative:
    def _run(self, tmp_path, monkeypatch, *, available=True, **kw):
        import uvicorn
        from braincell import gui
        captured: dict = {}
        served: list = []
        uv_runs: list = []
        monkeypatch.setattr(
            gui, "create_app", lambda **k: captured.update(k) or object()
        )
        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: uv_runs.append(k))
        monkeypatch.setattr(native_shell, "native_available", lambda: available)
        monkeypatch.setattr(
            native_shell, "serve_native", lambda app, **k: served.append(k)
        )
        monkeypatch.setenv("BRAINCELL_GUI_TOKEN", "tok")
        gui.run_gui(
            mode="project", port=8123, allow_writes=True, open_browser=True,
            path=str(tmp_path), **kw,
        )
        return captured, served, uv_runs

    def test_native_serves_via_shell_not_uvicorn(self, tmp_path, monkeypatch):
        captured, served, uv_runs = self._run(tmp_path, monkeypatch, native=True)
        assert served == [{"port": 8123, "url": "http://127.0.0.1:8123/?t=tok"}]
        assert uv_runs == []
        # The Qt window IS the UI — the lifespan must not also open a browser.
        assert captured["open_browser_url"] is None

    def test_native_restart_argv_relaunches_start_native(self, tmp_path, monkeypatch):
        captured, _, _ = self._run(tmp_path, monkeypatch, native=True)
        argv = captured["restart_argv"]
        assert "start" in argv and "--native" in argv
        assert "gui" not in argv
        # The tour must never ride a restart.
        assert not any("tour" in a for a in argv)

    def test_native_url_keeps_extra_query(self, tmp_path, monkeypatch):
        """`start --native` first-run: the window URL carries tour=1."""
        _, served, _ = self._run(
            tmp_path, monkeypatch, native=True, url_extra_query="tour=1"
        )
        assert served[0]["url"] == "http://127.0.0.1:8123/?t=tok&tour=1"

    def test_fallback_to_browser_when_unavailable(self, tmp_path, monkeypatch):
        captured, served, uv_runs = self._run(
            tmp_path, monkeypatch, native=True, available=False
        )
        assert served == []
        assert len(uv_runs) == 1
        # Full browser behavior restored, including the browser open and the
        # plain `gui` restart argv.
        assert captured["open_browser_url"] == "http://127.0.0.1:8123/?t=tok"
        assert "gui" in captured["restart_argv"]

    def test_default_stays_browser(self, tmp_path, monkeypatch):
        captured, served, uv_runs = self._run(tmp_path, monkeypatch)
        assert served == []
        assert len(uv_runs) == 1
        assert captured["open_browser_url"] == "http://127.0.0.1:8123/?t=tok"


# ── serve_native orchestration (fake server, fake window) ─────────────────────

class _FakeServer:
    def __init__(self, *, bind=True):
        self._bind = bind
        self.started = False
        self.should_exit = False

    def run(self):
        if self._bind:
            self.started = True
            # Simulate serving until asked to exit (bounded for safety).
            import time
            for _ in range(2000):
                if self.should_exit:
                    return
                time.sleep(0.005)


class TestServeNative:
    def test_window_opens_after_bind_and_server_stops_on_close(self, monkeypatch):
        server = _FakeServer()
        windows: list = []
        monkeypatch.setattr(native_shell, "_make_server", lambda app, port: server)
        monkeypatch.setattr(
            native_shell, "open_window",
            lambda url, **k: windows.append((url, server.started)),
        )
        native_shell.serve_native(object(), port=1, url="http://u")
        assert windows == [("http://u", True)]  # bound BEFORE the window opened
        assert server.should_exit is True       # window close shut the server down

    def test_server_stops_even_if_window_raises(self, monkeypatch):
        server = _FakeServer()
        monkeypatch.setattr(native_shell, "_make_server", lambda app, port: server)

        def _boom(url, **k):
            raise RuntimeError("Qt exploded")

        monkeypatch.setattr(native_shell, "open_window", _boom)
        with pytest.raises(RuntimeError, match="Qt exploded"):
            native_shell.serve_native(object(), port=1, url="http://u")
        assert server.should_exit is True

    def test_raises_when_server_never_binds(self, monkeypatch):
        server = _FakeServer(bind=False)  # run() returns without started=True
        monkeypatch.setattr(native_shell, "_make_server", lambda app, port: server)
        windows: list = []
        monkeypatch.setattr(
            native_shell, "open_window", lambda url, **k: windows.append(url)
        )
        with pytest.raises(RuntimeError, match="before binding"):
            native_shell.serve_native(object(), port=1, url="http://u")
        assert windows == []


# ── launcher Exec carries --native ────────────────────────────────────────────

class TestLauncherNativeExec:
    def test_desktop_exec_ends_with_native(self, tmp_path, monkeypatch):
        xdg = tmp_path / "xdg"
        proj = tmp_path / "proj"
        proj.mkdir()
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
        from braincell.gui import install_launcher

        _, desktop = install_launcher(proj)
        exec_line = next(
            ln for ln in desktop.read_text().splitlines() if ln.startswith("Exec=")
        )
        assert exec_line.endswith(f'start "{proj.resolve()}" --native')
