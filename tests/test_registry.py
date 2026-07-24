# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_registry.py — G3: save_families, add/remove mutators, list_projects,
list_families, and family CLI handlers.

Isolation: the autouse ``isolate_xdg`` fixture (conftest.py) redirects
XDG_DATA_HOME to a per-test tmp_path so families.json and path-registry.json
never touch the real ~/.local/share tree.

All tests are offline and deterministic (no Ollama, no network).
"""

from __future__ import annotations

import argparse
import asyncio
import json

import pytest

from braincell.config import get_families_path
from braincell.project_registry import (
    add_family_members,
    load_families,
    normalize_path,
    register_path,
    remove_family,
    save_families,
)


# ── save_families ─────────────────────────────────────────────────────────────

class TestSaveFamilies:
    def test_round_trip(self):
        """save_families persists and load_families reads it back exactly."""
        data: dict[str, list[str]] = {
            "proj-a": ["/home/user/a", "/home/user/b"],
            "proj-b": ["/home/user/c"],
        }
        save_families(data)
        assert load_families() == data

    def test_atomic_no_tmp_left(self):
        """After save_families, no .tmp file remains on disk."""
        p = get_families_path()
        save_families({"test": ["/tmp/x"]})
        tmp = p.with_suffix(".json.tmp")
        assert not tmp.exists(), ".tmp must be cleaned up by os.replace"

    def test_overwrite(self):
        """Second save_families completely replaces the first."""
        save_families({"a": ["/tmp/a"]})
        save_families({"b": ["/tmp/b"]})
        assert load_families() == {"b": ["/tmp/b"]}

    def test_creates_parent_dirs(self):
        """save_families creates parent dirs when they don't exist yet."""
        p = get_families_path()
        assert not p.exists()
        save_families({"x": ["/tmp/x"]})
        assert p.exists()

    def test_empty_dict(self):
        """Empty dict round-trips correctly."""
        save_families({})
        assert load_families() == {}

    def test_rejects_non_dict(self):
        """Non-dict input raises TypeError (defensive validation)."""
        with pytest.raises(TypeError):
            save_families(["not", "a", "dict"])  # type: ignore[arg-type]

    def test_json_on_disk_is_sorted_and_indented(self):
        """Output JSON has sort_keys + indent=2 (stable, diffable)."""
        save_families({"z-fam": ["/z"], "a-fam": ["/a"]})
        text = get_families_path().read_text(encoding="utf-8")
        obj = json.loads(text)
        keys = list(obj.keys())
        assert keys == sorted(keys), "keys must be sorted"
        assert "\n  " in text, "must use indent=2"


# ── add_family_members ────────────────────────────────────────────────────────

class TestAddFamilyMembers:
    def test_creates_new_family(self):
        """add_family_members creates the family when it does not exist."""
        result = add_family_members("my-family", ["/home/user/project"])
        assert "my-family" in result
        assert normalize_path("/home/user/project") in result["my-family"]

    def test_appends_to_existing(self):
        """add_family_members merges new paths into an existing family."""
        add_family_members("fam", ["/a/b"])
        result = add_family_members("fam", ["/c/d"])
        assert len(result["fam"]) == 2

    def test_deduplicates(self):
        """Duplicate normalized paths produce a single entry."""
        result = add_family_members("fam", ["/a/b", "/a/b"])
        assert result["fam"].count(normalize_path("/a/b")) == 1

    def test_members_stored_sorted(self):
        """Members list in families.json is sorted."""
        result = add_family_members("fam", ["/z/z", "/a/a"])
        assert result["fam"] == sorted(result["fam"])

    def test_normalizes_paths(self):
        """Redundant path components are normalized before storage."""
        result = add_family_members("fam", ["/a//b/../b"])
        assert normalize_path("/a//b/../b") in result["fam"]

    def test_does_not_discard_other_families(self):
        """add_family_members on one family leaves other families intact."""
        save_families({"other": ["/o/p"]})
        add_family_members("new-fam", ["/n/p"])
        loaded = load_families()
        assert "other" in loaded
        assert "new-fam" in loaded

    def test_persists_to_disk(self):
        """Result is written to families.json (a fresh load_families sees it)."""
        add_family_members("fam", ["/a/x"])
        assert "fam" in load_families()


# ── remove_family ─────────────────────────────────────────────────────────────

