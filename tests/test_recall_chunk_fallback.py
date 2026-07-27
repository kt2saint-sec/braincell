# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""Cold-start chunk fallback for recall (server.recall_notes).

A freshly built brain has thousands of searchable chunks and ~zero curated
notes, so `recall` — and the proactive family-recall hook, which is
`braincell recall --scope family` — delivered nothing on day one. These tests
pin the fix: when fewer than k curated notes match a non-empty query,
recall_notes backfills the remainder with provenance-marked transcript
excerpts (retrieval_origin='chunk', kind='excerpt', NEGATIVE id).

Provenance invariants pinned here (do not weaken):
  - an excerpt's id is negative → forget/supersede can never touch a real note;
  - kind='excerpt' is NOT a valid `remember` kind → an excerpt can never be
    written back as curated memory;
  - curated notes always rank BEFORE excerpts;
  - history/audit views (include_superseded=True) and empty-query recency
    listings stay notes-only;
  - a fallback failure never breaks recall.
"""

import asyncio

import pytest

from tests.conftest import _insert_doc_and_chunk, fake_vec, make_store

PROJ = "01TESTPROJECTULID0000000AA"  # 26 alnum chars — ULID-shaped


@pytest.fixture(autouse=True)
def _fast_embed(monkeypatch):
    """No live Ollama: patch the query embedder to a deterministic fake vector
    (same pattern as test_recall_cli). Chunks are seeded with distinctive
    keywords so FTS retrieval is deterministic regardless of vector ranking."""
    async def _fake_embed(text: str):
        return fake_vec(0)
    monkeypatch.setattr("braincell.server.embed_query_async", _fake_embed)
    monkeypatch.setenv("BRAINCELL_PROJECT_ID", PROJ)
    monkeypatch.delenv("BRAINCELL_FEDERATE", raising=False)


def _recall(store, query, k=5, **kw):
    from braincell.server import recall_notes
    return asyncio.run(recall_notes(store, query, project=PROJ, k=k, **kw))


def _seed_chunks(store, n=3):
    async def _run():
        for i in range(n):
            await _insert_doc_and_chunk(
                store, project=PROJ, doc_key=f"{PROJ}:session-{i}",
                text=f"halftone pipeline decision {i}: use error diffusion",
                seed=i,
            )
    asyncio.run(_run())


class TestColdStartFallback:
    """Fresh brain (chunks, zero notes) must deliver useful recall on day one."""

    def test_fresh_brain_returns_excerpts(self, tmp_path):
        store = make_store(tmp_path)
        _seed_chunks(store, 3)
        notes = _recall(store, "halftone error diffusion", k=5)
        assert notes, "cold-start recall returned nothing — the defect this fix removes"
        for n in notes:
            assert n.retrieval_origin == "chunk"
            assert n.kind == "excerpt"
            assert n.id < 0, "excerpt ids must be negative (never a real note id)"
            assert "halftone" in n.content
            assert n.project_id == PROJ  # parsed from the doc_key ULID prefix

    def test_excerpt_kind_is_not_a_valid_remember_kind(self, tmp_path):
        """kind='excerpt' can never be persisted as a curated note."""
        store = make_store(tmp_path)
        with pytest.raises(ValueError):
            asyncio.run(store.remember(text="x", kind="excerpt", project=PROJ))

    def test_forget_on_excerpt_id_is_a_safe_noop(self, tmp_path):
        """A consumer that passes an excerpt's negative id to forget must not
        delete anything (memory-poisoning guard: excerpts can't address notes)."""
        store = make_store(tmp_path)
        _seed_chunks(store, 1)

        async def _run():
            from braincell.server import recall_notes
            await store.remember(
                text="a real curated note about halftone",
                kind="note", project=PROJ, embedding=fake_vec(99),
            )
            excerpts = [
                n for n in await recall_notes(store, "halftone", project=PROJ, k=5)
                if n.retrieval_origin == "chunk"
            ]
            assert excerpts
            deleted = await store.forget(excerpts[0].id, PROJ)
            assert deleted is False
            # The real note is untouched.
            row = await (await (await store._conn_get()).execute(
                "SELECT COUNT(*) FROM memory_notes WHERE status='active'"
            )).fetchone()
            assert row[0] == 1
        asyncio.run(_run())


class TestOrderingAndGates:
    def test_curated_notes_rank_before_excerpts(self, tmp_path):
        store = make_store(tmp_path)
        _seed_chunks(store, 2)

        async def _seed_note():
            await store.remember(
                text="curated: halftone uses error diffusion",
                kind="decision", project=PROJ, embedding=fake_vec(0),
            )
        asyncio.run(_seed_note())
        notes = _recall(store, "halftone error diffusion", k=5)
        assert notes[0].retrieval_origin != "chunk"
        assert notes[0].kind == "decision"
        assert any(n.retrieval_origin == "chunk" for n in notes[1:])

    def test_k_filled_notes_get_no_excerpts(self, tmp_path):
        store = make_store(tmp_path)
        _seed_chunks(store, 2)

        async def _seed():
            for i in range(3):
                await store.remember(
                    text=f"curated halftone note {i}",
                    kind="note", project=PROJ, embedding=fake_vec(i),
                )
        asyncio.run(_seed())
        notes = _recall(store, "halftone", k=3)
        assert len(notes) == 3
        assert all(n.retrieval_origin != "chunk" for n in notes)

    def test_empty_query_stays_notes_only(self, tmp_path):
        store = make_store(tmp_path)
        _seed_chunks(store, 2)
        assert _recall(store, "", k=5) == []

    def test_include_superseded_audit_view_stays_notes_only(self, tmp_path):
        store = make_store(tmp_path)
        _seed_chunks(store, 2)
        notes = _recall(store, "halftone", k=5, include_superseded=True)
        assert all(n.retrieval_origin != "chunk" for n in notes)

    def test_env_off_switch_disables_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BRAINCELL_RECALL_CHUNK_FALLBACK", "off")
        store = make_store(tmp_path)
        _seed_chunks(store, 2)
        assert _recall(store, "halftone", k=5) == []


class TestDedup:
    def test_duplicate_chunk_text_yields_one_excerpt(self, tmp_path):
        """Two different docs/chunks carrying identical text (e.g. a transcript
        snippet duplicated across sessions) must surface as ONE excerpt, not
        one per chunk."""
        store = make_store(tmp_path)

        async def _seed():
            for i in range(2):
                await _insert_doc_and_chunk(
                    store, project=PROJ, doc_key=f"{PROJ}:dup-session-{i}",
                    text="halftone pipeline decision: use error diffusion",
                    seed=0,  # same seed -> same fake embedding -> same relevance
                )
        asyncio.run(_seed())
        notes = _recall(store, "halftone error diffusion", k=5)
        excerpts = [n for n in notes if n.retrieval_origin == "chunk"]
        assert excerpts, "expected at least one excerpt"
        contents = [n.content for n in excerpts]
        assert len(contents) == len(set(contents)), "duplicate excerpt content leaked through"


class TestFailureIsolation:
    def test_chunk_search_failure_never_breaks_recall(self, tmp_path, monkeypatch):
        store = make_store(tmp_path)
        _seed_chunks(store, 2)

        async def _boom(*a, **kw):
            raise RuntimeError("chunk search exploded")
        monkeypatch.setattr(type(store), "search", _boom)

        async def _seed():
            await store.remember(
                text="surviving curated note about halftone",
                kind="note", project=PROJ, embedding=fake_vec(0),
            )
        asyncio.run(_seed())
        notes = _recall(store, "halftone", k=5)
        assert [n.kind for n in notes] == ["note"]
