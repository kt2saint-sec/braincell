# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_global.py — G1 + G2 + G4 + G5: global-store foundation, scope/pin tests,
global build, and explicit multi-project filter.

Covers:
  - get_global_db_path() returns the expected XDG path.
  - open_store() global env-fallback: resolves the global DB when the file
    exists; fails closed (sys.exit 1) when it does not.
  - backup helper: backs up a store to an explicit out path; the backup opens
    with the correct schema_version and the same document row count.
  G2:
  - _resolve_scope: PROJECT mode still raises on 'family'/'all' (regression).
  - _resolve_scope: GLOBAL mode returns None for 'all', list for 'family'.
  - store.recall: accepts a ULID list, filters correctly.
  - _pin_read_project (M9): pins global-mode reads to BRAINCELL_PROJECT_ID.
  G4:
  - _run_build(mode='global') ingests into the global DB, not per-project.
  - Path attribution (path-registry) is written regardless of mode.
  - Project-mode build is byte-for-byte unchanged (regression guard).
  G5:
  - _resolve_filter: projects list takes precedence over project and scope.
  - In project mode, a multi-project or non-self projects list raises.
  - Validation rejects empty-string entries and lists exceeding 200.
  - Integration: store.recall with a projects list pools the correct rows.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path

import pytest

from braincell.config import get_global_db_path
from braincell.server import _pin_read_project, _resolve_filter, _resolve_scope
from braincell.store import SqliteStore, open_store, upsert_document
from tests.conftest import make_store


# ── get_global_db_path ────────────────────────────────────────────────────────

class TestGetGlobalDbPath:
    def test_returns_expected_path(self, monkeypatch, tmp_path):
        """Path resolves under <xdg>/<namespace>/global/braincell.db."""
        xdg = tmp_path / "xdg"
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
        monkeypatch.setenv("BRAINCELL_DATA_NAMESPACE", "braincell_test")

        # Must re-evaluate after env is patched.
        import importlib
        import braincell.config as cfg
        importlib.reload(cfg)

        path = cfg.get_global_db_path()
        assert path == xdg / "braincell_test" / "global" / "braincell.db"
        assert "projects" not in str(path), "global path must not be under projects/"

    def test_fixture_isolation(self):
        """isolate_xdg (autouse) means get_global_db_path() hits the test namespace."""
        # After autouse fixture, XDG_DATA_HOME → tmp_path/xdg, namespace → braincell_test
        p = get_global_db_path()
        assert "braincell_test" in str(p)
        assert p.name == "braincell.db"
        assert p.parent.name == "global"


# ── open_store global env-fallback ───────────────────────────────────────────

class TestOpenStoreGlobal:
    def _build_global_store(self) -> Path:
        """Create and initialise the global brain file, return its path."""
        path = get_global_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        store = SqliteStore(path)
        store.assert_schema_version()
        store.close()
        return path

    def test_global_mode_opens_existing_brain(self, monkeypatch):
        """BRAINCELL_MODE=global + existing file → open_store returns a SqliteStore."""
        self._build_global_store()
        monkeypatch.setenv("BRAINCELL_MODE", "global")
        monkeypatch.delenv("BRAINCELL_STORE", raising=False)
        monkeypatch.delenv("BRAINCELL_PROJECT_ID", raising=False)

        store = open_store()
        assert isinstance(store, SqliteStore)
        store.close()

    def test_global_mode_fails_closed_when_absent(self, monkeypatch):
        """BRAINCELL_MODE=global + no brain file → sys.exit(1), no file fabricated."""
        monkeypatch.setenv("BRAINCELL_MODE", "global")
        monkeypatch.delenv("BRAINCELL_STORE", raising=False)
        monkeypatch.delenv("BRAINCELL_PROJECT_ID", raising=False)

        # Do NOT build the global store first.
        with pytest.raises(SystemExit) as exc_info:
            open_store()

        assert exc_info.value.code == 1
        assert not get_global_db_path().exists(), (
            "open_store must not fabricate the global brain"
        )

    def test_project_mode_unchanged_missing_store_exits(self, monkeypatch):
        """Project-mode env-fallback still exits when BRAINCELL_STORE is unset."""
        monkeypatch.setenv("BRAINCELL_MODE", "project")
        monkeypatch.delenv("BRAINCELL_STORE", raising=False)
        monkeypatch.delenv("BRAINCELL_PROJECT_ID", raising=False)

        with pytest.raises(SystemExit) as exc_info:
            open_store()

        assert exc_info.value.code == 1


