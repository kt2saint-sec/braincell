# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
test_project_orphans.py — regression tests for BUGS.md "orphan reconciliation":
a READ-ONLY inventory of registry entries whose path was deleted/moved, and
project databases with no registry entry. Detection only — never deletes or
repairs; reassociating a moved Project stays a separate, explicit workflow
(`project_registry.reassociate_project_path`).
"""

from __future__ import annotations

from braincell.project_registry import find_orphans, register_path


def test_no_orphans_on_a_clean_registry(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    register_path(str(root), "01AAAAAAAAAAAAAAAAAAAAAAAA")
    result = find_orphans()
    assert result["orphaned_registry_entries"] == []
    assert result["orphaned_project_databases"] == []


def test_deleted_project_path_is_an_orphaned_registry_entry(tmp_path):
    """The registered path was removed from disk without `project reassociate`."""
    root = tmp_path / "repo"
    root.mkdir()
    register_path(str(root), "01BBBBBBBBBBBBBBBBBBBBBBBB")

    import shutil
    shutil.rmtree(root)

    result = find_orphans()
    assert result["orphaned_registry_entries"] == [
        {"path": str(root), "project_id": "01BBBBBBBBBBBBBBBBBBBBBBBB"}
    ]
    assert result["orphaned_project_databases"] == []


def test_moved_path_is_not_orphaned_after_reassociate(tmp_path):
    """Reassociating (the existing repair) clears the orphan, without this
    read-only inventory ever touching the registry itself."""
    from braincell.project_registry import reassociate_project_path

    old_root = tmp_path / "old"
    old_root.mkdir()
    register_path(str(old_root), "01CCCCCCCCCCCCCCCCCCCCCCCC")

    new_root = tmp_path / "new"
    new_root.mkdir()
    old_root.rmdir()

    # Before reassociation: the stale path is a detected orphan.
    assert find_orphans()["orphaned_registry_entries"] == [
        {"path": str(old_root), "project_id": "01CCCCCCCCCCCCCCCCCCCCCCCC"}
    ]

    reassociate_project_path("01CCCCCCCCCCCCCCCCCCCCCCCC", new_root)

    assert find_orphans()["orphaned_registry_entries"] == []


def test_database_with_no_registry_entry_is_an_orphaned_database(tmp_path, monkeypatch):
    """A projects/<ulid>/braincell.db with nothing pointing at it in the
    path-registry — e.g. the registry row was lost or never written."""
    from braincell import config

    ulid = "01DDDDDDDDDDDDDDDDDDDDDDDD"
    project_dir = config._xdg_data_home() / config.DATA_NAMESPACE / "projects" / ulid
    project_dir.mkdir(parents=True)
    (project_dir / "braincell.db").write_bytes(b"")  # presence is all find_orphans checks

    result = find_orphans()
    assert result["orphaned_project_databases"] == [
        {"project_id": ulid, "database": str(project_dir / "braincell.db")}
    ]
    assert result["orphaned_registry_entries"] == []


def test_registered_project_database_is_never_flagged(tmp_path):
    """A ULID present in the registry must never appear as an orphaned database,
    even though find_orphans() enumerates the same projects/ directory."""
    from braincell.config import get_db_path, get_project_id
    from braincell.store import SqliteStore

    root = tmp_path / "repo"
    root.mkdir()
    project_id = get_project_id(root)  # mints + registers
    store = SqliteStore(get_db_path(project_id))
    store.assert_schema_version()
    store.close()

    result = find_orphans()
    assert result["orphaned_project_databases"] == []
    assert result["orphaned_registry_entries"] == []


def test_find_orphans_never_mutates_the_registry_or_deletes_a_database(tmp_path):
    """Adversarial: calling find_orphans() repeatedly must be side-effect-free."""
    from braincell import config
    from braincell.project_registry import load_path_registry

    root = tmp_path / "repo"
    root.mkdir()
    register_path(str(root), "01EEEEEEEEEEEEEEEEEEEEEEEE")
    root.rmdir()

    ulid = "01FFFFFFFFFFFFFFFFFFFFFFFF"
    project_dir = config._xdg_data_home() / config.DATA_NAMESPACE / "projects" / ulid
    project_dir.mkdir(parents=True)
    db_path = project_dir / "braincell.db"
    db_path.write_bytes(b"")

    before_registry = load_path_registry()
    for _ in range(3):
        find_orphans()
    assert load_path_registry() == before_registry
    assert db_path.is_file()  # never deleted