class TestRemoveFamily:
    def test_remove_whole_family(self):
        """remove_family(name) with paths=None removes the entire family."""
        add_family_members("fam", ["/a/b"])
        changed = remove_family("fam")
        assert changed is True
        assert "fam" not in load_families()

    def test_remove_specific_member(self):
        """remove_family with paths removes only the listed member."""
        add_family_members("fam", ["/a/b", "/c/d"])
        changed = remove_family("fam", ["/a/b"])
        assert changed is True
        result = load_families()
        assert "fam" in result
        assert normalize_path("/a/b") not in result["fam"]
        assert normalize_path("/c/d") in result["fam"]

    def test_family_dropped_when_empty_after_removal(self):
        """Family key is removed when the last member is deleted."""
        add_family_members("fam", ["/a/b"])
        remove_family("fam", ["/a/b"])
        assert "fam" not in load_families()

    def test_returns_false_for_absent_family(self):
        """remove_family returns False when the family doesn't exist."""
        assert remove_family("nonexistent") is False

    def test_returns_false_when_member_not_present(self):
        """remove_family returns False when the member is not in the family."""
        add_family_members("fam", ["/a/b"])
        assert remove_family("fam", ["/not/a/member"]) is False

    def test_other_families_preserved(self):
        """Removing one family leaves other families intact."""
        save_families({"keep": ["/k"], "drop": ["/d"]})
        remove_family("drop")
        loaded = load_families()
        assert "keep" in loaded
        assert "drop" not in loaded


# ── list_projects MCP tool ────────────────────────────────────────────────────

class TestListProjectsTool:
    """list_projects() enumerates the path registry offline."""

    def test_empty_registry_returns_empty_list(self):
        from braincell.server import list_projects

        result = asyncio.run(list_projects())
        assert result == []

    def test_registered_projects_returned(self):
        from braincell.server import list_projects

        register_path("/home/user/proj-a", "01PROJA000000000000000001A")
        register_path("/home/user/proj-b", "01PROJB000000000000000001B")
        result = asyncio.run(list_projects())
        ids = {r.project_id for r in result}
        paths = {r.path for r in result}
        assert "01PROJA000000000000000001A" in ids
        assert "01PROJB000000000000000001B" in ids
        assert normalize_path("/home/user/proj-a") in paths

    def test_results_sorted_by_path(self):
        from braincell.server import list_projects

        register_path("/z/z", "01ZZZZZ000000000000000001Z")
        register_path("/a/a", "01AAAAA000000000000000001A")
        result = asyncio.run(list_projects())
        paths = [r.path for r in result]
        assert paths == sorted(paths)


# ── list_families MCP tool ────────────────────────────────────────────────────

class TestListFamiliesTool:
    """list_families() resolves member ULIDs; unregistered members get no ULID."""

    def test_empty_families_returns_empty_list(self):
        from braincell.server import list_families

        result = asyncio.run(list_families())
        assert result == []

    def test_registered_member_resolves_ulid(self):
        from braincell.server import list_families

        register_path("/home/user/proj-a", "01PROJA000000000000000001A")
        add_family_members("my-fam", ["/home/user/proj-a"])
        result = asyncio.run(list_families())
        assert len(result) == 1
        fam = result[0]
        assert fam.name == "my-fam"
        assert "01PROJA000000000000000001A" in fam.project_ids

    def test_unregistered_member_excluded_from_project_ids(self):
        from braincell.server import list_families

        register_path("/home/user/proj-a", "01PROJA000000000000000001A")
        add_family_members("my-fam", ["/home/user/proj-a", "/home/user/unregistered"])
        result = asyncio.run(list_families())
        fam = result[0]
        # Both paths appear in members
        assert any("unregistered" in m for m in fam.members)
        # But only the registered one contributes a ULID
        assert fam.project_ids == ["01PROJA000000000000000001A"]

    def test_families_sorted_by_name(self):
        from braincell.server import list_families

        add_family_members("z-fam", ["/tmp/z"])
        add_family_members("a-fam", ["/tmp/a"])
        result = asyncio.run(list_families())
        names = [f.name for f in result]
        assert names == sorted(names)

    def test_multiple_registered_members(self):
        from braincell.server import list_families

        register_path("/home/user/p1", "01P1000000000000000000001A")
        register_path("/home/user/p2", "01P2000000000000000000001B")
        add_family_members("multi", ["/home/user/p1", "/home/user/p2"])
        result = asyncio.run(list_families())
        fam = result[0]
        assert set(fam.project_ids) == {
            "01P1000000000000000000001A",
            "01P2000000000000000000001B",
        }


