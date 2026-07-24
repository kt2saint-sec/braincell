# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_gui_launcher.py — Milestone A regression tests for braincell/gui.py + cli.py.

Covers:
  A1  browser-open race    — create_app schedules the open only via the lifespan,
                             and only when open_browser_url is set.
  A2  global-brain missing — /api/status still 200 with global_brain.exists False.
  A3  one-click launcher   — install_launcher() writes icon + .desktop into XDG;
                             main_map() calls run_gui with the documented kwargs.
  A4  optional GUI token   — /api/* requires ?t= / header when auth_token is set;
                             unset token = unchanged behaviour.

All offline (TestClient), no real uvicorn, no browser, no Ollama.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def _app(tmp_path: Path, **kw):
    from braincell.gui import create_app
    return create_app(db_path=tmp_path / "braincell.db", **kw)


# ── A1: browser-open race ─────────────────────────────────────────────────────

class TestBrowserOpenA1:
    def test_no_open_when_url_none(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "braincell.gui._schedule_browser_open", lambda url, *a, **k: calls.append(url)
        )
        with TestClient(_app(tmp_path, open_browser_url=None)):
            pass
        assert calls == [], "browser open must not be scheduled when url is None"

    def test_schedules_exactly_one_open_with_url(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "braincell.gui._schedule_browser_open", lambda url, *a, **k: calls.append(url)
        )
        url = "http://127.0.0.1:8765"
        with TestClient(_app(tmp_path, open_browser_url=url)):
            pass
        assert calls == [url], "lifespan must schedule exactly one browser open"

    def test_schedule_helper_uses_call_later(self, monkeypatch):
        """With a running loop, _schedule_browser_open defers via call_later."""
        import asyncio

        opened = []
        monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u))

        async def _run():
            from braincell.gui import _schedule_browser_open
            _schedule_browser_open("http://x", delay=0)
            await asyncio.sleep(0.05)  # let the call_later fire

        asyncio.run(_run())
        assert opened == ["http://x"]


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
        assert "No global brain yet" in INDEX_HTML


# ── A3: launcher ──────────────────────────────────────────────────────────────

class TestInstallLauncherA3:
    def test_writes_icon_and_desktop(self, tmp_path, monkeypatch):
        xdg = tmp_path / "xdg"
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
        from braincell.gui import install_launcher

        icon, desktop = install_launcher()
        assert icon == xdg / "icons" / "braincell.svg"
        assert desktop == xdg / "applications" / "braincell-map.desktop"
        assert icon.exists() and desktop.exists()
        content = desktop.read_text()
        # Exec must be an ABSOLUTE path to the console script — a bare name fails
        # when a desktop environment launches the entry without the venv on PATH.
        exec_line = next(ln for ln in content.splitlines() if ln.startswith("Exec="))
        exec_target = exec_line[len("Exec="):]
        assert exec_target.endswith("braincell-map")
        assert exec_target.startswith("/"), f"Exec not absolute: {exec_target!r}"
        assert "Icon=braincell" in content
        assert "Name=BrainCell Map" in content
        assert icon.read_text().startswith("<?xml")
        # hicolor theme tree — the path GNOME/KDE resolve Icon=braincell from
        hicolor = xdg / "icons" / "hicolor"
        assert (hicolor / "scalable" / "apps" / "braincell.svg").exists()
        for size in (48, 128, 256, 512):
            assert (hicolor / f"{size}x{size}" / "apps" / "braincell.png").exists(), size

    def test_idempotent(self, tmp_path, monkeypatch):
        xdg = tmp_path / "xdg"
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
        from braincell.gui import install_launcher

        install_launcher()
        icon, desktop = install_launcher()  # second run must not error
        assert list((xdg / "applications").glob("*.desktop")) == [desktop]

    def test_main_map_calls_run_gui_with_documented_kwargs(self, monkeypatch):
        captured = {}
        monkeypatch.setattr("braincell.gui.run_gui", lambda **kw: captured.update(kw))
        from braincell.cli import main_map

        main_map([])
        assert captured == {
            "mode": "global",
            "port": 8765,
            "allow_writes": True,
            "open_browser": True,
            "path": ".",
        }

    def test_main_map_port_override(self, monkeypatch):
        captured = {}
        monkeypatch.setattr("braincell.gui.run_gui", lambda **kw: captured.update(kw))
        from braincell.cli import main_map

        main_map(["--port", "9999"])
        assert captured["port"] == 9999


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
