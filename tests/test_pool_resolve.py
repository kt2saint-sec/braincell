# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
test_pool_resolve.py — Unit tests for pool.resolve_pool_sources (pure resolver)
and store.SqliteStore.project_counts.

resolve_pool_sources is a pure function (no argparse, no printing, no SystemExit).
Tests verify it correctly resolves sources from --all / --family / paths, skips
unbuilt projects, skips unregistered paths/members, raises KeyError for unknown
families, and never returns the global DB as a source.

project_counts is tested against a two-project fixture whose counts match
hand-run COUNTs.
"""

from __future__ import annotations

import asyncio

import pytest

from braincell.config import get_db_path, get_global_db_path
from braincell.pool import resolve_pool_sources
from braincell.project_registry import add_family_members, register_path
from braincell.store import SqliteStore
from tests.conftest import _insert_doc_and_chunk, fake_vec

# ── shared helpers ────────────────────────────────────────────────────────────


def _init_global() -> None:
    g = SqliteStore(get_global_db_path())
    g.assert_schema_version()
    g.close()


async def _build_source(pid: str, *, doc_key: str, text: str, note: str, seed: int) -> None:
    """Build a minimal per-project brain (doc + chunk + note)."""
    src = SqliteStore(get_db_path(pid))
    src.assert_schema_version()
    await _insert_doc_and_chunk(src, project=pid, doc_key=doc_key, text=text, seed=seed)
    await src.remember(note, "note", pid, embedding=fake_vec(seed + 100))
    await src.aclose()


# ── resolve_pool_sources tests ────────────────────────────────────────────────


class TestResolvePoolSourcesAll:
    def test_include_all_returns_built_sources(self, tmp_path):
        """include_all=True returns one entry per registered project with a brain."""
        root_a = tmp_path / "repoA"
        root_b = tmp_path / "repoB"
        root_a.mkdir()
        root_b.mkdir()
        register_path(str(root_a), "PIDALL_A")
        register_path(str(root_b), "PIDALL_B")

        asyncio.run(_build_source("PIDALL_A", doc_key="a", text="alpha", note="na", seed=1))
        asyncio.run(_build_source("PIDALL_B", doc_key="b", text="beta", note="nb", seed=2))

        sources, skipped = resolve_pool_sources(include_all=True)

        pids = {pid for pid, _ in sources}
        assert "PIDALL_A" in pids
        assert "PIDALL_B" in pids
        # All paths in sources must exist on disk.
        for _pid, db_path in sources:
            assert db_path.exists()
        # No skip messages expected for the built projects.
        assert skipped == []

    def test_include_all_skips_unbuilt_project(self, tmp_path):
        """include_all=True skips a registered project whose brain has not been built."""
        root_x = tmp_path / "repoX"
        root_x.mkdir()
        register_path(str(root_x), "PIDNO_BRAIN")

        sources, skipped = resolve_pool_sources(include_all=True)

        pids = {pid for pid, _ in sources}
        assert "PIDNO_BRAIN" not in pids
        assert any("skip (no brain built)" in s and "PIDNO_BRAIN" in s for s in skipped)

    def test_global_db_never_in_sources(self, tmp_path):
        """The global DB itself is never returned as a source, even if registered."""
        _init_global()
        # Register the global DB path as though it were a project path.
        global_db = get_global_db_path()
        register_path(str(global_db.parent), "GLOBAL_FAKE")
        # Even if its brain exists (the global brain we just created), it is excluded.

        sources, _skipped = resolve_pool_sources(include_all=True)

        for _pid, db_path in sources:
            assert db_path.resolve() != global_db.resolve()


class TestResolvePoolSourcesFamily:
    def test_family_returns_built_members(self, tmp_path):
        """resolve with family= returns built registered members."""
        root_a = tmp_path / "fam_repoA"
        root_b = tmp_path / "fam_repoB"
        root_a.mkdir()
        root_b.mkdir()
        register_path(str(root_a), "PIDFAM_A")
        register_path(str(root_b), "PIDFAM_B")
        add_family_members("TestFamBuilt", [str(root_a), str(root_b)])

        asyncio.run(_build_source("PIDFAM_A", doc_key="fa", text="fam alpha", note="na", seed=10))
        asyncio.run(_build_source("PIDFAM_B", doc_key="fb", text="fam beta", note="nb", seed=11))

        sources, skipped = resolve_pool_sources(family="TestFamBuilt")

        pids = {pid for pid, _ in sources}
        assert pids == {"PIDFAM_A", "PIDFAM_B"}
        assert skipped == []

    def test_family_unregistered_member_goes_to_skipped(self, tmp_path):
        """An unregistered family member appears in skipped, not in sources."""
        root_reg = tmp_path / "reg_repo"
        root_unreg = tmp_path / "unreg_repo"
        root_reg.mkdir()
        root_unreg.mkdir()
        register_path(str(root_reg), "PIDFAM_REG")
        # root_unreg is NOT registered
        add_family_members("TestFamUnreg", [str(root_reg), str(root_unreg)])

        asyncio.run(_build_source("PIDFAM_REG", doc_key="r", text="reg text", note="rn", seed=20))

        sources, skipped = resolve_pool_sources(family="TestFamUnreg")

        pids = {pid for pid, _ in sources}
        assert "PIDFAM_REG" in pids
        assert any("skip (unregistered member)" in s for s in skipped)

    def test_unknown_family_raises_key_error(self):
        """A non-existent family name raises KeyError."""
        with pytest.raises(KeyError):
            resolve_pool_sources(family="NoSuchFamilyXYZ")


class TestResolvePoolSourcesPaths:
    def test_explicit_path_resolves_registered_project(self, tmp_path):
        """An explicit path that is registered and built appears in sources."""
        root = tmp_path / "path_repo"
        root.mkdir()
        register_path(str(root), "PIDPATH_OK")
        asyncio.run(_build_source("PIDPATH_OK", doc_key="p", text="path text", note="pn", seed=30))

        sources, skipped = resolve_pool_sources(paths=[str(root)])

        pids = {pid for pid, _ in sources}
        assert "PIDPATH_OK" in pids
        assert skipped == []

    def test_unregistered_path_goes_to_skipped(self, tmp_path):
        """An explicit path with no registry entry appears in skipped."""
        root = tmp_path / "unknown_repo"
        root.mkdir()

        sources, skipped = resolve_pool_sources(paths=[str(root)])

        assert sources == []
        assert any("skip (unregistered path)" in s for s in skipped)

    def test_no_brain_path_goes_to_skipped(self, tmp_path):
        """A registered but unbuilt explicit path appears in skipped."""
        root = tmp_path / "no_brain_repo"
        root.mkdir()
        register_path(str(root), "PIDPATH_NOBRAIN")

        sources, skipped = resolve_pool_sources(paths=[str(root)])

        pids = {pid for pid, _ in sources}
        assert "PIDPATH_NOBRAIN" not in pids
        assert any("skip (no brain built)" in s for s in skipped)


# ── project_counts tests ──────────────────────────────────────────────────────


class TestProjectCounts:
    def test_two_project_counts(self):
        """project_counts returns correct doc/chunk/note counts per project."""
        async def go():
            g = SqliteStore(get_global_db_path())
            g.assert_schema_version()

            await _insert_doc_and_chunk(g, project="PCNT_A", doc_key="a1", text="alpha one", seed=1)
            await _insert_doc_and_chunk(g, project="PCNT_A", doc_key="a2", text="alpha two", seed=2)
            await g.remember("note-a1", "note", "PCNT_A", embedding=fake_vec(50))
            await g.remember("note-a2", "note", "PCNT_A", embedding=fake_vec(51))
            await g.remember("note-a3", "note", "PCNT_A", embedding=fake_vec(52))

            await _insert_doc_and_chunk(g, project="PCNT_B", doc_key="b1", text="beta one", seed=3)
            await g.remember("note-b1", "note", "PCNT_B", embedding=fake_vec(60))

            counts = await g.project_counts()
            await g.aclose()
            return counts

        counts = asyncio.run(go())

        assert "PCNT_A" in counts
        assert counts["PCNT_A"]["docs"] == 2
        assert counts["PCNT_A"]["chunks"] == 2
        assert counts["PCNT_A"]["notes"] == 3

        assert "PCNT_B" in counts
        assert counts["PCNT_B"]["docs"] == 1
        assert counts["PCNT_B"]["chunks"] == 1
        assert counts["PCNT_B"]["notes"] == 1

    def test_deleted_notes_excluded(self):
        """Soft-deleted notes are NOT counted in notes (deleted_at IS NULL filter)."""
        async def go():
            g = SqliteStore(get_global_db_path())
            g.assert_schema_version()

            note_id = await g.remember("live note", "note", "PCNT_DEL", embedding=fake_vec(70))
            await g.remember("dead note", "note", "PCNT_DEL", embedding=fake_vec(71))
            # Soft-delete the second note.
            await g.forget(int(note_id) + 1, "PCNT_DEL", hard=False)

            counts = await g.project_counts()
            await g.aclose()
            return counts

        counts = asyncio.run(go())

        assert "PCNT_DEL" in counts
        assert counts["PCNT_DEL"]["notes"] == 1

    def test_empty_db_returns_empty_dict(self):
        """An empty store returns an empty dict (no projects)."""
        async def go():
            g = SqliteStore(get_global_db_path())
            g.assert_schema_version()
            counts = await g.project_counts()
            await g.aclose()
            return counts

        counts = asyncio.run(go())
        assert counts == {}
