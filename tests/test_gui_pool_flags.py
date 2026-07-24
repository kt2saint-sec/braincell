# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
test_gui_pool_flags.py — regression tests for the extended POST /api/pool body
(family / all_projects / prune) in braincell/gui.py.

Hermetic: resolve_pool_sources / pool_into_global are monkeypatched with
recording fakes (the endpoint imports them from braincell.pool at request time,
so patching the module attributes intercepts the call) — no real pooling, no
real global brain content. The global-brain-exists gate only checks the file
exists, so a touch()ed empty file satisfies it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

import braincell.pool as pool_mod


def _app(tmp_path: Path, *, allow_writes: bool = True):
    from braincell.gui import create_app
    return create_app(db_path=tmp_path / "braincell.db", allow_writes=allow_writes)


def _touch_global_brain() -> Path:
    """Create an (empty) global brain file so api_pool passes its exists() gate."""
    from braincell.config import get_global_db_path
    p = get_global_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    return p


def _record_fakes(monkeypatch):
    """Patch resolve_pool_sources / pool_into_global; return the recording dict."""
    recorded: dict = {}
    sources = [("01POOLPROJAAAAAAAAAAAAAAAA", Path("/src/braincell.db"))]

    def fake_resolve(*, family=None, paths=None, include_all=False):
        recorded["resolve"] = {"family": family, "include_all": include_all}
        return sources, ["skip (no brain built): /elsewhere"]

    def fake_pool(srcs, global_db, *, prune=False):
        recorded["pool"] = {"sources": srcs, "global_db": global_db, "prune": prune}
        return [SimpleNamespace(project_id=sources[0][0], notes_upserted=1)]

    monkeypatch.setattr(pool_mod, "resolve_pool_sources", fake_resolve)
    monkeypatch.setattr(pool_mod, "pool_into_global", fake_pool)
    recorded["expected_sources"] = sources
    return recorded


class TestPoolFlags:
    def test_400_without_family_or_all(self, tmp_path):
        _touch_global_brain()
        with TestClient(_app(tmp_path)) as client:
            r = client.post("/api/pool", json={})
        assert r.status_code == 400

    def test_409_without_global_brain_still_first(self, tmp_path):
        # No global brain in this isolated XDG → the 409 gate fires before the 400.
        with TestClient(_app(tmp_path)) as client:
            r = client.post("/api/pool", json={"all_projects": True})
        assert r.status_code == 409

    def test_all_projects_and_prune_reach_core_verbatim(self, tmp_path, monkeypatch):
        global_db = _touch_global_brain()
        recorded = _record_fakes(monkeypatch)
        with TestClient(_app(tmp_path)) as client:
            r = client.post("/api/pool", json={"all_projects": True, "prune": True})
        assert r.status_code == 200
        assert recorded["resolve"] == {"family": None, "include_all": True}
        assert recorded["pool"]["sources"] == recorded["expected_sources"]
        assert recorded["pool"]["global_db"] == global_db
        assert recorded["pool"]["prune"] is True
        body = r.json()
        assert body["pooled"][0]["project_id"] == recorded["expected_sources"][0][0]
        assert body["skipped"] == ["skip (no brain built): /elsewhere"]

    def test_family_only_keeps_map_button_path(self, tmp_path, monkeypatch):
        """The existing GUI map-button body ({family: name}) still works — new
        fields default to include_all=False / prune=False."""
        _touch_global_brain()
        recorded = _record_fakes(monkeypatch)
        with TestClient(_app(tmp_path)) as client:
            r = client.post("/api/pool", json={"family": "fam"})
        assert r.status_code == 200
        assert recorded["resolve"] == {"family": "fam", "include_all": False}
        assert recorded["pool"]["prune"] is False

    def test_unknown_family_404(self, tmp_path, monkeypatch):
        _touch_global_brain()

        def raise_keyerror(*, family=None, paths=None, include_all=False):
            raise KeyError(family)

        monkeypatch.setattr(pool_mod, "resolve_pool_sources", raise_keyerror)
        with TestClient(_app(tmp_path)) as client:
            r = client.post("/api/pool", json={"family": "ghost"})
        assert r.status_code == 404

    def test_absent_in_read_only_mode(self, tmp_path):
        with TestClient(_app(tmp_path, allow_writes=False)) as client:
            r = client.post("/api/pool", json={"all_projects": True})
        assert r.status_code in (404, 405)