# ── backup helper ─────────────────────────────────────────────────────────────

class TestBackup:
    def _seed_store(self, db_path: Path, n_docs: int = 3) -> None:
        """Bootstrap the schema and insert n_docs rows into bc_documents."""
        import asyncio
        import hashlib
        from braincell.store import upsert_document

        store = SqliteStore(db_path)
        store.assert_schema_version()

        async def _insert() -> None:
            cf = await store._conn_get()
            for i in range(n_docs):
                key = f"doc-{i:03d}"
                await upsert_document(
                    cf,
                    project_id="global",
                    doc_key=key,
                    title=f"Doc {i}",
                    content_hash=hashlib.sha256(key.encode()).digest(),
                )
            await cf.commit()

        asyncio.run(_insert())
        store.close()

    def test_backup_creates_file_with_correct_schema_and_rows(self, tmp_path):
        """backup writes a VACUUM INTO copy; schema_version and doc count match."""
        from braincell.schema import MEMORY_SCHEMA_VERSION

        src = tmp_path / "src" / "braincell.db"
        src.parent.mkdir(parents=True)
        self._seed_store(src, n_docs=4)

        dest = tmp_path / "backups" / "test-backup.db"

        # Call the underlying backup logic directly (avoid clock in filename).
        import sqlite3 as sl
        dest.parent.mkdir(parents=True)
        con = sl.connect(str(src))
        try:
            con.execute("VACUUM INTO ?", (str(dest),))
        finally:
            con.close()

        assert dest.exists(), "backup file must be created"

        # Verify schema_version and row count in the backup.
        bcon = sl.connect(str(dest))
        try:
            ver = bcon.execute("SELECT version FROM schema_version").fetchone()[0]
            count = bcon.execute(
                "SELECT COUNT(*) FROM bc_documents WHERE project_id = 'global'"
            ).fetchone()[0]
        finally:
            bcon.close()

        assert ver == MEMORY_SCHEMA_VERSION, (
            f"backup schema_version {ver} != expected {MEMORY_SCHEMA_VERSION}"
        )
        assert count == 4, f"expected 4 docs in backup, got {count}"

    def test_cmd_backup_global_mode(self, tmp_path, monkeypatch):
        """cmd_backup --mode global backs up the global brain to --out path."""
        import argparse

        # Build the global brain in the isolated XDG dir.
        global_path = get_global_db_path()
        self._seed_store(global_path, n_docs=2)

        out = tmp_path / "my-backup.db"

        args = argparse.Namespace(
            mode="global",
            path=".",
            out=str(out),
            verbose=False,
        )

        from braincell.cli import cmd_backup
        cmd_backup(args)  # must not raise

        assert out.exists()
        con = sqlite3.connect(str(out))
        try:
            count = con.execute(
                "SELECT COUNT(*) FROM bc_documents WHERE project_id = 'global'"
            ).fetchone()[0]
        finally:
            con.close()
        assert count == 2

    def test_cmd_backup_fails_on_missing_source(self, tmp_path, monkeypatch):
        """cmd_backup exits 1 when the source DB does not exist."""
        import argparse

        out = tmp_path / "should-not-appear.db"
        args = argparse.Namespace(
            mode="global",
            path=".",
            out=str(out),
            verbose=False,
        )

        from braincell.cli import cmd_backup
        with pytest.raises(SystemExit) as exc_info:
            cmd_backup(args)

        assert exc_info.value.code == 1
        assert not out.exists()


