# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for the Project-bound public FastMCP surface."""

from __future__ import annotations

import asyncio

import pytest

from braincell import server

ALLOWED_TOOLS = {
    "search",
    "recall",
    "remember",
    "forget",
    "supersede",
    "get_document",
    "ingest_status",
    "list_documents",
    "list_projects",
    "list_pools",
}


def test_mcp_tool_registry_is_project_only() -> None:
    """The registered schemas expose no aggregate or retired query surface."""
    tools = server.mcp._tool_manager._tools
    assert set(tools) == ALLOWED_TOOLS
    assert "list_families" not in tools
    for name, tool in tools.items():
        properties = tool.parameters.get("properties", {})
        assert "projects" not in properties, name
        assert "scope" not in properties, name
        description = tool.description.lower()
        assert "family" not in description, name
        assert "global mode" not in description, name


def test_normal_query_schema_keeps_only_validated_singular_project_compatibility() -> None:
    tools = server.mcp._tool_manager._tools
    assert set(tools["search"].parameters["properties"]) == {
        "query", "project", "k", "rank", "pool",
    }
    assert set(tools["recall"].parameters["properties"]) == {
        "query", "project", "k", "min_cosine", "dedup", "include_superseded", "pool",
    }


def test_connected_project_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRAINCELL_PROJECT_ID", raising=False)
    with pytest.raises(ValueError, match="BRAINCELL_PROJECT_ID"):
        server.connected_project_id()


def test_other_project_is_rejected_before_store_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAINCELL_PROJECT_ID", "01CONNECTEDPROJECT0000000001")
    with pytest.raises(ValueError, match="cannot be selected"):
        asyncio.run(server.search("query", project="01OTHERPROJECT000000000000"))
    with pytest.raises(ValueError, match="cannot be selected"):
        asyncio.run(server.recall("query", project="01OTHERPROJECT000000000000"))


def test_pool_cannot_combine_with_project_compatibility_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAINCELL_PROJECT_ID", "01CONNECTEDPROJECT0000000001")
    with pytest.raises(ValueError, match="cannot be combined"):
        asyncio.run(server.search("query", project="01CONNECTEDPROJECT0000000001", pool="release"))
    with pytest.raises(ValueError, match="cannot be combined"):
        asyncio.run(server.recall("query", project="01CONNECTEDPROJECT0000000001", pool="release"))


def test_project_and_pool_catalog_tools_are_metadata_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        server,
        "load_path_registry",
        lambda: {"/workspace/a": "01A", "/workspace/b": "01B"},
    )
    monkeypatch.setattr(server, "load_pools", lambda: {"Release": ("01A", "01MISSING")})

    projects = asyncio.run(server.list_projects())
    pools = asyncio.run(server.list_pools())

    assert [(item.project_id, item.path) for item in projects] == [
        ("01A", "/workspace/a"),
        ("01B", "/workspace/b"),
    ]
    assert pools[0].name == "Release"
    assert pools[0].member_project_ids == ["01A", "01MISSING"]
    assert pools[0].member_status == {"01A": "registered", "01MISSING": "unregistered"}
