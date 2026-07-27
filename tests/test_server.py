# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_server.py — Offline regression tests for braincell/server.py.

Covers:
  - M2: tool annotation hints (readOnlyHint, destructiveHint, idempotentHint)
  - M3: structured-output Pydantic models (validation + outputSchema wiring)

No live store or Ollama required — all tests introspect the FastMCP
tool registry and the Pydantic models at import time.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

import pytest
from pydantic import ValidationError

from braincell.server import (
    DocumentResult,
    DocumentSummary,
    ForgetResult,
    IngestStatusResult,
    MemoryNote,
    RememberResult,
    SearchHit,
    SupersedeResult,
    mcp,
)

# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_tool(name: str):
    """Return the internal FastMCP Tool object by name (synchronous)."""
    tool = mcp._tool_manager.get_tool(name)
    assert tool is not None, f"Tool {name!r} not registered"
    return tool


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1: Pydantic model validation (M3)
# ══════════════════════════════════════════════════════════════════════════════

class TestSearchHitModel:
    """SearchHit validates correct dicts and rejects malformed ones."""

    def test_valid_with_all_fields(self):
        hit = SearchHit(
            chunk_id=1,
            doc_key="docs/readme.md",
            title="README",
            snippet="some snippet text",
            score=0.016393,
            cosine=0.87,
            fts_matched=True,
            source_path="/home/user/docs/readme.md",
        )
        assert hit.chunk_id == 1
        assert hit.cosine == 0.87
        assert hit.fts_matched is True

    def test_valid_with_optional_nulls(self):
        hit = SearchHit(
            chunk_id=42,
            doc_key="k",
            title="T",
            snippet="s",
            score=0.0,
            cosine=None,
            fts_matched=False,
            source_path=None,
        )
        assert hit.cosine is None
        assert hit.source_path is None

    def test_rejects_missing_required_fields(self):
        with pytest.raises(ValidationError):
            SearchHit(doc_key="k", title="t")  # missing chunk_id, snippet, score, fts_matched


class TestMemoryNoteModel:
    """MemoryNote validates a full note and rejects one missing the required id."""

    def test_valid_full(self):
        note = MemoryNote(
            id=7,
            project_id="01PROJ000000000000000000000",
            scope="project",
            kind="decision",
            content="Use hybrid search by default.",
            tags=["search", "arch"],
            confidence=0.9,
            source_hint="conversation-123",
            superseded_by=None,
            created_at="2026-06-30 12:00:00",
        )
        assert note.id == 7
        assert note.tags == ["search", "arch"]

    def test_valid_minimal_optionals_null(self):
        note = MemoryNote(
            id=1,
            project_id="p",
            scope="project",
            kind="note",
            content="minimal",
            tags=None,
            confidence=None,
            source_hint=None,
            superseded_by=None,
            created_at="2026-01-01 00:00:00",
        )
        assert note.confidence is None
        assert note.tags is None

    def test_rejects_missing_id(self):
        with pytest.raises(ValidationError):
            MemoryNote(  # type: ignore[call-arg]
                project_id="p",
                scope="project",
                kind="note",
                content="x",
                tags=None,
                confidence=None,
                source_hint=None,
                superseded_by=None,
                created_at="2026-01-01",
            )


class TestRememberResultModel:
    def test_valid_with_embedded_true(self):
        r = RememberResult(note_id="42", embedded=True)
        assert r.note_id == "42"
        assert r.embedded is True

    def test_valid_with_embedded_false(self):
        r = RememberResult(note_id="1", embedded=False)
        assert r.embedded is False

    def test_embedded_defaults_to_false(self):
        # embedded has a default of False for backward compat.
        r = RememberResult(note_id="42")
        assert r.embedded is False

    def test_rejects_missing_note_id(self):
        with pytest.raises(ValidationError):
            RememberResult()  # type: ignore[call-arg]


class TestForgetResultModel:
    def test_deleted_true(self):
        assert ForgetResult(deleted=True).deleted is True

    def test_deleted_false(self):
        assert ForgetResult(deleted=False).deleted is False

    def test_rejects_missing_deleted(self):
        with pytest.raises(ValidationError):
            ForgetResult()  # type: ignore[call-arg]


class TestSupersedeResultModel:
    def test_valid_with_embedded_true(self):
        r = SupersedeResult(new_id=10, superseded=5, embedded=True)
        assert r.new_id == 10
        assert r.superseded == 5
        assert r.embedded is True

    def test_embedded_defaults_to_false(self):
        # embedded has a default of False for backward compat.
        r = SupersedeResult(new_id=10, superseded=5)
        assert r.embedded is False

    def test_rejects_non_int_new_id(self):
        with pytest.raises(ValidationError):
            SupersedeResult(new_id="bad", superseded=1)  # type: ignore[arg-type]


