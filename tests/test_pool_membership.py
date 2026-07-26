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
    resolve_ulid_to_path,
)


def test_pool_name_normalization_and_duplicate_membership():
    create_pool("  Release   Notes ")
    with pytest.raises(ValueError, match="already exists"):
        create_pool("release notes")

    members = add_to_pool("RELEASE NOTES", ["01A", "01A", "01B"])

    assert members == ("01A", "01B")
    assert load_pools() == {"Release Notes": ("01A", "01B")}


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
