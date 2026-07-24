# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_note_links.py — B3 graph note-links (auto-link on write, expand on read).

Covers:
  - auto-link inserts bidirectional `related` links between similar notes;
  - INSERT OR IGNORE makes re-linking idempotent (no dup rows);
  - NULL-embedding notes are never auto-linked;
  - expand-on-read is OFF by default (recall byte-identical);
  - with BRAINCELL_LINK_EXPAND>0 a linked note is appended, flagged expansion,
    without displacing a direct hit.

All offline: fake unit vectors, no Ollama.
"""

from __future__ import annotations

import asyncio


from braincell import store as store_mod
from tests.conftest import fake_vec, make_store


def _run(coro):
    return asyncio.run(coro)


# ── auto-link on write ────────────────────────────────────────────────────────

class TestAutoLink:
    def test_similar_notes_get_bidirectional_related_link(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store_mod, "_LINK_COS", 0.5)
        s = make_store(tmp_path)
        v = fake_vec(1)

        async def go():
            a = int(await s.remember("alpha note", "note", "P", embedding=v))
            b = int(await s.remember("beta note", "note", "P", embedding=v))  # identical vec
            mem = await s._conn_get()
            rows = await (await mem.execute(
                "SELECT src_id, dst_id, kind FROM bc_note_links"
            )).fetchall()
            await s.aclose()
            return a, b, rows

        a, b, rows = _run(go())
        pairs = {(r[0], r[1]) for r in rows}
        assert (b, a) in pairs and (a, b) in pairs, f"expected bidirectional link; got {pairs}"
        assert all(r[2] == "related" for r in rows)

    def test_dissimilar_notes_not_linked(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store_mod, "_LINK_COS", 0.99)  # very high → no link
        s = make_store(tmp_path)

        async def go():
            await s.remember("alpha", "note", "P", embedding=fake_vec(1))
            await s.remember("beta", "note", "P", embedding=fake_vec(2))
            mem = await s._conn_get()
            n = (await (await mem.execute("SELECT COUNT(*) FROM bc_note_links")).fetchone())[0]
            await s.aclose()
            return n

        assert _run(go()) == 0

    def test_null_embedding_notes_not_linked(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store_mod, "_LINK_COS", 0.0)  # would link anything WITH a vec
        s = make_store(tmp_path)

        async def go():
            await s.remember("alpha", "note", "P")  # no embedding
            await s.remember("beta", "note", "P")   # no embedding
            mem = await s._conn_get()
            n = (await (await mem.execute("SELECT COUNT(*) FROM bc_note_links")).fetchone())[0]
            await s.aclose()
            return n

        assert _run(go()) == 0

    def test_autolink_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store_mod, "_LINK_COS", 0.5)
        s = make_store(tmp_path)
        v = fake_vec(1)

        async def go():
            a = int(await s.remember("alpha", "note", "P", embedding=v))
            b = int(await s.remember("beta", "note", "P", embedding=v))
            mem = await s._conn_get()
            before = (await (await mem.execute("SELECT COUNT(*) FROM bc_note_links")).fetchone())[0]
            # Re-run autolink for b explicitly — must not create duplicates.
            await s._autolink_note(mem, b, "P", v)
            await mem.commit()
            after = (await (await mem.execute("SELECT COUNT(*) FROM bc_note_links")).fetchone())[0]
            await s.aclose()
            return a, before, after

        _a, before, after = _run(go())
        assert before == after, "re-linking must be idempotent (INSERT OR IGNORE)"


# ── expand on read ────────────────────────────────────────────────────────────

class TestExpandOnRead:
    def _seed_with_manual_link(self, tmp_path, monkeypatch):
        """Two dissimilar notes A,B + a manual A→B link; return (store, A, B, vA)."""
        monkeypatch.setattr(store_mod, "_LINK_COS", 0.99)  # no auto-link interference
        s = make_store(tmp_path)
        vA, vB = fake_vec(1), fake_vec(2)  # near-orthogonal

        async def go():
            a = int(await s.remember("alpha topic", "note", "P", embedding=vA))
            b = int(await s.remember("unrelated beta", "note", "P", embedding=vB))
            mem = await s._conn_get()
            await mem.execute(
                "INSERT INTO bc_note_links (src_id, dst_id, kind, weight) "
                "VALUES (?, ?, 'related', 0.9)",
                (a, b),
            )
            await mem.commit()
            return a, b

        a, b = _run(go())
        return s, a, b, vA

    def test_expansion_off_by_default(self, tmp_path, monkeypatch):
        s, a, b, vA = self._seed_with_manual_link(tmp_path, monkeypatch)
        # _LINK_EXPAND defaults to 0
        assert store_mod._LINK_EXPAND == 0

        async def go():
            notes = await s.recall(vA, "P", k=1)
            await s.aclose()
            return notes

        notes = _run(go())
        ids = [n.id for n in notes]
        assert ids == [a], f"only the direct hit A should be present; got {ids}"
        assert all(not n.expansion for n in notes)

    def test_expansion_on_appends_linked_note(self, tmp_path, monkeypatch):
        s, a, b, vA = self._seed_with_manual_link(tmp_path, monkeypatch)
        monkeypatch.setattr(store_mod, "_LINK_EXPAND", 2)

        async def go():
            notes = await s.recall(vA, "P", k=1)
            await s.aclose()
            return notes

        notes = _run(go())
        ids = [n.id for n in notes]
        assert ids[0] == a, "direct hit A must stay first (never displaced)"
        assert b in ids, "linked note B must be appended via expansion"
        b_note = next(n for n in notes if n.id == b)
        assert b_note.expansion is True
        a_note = next(n for n in notes if n.id == a)
        assert a_note.expansion is False