# ── G2: _resolve_scope mode-gating ───────────────────────────────────────────

class TestResolveScopeG2:
    """G2: _resolve_scope — mode-gated family/all scopes."""

    def test_project_mode_family_still_raises(self, monkeypatch):
        """PROJECT mode: scope='family' still raises ValueError (regression)."""
        monkeypatch.setenv("BRAINCELL_MODE", "project")
        monkeypatch.delenv("BRAINCELL_PROJECT_ID", raising=False)
        with pytest.raises(ValueError, match="requires global mode"):
            _resolve_scope(None, "family")

    def test_project_mode_all_still_raises(self, monkeypatch):
        """PROJECT mode: scope='all' still raises ValueError (regression)."""
        monkeypatch.setenv("BRAINCELL_MODE", "project")
        with pytest.raises(ValueError, match="requires global mode"):
            _resolve_scope(None, "all")

    def test_global_mode_all_returns_none(self, monkeypatch):
        """GLOBAL mode: scope='all' → None (no project filter)."""
        monkeypatch.setenv("BRAINCELL_MODE", "global")
        result = _resolve_scope(None, "all")
        assert result is None

    def test_global_mode_family_with_project_id_returns_list(self, monkeypatch):
        """GLOBAL mode + BRAINCELL_PROJECT_ID → sorted list including the seed."""
        monkeypatch.setenv("BRAINCELL_MODE", "global")
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", "01PROJA000000000000000001A")
        result = _resolve_scope(None, "family")
        # No families.json in the isolated XDG dir → resolve_family_ulids
        # returns just {seed}; the server sorts it.
        assert isinstance(result, list)
        assert "01PROJA000000000000000001A" in result

    def test_global_mode_family_no_project_id_raises(self, monkeypatch):
        """GLOBAL mode + no BRAINCELL_PROJECT_ID → ValueError (family needs a seed)."""
        monkeypatch.setenv("BRAINCELL_MODE", "global")
        monkeypatch.delenv("BRAINCELL_PROJECT_ID", raising=False)
        with pytest.raises(ValueError, match="BRAINCELL_PROJECT_ID"):
            _resolve_scope(None, "family")

    def test_explicit_project_overrides_scope_in_global_mode(self, monkeypatch):
        """Explicit project always wins, even in global mode with scope='all'."""
        monkeypatch.setenv("BRAINCELL_MODE", "global")
        result = _resolve_scope("01EXPLICIT00000000000000AA", "all")
        assert result == "01EXPLICIT00000000000000AA"

    def test_self_scope_unchanged_in_global_mode(self, monkeypatch):
        """scope='self' in global mode still returns BRAINCELL_PROJECT_ID."""
        monkeypatch.setenv("BRAINCELL_MODE", "global")
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", "01PROJA000000000000000001A")
        assert _resolve_scope(None, "self") == "01PROJA000000000000000001A"


# ── G2: store.recall with ULID list ──────────────────────────────────────────

_PROJ_A = "01PROJA000000000000000001A"
_PROJ_B = "01PROJB000000000000000001B"


def _build_two_project_store(tmp_path: Path):
    """Create a store with one note per project; return (store, proj_a, proj_b)."""
    store = make_store(tmp_path)

    async def _insert():
        await store.remember("note from project A", "note", _PROJ_A)
        await store.remember("note from project B", "note", _PROJ_B)

    asyncio.run(_insert())
    return store, _PROJ_A, _PROJ_B