class TestDocumentResultModel:
    def test_valid_full(self):
        doc = DocumentResult(
            id=1,
            doc_key="session-abc",
            title="My Doc",
            content_type="cell",
            commit_sha="abc123",
            created_at="2026-06-01 00:00:00",
            updated_at="2026-06-02 00:00:00",
            chunks=[{"idx": 0, "text": "hello"}],
            metadata={"source": "ingest"},
        )
        assert doc.id == 1
        assert len(doc.chunks) == 1

    def test_valid_optional_fields_null(self):
        doc = DocumentResult(
            id=2,
            doc_key="k",
            title="t",
            content_type="cell",
            commit_sha=None,
            created_at="2026-01-01",
            updated_at=None,
            chunks=[],
            metadata=None,
        )
        assert doc.commit_sha is None
        assert doc.metadata is None

    def test_rejects_missing_id(self):
        with pytest.raises(ValidationError):
            DocumentResult(  # type: ignore[call-arg]
                doc_key="k",
                title="t",
                content_type="cell",
                commit_sha=None,
                created_at="2026-01-01",
                updated_at=None,
                chunks=[],
                metadata=None,
            )


class TestIngestStatusResultModel:
    def test_valid_indexed(self):
        s = IngestStatusResult(
            indexed=True,
            doc_count=10,
            chunk_count=200,
            last_ingest_ts="2026-06-30 12:00:00",
            head_sha="deadbeef",
            stale=False,
        )
        assert s.indexed is True
        assert s.chunk_count == 200

    def test_valid_null_optionals(self):
        s = IngestStatusResult(
            indexed=False,
            doc_count=0,
            chunk_count=0,
            last_ingest_ts=None,
            head_sha=None,
            stale=False,
        )
        assert s.last_ingest_ts is None
        assert s.head_sha is None

    def test_rejects_missing_indexed(self):
        with pytest.raises(ValidationError):
            IngestStatusResult(  # type: ignore[call-arg]
                doc_count=0, chunk_count=0, last_ingest_ts=None, head_sha=None, stale=False
            )


class TestDocumentSummaryModel:
    def test_valid(self):
        ds = DocumentSummary(
            doc_key="readme",
            title="README",
            content_type="cell",
            created_at="2026-01-01",
            updated_at=None,
        )
        assert ds.doc_key == "readme"
        assert ds.updated_at is None

    def test_rejects_missing_doc_key(self):
        with pytest.raises(ValidationError):
            DocumentSummary(  # type: ignore[call-arg]
                title="T", content_type="cell", created_at="2026-01-01"
            )


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2: Tool annotations (M2)
# ══════════════════════════════════════════════════════════════════════════════

class TestToolAnnotations:
    """Verify ToolAnnotations are registered on each tool in the FastMCP registry."""

    def test_search_readonly(self):
        t = _get_tool("search")
        assert t.annotations is not None
        assert t.annotations.readOnlyHint is True

    def test_recall_readonly(self):
        t = _get_tool("recall")
        assert t.annotations is not None
        assert t.annotations.readOnlyHint is True

    def test_get_document_readonly(self):
        t = _get_tool("get_document")
        assert t.annotations is not None
        assert t.annotations.readOnlyHint is True

    def test_ingest_status_readonly(self):
        t = _get_tool("ingest_status")
        assert t.annotations is not None
        assert t.annotations.readOnlyHint is True

    def test_list_documents_readonly(self):
        t = _get_tool("list_documents")
        assert t.annotations is not None
        assert t.annotations.readOnlyHint is True

    def test_forget_destructive(self):
        t = _get_tool("forget")
        assert t.annotations is not None
        assert t.annotations.destructiveHint is True

    def test_supersede_idempotent(self):
        t = _get_tool("supersede")
        assert t.annotations is not None
        assert t.annotations.idempotentHint is True

    def test_remember_has_no_annotations(self):
        """remember is a bare @mcp.tool() — no ToolAnnotations object attached."""
        t = _get_tool("remember")
        assert t.annotations is None


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3: Structured output wiring — outputSchema (M3)
# ══════════════════════════════════════════════════════════════════════════════

