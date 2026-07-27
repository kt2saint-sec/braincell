# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression coverage for the native Memory Map's project boundary.

Ordinary GUI routes use the connected project's already-open store only.
The explicit named-Pool routes are the sole API surface permitted to read a
second Project database, and do so through the federation read-only plan.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient


PROJECT_A = "GUIISOLATIONA01"
PROJECT_B = "GUIISOLATIONB02"


def _register(tmp_path: Path, name: str, project_id: str) -> Path:
    from braincell.project_registry import register_path

    path = tmp_path / name
    path.mkdir()
    register_path(path, project_id)
    return path


def _db(project_id: str) -> Path:
    from braincell.config import get_db_path

    return get_db_path(project_id)


def _seed(project_id: str, text: str) -> None:
    from braincell.store import SqliteStore

    store = SqliteStore(_db(project_id))
    store.assert_schema_version()

    async def _write() -> None:
        await store.remember(text, "note", project_id)
        await store.aclose()

    asyncio.run(_write())


def _app(tmp_path: Path, *, writes: bool = False):
    from braincell.gui import create_app

    _register(tmp_path, "project-a", PROJECT_A)
    _register(tmp_path, "project-b", PROJECT_B)
    _seed(PROJECT_A, "connected project note")
    _seed(PROJECT_B, "sibling project note")
    return create_app(
        db_path=_db(PROJECT_A), allow_writes=writes, seed_project_id=PROJECT_A
    )


class TestNormalGuiProjectIsolation:
    def test_normal_recall_rejects_a_sibling_without_resolving_its_database(self, tmp_path):
        with TestClient(_app(tmp_path)) as client:
            lookup = Mock(side_effect=AssertionError("normal recall resolved a sibling DB"))
            with patch("braincell.config.get_db_path", lookup):
                response = client.get(f"/api/notes?projects={PROJECT_B}")

        assert response.status_code == 400
        assert "connected Project" in response.json()["detail"]
        lookup.assert_not_called()

    def test_normal_search_and_feed_reject_sibling_or_retired_cross_project_queries(self, tmp_path):
        with TestClient(_app(tmp_path)) as client:
            requests = (
                f"/api/search?q=note&projects={PROJECT_B}",
                f"/api/feed?projects={PROJECT_B}",
                "/api/notes?federate=true",
                f"/api/search?q=note&seed={PROJECT_B}",
            )
            for url in requests:
                response = client.get(url)
                assert response.status_code == 400, url
                assert "Pool" in response.json()["detail"]

    def test_normal_routes_read_only_the_connected_project(self, tmp_path):
        with TestClient(_app(tmp_path)) as client:
            notes = client.get("/api/notes").json()["notes"]
            feed = client.get("/api/feed").json()["notes"]

        assert [note["content"] for note in notes] == ["connected project note"]
        assert {event["project"] for event in feed} == {PROJECT_A}

    def test_project_catalog_does_not_open_a_sibling_database(self, tmp_path):
        with TestClient(_app(tmp_path)) as client:
            lookup = Mock(side_effect=AssertionError("catalog resolved a sibling DB"))
            with patch("braincell.config.get_db_path", lookup):
                response = client.get("/api/projects")

        assert response.status_code == 200
        lookup.assert_not_called()

    def test_normal_write_rejects_a_sibling_project_id(self, tmp_path):
        with TestClient(_app(tmp_path, writes=True)) as client:
            forget = client.post(
                "/api/forget", json={"note_id": 1, "project": PROJECT_B}
            )
            clear = client.post("/api/clear", json={"project_id": PROJECT_B})
            operation = client.post(
                "/api/ops/reembed-notes", json={"project_id": PROJECT_B}
            )
            sibling = next(
                row["path"]
                for row in client.get("/api/projects").json()
                if row["project_id"] == PROJECT_B
            )
            build = client.post("/api/ingest", json={"path": sibling})

        for response in (forget, clear, operation, build):
            assert response.status_code == 409
            assert "connected Project" in response.json()["detail"]
