# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
test_status_lifecycle.py — v6 `status` column: liveness authority + migration.

v6 makes `status` ('active' | 'superseded' | 'tombstoned') the single liveness
authority; `deleted_at` / `superseded_by` are demoted to provenance. These tests
pin: the v5→v6 backfill derivation, every write path stamping status, the
purged-replacement resurrection, and the pool convergence of a status-ONLY change
(no revision bump — the case the upsert tuple would miss without status in it).
"""

import asyncio
import sqlite3
from pathlib import Path

from tests.conftest import fake_vec, make_store

# v5-shaped memory_notes: the v6 column list minus `status` (see schema.py — v6
# appends status last, so this is byte-what-a-v5-store-holds).
_V5_MEMORY_NOTES_DDL = """
CREATE TABLE memory_notes (
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
    deleted_at      TEXT,
    note_uid        TEXT,
    revision        INTEGER NOT NULL DEFAULT 1,
    pooled_from     TEXT
);
"""


def _make_v5_db(db_path: Path) -> None:
    """Hand-craft a v5 store holding one note in each pre-v6 liveness state."""
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE schema_version (version INTEGER NOT NULL, "
                "applied_at TEXT NOT NULL DEFAULT (datetime('now')))")
    con.execute("INSERT INTO schema_version(version) VALUES (5)")
    con.execute(_V5_MEMORY_NOTES_DDL)
    rows = [
        # (id, content, superseded_by, deleted_at, note_uid)
        (1, "active note", None, None, "uid-active"),
        (2, "superseded note", 1, None, "uid-superseded"),
        (3, "tombstoned note", None, "2026-01-01 00:00:00", "uid-tombstoned"),
        # Reflect-source shape: superseded AND tombstoned → must backfill as
        # tombstoned (tombstone dominates), never as merely 'superseded'.
        (4, "superseded and tombstoned", 1, "2026-01-01 00:00:00", "uid-both"),
    ]
    for nid, content, sup, deleted, uid in rows:
        con.execute(
            "INSERT INTO memory_notes (id, project_id, kind, content, superseded_by, "
            "deleted_at, note_uid) VALUES (?, 'P1', 'note', ?, ?, ?, ?)",
            (nid, content, sup, deleted, uid),
        )
    con.commit()
    con.close()


class TestV6Migration:
    def test_backfill_derives_status_from_provenance(self, tmp_path):
        from braincell.store import SqliteStore

        db = tmp_path / "braincell.db"
        _make_v5_db(db)
        store = SqliteStore(db)
        store.assert_schema_version()  # runs the v5→v6 ladder step
        store.close()

        con = sqlite3.connect(str(db))
        got = dict(con.execute("SELECT id, status FROM memory_notes").fetchall())
        version = con.execute("SELECT version FROM schema_version").fetchone()[0]
        con.close()
        assert got == {1: "active", 2: "superseded", 3: "tombstoned", 4: "tombstoned"}
        assert version >= 6

    def test_migration_is_reopen_safe(self, tmp_path):
        """A second open of a just-migrated store must not re-alter or re-stamp."""
        from braincell.store import SqliteStore

        db = tmp_path / "braincell.db"
        _make_v5_db(db)
        for _ in range(2):
            store = SqliteStore(db)
            store.assert_schema_version()
            store.close()
        con = sqlite3.connect(str(db))
        cols = [r[1] for r in con.execute("PRAGMA table_info(memory_notes)").fetchall()]
        con.close()
        assert cols.count("status") == 1


class TestStatusWritePaths:
    def _status_of(self, store, note_id: int) -> str:
        async def _q():
            mem = await store._conn_get()
            row = await (await mem.execute(
                "SELECT status FROM memory_notes WHERE id = ?", (note_id,),
            )).fetchone()
            return row[0]
        return asyncio.run(_q())

    def test_remember_supersede_forget_stamp_status(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            nid = int(await store.remember("first truth", "note", "P1"))
            new_id = await store.supersede(nid, "second truth", "P1")
            third = int(await store.remember("doomed", "note", "P1"))
            await store.forget(third, "P1", hard=False)
            mem = await store._conn_get()
            rows = dict(await (await mem.execute(
                "SELECT id, status FROM memory_notes")).fetchall())
            return nid, new_id, third, rows

        nid, new_id, third, rows = asyncio.run(_run())
        assert rows[nid] == "superseded"
        assert rows[new_id] == "active"
        assert rows[third] == "tombstoned"

    def test_tombstoning_a_superseded_note_ends_tombstoned(self, tmp_path):
        """The reflect flow: supersede then forget — tombstone must dominate."""
        store = make_store(tmp_path)

        async def _run():
            nid = int(await store.remember("source", "note", "P1"))
            await store.supersede(nid, "synthesis", "P1")
            await store.forget(nid, "P1", hard=False)
            mem = await store._conn_get()
            row = await (await mem.execute(
                "SELECT status, superseded_by FROM memory_notes WHERE id = ?",
                (nid,),
            )).fetchone()
            return row

        status, superseded_by = asyncio.run(_run())
        assert status == "tombstoned"
        assert superseded_by is not None  # provenance survives the tombstone

    def test_purged_replacement_resurrects_survivor(self, tmp_path):
        """Hard-deleting the replacement returns the survivor to current truth
        (pre-v6 behaviour, now carried by an explicit status flip)."""
        store = make_store(tmp_path)

        async def _run():
            nid = int(await store.remember("original decision", "decision", "P1"))
            new_id = await store.supersede(nid, "replacement decision", "P1")
            await store.forget(new_id, "P1", hard=True)
            mem = await store._conn_get()
            row = await (await mem.execute(
                "SELECT status, superseded_by FROM memory_notes WHERE id = ?",
                (nid,),
            )).fetchone()
            notes = await store.recall(None, "P1", k=5, qtext=None)
            return nid, row, [n.id for n in notes]

        nid, (status, superseded_by), recalled = asyncio.run(_run())
        assert status == "active"
        assert superseded_by is None
        assert nid in recalled  # the survivor is current truth again

    def test_recall_notes_carry_status(self, tmp_path):
        store = make_store(tmp_path)

        async def _run():
            await store.remember("a live fact", "note", "P1")
            notes = await store.recall(None, "P1", k=5, qtext=None)
            return notes

        notes = asyncio.run(_run())
        assert notes and all(n.status == "active" for n in notes)


class TestPoolStatusConvergence:
    def test_status_only_change_converges_on_repool(self, tmp_path):
        """Source: A superseded by B → pool → purge B at source (A flips back to
        'active' with NO revision bump) → re-pool. The global copy of A must be
        active again — this is exactly the case that needs status in the upsert
        tuple, since nothing else about the row changed."""
        import braincell.pool as pool_mod
        from braincell.store import SqliteStore

        src_dir = tmp_path / "src"
        glob_dir = tmp_path / "glob"
        src_dir.mkdir()
        glob_dir.mkdir()
        src = make_store(src_dir)

        async def _seed():
            nid = int(await src.remember("keep Redis", "decision", "P1",
                                         embedding=fake_vec(1)))
            new_id = await src.supersede(nid, "drop Redis", "P1",
                                         embedding=fake_vec(2))
            return nid, new_id

        _nid, new_id = asyncio.run(_seed())
        src.close()

        global_db = glob_dir / "braincell.db"
        SqliteStore(global_db).assert_schema_version()
        src_db = src_dir / "braincell.db"
        stats1 = pool_mod.pool_into_global([("P1", src_db)], global_db)
        assert stats1[0].notes_copied == 2

        # Purge the replacement at source: A's pointer clears, status → active,
        # revision unchanged (the flip rides on no other column).
        src2 = make_store(src_dir)
        asyncio.run(src2.forget(new_id, "P1", hard=True))
        src2.close()

        pool_mod.pool_into_global([("P1", src_db)], global_db)
        con = sqlite3.connect(str(global_db))
        row = con.execute(
            "SELECT status, superseded_by FROM memory_notes WHERE note_uid = "
            "(SELECT note_uid FROM memory_notes WHERE content = 'keep Redis')"
        ).fetchone()
        con.close()
        assert row == ("active", None)
