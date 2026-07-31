# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
test_gui_feed.py — regression tests for the GUI activity feed:
SqliteStore.tail_since (store.py) and GET /api/feed (gui.py).

Store tests seed notes/documents and drive tail_since directly; endpoint tests
assert the exact response contract ({notes, documents, cursors, job}), that the
route is mounted unconditionally (read-only launches included), token gating,
and the job field sourced from app.state.ingest_manager.

All async work per store happens inside ONE asyncio.run() (reusing a store
across two asyncio.run() calls hangs — see the test-store lifecycle note in the
maintainer working notes).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import _insert_doc_and_chunk, make_store

PID_A = "01FEEDPROJAAAAAAAAAAAAAAAA"
PID_B = "01FEEDPROJBBBBBBBBBBBBBBBB"


def _app(tmp_path: Path, *, allow_writes: bool = False,
         auth_token: str | None = None):
    from braincell.gui import create_app
    return create_app(
        db_path=tmp_path / "braincell.db",
        allow_writes=allow_writes,
        auth_token=auth_token,
    )


# ── SqliteStore.tail_since ────────────────────────────────────────────────────

class TestTailSince:
    def test_initial_batch_newest_first_with_cursors(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            n1 = int(await store.remember(text="first note", kind="note", project=PID_A))
            n2 = int(await store.remember(text="second note", kind="note", project=PID_A))
            d1 = await _insert_doc_and_chunk(
                store, project=PID_A, doc_key="doc-a", text="alpha", seed=1)
            d2 = await _insert_doc_and_chunk(
                store, project=PID_A, doc_key="doc-b", text="beta", seed=2)
            data = await store.tail_since(
                note_after=0, doc_after=0, projects=None, limit=30)
            await store.aclose()
            return n1, n2, d1, d2, data

        n1, n2, d1, d2, data = asyncio.run(_run())

        assert [n["id"] for n in data["notes"]] == [n2, n1]  # newest-first
        assert [d["id"] for d in data["documents"]] == [d2, d1]
        note = data["notes"][0]
        assert set(note) == {"id", "project", "kind", "content", "created_at", "status"}
        assert note["project"] == PID_A
        assert note["kind"] == "note"
        assert note["content"] == "second note"
        assert note["status"] == "active"
        doc = data["documents"][0]
        assert set(doc) == {"id", "project", "title", "chunks", "created_at", "preview"}
        assert doc["project"] == PID_A
        assert doc["title"] == "doc-b"
        assert doc["chunks"] == 1
        assert doc["preview"] == "beta"  # short chunk → verbatim, no ellipsis
        assert data["cursors"] == {"note": n2, "doc": d2}

    def test_after_max_returns_empty_but_cursors_stay(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            nid = int(await store.remember(text="only note", kind="note", project=PID_A))
            did = await _insert_doc_and_chunk(
                store, project=PID_A, doc_key="doc", text="txt")
            data = await store.tail_since(
                note_after=nid, doc_after=did, projects=None, limit=30)
            await store.aclose()
            return nid, did, data

        nid, did, data = asyncio.run(_run())
        assert data["notes"] == [] and data["documents"] == []
        assert data["cursors"] == {"note": nid, "doc": did}

    def test_new_rows_arrive_as_exact_delta(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            await store.remember(text="old note", kind="note", project=PID_A)
            await _insert_doc_and_chunk(store, project=PID_A, doc_key="old", text="a")
            first = await store.tail_since(
                note_after=0, doc_after=0, projects=None, limit=30)
            cur = first["cursors"]
            new_note = int(await store.remember(
                text="new note", kind="decision", project=PID_A))
            new_doc = await _insert_doc_and_chunk(
                store, project=PID_A, doc_key="new", text="b", seed=3)
            delta = await store.tail_since(
                note_after=cur["note"], doc_after=cur["doc"], projects=None, limit=30)
            await store.aclose()
            return new_note, new_doc, delta

        new_note, new_doc, delta = asyncio.run(_run())
        assert [n["id"] for n in delta["notes"]] == [new_note]
        assert [d["id"] for d in delta["documents"]] == [new_doc]
        assert delta["cursors"] == {"note": new_note, "doc": new_doc}

    def test_limit_keeps_newest(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            ids = [int(await store.remember(text=f"note {i}", kind="note", project=PID_A))
                   for i in range(3)]
            data = await store.tail_since(
                note_after=0, doc_after=0, projects=None, limit=2)
            await store.aclose()
            return ids, data

        ids, data = asyncio.run(_run())
        assert [n["id"] for n in data["notes"]] == [ids[2], ids[1]]  # newest 2
        assert data["cursors"]["note"] == ids[2]

    def test_projects_filter(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            await store.remember(text="a-note", kind="note", project=PID_A)
            await store.remember(text="b-note", kind="note", project=PID_B)
            await _insert_doc_and_chunk(store, project=PID_A, doc_key="da", text="x")
            await _insert_doc_and_chunk(store, project=PID_B, doc_key="db", text="y", seed=4)
            data = await store.tail_since(
                note_after=0, doc_after=0, projects=[PID_B], limit=30)
            await store.aclose()
            return data

        data = asyncio.run(_run())
        assert [n["project"] for n in data["notes"]] == [PID_B]
        assert [d["project"] for d in data["documents"]] == [PID_B]

    def test_preview_truncates_long_first_chunk(self, tmp_path):
        store = make_store(tmp_path)
        long_text = "x" * 500

        async def _run():
            await _insert_doc_and_chunk(
                store, project=PID_A, doc_key="long", text=long_text)
            data = await store.tail_since(
                note_after=0, doc_after=0, projects=None, limit=30)
            await store.aclose()
            return data

        data = asyncio.run(_run())
        preview = data["documents"][0]["preview"]
        assert preview == long_text[:280] + "…"
        assert len(preview) == 281

    def test_preview_uses_first_chunk_by_index(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            from braincell.store import upsert_chunk
            from tests.conftest import fake_vec
            doc_id = await _insert_doc_and_chunk(
                store, project=PID_A, doc_key="multi", text="first page")
            cf = await store._conn_get()
            await upsert_chunk(cf, doc_id, 1, "second page", fake_vec(9))
            await cf.commit()
            data = await store.tail_since(
                note_after=0, doc_after=0, projects=None, limit=30)
            await store.aclose()
            return data

        data = asyncio.run(_run())
        doc = data["documents"][0]
        assert doc["chunks"] == 2
        assert doc["preview"] == "first page"

    def test_empty_store_cursors_zero(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            data = await store.tail_since(
                note_after=0, doc_after=0, projects=None, limit=30)
            await store.aclose()
            return data

        data = asyncio.run(_run())
        assert data == {"notes": [], "documents": [], "cursors": {"note": 0, "doc": 0}}


# ── GET /api/feed ─────────────────────────────────────────────────────────────

class TestFeedEndpoint:
    def _seed(self, tmp_path) -> None:
        store = make_store(tmp_path)

        async def _w():
            await store.remember(text="seeded note", kind="note", project=PID_A)
            await store.remember(text="other project", kind="note", project=PID_B)
            await _insert_doc_and_chunk(store, project=PID_A, doc_key="d1", text="hello")
            await store.aclose()

        asyncio.run(_w())

    def test_available_read_only_with_null_job(self, tmp_path):
        """The feed is a read view — mounted even without --allow-writes; no
        ingest manager exists there, so job is null."""
        self._seed(tmp_path)
        with TestClient(_app(tmp_path, allow_writes=False)) as client:
            r = client.get("/api/feed")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"notes", "documents", "cursors", "job"}
        assert body["job"] is None
        assert len(body["notes"]) == 2
        assert len(body["documents"]) == 1
        assert body["documents"][0]["preview"] == "hello"  # first chunk's text
        assert body["cursors"]["note"] > 0 and body["cursors"]["doc"] > 0

    def test_401_without_token(self, tmp_path):
        with TestClient(_app(tmp_path, auth_token="s3cret")) as client:
            assert client.get("/api/feed").status_code == 401
        with TestClient(_app(tmp_path, auth_token="s3cret")) as client:
            assert client.get("/api/feed", params={"t": "s3cret"}).status_code == 200

    def test_after_cursor_params(self, tmp_path):
        self._seed(tmp_path)
        with TestClient(_app(tmp_path)) as client:
            first = client.get("/api/feed").json()
            cur = first["cursors"]
            again = client.get(
                "/api/feed",
                params={"after_note": cur["note"], "after_doc": cur["doc"]},
            ).json()
        assert again["notes"] == [] and again["documents"] == []
        assert again["cursors"] == cur

    def test_projects_csv_filter(self, tmp_path):
        self._seed(tmp_path)
        with TestClient(_app(tmp_path)) as client:
            body = client.get("/api/feed", params={"projects": PID_B}).json()
        assert [n["project"] for n in body["notes"]] == [PID_B]
        assert body["documents"] == []

    def test_k_capped_at_50(self, tmp_path):
        store = make_store(tmp_path)

        async def _w():
            for i in range(55):
                await store.remember(text=f"bulk {i}", kind="note", project=PID_A)
            await store.aclose()

        asyncio.run(_w())
        with TestClient(_app(tmp_path)) as client:
            body = client.get("/api/feed", params={"k": 500}).json()
        assert len(body["notes"]) == 50

    def test_job_present_with_ingest_manager(self, tmp_path):
        """allow_writes app: a running job on the ingest manager surfaces in the
        contract shape; stubbed done/total counters pass through."""
        from braincell.gui_ingest import IngestJob
        app = _app(tmp_path, allow_writes=True)
        with TestClient(app) as client:
            mgr = app.state.ingest_manager
            mgr.job = IngestJob(path="/some/proj")
            body = client.get("/api/feed").json()
            assert body["job"] == {
                "state": "running", "path": "/some/proj", "done": 0, "total": 0,
            }
            mgr.job.done = 3    # future counters pass through the getattr seam
            mgr.job.total = 9
            body = client.get("/api/feed").json()
            assert body["job"]["done"] == 3 and body["job"]["total"] == 9
