# SPDX-License-Identifier: AGPL-3.0-or-later
"""Project-only Memory Map API regression tests.

The native Memory Map is connected to exactly one Project.  Ordinary reads and
writes stay in its already-open database; a named Pool is the sole explicit,
read-only cross-project query path.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from fastapi.testclient import TestClient


PROJECT_A = "ACTMEMCONNECTED01"
PROJECT_B = "ACTMEMPOOLMEMBER2"


def _register(tmp_path: Path, name: str, project_id: str) -> Path:
    from braincell.project_registry import register_path

    path = tmp_path / name
    path.mkdir(exist_ok=True)
    register_path(path, project_id)
    return path


def _db(project_id: str) -> Path:
    from braincell.config import get_db_path

    return get_db_path(project_id)


def _seed(project_id: str, text: str) -> int:
    from braincell.store import SqliteStore

    store = SqliteStore(_db(project_id))
    store.assert_schema_version()

    async def _write() -> int:
        note_id = int(await store.remember(text, "note", project_id))
        await store.aclose()
        return note_id

    return asyncio.run(_write())


def _app(project_id: str, *, allow_writes: bool = False):
    from braincell.gui import create_app

    return create_app(
        db_path=_db(project_id),
        allow_writes=allow_writes,
        seed_project_id=project_id,
    )


def _setup_two_projects(tmp_path: Path) -> tuple[int, int]:
    _register(tmp_path, "project-a", PROJECT_A)
    _register(tmp_path, "project-b", PROJECT_B)
    return (
        _seed(PROJECT_A, "connected Project note"),
        _seed(PROJECT_B, "Pool member note"),
    )


class TestConnectedProjectBoundary:
    def _setup(self, tmp_path: Path) -> tuple[int, int]:
        return _setup_two_projects(tmp_path)

    def test_normal_recall_never_reads_a_sibling(self, tmp_path):
        self._setup(tmp_path)
        with TestClient(_app(PROJECT_A)) as client:
            normal = client.get("/api/notes")
            sibling = client.get(f"/api/notes?projects={PROJECT_B}")
            multiple = client.get(f"/api/notes?projects={PROJECT_A},{PROJECT_B}")

        assert [row["content"] for row in normal.json()["notes"]] == ["connected Project note"]
        for response in (sibling, multiple):
            assert response.status_code == 400
            assert "connected Project" in response.json()["detail"]

    def test_normal_search_and_recalled_family_flags_are_rejected(self, tmp_path):
        self._setup(tmp_path)
        with TestClient(_app(PROJECT_A)) as client:
            responses = (
                client.get(f"/api/search?q=Pool&projects={PROJECT_B}"),
                client.get("/api/notes?federate=true"),
                client.get(f"/api/search?q=Pool&seed={PROJECT_B}"),
            )

        for response in responses:
            assert response.status_code == 400
            assert "Pool" in response.json()["detail"]

    def test_forget_rejects_sibling_memory_even_when_note_ids_overlap(self, tmp_path):
        note_a, note_b = self._setup(tmp_path)
        assert note_a == note_b  # catches any future id-only write implementation

        with TestClient(_app(PROJECT_A, allow_writes=True)) as client:
            response = client.post(
                "/api/forget", json={"note_id": note_b, "project": PROJECT_B}
            )
            remaining = client.get("/api/notes").json()["notes"]

        assert response.status_code == 409
        assert [row["content"] for row in remaining] == ["connected Project note"]

    def test_catalog_reports_only_connected_project_counts(self, tmp_path):
        self._setup(tmp_path)
        with TestClient(_app(PROJECT_A)) as client:
            rows = {row["project_id"]: row for row in client.get("/api/projects").json()}

        assert rows[PROJECT_A]["notes"] == 1
        assert (rows[PROJECT_B]["docs"], rows[PROJECT_B]["chunks"], rows[PROJECT_B]["notes"]) == (0, 0, 0)


class TestExplicitPoolRead:
    def test_named_pool_is_the_only_cross_project_recall_path(self, tmp_path):
        _setup_two_projects(tmp_path)
        from braincell.project_registry import add_to_pool, create_pool

        create_pool("Release readiness")
        add_to_pool("Release readiness", [PROJECT_A, PROJECT_B])

        with TestClient(_app(PROJECT_A)) as client:
            response = client.post(
                "/api/pools/recall",
                json={"pool": "Release readiness", "query": "", "k": 10},
            )

        assert response.status_code == 200
        assert {row["content"] for row in response.json()["notes"]} == {
            "connected Project note",
            "Pool member note",
        }


class TestConfig:
    def test_seeded_config_identifies_the_connected_project(self, tmp_path):
        _register(tmp_path, "project-a", PROJECT_A)
        _seed(PROJECT_A, "connected Project note")
        with TestClient(_app(PROJECT_A)) as client:
            config = client.get("/api/config").json()

        assert config["seed_project_id"] == PROJECT_A
        assert config["launch_project_id"] == PROJECT_A


class TestScopeInvariants:
    def test_server_module_never_imports_gui(self):
        import braincell

        source = (Path(braincell.__file__).parent / "server.py").read_text(encoding="utf-8")
        assert "from .gui" not in source
        assert "import gui" not in source
        assert "braincell.gui" not in source

    def test_braincell_mode_is_never_assigned_in_package(self):
        import braincell

        pattern = re.compile(
            r"environ\[\s*['\"]BRAINCELL_MODE['\"]\s*\]\s*=|putenv\(\s*['\"]BRAINCELL_MODE"
        )
        package = Path(braincell.__file__).parent
        offenders = [
            path.name
            for path in package.rglob("*.py")
            if pattern.search(path.read_text(encoding="utf-8"))
        ]
        assert offenders == [], f"BRAINCELL_MODE assigned in: {offenders}"
