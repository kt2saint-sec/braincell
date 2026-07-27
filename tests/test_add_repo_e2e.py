# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end Project isolation and explicit named-Pool acceptance coverage."""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest

from braincell import server
from braincell.cli import main
from braincell.config import get_db_path, get_project_id
from braincell.project_registry import add_to_pool, create_pool
from braincell.store import SqliteStore
from tests.conftest import fake_vec

# ── Helpers (mirrors tests/test_federate.py + tests/test_install.py idioms) ────

def _make_member(root: Path, notes: list[str]) -> str:
    """Register `root` as a project, build its brain, seed `notes`. Return its ULID."""
    pid = get_project_id(root)  # mints + registers in the path-registry
    db = get_db_path(pid)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.assert_schema_version()

    async def _seed() -> None:
        for i, text in enumerate(notes):
            await store.remember(text, "note", pid, embedding=fake_vec(i + 1))

    asyncio.run(_seed())
    store.close()
    return pid


def _fail_embed(monkeypatch) -> None:
    """Force the recall path's embed-query call to fail so it falls back to
    keyword/recency ranking deterministically, regardless of whether an ollama
    daemon happens to be reachable on the box running this suite (hermetic)."""

    async def _boom(_text: str) -> np.ndarray:
        raise RuntimeError("no embedder in hermetic test — forced keyword fallback")

    monkeypatch.setattr(server, "embed_query_async", _boom)


class TestProjectAndNamedPoolE2E:
    def test_normal_recall_is_connected_project_only(self, tmp_path, monkeypatch):
        a = tmp_path / "repoA"
        b = tmp_path / "repoB"
        a.mkdir()
        b.mkdir()
        pid_a = _make_member(a, ["alpha specific fact about widgets"])
        pid_b = _make_member(b, ["beta specific fact about gadgets"])
        _fail_embed(monkeypatch)
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", pid_a)
        monkeypatch.setenv("BRAINCELL_FEDERATE", "on")

        store_a = SqliteStore(get_db_path(pid_a))
        try:
            notes = asyncio.run(server.recall_notes(store_a, "gadgets", project=pid_a))
        finally:
            store_a.close()
        assert pid_b not in {note.project_id for note in notes}

    def test_explicit_named_pool_recall_surfaces_both_projects(self, tmp_path, monkeypatch):
        a = tmp_path / "repoA"
        b = tmp_path / "repoB"
        a.mkdir()
        b.mkdir()
        pid_a = _make_member(a, ["alpha fact about widgets"])
        pid_b = _make_member(b, ["beta fact about gadgets"])
        create_pool("Research")
        add_to_pool("Research", [pid_a, pid_b])
        _fail_embed(monkeypatch)
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", pid_a)

        main(["pool", "recall", "Research", "fact", "--path", str(a), "--json"])
        # The detailed JSON shape is covered in the Pool CLI module; this E2E
        # assertion pins that the explicit Pool operation can reach both ULIDs.
        from braincell.federate import federated_recall, plan_for_pool

        plan = plan_for_pool("Research", pid_a)
        notes = asyncio.run(federated_recall(None, plan, None, 10, qtext="fact"))
        assert {note.project_id for note in notes} == {pid_a, pid_b}

    def test_named_pool_recall_leaves_member_database_untouched(self, tmp_path, monkeypatch):
        a = tmp_path / "repoA"
        b = tmp_path / "repoB"
        a.mkdir()
        b.mkdir()
        pid_a = _make_member(a, ["alpha fact about widgets"])
        pid_b = _make_member(b, ["beta fact about gadgets"])
        create_pool("Research")
        add_to_pool("Research", [pid_a, pid_b])
        _fail_embed(monkeypatch)
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", pid_a)

        b_db = get_db_path(pid_b)
        bytes_before = b_db.read_bytes()
        mtime_before = b_db.stat().st_mtime_ns
        from braincell.federate import federated_recall, plan_for_pool

        notes = asyncio.run(
            federated_recall(
                None, plan_for_pool("Research", pid_a), None, 10, qtext="gadgets"
            )
        )
        assert any(note.project_id == pid_b for note in notes)

        assert b_db.read_bytes() == bytes_before, "sibling db bytes must be unchanged"
        assert b_db.stat().st_mtime_ns == mtime_before, "sibling db mtime must be unchanged"

    def test_retired_federate_install_argument_is_rejected(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        with pytest.raises(SystemExit) as exc:
            main(["install", str(repo), "--federate"])
        assert exc.value.code == 2
