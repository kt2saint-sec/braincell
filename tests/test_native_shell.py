# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the mandatory PySide6 native application shell."""

from __future__ import annotations

import argparse
import sys

import pytest

from braincell import launch, native_shell


def _start_args(path, **kw):
    defaults = dict(path=str(path), port=8765, global_brain=False, native=False)
    defaults.update(kw)
    return argparse.Namespace(**defaults)


# ── native_available ──────────────────────────────────────────────────────────

class TestNativeAvailable:
    @pytest.mark.skipif(
        sys.platform != "linux",
        reason="the display-env gate is Linux-only (native_shell.native_available)",
    )
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
    def test_start_always_reaches_native_run_gui(self, tmp_path, monkeypatch):
        from braincell.cli import cmd_start
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr(native_shell, "native_available", lambda: True)
        monkeypatch.setattr(
            launch, "preflight", lambda *a, **k: launch.Preflight(action="launch")
        )
        captured: dict = {}
        monkeypatch.setattr("braincell.gui.run_gui", lambda **kw: captured.update(kw))
        cmd_start(_start_args(repo))
        assert captured["restart_command"] == "start"
        assert "open_browser" not in captured
        assert "native" not in captured

    def test_hidden_native_flag_is_compatibility_only(self, tmp_path, monkeypatch):
        from braincell.cli import cmd_start
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr(native_shell, "native_available", lambda: True)
        monkeypatch.setattr(
            launch, "preflight", lambda *a, **k: launch.Preflight(action="launch")
        )
        captured: dict = {}
        monkeypatch.setattr("braincell.gui.run_gui", lambda **kw: captured.update(kw))
        cmd_start(_start_args(repo, native=True))
        assert captured["restart_command"] == "start"

    def test_reuse_activates_existing_window(self, tmp_path, monkeypatch):
        from braincell.cli import cmd_start
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr(native_shell, "native_available", lambda: True)
        monkeypatch.setattr(
            launch,
            "preflight",
            lambda *a, **k: launch.Preflight(
                action="reuse", activation_token="tok", expected_db="/brain.db"
            ),
        )
        activated: list = []
        monkeypatch.setattr(
            launch,
            "activate_existing",
            lambda port, token: activated.append((port, token)) or True,
        )
        ran: list = []
        monkeypatch.setattr("braincell.gui.run_gui", lambda **kw: ran.append(kw))
        cmd_start(_start_args(repo))
        assert activated == [(8765, "tok")]
        assert ran == []

    def test_unavailable_native_is_a_visible_hard_failure(self, tmp_path, monkeypatch):
        from braincell.cli import cmd_start
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr(native_shell, "native_available", lambda: False)
        alerts: list = []
        monkeypatch.setattr(native_shell, "alert", lambda msg: alerts.append(msg))
        with pytest.raises(SystemExit) as exc:
            cmd_start(_start_args(repo))
        assert exc.value.code == 1
        assert alerts and "graphical desktop" in alerts[0]

    def test_conflict_native_shows_dialog_and_exits_1(self, tmp_path, monkeypatch):
        from braincell.cli import cmd_start
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr(native_shell, "native_available", lambda: True)
        monkeypatch.setattr(
            launch,
            "preflight",
            lambda *a, **k: launch.Preflight(
                action="conflict", conflict_db="/other.db", expected_db="/target.db"
            ),
        )
        dialogs: list = []
        monkeypatch.setattr(native_shell, "alert", lambda msg: dialogs.append(msg))
        with pytest.raises(SystemExit) as exc:
            cmd_start(_start_args(repo))
        assert exc.value.code == 1
        assert len(dialogs) == 1 and "DIFFERENT brain" in dialogs[0]


# ── run_gui native path ───────────────────────────────────────────────────────