class TestStoreRecallWithList:
    """G2: store.recall accepts a ULID list and filters by it correctly."""

    def test_recall_ulid_list_returns_both_projects(self, tmp_path):
        """A list of two project ULIDs returns notes from both."""
        store, proj_a, proj_b = _build_two_project_store(tmp_path)
        notes = asyncio.run(store.recall(None, [proj_a, proj_b], k=10))
        contents = {n.content for n in notes}
        assert "note from project A" in contents
        assert "note from project B" in contents

    def test_recall_single_str_returns_one_project(self, tmp_path):
        """A single ULID string scopes to that project only."""
        store, proj_a, proj_b = _build_two_project_store(tmp_path)
        notes = asyncio.run(store.recall(None, proj_a, k=10))
        assert len(notes) > 0
        assert all(n.project_id == proj_a for n in notes)
        assert not any(n.project_id == proj_b for n in notes)

    def test_recall_none_returns_all_projects(self, tmp_path):
        """project=None returns notes from every project."""
        store, proj_a, proj_b = _build_two_project_store(tmp_path)
        notes = asyncio.run(store.recall(None, None, k=10))
        project_ids = {n.project_id for n in notes}
        assert proj_a in project_ids
        assert proj_b in project_ids

    def test_recall_empty_list_returns_all(self, tmp_path):
        """An empty sequence behaves like project=None (no filter)."""
        store, proj_a, proj_b = _build_two_project_store(tmp_path)
        notes = asyncio.run(store.recall(None, [], k=10))
        project_ids = {n.project_id for n in notes}
        assert proj_a in project_ids
        assert proj_b in project_ids


# ── M9: _pin_read_project ────────────────────────────────────────────────────

class TestPinReadProject:
    """M9: _pin_read_project pins global-mode reads to BRAINCELL_PROJECT_ID."""

    def test_explicit_project_returned_unchanged(self, monkeypatch):
        """Explicit project is always returned as-is in any mode."""
        monkeypatch.setenv("BRAINCELL_MODE", "global")
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", "01PROJA000000000000000001A")
        assert _pin_read_project("01EXPLICIT00000000000000AA") == "01EXPLICIT00000000000000AA"

    def test_global_mode_none_pins_to_self(self, monkeypatch):
        """In global mode, project=None is pinned to BRAINCELL_PROJECT_ID."""
        monkeypatch.setenv("BRAINCELL_MODE", "global")
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", "01PROJA000000000000000001A")
        assert _pin_read_project(None) == "01PROJA000000000000000001A"

    def test_project_mode_none_returns_none(self, monkeypatch):
        """In project mode, project=None is returned as-is (no pin)."""
        monkeypatch.setenv("BRAINCELL_MODE", "project")
        monkeypatch.delenv("BRAINCELL_PROJECT_ID", raising=False)
        assert _pin_read_project(None) is None

    def test_global_mode_none_no_pid_returns_none(self, monkeypatch):
        """Global mode + no BRAINCELL_PROJECT_ID → None (no unset substitution)."""
        monkeypatch.setenv("BRAINCELL_MODE", "global")
        monkeypatch.delenv("BRAINCELL_PROJECT_ID", raising=False)
        assert _pin_read_project(None) is None

    def test_m9_list_documents_pins_to_self_excludes_other_project(
        self, tmp_path, monkeypatch
    ):
        """Integration: list_documents with pinned project=self excludes other projects."""
        store = make_store(tmp_path)

        async def _insert():
            cf = await store._conn_get()
            await upsert_document(
                cf, project_id=_PROJ_A, doc_key="doc-a", title="Doc A",
                content_hash=hashlib.sha256(b"a").digest(),
            )
            await upsert_document(
                cf, project_id=_PROJ_B, doc_key="doc-b", title="Doc B",
                content_hash=hashlib.sha256(b"b").digest(),
            )
            await cf.commit()

        asyncio.run(_insert())

        monkeypatch.setenv("BRAINCELL_MODE", "global")
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", _PROJ_A)

        pinned = _pin_read_project(None)
        assert pinned == _PROJ_A

        rows = asyncio.run(store.list_documents(pinned, None, 200))
        doc_keys = {r["doc_key"] for r in rows}
        assert "doc-a" in doc_keys
        assert "doc-b" not in doc_keys


