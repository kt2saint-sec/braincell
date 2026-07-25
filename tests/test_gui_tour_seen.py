# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_gui_tour_seen.py — server-persisted onboarding flag (2026-07-25).

The guided tour's auto-start used to depend on browser localStorage plus an
empty brain: the native window's webview profile has no persistent
localStorage (re-ambush every launch) and a populated brain suppressed
onboarding forever. The durable signal is now a namespace-level flag file:

  GET  /api/config      -> tour_seen: bool (flag file exists)
  POST /api/tour-seen   -> touch the flag (idempotent), token-gated like all /api/*

Hermetic: XDG_DATA_HOME is pointed at tmp so the real namespace dir is never
touched.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def _app(tmp_path: Path, **kw):
    from braincell.gui import create_app
    return create_app(db_path=tmp_path / "braincell.db", **kw)


class TestTourSeen:
    def test_config_reports_false_on_fresh_install(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        with TestClient(_app(tmp_path)) as client:
            r = client.get("/api/config")
        assert r.status_code == 200
        assert r.json()["tour_seen"] is False

    def test_post_marks_and_config_flips(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        with TestClient(_app(tmp_path)) as client:
            r = client.post("/api/tour-seen")
            assert r.status_code == 200
            assert r.json() == {"ok": True, "tour_seen": True}
            assert client.get("/api/config").json()["tour_seen"] is True

    def test_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        with TestClient(_app(tmp_path)) as client:
            assert client.post("/api/tour-seen").status_code == 200
            assert client.post("/api/tour-seen").status_code == 200
            assert client.get("/api/config").json()["tour_seen"] is True

    def test_persists_across_server_instances(self, tmp_path, monkeypatch):
        """The whole point: a fresh app (new launch, new browser profile, the
        native window's non-persistent webview) still sees tour_seen=True."""
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        with TestClient(_app(tmp_path)) as client:
            client.post("/api/tour-seen")
        with TestClient(_app(tmp_path)) as fresh:
            assert fresh.get("/api/config").json()["tour_seen"] is True

    def test_token_gated_like_every_api_route(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        with TestClient(_app(tmp_path, auth_token="s3cret")) as client:
            assert client.post("/api/tour-seen").status_code == 401
            assert client.post("/api/tour-seen?t=s3cret").status_code == 200

    def test_available_in_read_only_launches(self, tmp_path, monkeypatch):
        """Mounted unconditionally (a UX flag, not a memory write): a read-only
        map can still stop re-offering the tour after a manual run."""
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        with TestClient(_app(tmp_path, allow_writes=False)) as client:
            assert client.post("/api/tour-seen").status_code == 200
