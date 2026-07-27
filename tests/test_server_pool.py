# SPDX-License-Identifier: AGPL-3.0-or-later
"""MCP named-Pool Search/Recall boundary tests with real disposable databases."""

from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import MagicMock

from tests.conftest import _insert_doc_and_chunk, fake_vec


def _ctx(server, store):
    context = MagicMock()
    context.request_context.lifespan_context = server.AppState(store=store)
    return context


def test_mcp_pool_search_and_recall_are_live_read_only(tmp_path, monkeypatch):
    import braincell.federate as federate
    import braincell.server as server
    from braincell.config import get_db_path
    from braincell.project_registry import add_to_pool, create_pool, register_path
    from braincell.store import SqliteStore

    connected_path = tmp_path / "connected"
    member_path = tmp_path / "member"
    connected_path.mkdir()
    member_path.mkdir()
    register_path(connected_path, "01CONNECTED")
    register_path(member_path, "01MEMBER")
    create_pool("Research")
    add_to_pool("Research", ["01CONNECTED", "01MEMBER"])

    connected = SqliteStore(get_db_path("01CONNECTED"))
    member = SqliteStore(get_db_path("01MEMBER"))
    connected.assert_schema_version()
    member.assert_schema_version()

    async def seed():
        await _insert_doc_and_chunk(
            connected, project="01CONNECTED", doc_key="connected-doc",
            text="connected alpha", seed=1,
        )
        await _insert_doc_and_chunk(
            member, project="01MEMBER", doc_key="member-doc",
            text="member beta", seed=2,
        )
        await connected.remember(
            "connected memory", "note", "01CONNECTED", embedding=fake_vec(1)
        )
        await member.remember(
            "member memory", "note", "01MEMBER", embedding=fake_vec(2)
        )

    asyncio.run(seed())
    member.close()

    async def embed(_query):
        return fake_vec(3)

    monkeypatch.setattr(server, "embed_query_async", embed)
    monkeypatch.setenv("BRAINCELL_PROJECT_ID", "01CONNECTED")
    opened = []
    real_store = federate.SqliteStore

    def open_sentinel(path, *args, **kwargs):
        opened.append((path, kwargs.get("read_only", False)))
        return real_store(path, *args, **kwargs)

    monkeypatch.setattr(federate, "SqliteStore", open_sentinel)

    async def query():
        search_result = await server.search(
            "beta", pool="research", rank="keyword", ctx=_ctx(server, connected)
        )
        recall_result = await server.recall(
            "memory", pool="Research", ctx=_ctx(server, connected)
        )
        return search_result, recall_result

    search_result, recall_result = asyncio.run(query())
    assert isinstance(search_result, server.PoolSearchResult)
    assert isinstance(recall_result, server.PoolRecallResult)
    assert {hit.doc_key for hit in search_result.results} == {"member-doc"}
    assert {note.project_id for note in recall_result.results} == {
        "01CONNECTED", "01MEMBER"
    }
    assert {status.status for status in search_result.members} == {"ready"}
    assert opened and all(read_only for _path, read_only in opened)
    connected.close()


def test_mcp_pool_rejects_outsider_before_database_resolution(tmp_path, monkeypatch):
    import braincell.server as server
    from braincell.project_registry import add_to_pool, create_pool
    from braincell.store import SqliteStore

    create_pool("Private")
    add_to_pool("Private", ["01MEMBER"])
    connected = SqliteStore(tmp_path / "connected.db")
    connected.assert_schema_version()
    monkeypatch.setenv("BRAINCELL_PROJECT_ID", "01OUTSIDER")

    def forbidden(_project_id):
        raise AssertionError("member database resolution must not run")

    monkeypatch.setattr("braincell.federate.resolve_ulid_to_path", forbidden)
    try:
        async def query():
            await server.recall("", pool="Private", ctx=_ctx(server, connected))

        try:
            asyncio.run(query())
        except ValueError as exc:
            assert "not a member" in str(exc)
        else:
            raise AssertionError("outsider Pool query was accepted")
    finally:
        connected.close()


def test_pool_member_status_categories_use_disposable_real_databases(tmp_path):
    from braincell.config import get_db_path
    from braincell.federate import plan_for_pool
    from braincell.project_registry import add_to_pool, create_pool, register_path
    from braincell.store import SqliteStore

    ready_path = tmp_path / "ready"
    unavailable_path = tmp_path / "gone"
    corrupt_path = tmp_path / "corrupt"
    incompatible_path = tmp_path / "incompatible"
    for path in (ready_path, corrupt_path, incompatible_path):
        path.mkdir()
    register_path(ready_path, "01READY")
    register_path(unavailable_path, "01UNAVAILABLE")
    register_path(corrupt_path, "01CORRUPT")
    register_path(incompatible_path, "01INCOMPATIBLE")
    create_pool("Statuses")
    add_to_pool(
        "Statuses",
        ["01READY", "01MISSING", "01UNAVAILABLE", "01CORRUPT", "01INCOMPATIBLE"],
    )

    ready = SqliteStore(get_db_path("01READY"))
    ready.assert_schema_version()
    ready.close()
    corrupt_db = get_db_path("01CORRUPT")
    corrupt_db.parent.mkdir(parents=True, exist_ok=True)
    corrupt_db.write_bytes(b"not a sqlite database")
    incompatible = SqliteStore(get_db_path("01INCOMPATIBLE"))
    incompatible.assert_schema_version()
    incompatible.close()
    with sqlite3.connect(get_db_path("01INCOMPATIBLE")) as connection:
        connection.execute("UPDATE schema_version SET version = 999")

    plan = plan_for_pool("Statuses", "01READY")
    by_project = {status.project_id: status.status for status in plan.member_status}
    assert by_project == {
        "01READY": "ready",
        "01CORRUPT": "corrupt",
        "01INCOMPATIBLE": "incompatible",
        "01MISSING": "missing",
        "01UNAVAILABLE": "unavailable",
    }
