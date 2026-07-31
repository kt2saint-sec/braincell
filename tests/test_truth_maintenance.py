# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_truth_maintenance.py — M8: recall returns CURRENT truth, not whatever embeds closest.

The failure these lock down: braincell recorded supersession but never resolved it,
so "use Redis" could outrank the decision that replaced it purely because the query
rhymed with the retired wording.

Covers:
  - a superseded note is never the primary answer;
  - a query phrased in the OLD vocabulary resolves to the replacement (+ history);
  - multi-hop chains resolve to the terminal note; a cyclic chain terminates;
  - a chain ending in a tombstone yields nothing (no stale resurrection);
  - concurrent supersedes: exactly one winner, the loser raises SupersedeConflict;
  - hard delete leaves no orphan graph edges and a clean foreign_key_check;
  - graph expansion respects the project scope filter;
  - every write path stamps a note_uid;
  - include_superseded=True reproduces the pre-M8 historical view.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from braincell.schema import MEMORY_SCHEMA_VERSION
from braincell.store import SupersedeConflict
from tests.conftest import fake_vec, make_store

PID = "01TRUTHPROJECT0000000000"


def _run(coro):
    return asyncio.run(coro)


async def _close(store):
    await store.aclose()


class TestSupersessionResolution:
    def test_superseded_note_is_never_the_primary_answer(self, tmp_path):
        async def go():
            store = make_store(tmp_path)
            old = int(await store.remember(
                "Use Redis for the shared cache", "decision", PID, embedding=fake_vec(1)
            ))
            await store.supersede(
                old, "Redis was rejected; use an in-process cache", PID,
                embedding=fake_vec(2),
            )
            # Query the OLD wording — the retired note is the closest lexical match.
            notes = await store.recall(None, PID, k=5, qtext="Redis")
            await _close(store)
            return notes

        notes = _run(go())
        assert notes, "recall returned nothing — the replacement should have surfaced"
        assert all(n.superseded_by is None for n in notes), (
            f"a superseded note came back as an answer: "
            f"{[(n.id, n.content, n.superseded_by) for n in notes]}"
        )
        assert "in-process cache" in notes[0].content

    def test_stale_wording_resolves_to_replacement_with_history(self, tmp_path):
        """The whole point: the replacement need not contain the query's words."""
        async def go():
            store = make_store(tmp_path)
            old = int(await store.remember(
                "Use Redis for the shared cache", "decision", PID, embedding=fake_vec(1)
            ))
            new = int(await store.supersede(
                old, "Cache lives in the process; no external store", PID,
                embedding=fake_vec(2),
            ))
            notes = await store.recall(None, PID, k=5, qtext="Redis")
            await _close(store)
            return old, new, notes

        old, new, notes = _run(go())
        assert len(notes) == 1
        answer = notes[0]
        assert answer.id == new
        assert answer.retrieval_origin == "resolved"
        assert answer.resolved_from == old
        assert [h["id"] for h in answer.history] == [old]
        assert answer.history[0]["status"] == "superseded"
        assert "Redis" in answer.history[0]["content"], "history must show what was replaced"

    def test_multi_hop_chain_resolves_to_terminal_note(self, tmp_path):
        async def go():
            store = make_store(tmp_path)
            n1 = int(await store.remember("cache via Redis", "decision", PID,
                                          embedding=fake_vec(1)))
            n2 = int(await store.supersede(n1, "cache via Memcached", PID,
                                           embedding=fake_vec(2)))
            n3 = int(await store.supersede(n2, "cache in-process, final", PID,
                                           embedding=fake_vec(3)))
            notes = await store.recall(None, PID, k=5, qtext="Redis")
            await _close(store)
            return n3, notes

        n3, notes = _run(go())
        assert [n.id for n in notes] == [n3]
        assert notes[0].retrieval_origin == "resolved"

    def test_cyclic_chain_terminates(self, tmp_path):
        """A hand-corrupted cycle must not hang recall (depth cap)."""
        async def go():
            store = make_store(tmp_path)
            a = int(await store.remember("alpha", "note", PID, embedding=fake_vec(1)))
            b = int(await store.supersede(a, "beta", PID, embedding=fake_vec(2)))
            mem = await store._conn_get()
            # Force a cycle the public API cannot create: b -> a while a -> b.
            await mem.execute(
                "UPDATE memory_notes SET superseded_by = ? WHERE id = ?", (a, b)
            )
            await mem.commit()
            notes = await asyncio.wait_for(
                store.recall(None, PID, k=5, qtext="alpha"), timeout=15
            )
            await _close(store)
            return notes

        _run(go())  # the assertion is that this returns at all

    def test_chain_ending_in_tombstone_returns_nothing(self, tmp_path):
        """If the replacement was retracted there is no current truth to report."""
        async def go():
            store = make_store(tmp_path)
            old = int(await store.remember("Use Redis", "decision", PID,
                                           embedding=fake_vec(1)))
            new = int(await store.supersede(old, "Use an in-process cache", PID,
                                            embedding=fake_vec(2)))
            await store.forget(new, PID)
            notes = await store.recall(None, PID, k=5, qtext="Redis")
            await _close(store)
            return notes

        assert _run(go()) == []

    def test_include_superseded_restores_historical_view(self, tmp_path):
        async def go():
            store = make_store(tmp_path)
            old = int(await store.remember("Use Redis", "decision", PID,
                                           embedding=fake_vec(1)))
            await store.supersede(old, "Use an in-process cache", PID,
                                  embedding=fake_vec(2))
            hist = await store.recall(None, PID, k=5, qtext="Redis",
                                      include_superseded=True)
            await _close(store)
            return old, hist

        old, hist = _run(go())
        assert old in [n.id for n in hist], "history view must still show the retired note"
        assert all(n.retrieval_origin == "direct" for n in hist), (
            "include_superseded must not resolve — it is the raw historical set"
        )

    def test_hybrid_path_also_resolves(self, tmp_path):
        """Resolution must work on the vector path, not only the keyword path."""
        async def go():
            store = make_store(tmp_path)
            old = int(await store.remember("Use Redis", "decision", PID,
                                           embedding=fake_vec(1)))
            new = int(await store.supersede(old, "Use an in-process cache", PID,
                                            embedding=fake_vec(2)))
            # Query vector == the OLD note's vector: it is the top vector hit.
            notes = await store.recall(fake_vec(1), PID, k=5, qtext="")
            await _close(store)
            return new, notes

        new, notes = _run(go())
        assert notes and notes[0].id == new
        assert notes[0].retrieval_origin == "resolved"


