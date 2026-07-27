# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_gui_launcher.py — Milestone A regression tests for braincell/gui.py + cli.py.

Covers:
  A1  native activation    — /api/activate raises the existing Qt window through
                             the authenticated native bridge.
  A2  global-brain missing — /api/status still 200 with global_brain.exists False.
  A3  one-click launcher   — install_launcher() writes icon + .desktop into XDG;
                             main_map() calls run_gui with the documented kwargs.
  A4  optional GUI token   — /api/* requires ?t= / header when auth_token is set;
                             unset token = unchanged behaviour.

All offline (TestClient), no real uvicorn, Qt window, or Ollama.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def _app(tmp_path: Path, **kw):
    from braincell.gui import create_app
    return create_app(db_path=tmp_path / "braincell.db", **kw)


# ── A1: native activation ─────────────────────────────────────────────────────

class TestNativeActivationA1:
    class _Bridge:
        def __init__(self, available=True):
            self.available = available
            self.calls = 0

        def activate(self):
            self.calls += 1
            return self.available

    def test_activate_raises_existing_window(self, tmp_path):
        bridge = self._Bridge()
        with TestClient(_app(tmp_path, native_bridge=bridge)) as client:
            response = client.post("/api/activate")
        assert response.status_code == 200
        assert bridge.calls == 1

    def test_activate_requires_ready_native_window(self, tmp_path):
        bridge = self._Bridge(available=False)
        with TestClient(_app(tmp_path, native_bridge=bridge)) as client:
            response = client.post("/api/activate")
        assert response.status_code == 409
        assert bridge.calls == 1

    def test_activate_is_token_guarded(self, tmp_path):
        bridge = self._Bridge()
        with TestClient(
            _app(tmp_path, native_bridge=bridge, auth_token="s3cret")
        ) as client:
            assert client.post("/api/activate").status_code == 401
            assert client.post(
                "/api/activate", headers={"X-BrainCell-Token": "s3cret"}
            ).status_code == 200
        assert bridge.calls == 1


# ── A2: global-brain missing ──────────────────────────────────────────────────

class TestGlobalMissingA2:
    def test_status_ok_when_global_absent(self, tmp_path):
        with TestClient(_app(tmp_path)) as client:
            r = client.get("/api/status")
        assert r.status_code == 200
        assert r.json()["global_brain"]["exists"] is False

    def test_index_ok_when_global_absent(self, tmp_path):
        with TestClient(_app(tmp_path)) as client:
            r = client.get("/")
        assert r.status_code == 200

    def test_template_has_global_cta(self):
        from braincell.gui_template import INDEX_HTML
        assert "No global brain yet" not in INDEX_HTML


# ── A3: launcher ──────────────────────────────────────────────────────────────

class TestInstallLauncherA3:
    def test_writes_icon_and_desktop(self, tmp_path, monkeypatch):
        xdg = tmp_path / "xdg"
        proj = tmp_path / "proj"
        proj.mkdir()
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
        from braincell.gui import install_launcher

        icon, desktop = install_launcher(proj)
        assert icon == xdg / "icons" / "braincell.svg"
        # Filename stays braincell-map.desktop — GNOME favorites pin the
        # desktop-file id, so a rename would silently unpin the icon.
        assert desktop == xdg / "applications" / "braincell-map.desktop"
        assert icon.exists() and desktop.exists()
        content = desktop.read_text()
        # Exec must run the full launcher (`braincell start <project>`), via an
        # ABSOLUTE console-script path — a bare name fails when a desktop
        # environment launches the entry without the venv on PATH. It must NOT
        # be the old braincell-map global-only viewer (empty map on machines
        # with only per-project brains).
        exec_line = next(ln for ln in content.splitlines() if ln.startswith("Exec="))
        exec_target = exec_line[len("Exec="):]
        assert "braincell-map" not in exec_target
        assert f'" start "{proj.resolve()}"' in exec_target
        assert exec_target.startswith('"/'), f"Exec not absolute: {exec_target!r}"
        assert exec_target.split(" start ")[0].strip('"').endswith("/braincell")
        assert "Icon=braincell" in content
        assert "Name=BrainCell Map" in content
        assert icon.read_text().startswith("<?xml")
        # hicolor theme tree — the path GNOME/KDE resolve Icon=braincell from
        hicolor = xdg / "icons" / "hicolor"
        assert (hicolor / "scalable" / "apps" / "braincell.svg").exists()
        for size in (48, 128, 256, 512):
            assert (hicolor / f"{size}x{size}" / "apps" / "braincell.png").exists(), size

    def test_default_project_path_is_cwd(self, tmp_path, monkeypatch):
        xdg = tmp_path / "xdg"
        cwd = tmp_path / "here"
        cwd.mkdir()
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
        monkeypatch.chdir(cwd)
        from braincell.gui import install_launcher

        _, desktop = install_launcher()
        exec_line = next(
            ln for ln in desktop.read_text().splitlines() if ln.startswith("Exec=")
        )
        assert f'start "{cwd.resolve()}"' in exec_line

    def test_idempotent(self, tmp_path, monkeypatch):
        xdg = tmp_path / "xdg"
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
        from braincell.gui import install_launcher

        install_launcher(tmp_path)
        icon, desktop = install_launcher(tmp_path)  # second run must not error
        assert list((xdg / "applications").glob("*.desktop")) == [desktop]

    def test_main_map_calls_run_gui_with_documented_kwargs(self, monkeypatch):
        captured = {}
        from braincell import launch, native_shell
        monkeypatch.setattr(native_shell, "native_available", lambda: True)
        monkeypatch.setattr(
            launch,
            "preflight",
            lambda *a, **k: launch.Preflight(action="launch"),
        )
        monkeypatch.setattr("braincell.gui.run_gui", lambda **kw: captured.update(kw))
        from braincell.cli import main_map

        main_map([])
        assert captured == {
            "mode": "project",
            "port": 8765,
            "allow_writes": True,
            "path": ".",
            "url_extra_query": None,
            "restart_command": "start",
        }

    def test_main_map_port_override(self, monkeypatch):
        captured = {}
        from braincell import launch, native_shell
        monkeypatch.setattr(native_shell, "native_available", lambda: True)
        monkeypatch.setattr(
            launch,
            "preflight",
            lambda *a, **k: launch.Preflight(action="launch"),
        )
        monkeypatch.setattr("braincell.gui.run_gui", lambda **kw: captured.update(kw))
        from braincell.cli import main_map

        main_map(["--port", "9999"])
        assert captured["port"] == 9999

    def test_main_map_reuses_running_gui(self, monkeypatch):
        """A running GUI is activated instead of binding a second server."""
        from braincell import launch, native_shell

        activated = []
        run_gui_called = []
        monkeypatch.setattr(native_shell, "native_available", lambda: True)
        monkeypatch.setattr(
            launch,
            "preflight",
            lambda *a, **k: launch.Preflight(
                action="reuse",
                activation_token="tok",
                expected_db="/braincell.db",
            ),
        )
        monkeypatch.setattr(
            launch,
            "activate_existing",
            lambda port, token: activated.append((port, token)) or True,
        )
        monkeypatch.setattr("braincell.gui.run_gui", lambda **kw: run_gui_called.append(kw))
        from braincell.cli import main_map

        main_map([])
        assert activated == [(8765, "tok")]
        assert run_gui_called == []


# ── A4: optional GUI token ────────────────────────────────────────────────────

class TestGuiTokenA4:
    def test_no_token_unchanged(self, tmp_path):
        with TestClient(_app(tmp_path)) as client:
            assert client.get("/api/status").status_code == 200

    def test_token_required(self, tmp_path):
        with TestClient(_app(tmp_path, auth_token="s3cret")) as client:
            assert client.get("/api/status").status_code == 401

    def test_token_query_param_ok(self, tmp_path):
        with TestClient(_app(tmp_path, auth_token="s3cret")) as client:
            assert client.get("/api/status?t=s3cret").status_code == 200

    def test_token_header_ok(self, tmp_path):
        with TestClient(_app(tmp_path, auth_token="s3cret")) as client:
            r = client.get("/api/status", headers={"X-BrainCell-Token": "s3cret"})
        assert r.status_code == 200

    def test_index_not_guarded(self, tmp_path):
        """Only /api/* is guarded — the page itself must load to read the token."""
        with TestClient(_app(tmp_path, auth_token="s3cret")) as client:
            assert client.get("/").status_code == 200

    # ── durable-cookie auth across native-window restarts ───────────────────────

    def test_index_bare_sets_auth_cookie_and_serves_html(self, tmp_path):
        """Bare / (no ?t=) serves the page AND sets the durable auth cookie."""
        with TestClient(_app(tmp_path, auth_token="s3cret")) as client:
            r = client.get("/", follow_redirects=False)
        assert r.status_code == 200
        assert "BrainCell" in r.text
        sc = r.headers["set-cookie"]
        assert "bc_gui_token=s3cret" in sc
        assert "HttpOnly" in sc
        assert "samesite=strict" in sc.lower()
        assert "Secure" not in sc  # http loopback — Secure would make browsers drop it

    def test_index_strips_token_from_url_and_sets_cookie(self, tmp_path):
        """A ?t= is stripped to a clean URL (cookie carries auth now), params kept."""
        with TestClient(_app(tmp_path, auth_token="s3cret")) as client:
            r = client.get("/?t=s3cret&scope=family", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/?scope=family"
        assert "bc_gui_token=s3cret" in r.headers["set-cookie"]

    def test_index_strips_bare_token_to_root(self, tmp_path):
        """A lone ?t= redirects to a clean "/" and sets the cookie."""
        with TestClient(_app(tmp_path, auth_token="s3cret")) as client:
            r = client.get("/?t=s3cret", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/"
        assert "bc_gui_token=s3cret" in r.headers["set-cookie"]

    def test_cookie_authenticates_api(self, tmp_path):
        """A request carrying the auth cookie (no ?t=) is accepted by /api/*."""
        with TestClient(_app(tmp_path, auth_token="s3cret")) as client:
            client.cookies.set("bc_gui_token", "s3cret")
            assert client.get("/api/status").status_code == 200

    def test_stale_cookie_rejected(self, tmp_path):
        """A wrong cookie value is rejected — the cookie is a real credential."""
        with TestClient(_app(tmp_path, auth_token="s3cret")) as client:
            client.cookies.set("bc_gui_token", "wrong")
            assert client.get("/api/status").status_code == 401

    def test_bare_visit_then_api_flows_via_cookie(self, tmp_path):
        """End-to-end: visit / (bare) sets the cookie, then /api/* just works —
        the exact 'reopen the address after closing the browser' path."""
        with TestClient(_app(tmp_path, auth_token="s3cret")) as client:
            assert client.get("/").status_code == 200          # sets cookie in jar
            assert client.get("/api/status").status_code == 200  # cookie flows

    def test_index_no_cookie_without_auth(self, tmp_path):
        """No-auth mode serves the page directly and sets no auth cookie."""
        with TestClient(_app(tmp_path)) as client:
            r = client.get("/", follow_redirects=False)
        assert r.status_code == 200
        assert "bc_gui_token" not in r.headers.get("set-cookie", "")


# ── favicon (the one console 404 every page load logged) ─────────────────────

class TestFavicon:
    def test_served_from_package_assets(self, tmp_path):
        """GET /favicon.ico serves the packaged braincell.ico — the same
        braincell/assets tree the desktop launcher installs from (single
        source of truth); browsers AND the native webview auto-request it."""
        from importlib.resources import files
        with TestClient(_app(tmp_path)) as client:
            r = client.get("/favicon.ico")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/x-icon")
        expected = files("braincell").joinpath("assets", "braincell.ico").read_bytes()
        assert r.content == expected

    def test_not_token_gated(self, tmp_path):
        """Favicon requests carry no token/cookie context worth gating — the
        guard covers /api/* only, same posture as GET /."""
        with TestClient(_app(tmp_path, auth_token="s3cret")) as client:
            assert client.get("/favicon.ico").status_code == 200
