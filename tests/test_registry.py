# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_registry.py — legacy-recovery metadata, Project catalog metadata, and
the project-only named-Pool registry/CLI contract.

Isolation: the autouse ``isolate_xdg`` fixture (conftest.py) redirects
XDG_DATA_HOME to a per-test tmp_path so families.json and path-registry.json
never touch the real ~/.local/share tree.

All tests are offline and deterministic (no Ollama, no network).
"""

from __future__ import annotations

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

    @pytest.fixture(autouse=True)
    def _connected_project(self, monkeypatch):
        """Catalog tools serve only a connected MCP process; connect one."""
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", "01TESTCONNECTEDPROJECT001A")

    def test_empty_registry_returns_empty_list(self):
        from braincell.server import list_projects

        result = asyncio.run(list_projects())
        assert result == []

    def test_registered_projects_returned(self, tmp_path):
        from braincell.server import list_projects

        # Native absolute paths: POSIX literals like "/home/user/proj-a" are
        # not absolute on Windows and fail registry validation there.
        proj_a = str(tmp_path / "proj-a")
        proj_b = str(tmp_path / "proj-b")
        register_path(proj_a, "01PROJA000000000000000001A")
        register_path(proj_b, "01PROJB000000000000000001B")
        result = asyncio.run(list_projects())
        ids = {r.project_id for r in result}
        paths = {r.path for r in result}
        assert "01PROJA000000000000000001A" in ids
        assert "01PROJB000000000000000001B" in ids
        assert normalize_path(proj_a) in paths

    def test_results_sorted_by_path(self, tmp_path):
        from braincell.server import list_projects

        register_path(str(tmp_path / "z" / "z"), "01ZZZZZ000000000000000001Z")
        register_path(str(tmp_path / "a" / "a"), "01AAAAA000000000000000001A")
        result = asyncio.run(list_projects())
        paths = [r.path for r in result]
        assert paths == sorted(paths)


# ── list_pools MCP metadata tool ─────────────────────────────────────────────

class TestListPoolsTool:
    """Pool catalog reads membership metadata only; members are stable ULIDs."""

    @pytest.fixture(autouse=True)
    def _connected_project(self, monkeypatch):
        """Catalog tools serve only a connected MCP process; connect one."""
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", "01TESTCONNECTEDPROJECT001A")

    def test_empty_pools_returns_empty_list(self):
        from braincell.server import list_pools

        assert asyncio.run(list_pools()) == []

    def test_registered_and_unregistered_ulid_status(self, tmp_path):
        from braincell.project_registry import add_to_pool, create_pool
        from braincell.server import list_pools

        registered = "01PROJA000000000000000001A"
        unregistered = "01MISSING0000000000000001Z"
        register_path(str(tmp_path / "proj-a"), registered)
        create_pool("my-pool")
        add_to_pool("my-pool", [registered, unregistered])

        pool = asyncio.run(list_pools())[0]
        assert pool.name == "my-pool"
        assert pool.member_project_ids == [unregistered, registered]
        assert pool.member_status == {
            registered: "registered",
            unregistered: "unregistered",
        }

    def test_pools_sorted_by_name(self):
        from braincell.project_registry import create_pool
        from braincell.server import list_pools

        create_pool("z-pool")
        create_pool("a-pool")
        assert [pool.name for pool in asyncio.run(list_pools())] == [
            "a-pool",
            "z-pool",
        ]


# ── named-Pool CLI argparse round-trips ─────────────────────────────────────

class TestPoolCLIArgparse:
    def test_create_add_list_and_decouple_membership(self, capsys):
        from braincell.cli import main
        from braincell.project_registry import load_pools

        project_a = "01PROJA000000000000000001A"
        project_b = "01PROJB000000000000000001B"
        main(["pool", "create", "Research"])
        main(["pool", "add", "Research", project_a, project_b])
        main(["pool", "list"])
        listing = capsys.readouterr().out
        assert "Research" in listing
        assert project_a in listing and project_b in listing

        main(["pool", "decouple", "Research", project_b])
        assert load_pools()["Research"] == (project_a,)

    def test_decouple_changes_membership_only(self, tmp_path):
        from braincell.cli import main
        from braincell.config import get_db_path
        from braincell.project_registry import load_pools

        project_id = "01PROJA000000000000000001A"
        db = get_db_path(project_id)
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"project-memory-sentinel")

        main(["pool", "create", "Research"])
        main(["pool", "add", "Research", project_id])
        before = db.read_bytes()
        main(["pool", "decouple", "Research", project_id])

        assert load_pools()["Research"] == ()
        assert db.read_bytes() == before

    def test_family_command_is_retired(self):
        from braincell.cli import main

        with pytest.raises(SystemExit) as exc:
            main(["family", "ls"])
        assert exc.value.code == 2