class TestSupersedeConcurrency:
    def test_second_supersede_of_same_note_conflicts(self, tmp_path):
        async def go():
            store = make_store(tmp_path)
            note = int(await store.remember("original", "decision", PID,
                                            embedding=fake_vec(1)))
            first = await store.supersede(note, "replacement A", PID,
                                          embedding=fake_vec(2))
            with pytest.raises(SupersedeConflict):
                await store.supersede(note, "replacement B", PID, embedding=fake_vec(3))
            # The loser wrote NOTHING: no orphan replacement row survives.
            mem = await store._conn_get()
            rows = await (await mem.execute(
                "SELECT content FROM memory_notes ORDER BY id"
            )).fetchall()
            await _close(store)
            return first, [r[0] for r in rows]

        first, contents = _run(go())
        assert contents == ["original", "replacement A"], (
            f"the losing supersede left a partial write: {contents}"
        )
        assert first

    def test_supersede_of_tombstoned_note_conflicts(self, tmp_path):
        async def go():
            store = make_store(tmp_path)
            note = int(await store.remember("original", "decision", PID,
                                            embedding=fake_vec(1)))
            await store.forget(note, PID)
            with pytest.raises(SupersedeConflict):
                await store.supersede(note, "replacement", PID, embedding=fake_vec(2))
            await _close(store)

        _run(go())

    def test_revision_bumps_on_supersede(self, tmp_path):
        async def go():
            store = make_store(tmp_path)
            note = int(await store.remember("original", "decision", PID,
                                            embedding=fake_vec(1)))
            await store.supersede(note, "replacement", PID, embedding=fake_vec(2))
            mem = await store._conn_get()
            row = await (await mem.execute(
                "SELECT revision FROM memory_notes WHERE id = ?", (note,)
            )).fetchone()
            await _close(store)
            return row[0]

        assert _run(go()) == 2