class TestRunGuiNative:
    def _run(self, tmp_path, monkeypatch, *, available=True, **kw):
        from braincell import gui
        captured: dict = {}
        served: list = []
        monkeypatch.setattr(
            gui, "create_app", lambda **k: captured.update(k) or object()
        )
        monkeypatch.setattr(native_shell, "native_available", lambda: available)
        monkeypatch.setattr(
            native_shell, "serve_native", lambda app, **k: served.append(k)
        )
        monkeypatch.setenv("BRAINCELL_GUI_TOKEN", "tok")
        gui.run_gui(
            mode="project", port=8123, allow_writes=True, path=str(tmp_path), **kw,
        )
        return captured, served

    def test_serves_via_native_shell(self, tmp_path, monkeypatch):
        captured, served = self._run(tmp_path, monkeypatch)
        assert served[0]["port"] == 8123
        assert served[0]["url"] == "http://127.0.0.1:8123/?t=tok"
        assert served[0]["bridge"] is captured["native_bridge"]

    def test_restart_argv_relaunches_start(self, tmp_path, monkeypatch):
        captured, _ = self._run(
            tmp_path, monkeypatch, restart_command="start"
        )
        argv = captured["restart_argv"]
        assert "start" in argv and "--native" not in argv
        assert "gui" not in argv
        assert not any("tour" in a for a in argv)

    def test_window_url_keeps_extra_query(self, tmp_path, monkeypatch):
        _, served = self._run(
            tmp_path, monkeypatch, url_extra_query="tour=1"
        )
        assert served[0]["url"] == "http://127.0.0.1:8123/?t=tok&tour=1"

    def test_unavailable_native_never_builds_the_app(self, tmp_path, monkeypatch):
        from braincell import gui
        monkeypatch.setattr(native_shell, "native_available", lambda: False)
        built: list = []
        monkeypatch.setattr(gui, "create_app", lambda **k: built.append(k))
        with pytest.raises(RuntimeError, match="graphical desktop"):
            gui.run_gui(
                mode="project", port=8123, allow_writes=True, path=str(tmp_path)
            )
        assert built == []


class TestNativeBridge:
    def test_activation_and_picker_cross_thread_callbacks(self, tmp_path):
        bridge = native_shell.NativeBridge()
        activated: list = []

        def pick(request):
            request.finish({"path": str(tmp_path)})

        bridge.attach(activate=lambda: activated.append(True), pick_folder=pick)
        assert bridge.activate() is True
        assert activated == [True]
        assert bridge.pick_folder() == {"path": str(tmp_path)}
        bridge.detach()
        assert bridge.activate() is False


# ── serve_native orchestration (fake server, fake window) ─────────────────────

class _FakeServer:
    def __init__(self, *, bind=True, linger=False):
        self._bind = bind
        self._linger = linger
        self.started = False
        self.should_exit = False

    def run(self):
        if self._bind:
            self.started = True
        if self._bind or self._linger:
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

    def test_startup_timeout_stops_and_joins_server_thread(self, monkeypatch):
        server = _FakeServer(bind=False, linger=True)
        monkeypatch.setattr(native_shell, "_make_server", lambda app, port: server)
        monkeypatch.setattr(
            native_shell,
            "open_window",
            lambda *a, **k: pytest.fail("window must not open before server bind"),
        )
        with pytest.raises(RuntimeError, match="did not start"):
            native_shell.serve_native(
                object(), port=1, url="http://u", startup_timeout=0.01
            )
        assert server.should_exit is True


# ── launcher Exec uses the native-by-default command ──────────────────────────

class TestLauncherNativeExec:
    def test_desktop_exec_needs_no_mode_flag(self, tmp_path, monkeypatch):
        xdg = tmp_path / "xdg"
        proj = tmp_path / "proj"
        proj.mkdir()
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
        from braincell.gui import install_launcher

        _, desktop = install_launcher(proj)
        exec_line = next(
            ln for ln in desktop.read_text(encoding="utf-8").splitlines() if ln.startswith("Exec=")
        )
        assert exec_line.endswith(f'start "{proj.resolve()}"')
        assert "--native" not in exec_line