# ── G4: build into the global brain ──────────────────────────────────────────

class TestG4BuildGlobal:
    """G4: _run_build(mode='global') ingests into the global DB, not per-project."""

    def test_global_build_creates_global_db(self, tmp_path):
        """mode='global' creates the shared global DB at get_global_db_path()."""
        from braincell.cli import _run_build
        from braincell.schema import MEMORY_SCHEMA_VERSION

        root = tmp_path / "repo"
        root.mkdir()

        _run_build(root, skip_transcripts=True, reembed=False, verbose=False, mode="global")

        global_path = get_global_db_path()
        assert global_path.exists(), "global DB must be created by mode='global' build"

        con = sqlite3.connect(str(global_path))
        try:
            ver = con.execute("SELECT version FROM schema_version").fetchone()[0]
        finally:
            con.close()
        assert ver == MEMORY_SCHEMA_VERSION

    def test_global_build_registers_path(self, tmp_path):
        """mode='global' still registers the repo path for attribution (path-registry)."""
        from braincell.cli import _run_build
        from braincell.project_registry import load_path_registry

        root = tmp_path / "repo"
        root.mkdir()

        _run_build(root, skip_transcripts=True, reembed=False, verbose=False, mode="global")

        registry = load_path_registry()
        assert str(root) in registry, "path must be registered even when targeting global brain"
        assert len(registry[str(root)]) > 0, "registered ULID must be non-empty"

    def test_global_build_does_not_create_per_project_db(self, tmp_path):
        """mode='global' targets the global DB only — per-project DB is NOT created."""
        from braincell.cli import _run_build
        from braincell.config import get_db_path
        from braincell.project_registry import load_path_registry

        root = tmp_path / "repo"
        root.mkdir()

        _run_build(root, skip_transcripts=True, reembed=False, verbose=False, mode="global")

        registry = load_path_registry()
        project_id = registry[str(root)]
        per_project_db = get_db_path(project_id)
        assert not per_project_db.exists(), (
            "global-mode build must NOT create the per-project DB"
        )

    def test_project_mode_build_targets_per_project_db(self, tmp_path):
        """mode=None (default project mode) still targets the per-project DB (regression)."""
        from braincell.cli import _run_build
        from braincell.config import get_db_path
        from braincell.project_registry import load_path_registry

        root = tmp_path / "repo2"
        root.mkdir()

        _run_build(root, skip_transcripts=True, reembed=False, verbose=False)

        registry = load_path_registry()
        project_id = registry[str(root)]

        assert get_db_path(project_id).exists(), (
            "project-mode build must create the per-project DB"
        )
        assert not get_global_db_path().exists(), (
            "project-mode build must NOT create the global DB"
        )


# ── G5: explicit projects=[...] selection on search + recall ──────────────────