class TestForeignKeyIntegrity:
    def test_hard_delete_leaves_no_orphan_links(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BRAINCELL_LINK_COS", "-1.0")  # force auto-linking

        async def go():
            import importlib

            import braincell.store as store_mod
            importlib.reload(store_mod)
            db = tmp_path / "braincell.db"
            store = store_mod.SqliteStore(db)
            store.assert_schema_version()
            a = int(await store.remember("alpha note", "note", PID, embedding=fake_vec(1)))
            b = int(await store.remember("beta note", "note", PID, embedding=fake_vec(2)))
            mem = await store._conn_get()
            before = await (await mem.execute("SELECT COUNT(*) FROM bc_note_links")).fetchone()
            await store.forget(a, PID, hard=True)
            after = await (await mem.execute("SELECT COUNT(*) FROM bc_note_links")).fetchone()
            dangling = await (await mem.execute(
                "SELECT COUNT(*) FROM bc_note_links WHERE "
                "src_id NOT IN (SELECT id FROM memory_notes) OR "
                "dst_id NOT IN (SELECT id FROM memory_notes)"
            )).fetchone()
            await store.aclose()
            importlib.reload(store_mod)  # restore module-level env constants
            return b, before[0], after[0], dangling[0]

        _b, before, after, dangling = _run(go())
        assert before > 0, "fixture failed: no links were auto-created to begin with"
        assert dangling == 0, "hard delete left orphan graph edges"
        assert after < before, "the deleted note's edges were not cascaded away"

    def test_hard_delete_of_a_superseding_note_succeeds(self, tmp_path):
        """FK enforcement must not make a legitimate purge impossible."""
        async def go():
            store = make_store(tmp_path)
            old = int(await store.remember("original", "decision", PID,
                                           embedding=fake_vec(1)))
            new = int(await store.supersede(old, "replacement", PID,
                                            embedding=fake_vec(2)))
            assert await store.forget(new, PID, hard=True) is True
            mem = await store._conn_get()
            row = await (await mem.execute(
                "SELECT superseded_by FROM memory_notes WHERE id = ?", (old,)
            )).fetchone()
            await _close(store)
            return row[0]

        assert _run(go()) is None, "the dangling pointer to a purged note must be cleared"

    def test_foreign_key_check_is_clean(self, tmp_path):
        async def go():
            store = make_store(tmp_path)
            note = int(await store.remember("a", "note", PID, embedding=fake_vec(1)))
            await store.supersede(note, "b", PID, embedding=fake_vec(2))
            await _close(store)

        _run(go())
        con = sqlite3.connect(str(tmp_path / "braincell.db"))
        try:
            con.execute("PRAGMA foreign_keys=ON")
            assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            con.close()


class TestNoteUid:
    def test_every_write_path_stamps_a_uid(self, tmp_path):
        async def go():
            store = make_store(tmp_path)
            note = int(await store.remember("a", "note", PID, embedding=fake_vec(1)))
            await store.supersede(note, "b", PID, embedding=fake_vec(2))
            mem = await store._conn_get()
            nulls = await (await mem.execute(
                "SELECT COUNT(*) FROM memory_notes WHERE note_uid IS NULL"
            )).fetchone()
            uids = await (await mem.execute(
                "SELECT COUNT(DISTINCT note_uid) FROM memory_notes"
            )).fetchone()
            await _close(store)
            return nulls[0], uids[0]

        nulls, distinct = _run(go())
        assert nulls == 0, "a write path left note_uid NULL"
        assert distinct == 2, "note_uid must be unique per note"

    def test_migrated_v3_store_gets_uids(self, tmp_path):
        """A pre-v4 brain is backfilled in place, keeping its notes."""
        from braincell.store import SqliteStore

        db = tmp_path / "braincell.db"
        _run(_seed_and_close(db))
        con = sqlite3.connect(str(db))
        try:  # rewind to v3: drop the v4 columns' contents + stamp the old version
            con.execute("UPDATE memory_notes SET note_uid = NULL")
            con.execute("UPDATE schema_version SET version = 3")
            con.commit()
        finally:
            con.close()

        SqliteStore(db).assert_schema_version()

        con = sqlite3.connect(str(db))
        try:
            nulls = con.execute(
                "SELECT COUNT(*) FROM memory_notes WHERE note_uid IS NULL"
            ).fetchone()[0]
            kept = con.execute("SELECT content FROM memory_notes").fetchone()[0]
        finally:
            con.close()
        assert nulls == 0, "migration left notes without a stable id"
        assert kept == "survivor", "migration lost note content"


async def _seed_and_close(db):
    from braincell.store import SqliteStore

    store = SqliteStore(db)
    store.assert_schema_version()
    await store.remember("survivor", "note", PID, embedding=fake_vec(1))
    await store.aclose()


# The v3 shape, verbatim from the pre-M8 schema: no note_uid, no revision, and
# bc_note_links WITHOUT ON DELETE CASCADE. Written out longhand (rather than
# rewinding a v4 store) because the table REBUILD is the risky part of the upgrade
# and only a genuinely-old table exercises it.
_V3_DDL = [
    (
        "CREATE TABLE schema_version (version INTEGER NOT NULL, "
        "applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    ),
    (
        "CREATE TABLE embed_fingerprint (fingerprint TEXT NOT NULL, "
        "applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    ),
    """CREATE TABLE memory_notes (
        id              INTEGER PRIMARY KEY,
        project_id      TEXT    NOT NULL,
        scope           TEXT    NOT NULL DEFAULT 'project',
        kind            TEXT    NOT NULL CHECK (kind IN ('decision','bug_lesson','note','observation')),
        content         TEXT    NOT NULL,
        tags            TEXT,
        confidence      REAL,
        source_hint     TEXT,
        superseded_by   INTEGER REFERENCES memory_notes(id),
        created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
        embedding       BLOB,
        deleted_at      TEXT
    )""",
    "CREATE INDEX memory_notes_project_idx ON memory_notes(project_id)",
    "CREATE VIRTUAL TABLE memory_fts USING fts5(content, content='memory_notes', content_rowid='id')",
    """CREATE TABLE bc_note_links (
        src_id      INTEGER NOT NULL REFERENCES memory_notes(id),
        dst_id      INTEGER NOT NULL REFERENCES memory_notes(id),
        kind        TEXT    NOT NULL DEFAULT 'related'
                    CHECK (kind IN ('related','causes','refines')),
        weight      REAL,
        created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(src_id, dst_id, kind)
    )""",
    "CREATE INDEX bc_note_links_src_idx ON bc_note_links(src_id)",
]


class TestV3Upgrade:
    """The real upgrade path: a brain written by the PREVIOUS release."""

    def _build_v3(self, db_path):
        from braincell import embed_spec

        con = sqlite3.connect(str(db_path))
        try:
            for stmt in _V3_DDL:
                con.execute(stmt)
            con.execute("INSERT INTO schema_version(version) VALUES (3)")
            con.execute("INSERT INTO embed_fingerprint(fingerprint) VALUES (?)",
                        (embed_spec.FINGERPRINT,))
            con.executemany(
                "INSERT INTO memory_notes(id, project_id, kind, content) VALUES (?, ?, ?, ?)",
                [(1, PID, "decision", "kept one"),
                 (2, PID, "decision", "kept two"),
                 (3, PID, "note", "points at a purged note")],
            )
            # A dangling supersession: note 3 -> 99, which a pre-v4 hard delete removed.
            con.execute("UPDATE memory_notes SET superseded_by = 99 WHERE id = 3")
            con.executemany(
                "INSERT INTO bc_note_links(src_id, dst_id, kind, weight) VALUES (?, ?, ?, ?)",
                [(1, 2, "related", 0.9),    # valid edge — must survive
                 (1, 77, "related", 0.5),   # orphan edge — dst was hard-deleted
                 (88, 2, "related", 0.4)],  # orphan edge — src was hard-deleted
            )
            con.commit()
        finally:
            con.close()

    def test_v3_brain_upgrades_cleanly(self, tmp_path):
        from braincell.store import SqliteStore

        db = tmp_path / "braincell.db"
        self._build_v3(db)
        SqliteStore(db).assert_schema_version()

        con = sqlite3.connect(str(db))
        try:
            con.execute("PRAGMA foreign_keys=ON")
            version = con.execute("SELECT version FROM schema_version").fetchone()[0]
            null_uids = con.execute(
                "SELECT COUNT(*) FROM memory_notes WHERE note_uid IS NULL"
            ).fetchone()[0]
            distinct_uids = con.execute(
                "SELECT COUNT(DISTINCT note_uid) FROM memory_notes"
            ).fetchone()[0]
            contents = [r[0] for r in con.execute(
                "SELECT content FROM memory_notes ORDER BY id"
            ).fetchall()]
            dangling = con.execute(
                "SELECT superseded_by FROM memory_notes WHERE id = 3"
            ).fetchone()[0]
            links = con.execute(
                "SELECT src_id, dst_id FROM bc_note_links ORDER BY src_id"
            ).fetchall()
            link_sql = con.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'bc_note_links'"
            ).fetchone()[0]
            revisions = {r[0] for r in con.execute(
                "SELECT revision FROM memory_notes"
            ).fetchall()}
            fk_violations = con.execute("PRAGMA foreign_key_check").fetchall()
            leftovers = con.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '%_v3'"
            ).fetchall()
        finally:
            con.close()

        # Track the constant, not a literal: a v3 brain must land on whatever the
        # CURRENT version is (v5 added the merge operation log), and pinning the
        # number here just re-breaks this test on every future bump.
        assert version == MEMORY_SCHEMA_VERSION
        assert null_uids == 0, "migration left notes without a stable identity"
        assert distinct_uids == 3, "backfilled uids must be unique per note"
        assert contents == ["kept one", "kept two", "points at a purged note"], (
            "migration lost or altered note content"
        )
        assert dangling is None, "a supersession pointing at a purged note was not cleared"
        assert links == [(1, 2)], f"orphan edges survived the rebuild: {links}"
        assert "ON DELETE CASCADE" in link_sql, "the link table was not rebuilt"
        assert revisions == {1}, "existing notes should start at revision 1"
        assert fk_violations == [], f"foreign keys are violated after upgrade: {fk_violations}"
        assert leftovers == [], f"the rebuild left scaffolding behind: {leftovers}"

    def test_v3_upgrade_is_idempotent(self, tmp_path):
        """Re-opening (or a crash mid-migration) must not corrupt or duplicate."""
        from braincell.store import SqliteStore

        db = tmp_path / "braincell.db"
        self._build_v3(db)
        SqliteStore(db).assert_schema_version()
        con = sqlite3.connect(str(db))
        try:
            first = con.execute(
                "SELECT id, note_uid FROM memory_notes ORDER BY id"
            ).fetchall()
        finally:
            con.close()

        SqliteStore(db).assert_schema_version()  # second open
        con = sqlite3.connect(str(db))
        try:
            second = con.execute(
                "SELECT id, note_uid FROM memory_notes ORDER BY id"
            ).fetchall()
            links = con.execute("SELECT COUNT(*) FROM bc_note_links").fetchone()[0]
        finally:
            con.close()

        assert first == second, "a second open re-stamped identities"
        assert links == 1, "a second open duplicated graph edges"

    def test_upgraded_v3_brain_resolves_supersession(self, tmp_path):
        """End-to-end: an upgraded old brain answers with current truth."""
        from braincell.store import SqliteStore

        db = tmp_path / "braincell.db"
        self._build_v3(db)
        con = sqlite3.connect(str(db))
        try:  # note 1 was superseded by note 2 back in v3
            con.execute("UPDATE memory_notes SET superseded_by = 2 WHERE id = 1")
            con.execute(
                "INSERT INTO memory_fts(rowid, content) "
                "SELECT id, content FROM memory_notes"
            )
            con.commit()
        finally:
            con.close()

        async def go():
            store = SqliteStore(db)
            store.assert_schema_version()
            notes = await store.recall(None, PID, k=5, qtext="kept")
            await store.aclose()
            return notes

        notes = _run(go())
        assert 1 not in [n.id for n in notes], (
            "an upgraded brain still answers with a note superseded before the upgrade"
        )
        assert 2 in [n.id for n in notes]


class TestGraphExpansionScope:
    def test_expansion_respects_project_scope(self, tmp_path, monkeypatch):
        """A cross-project edge must not leak past a scope='self' recall."""
        monkeypatch.setenv("BRAINCELL_LINK_EXPAND", "5")

        async def go():
            import importlib

            import braincell.store as store_mod
            importlib.reload(store_mod)
            db = tmp_path / "braincell.db"
            store = store_mod.SqliteStore(db)
            store.assert_schema_version()
            mine = int(await store.remember("my project note", "note", PID,
                                            embedding=fake_vec(1)))
            theirs = int(await store.remember("other project secret", "note",
                                              "01OTHERPROJECT000000000", embedding=fake_vec(2)))
            mem = await store._conn_get()
            await mem.execute(
                "INSERT INTO bc_note_links (src_id, dst_id, kind, weight) "
                "VALUES (?, ?, 'related', 0.99)", (mine, theirs),
            )
            await mem.commit()
            notes = await store.recall(None, PID, k=5, qtext="")
            await store.aclose()
            importlib.reload(store_mod)
            return theirs, [n.id for n in notes]

        theirs, ids = _run(go())
        assert theirs not in ids, (
            "graph expansion leaked another project's note into a scope='self' recall"
        )

    def test_expansion_exposes_provenance(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BRAINCELL_LINK_EXPAND", "5")

        async def go():
            import importlib

            import braincell.store as store_mod
            importlib.reload(store_mod)
            db = tmp_path / "braincell.db"
            store = store_mod.SqliteStore(db)
            store.assert_schema_version()
            a = int(await store.remember("anchor note", "note", PID, embedding=fake_vec(1)))
            b = int(await store.remember("linked note", "note", PID, embedding=fake_vec(2)))
            mem = await store._conn_get()
            await mem.execute(
                "INSERT INTO bc_note_links (src_id, dst_id, kind, weight) "
                "VALUES (?, ?, 'refines', 0.84)", (a, b),
            )
            await mem.commit()
            notes = await store.recall(None, PID, k=1, qtext="")
            await store.aclose()
            importlib.reload(store_mod)
            return a, b, notes

        a, b, notes = _run(go())
        linked = [n for n in notes if n.id == b]
        assert linked, "the linked note was not appended by expansion"
        assert linked[0].retrieval_origin == "linked"
        assert linked[0].linked_from == a
        assert linked[0].relation == "refines"
        assert linked[0].relation_weight == pytest.approx(0.84)