class TestOutputSchema:
    """Verify all tools expose outputSchema (Pydantic return types → structured output)."""

    # Checked via the synchronous Tool.output_schema cached_property (no asyncio needed).
    _READ_TOOLS: ClassVar[list[str]] = [
        "search", "recall", "get_document", "ingest_status", "list_documents",
    ]
    _WRITE_TOOLS: ClassVar[list[str]] = ["remember", "forget", "supersede"]

    @pytest.mark.parametrize("tool_name", _READ_TOOLS)
    def test_read_tool_has_output_schema(self, tool_name: str):
        schema = _get_tool(tool_name).output_schema
        assert schema is not None, (
            f"Tool {tool_name!r} has no outputSchema — structured output not wired"
        )

    @pytest.mark.parametrize("tool_name", _WRITE_TOOLS)
    def test_write_tool_has_output_schema(self, tool_name: str):
        schema = _get_tool(tool_name).output_schema
        assert schema is not None, (
            f"Tool {tool_name!r} has no outputSchema — structured output not wired"
        )

    def test_list_tools_async_exposes_output_schema(self):
        """asyncio.run(mcp.list_tools()) returns MCPTool objects with outputSchema set."""
        mcp_tools = asyncio.run(mcp.list_tools())
        tool_map = {t.name: t for t in mcp_tools}

        for name in ["search", "recall", "ingest_status", "list_documents"]:
            assert tool_map[name].outputSchema is not None, (
                f"mcp.list_tools() MCPTool {name!r} missing outputSchema"
            )

    def test_ingest_status_schema_mentions_indexed(self):
        """IngestStatusResult schema must reference the 'indexed' field."""
        schema = _get_tool("ingest_status").output_schema
        assert schema is not None
        assert "indexed" in str(schema)

    def test_search_schema_mentions_chunk_id(self):
        """SearchHit schema must reference the 'chunk_id' field."""
        schema = _get_tool("search").output_schema
        assert schema is not None
        assert "chunk_id" in str(schema)

    def test_remember_schema_mentions_embedded(self):
        """RememberResult schema must reference the 'embedded' field (M1)."""
        schema = _get_tool("remember").output_schema
        assert schema is not None
        assert "embedded" in str(schema)

    def test_supersede_schema_mentions_embedded(self):
        """SupersedeResult schema must reference the 'embedded' field (M1)."""
        schema = _get_tool("supersede").output_schema
        assert schema is not None
        assert "embedded" in str(schema)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4: M1 — server-level embed-down best-effort path
# ══════════════════════════════════════════════════════════════════════════════

class TestServerRememberEmbedBestEffort:
    """server.remember: when embed_texts_async raises, the note still persists
    and RememberResult.embedded is False.

    Tests the best-effort path at the server level by monkeypatching
    braincell.server.embed_texts_async (imported at module level, so patchable
    with monkeypatch.setattr). A MagicMock Context routes _store(ctx) to an
    in-memory store so no real MCP infrastructure is needed.
    """

    def test_embed_down_persists_with_embedded_false(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        import braincell.server as srv
        from tests.conftest import make_store

        store = make_store(tmp_path)

        # Stub the MCP Context so _store(ctx) returns our in-memory store.
        mock_ctx = MagicMock()
        mock_ctx.request_context.lifespan_context = srv.AppState(store=store)

        # Simulate embedder being offline.
        async def failing_embed(texts):
            raise ConnectionError("embedder offline")

        monkeypatch.setattr(srv, "embed_texts_async", failing_embed)
        # Bypass the project-ID env-var check.
        monkeypatch.setattr(srv, "_pin_write_project", lambda p: p or "offline-proj")

        async def _run():
            result = await srv.remember(
                text="embed-down note content unique",
                kind="note",
                project="offline-proj",
                ctx=mock_ctx,
            )
            return result

        result = asyncio.run(_run())
        assert isinstance(result, srv.RememberResult)
        assert result.embedded is False
        assert result.note_id is not None

        # Verify the note was actually persisted (FTS-only).
        async def _verify():
            notes = await store.recall(
                None, "offline-proj", k=5, qtext="embed-down note"
            )
            return notes

        notes = asyncio.run(_verify())
        assert any("embed-down note content unique" in n.content for n in notes)

    def test_embed_texts_async_is_module_level_patchable(self):
        """Verify embed_texts_async is importable from braincell.server (patchable)."""
        import braincell.server as srv
        assert hasattr(srv, "embed_texts_async"), (
            "embed_texts_async must be imported at module level in server.py "
            "so monkeypatch.setattr(srv, 'embed_texts_async', ...) works."
        )


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5: Project-only Recall schema boundary
# ══════════════════════════════════════════════════════════════════════════════

class TestRecallScopeParameter:
    """Recall has no implicit cross-Project scope selector."""

    def test_recall_tool_excludes_retired_cross_project_inputs(self):
        """Recall permits only its singular connected-Project compatibility input."""
        tool = _get_tool("recall")
        assert tool is not None
        import inspect
        sig = inspect.signature(tool.fn)
        assert "scope" not in sig.parameters
        assert "projects" not in sig.parameters
