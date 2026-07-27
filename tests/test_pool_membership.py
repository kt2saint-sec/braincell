# SPDX-License-Identifier: AGPL-3.0-or-later
"""ULID-only Pool membership regression coverage."""

from __future__ import annotations

import pytest

from braincell.project_registry import (
    add_to_pool,
    create_pool,
    decouple_from_pool,
    delete_pool,
    load_pools,
    pools_for_project,
    reassociate_project_path,
    register_path,
    resolve_pool,
    resolve_ulid_to_path,
)


def test_pool_name_normalization_and_duplicate_membership():
    create_pool("  Release   Notes ")
    with pytest.raises(ValueError, match="already exists"):
        create_pool("release notes")

    members = add_to_pool("RELEASE NOTES", ["01A", "01A", "01B"])

    assert members == ("01A", "01B")
    assert load_pools() == {"Release Notes": ("01A", "01B")}
    assert resolve_pool("release notes") == ("Release Notes", ("01A", "01B"))


def test_decouple_changes_only_one_membership_definition():
    create_pool("A")
    create_pool("B")
    add_to_pool("A", ["01PROJECT"])
    add_to_pool("B", ["01PROJECT"])

    assert decouple_from_pool("A", "01PROJECT") is True
    assert load_pools()["A"] == ()
    assert load_pools()["B"] == ("01PROJECT",)
    assert pools_for_project("01PROJECT") == ("B",)


def test_pool_deletion_never_deletes_project_registration_or_memory(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    register_path(project, "01PROJECT")
    create_pool("Disposable")
    add_to_pool("Disposable", ["01PROJECT"])

    assert delete_pool("Disposable") is True
    assert resolve_ulid_to_path("01PROJECT") == project


def test_reassociate_keeps_pool_membership_for_moved_project(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    register_path(old, "01MOVED")
    create_pool("Shared")
    add_to_pool("Shared", ["01MOVED"])

    reassociate_project_path("01MOVED", new)

    assert resolve_ulid_to_path("01MOVED") == new
    assert load_pools()["Shared"] == ("01MOVED",)


def test_pool_query_resolves_current_paths_and_skips_missing_or_corrupt_members(tmp_path):
    from braincell.config import get_db_path
    from braincell.federate import plan_for_pool
    from braincell.store import SqliteStore

    connected = tmp_path / "connected"
    valid = tmp_path / "valid"
    connected.mkdir()
    valid.mkdir()
    register_path(connected, "01CONNECTED")
    register_path(valid, "01VALID")
    create_pool("Read only")
    add_to_pool("Read only", ["01CONNECTED", "01VALID", "01MISSING", "01CORRUPT"])

    for project_id in ("01CONNECTED", "01VALID"):
        store = SqliteStore(get_db_path(project_id))
        store.assert_schema_version()
        store.close()
    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    register_path(corrupt, "01CORRUPT")
    get_db_path("01CORRUPT").parent.mkdir(parents=True, exist_ok=True)
    get_db_path("01CORRUPT").write_text("not sqlite", encoding="utf-8")

    plan = plan_for_pool("read only", "01CONNECTED")

    assert [target.project_id for target in plan.targets] == ["01CONNECTED", "01VALID"]
    assert {status.status for status in plan.member_status} >= {
        "ready", "missing", "corrupt"
    }


def test_pool_query_rejects_non_member_before_resolving_any_member_path(tmp_path, monkeypatch):
    from braincell.federate import plan_for_pool

    create_pool("Private")
    add_to_pool("Private", ["01MEMBER"])

    def fail_lookup(_project_id):
        raise AssertionError("a non-member query must not resolve a member database")

    monkeypatch.setattr("braincell.federate.resolve_ulid_to_path", fail_lookup)
    with pytest.raises(ValueError, match="not a member"):
        plan_for_pool("Private", "01OUTSIDER")


def test_pool_cli_never_exposes_materialized_all_projects_selector(capsys):
    from braincell.cli import main

    main(["pool", "create", "Focused"])
    main(["pool", "add", "Focused", "01A", "01B"])
    main(["pool", "decouple", "Focused", "01A"])
    main(["pool", "list"])

    output = capsys.readouterr().out
    assert "01B" in output
    assert "01A" not in output.split("[Focused]")[-1]
    with pytest.raises(SystemExit) as exc:
        main(["pool", "--all"])
    assert exc.value.code == 2
