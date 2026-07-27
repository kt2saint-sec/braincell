# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression coverage for retired global surfaces and Project isolation."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3

import pytest

from braincell import server
from braincell.cli import _run_build
from braincell.config import get_db_path
from braincell.project_registry import load_path_registry
from braincell.store import SqliteStore, upsert_document

_PROJ_A = "01PROJA000000000000000001A"
_PROJ_B = "01PROJB000000000000000001B"


class TestProjectBuildAndBackup:
    def test_build_creates_one_project_database_and_preserves_its_ulid(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()

        _run_build(root, skip_transcripts=True, reembed=False, verbose=False)
        first_id = load_path_registry()[str(root)]
        assert get_db_path(first_id).exists()

        _run_build(root, skip_transcripts=True, reembed=False, verbose=False)
        assert load_path_registry()[str(root)] == first_id

    def test_backup_copy_preserves_schema_and_project_rows(self, tmp_path):
        source = tmp_path / "source.db"
        destination = tmp_path / "backup.db"
        store = SqliteStore(source)
        store.assert_schema_version()

        async def seed() -> None:
            connection = await store._conn_get()
            await upsert_document(
                connection,
                project_id=_PROJ_A,
                doc_key="doc-a",
                title="Doc A",
                content_hash=hashlib.sha256(b"a").digest(),
            )
            await connection.commit()

        asyncio.run(seed())
        store.close()

        with sqlite3.connect(source) as connection:
            connection.execute("VACUUM INTO ?", (str(destination),))
        with sqlite3.connect(destination) as connection:
            rows = connection.execute(
                "SELECT project_id, doc_key FROM bc_documents"
            ).fetchall()
        assert rows == [(_PROJ_A, "doc-a")]


class TestRetiredGlobalAndImplicitCrossProjectSurfaces:
    def test_normal_mcp_schemas_exclude_scope_and_projects(self):
        tools = server.mcp._tool_manager.list_tools()
        by_name = {tool.name: tool for tool in tools}
        assert "list_families" not in by_name
        for name in ("search", "recall"):
            properties = by_name[name].parameters["properties"]
            assert "scope" not in properties
            assert "projects" not in properties

    def test_normal_recall_rejects_another_project_before_store_access(self, monkeypatch):
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", _PROJ_A)
        monkeypatch.setattr(server, "_store", lambda _ctx: pytest.fail("store opened"))

        with pytest.raises(ValueError, match="different project"):
            asyncio.run(server.recall("query", project=_PROJ_B))

    def test_named_pool_cannot_be_combined_with_project_selection(self, monkeypatch):
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", _PROJ_A)
        monkeypatch.setattr(server, "_store", lambda _ctx: pytest.fail("store opened"))

        with pytest.raises(ValueError, match="cannot be combined"):
            asyncio.run(server.search("query", project=_PROJ_A, pool="release"))
