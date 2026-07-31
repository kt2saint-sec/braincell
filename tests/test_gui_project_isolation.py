# SPDX-License-Identifier: AGPL-3.0-or-later
"""Boundary coverage for the native Memory Map's project-only HTTP transport.

These are transport-level regressions for the Qt-hosted application. They do
not claim browser automation is native-GUI acceptance coverage.
"""

from __future__ import annotations

import asyncio
import sqlite3
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
    def test_normal_routes_reject_sibling_and_retired_selectors_without_db_lookup(self, tmp_path):
        with TestClient(_app(tmp_path)) as client:
            lookup = Mock(side_effect=AssertionError("normal route resolved a sibling DB"))
            with patch("braincell.config.get_db_path", lookup):
                responses = (
                    client.get(f"/api/notes?projects={PROJECT_B}"),
                    client.get(f"/api/search?q=note&projects={PROJECT_B}"),
                    client.get(f"/api/feed?projects={PROJECT_B}"),
                    client.get("/api/notes?federate=true"),
                    client.get(f"/api/search?q=note&seed={PROJECT_B}"),
                    client.get("/api/projects"),
                )

        for response in responses[:-1]:
            assert response.status_code == 400
        assert responses[-1].status_code == 200
        lookup.assert_not_called()

    def test_normal_startup_search_recall_never_calls_global_db_resolver(self, tmp_path):
        with patch(
            "braincell.config.get_global_db_path",
            side_effect=AssertionError("normal GUI touched legacy global database"),
        ), TestClient(_app(tmp_path)) as client:
            assert client.get("/api/status").status_code == 200
            assert client.get("/api/notes").status_code == 200
            assert client.get("/api/search?q=connected").status_code == 200

    def test_sqlite_open_sentinel_allows_only_connected_then_explicit_pool_members(self, tmp_path):
        """Record every SQLite target opened by GUI startup and query operations."""
        from braincell.config import get_global_db_path
        from braincell.project_registry import add_to_pool, create_pool
        from braincell.store import SqliteStore as RealStore

        app = _app(tmp_path)
        create_pool("Shared")
        add_to_pool("Shared", [PROJECT_A, PROJECT_B])
        opened: set[Path] = set()

        def record_store(path, *args, **kwargs):
            opened.add(Path(path).resolve())
            return RealStore(path, *args, **kwargs)

        real_connect = sqlite3.connect

        def record_connect(database, *args, **kwargs):
            if isinstance(database, str) and database.startswith("file:"):
                opened.add(Path(database[5:].split("?", 1)[0]).resolve())
            return real_connect(database, *args, **kwargs)

        with (
            patch("braincell.gui.SqliteStore", side_effect=record_store),
            patch("braincell.federate.SqliteStore", side_effect=record_store),
            patch("braincell.federate.sqlite3.connect", side_effect=record_connect),
            TestClient(app) as client,
        ):
            assert client.get("/api/status").status_code == 200
            assert client.get("/api/notes").status_code == 200
            assert client.get("/api/search?q=connected").status_code == 200
            assert _db(PROJECT_B).resolve() not in opened

            response = client.post("/api/pools/recall", json={"pool": "Shared"})
            assert response.status_code == 200

        assert _db(PROJECT_A).resolve() in opened
        assert _db(PROJECT_B).resolve() in opened
        assert get_global_db_path().resolve() not in opened

    def test_legacy_materialized_routes_are_not_mounted(self, tmp_path):
        with TestClient(_app(tmp_path, writes=True)) as client:
            assert client.post("/api/family", json={}).status_code == 404
            assert client.post("/api/pool", json={}).status_code == 404

    def test_decouple_and_readd_change_live_pool_results_without_copying_memory(self, tmp_path):
        from braincell.project_registry import (
            add_to_pool,
            create_pool,
            decouple_from_pool,
        )

        app = _app(tmp_path)
        create_pool("Shared")
        add_to_pool("Shared", [PROJECT_A, PROJECT_B])
        before_a = _db(PROJECT_A).read_bytes()
        before_b = _db(PROJECT_B).read_bytes()
        with TestClient(app) as client:
            first = client.post("/api/pools/recall", json={"pool": "Shared", "query": ""})
            assert first.status_code == 200
            assert {note["content"] for note in first.json()["notes"]} >= {
                "connected project note", "sibling project note"
            }

            assert decouple_from_pool("Shared", PROJECT_B) is True
            detached = client.post("/api/pools/recall", json={"pool": "Shared", "query": ""})
            assert detached.status_code == 200
            assert "sibling project note" not in {
                note["content"] for note in detached.json()["notes"]
            }

            add_to_pool("Shared", [PROJECT_B])
            restored = client.post("/api/pools/recall", json={"pool": "Shared", "query": ""})
            assert restored.status_code == 200
            assert "sibling project note" in {
                note["content"] for note in restored.json()["notes"]
            }

        assert _db(PROJECT_A).read_bytes() == before_a
        assert _db(PROJECT_B).read_bytes() == before_b
        for db in (_db(PROJECT_A), _db(PROJECT_B)):
            connection = sqlite3.connect(db)
            try:
                assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
                connection.execute("SELECT COUNT(*) FROM memory_fts").fetchone()
            finally:
                connection.close()

    def test_normal_write_rejects_a_sibling_project_id(self, tmp_path):
        with TestClient(_app(tmp_path, writes=True)) as client:
            responses = (
                client.post("/api/forget", json={"note_id": 1, "project": PROJECT_B}),
                client.post("/api/clear", json={"project_id": PROJECT_B}),
                client.post("/api/ops/reembed-notes", json={"project_id": PROJECT_B}),
            )
        for response in responses:
            assert response.status_code == 409
            assert "connected Project" in response.json()["detail"]
