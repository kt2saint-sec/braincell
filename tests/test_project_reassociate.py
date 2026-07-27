# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stable-ULID Project path reassociation through CLI and Memory Map."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _git_project(path: Path) -> Path:
    path.mkdir()
    (path / ".git").mkdir()
    return path


def test_cli_reassociate_preserves_database_and_pool_membership(
    tmp_path, capsys, monkeypatch
):
    from braincell.cli import main
    from braincell.config import get_db_path
    from braincell.project_registry import (
        add_to_pool,
        create_pool,
        register_path,
        resolve_pool,
        resolve_ulid_to_path,
    )
    from braincell.store import SqliteStore

    monkeypatch.setattr("braincell.project_target._is_privileged", lambda: False)
    old = _git_project(tmp_path / "old")
    new = _git_project(tmp_path / "new")
    register_path(old, "01STABLE")
    create_pool("Keep")
    add_to_pool("Keep", ["01STABLE"])
    database = get_db_path("01STABLE")
    store = SqliteStore(database)
    store.assert_schema_version()
    store.close()
    before = (database.stat().st_ino, database.stat().st_size)

    main(["project", "reassociate", "01STABLE", str(new)])

    output = capsys.readouterr().out
    assert f"Old path: {old}" in output
    assert f"New path: {new}" in output
    assert resolve_ulid_to_path("01STABLE") == new
    assert resolve_pool("Keep")[1] == ("01STABLE",)
    assert (database.stat().st_ino, database.stat().st_size) == before


def test_reassociate_rejects_destination_owned_by_another_project(
    tmp_path, monkeypatch
):
    from braincell.cli import main
    from braincell.project_registry import register_path, resolve_ulid_to_path

    monkeypatch.setattr("braincell.project_target._is_privileged", lambda: False)
    old = _git_project(tmp_path / "old")
    destination = _git_project(tmp_path / "destination")
    register_path(old, "01SOURCE")
    register_path(destination, "01OWNER")

    with pytest.raises(SystemExit, match="already owned"):
        main(["project", "reassociate", "01SOURCE", str(destination)])

    assert resolve_ulid_to_path("01SOURCE") == old
    assert resolve_ulid_to_path("01OWNER") == destination


def test_memory_map_reassociate_action_uses_same_safety_checks(
    tmp_path, monkeypatch
):
    from braincell.config import get_db_path
    from braincell.gui import create_app
    from braincell.project_registry import register_path, resolve_ulid_to_path
    from braincell.store import SqliteStore

    monkeypatch.setattr("braincell.project_target._is_privileged", lambda: False)
    old = _git_project(tmp_path / "old")
    non_git = tmp_path / "moved"
    non_git.mkdir()
    register_path(old, "01GUI")
    db = get_db_path("01GUI")
    store = SqliteStore(db)
    store.assert_schema_version()
    store.close()
    app = create_app(db_path=db, allow_writes=True, seed_project_id="01GUI")

    with TestClient(app) as client:
        rejected = client.post(
            "/api/projects/reassociate",
            json={"project_id": "01GUI", "new_path": str(non_git)},
        )
        accepted = client.post(
            "/api/projects/reassociate",
            json={
                "project_id": "01GUI",
                "new_path": str(non_git),
                "acknowledge_non_git": True,
            },
        )

    assert rejected.status_code == 409
    assert accepted.status_code == 200
    assert accepted.json()["old_path"] == str(old)
    assert accepted.json()["new_path"] == str(non_git)
    assert resolve_ulid_to_path("01GUI") == non_git