class TestG5ResolveFilter:
    """G5: _resolve_filter precedence, validation, and mode-gating."""

    def test_projects_list_in_global_mode_returned_as_is(self, monkeypatch):
        """projects=[A, B] in global mode returns the list without scope resolution."""
        monkeypatch.setenv("BRAINCELL_MODE", "global")
        result = _resolve_filter([_PROJ_A, _PROJ_B], None, "self")
        assert result == [_PROJ_A, _PROJ_B]

    def test_projects_single_self_in_project_mode_allowed(self, monkeypatch):
        """projects=[self] in project mode is permitted (single-project, matches self)."""
        monkeypatch.setenv("BRAINCELL_MODE", "project")
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", _PROJ_A)
        result = _resolve_filter([_PROJ_A], None, "self")
        assert result == [_PROJ_A]

    def test_projects_multi_in_project_mode_raises(self, monkeypatch):
        """projects=[A, B] in project mode raises ValueError (cross-project)."""
        monkeypatch.setenv("BRAINCELL_MODE", "project")
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", _PROJ_A)
        with pytest.raises(ValueError, match="requires global mode"):
            _resolve_filter([_PROJ_A, _PROJ_B], None, "self")

    def test_projects_non_self_single_in_project_mode_raises(self, monkeypatch):
        """projects=[B] when self=A in project mode raises ValueError."""
        monkeypatch.setenv("BRAINCELL_MODE", "project")
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", _PROJ_A)
        with pytest.raises(ValueError, match="requires global mode"):
            _resolve_filter([_PROJ_B], None, "self")

    def test_projects_none_falls_through_to_resolve_scope(self, monkeypatch):
        """projects=None falls through to _resolve_scope (returns self project)."""
        monkeypatch.setenv("BRAINCELL_MODE", "project")
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", _PROJ_A)
        result = _resolve_filter(None, None, "self")
        assert result == _PROJ_A

    def test_projects_empty_list_falls_through_to_resolve_scope(self, monkeypatch):
        """projects=[] falls through to _resolve_scope (returns self project)."""
        monkeypatch.setenv("BRAINCELL_MODE", "project")
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", _PROJ_A)
        result = _resolve_filter([], None, "self")
        assert result == _PROJ_A

    def test_projects_overrides_explicit_project_arg(self, monkeypatch):
        """Non-empty projects list takes precedence over the single `project` arg."""
        monkeypatch.setenv("BRAINCELL_MODE", "global")
        result = _resolve_filter([_PROJ_A, _PROJ_B], _PROJ_A, "self")
        assert result == [_PROJ_A, _PROJ_B]

    def test_projects_empty_string_entry_raises(self, monkeypatch):
        """projects containing an empty string raises ValueError."""
        monkeypatch.setenv("BRAINCELL_MODE", "global")
        with pytest.raises(ValueError, match="non-empty"):
            _resolve_filter([_PROJ_A, ""], None, "self")

    def test_projects_whitespace_entry_raises(self, monkeypatch):
        """projects containing a whitespace-only entry raises ValueError."""
        monkeypatch.setenv("BRAINCELL_MODE", "global")
        with pytest.raises(ValueError, match="non-empty"):
            _resolve_filter([_PROJ_A, "   "], None, "self")

    def test_projects_exceeds_max_length_raises(self, monkeypatch):
        """projects list with >200 entries raises ValueError."""
        monkeypatch.setenv("BRAINCELL_MODE", "global")
        too_many = [f"ULID{i:020d}" for i in range(201)]
        with pytest.raises(ValueError, match="200"):
            _resolve_filter(too_many, None, "self")

    def test_projects_exactly_200_entries_allowed(self, monkeypatch):
        """projects list with exactly 200 entries does not raise."""
        monkeypatch.setenv("BRAINCELL_MODE", "global")
        exactly_200 = [f"ULID{i:020d}" for i in range(200)]
        result = _resolve_filter(exactly_200, None, "self")
        assert result == exactly_200

    def test_recall_with_projects_list_pools_both(self, tmp_path, monkeypatch):
        """Integration: store.recall([A, B]) from _resolve_filter returns notes from both."""
        monkeypatch.setenv("BRAINCELL_MODE", "global")
        store, proj_a, proj_b = _build_two_project_store(tmp_path)

        proj_filter = _resolve_filter([proj_a, proj_b], None, "self")
        notes = asyncio.run(store.recall(None, proj_filter, k=10))
        contents = {n.content for n in notes}
        assert "note from project A" in contents
        assert "note from project B" in contents

    def test_recall_with_projects_single_returns_only_that(self, tmp_path, monkeypatch):
        """Integration: store.recall([A]) from _resolve_filter returns only A's notes."""
        monkeypatch.setenv("BRAINCELL_MODE", "global")
        store, proj_a, proj_b = _build_two_project_store(tmp_path)

        proj_filter = _resolve_filter([proj_a], None, "self")
        notes = asyncio.run(store.recall(None, proj_filter, k=10))
        assert len(notes) > 0
        assert all(n.project_id == proj_a for n in notes)