# ── family CLI handlers ───────────────────────────────────────────────────────

class TestFamilyCLIHandlers:
    """Direct invocation of cmd_family_* handler functions."""

    def test_add_creates_family(self):
        from braincell.cli import cmd_family_add

        args = argparse.Namespace(name="cli-fam", paths=["/home/user/x"])
        cmd_family_add(args)
        fams = load_families()
        assert "cli-fam" in fams
        assert normalize_path("/home/user/x") in fams["cli-fam"]

    def test_add_idempotent(self):
        """Running add twice for the same path doesn't duplicate members."""
        from braincell.cli import cmd_family_add

        args = argparse.Namespace(name="cli-fam", paths=["/home/user/x"])
        cmd_family_add(args)
        cmd_family_add(args)
        fams = load_families()
        assert fams["cli-fam"].count(normalize_path("/home/user/x")) == 1

    def test_rm_removes_entire_family(self):
        """cmd_family_rm with empty paths list removes the whole family."""
        from braincell.cli import cmd_family_add, cmd_family_rm

        cmd_family_add(argparse.Namespace(name="cli-fam", paths=["/home/user/x"]))
        cmd_family_rm(argparse.Namespace(name="cli-fam", paths=[]))
        assert "cli-fam" not in load_families()

    def test_rm_removes_specific_member(self):
        """cmd_family_rm with a path removes just that member."""
        from braincell.cli import cmd_family_add, cmd_family_rm

        cmd_family_add(argparse.Namespace(name="cli-fam", paths=["/home/user/x", "/home/user/y"]))
        cmd_family_rm(argparse.Namespace(name="cli-fam", paths=["/home/user/x"]))
        fams = load_families()
        assert "cli-fam" in fams
        assert normalize_path("/home/user/x") not in fams["cli-fam"]
        assert normalize_path("/home/user/y") in fams["cli-fam"]

    def test_ls_no_families(self, capsys):
        """cmd_family_ls prints a helpful message when no families exist."""
        from braincell.cli import cmd_family_ls

        cmd_family_ls(argparse.Namespace())
        out = capsys.readouterr().out
        assert "No families" in out

    def test_ls_shows_family_and_ulid(self, capsys):
        """cmd_family_ls shows the family name and resolved ULID."""
        from braincell.cli import cmd_family_ls

        register_path("/home/user/proj-a", "01PROJA000000000000000001A")
        add_family_members("ls-fam", ["/home/user/proj-a"])
        cmd_family_ls(argparse.Namespace())
        out = capsys.readouterr().out
        assert "ls-fam" in out
        assert "01PROJA000000000000000001A" in out

    def test_ls_shows_unregistered_label(self, capsys):
        """cmd_family_ls labels paths not in the path registry as (unregistered)."""
        from braincell.cli import cmd_family_ls

        add_family_members("ls-fam", ["/home/user/no-ulid-here"])
        cmd_family_ls(argparse.Namespace())
        out = capsys.readouterr().out
        assert "unregistered" in out


# ── end-to-end argparse round-trips ──────────────────────────────────────────

class TestFamilyCLIArgparse:
    """Full argparse dispatch: braincell family {add,rm,ls}."""

    def test_family_add_via_main(self):
        from braincell.cli import main

        main(["family", "add", "argparse-fam", "/some/path"])
        fams = load_families()
        assert "argparse-fam" in fams

    def test_family_add_multiple_paths(self):
        from braincell.cli import main

        main(["family", "add", "multi-fam", "/path/a", "/path/b"])
        fams = load_families()
        assert len(fams["multi-fam"]) == 2

    def test_family_ls_via_main(self, capsys):
        from braincell.cli import main

        add_family_members("exist-fam", ["/a/b"])
        main(["family", "ls"])
        out = capsys.readouterr().out
        assert "exist-fam" in out

    def test_family_rm_whole_family_via_main(self):
        from braincell.cli import main

        add_family_members("to-drop", ["/a/b"])
        main(["family", "rm", "to-drop"])
        assert "to-drop" not in load_families()

    def test_family_rm_specific_member_via_main(self):
        from braincell.cli import main

        add_family_members("partial-drop", ["/a/b", "/c/d"])
        main(["family", "rm", "partial-drop", "/a/b"])
        fams = load_families()
        assert "partial-drop" in fams
        assert normalize_path("/a/b") not in fams["partial-drop"]
