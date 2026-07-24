# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_add_repo_e2e.py — cross-project acceptance test for the add-a-repo flow
(docs/add-repo-gui-install-spec-2026-07-08.md, row 11).

Proves deliverable 4 ("cross-project (family federation) fully functional after
add") end-to-end, hermetically: two throwaway repos are registered + built (notes
seeded directly via SqliteStore, mirroring tests/test_federate.py — no ollama, no
network), added to a family, then ``braincell install --federate`` is run against
a faked ``claude`` CLI so the ACTUAL env dict the installer stamps (captured off
the real `claude mcp add` argv, same idiom as
tests/test_install.py::test_cmd_install_federate_stamps_env) is what gets fed into
the post-install MCP env simulation — proving the flag the installer writes is
exactly the flag ``build_federation_plan`` reads (federate.py), not just two
independently-asserted halves of the same claim.

Covers: sibling-note recall under scope='family', raise-when-FEDERATE-unset,
scope='self' isolation, and the read-only guarantee (sibling db untouched).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import numpy as np
import pytest

from braincell import server
from braincell.cli import main
from braincell.config import get_db_path, get_project_id
from braincell.project_registry import add_family_members
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


def _capture_install_env(tmp_path: Path, monkeypatch, repo: Path) -> dict[str, str]:
    """Run `braincell install --federate` against a faked `claude` CLI and return
    the ACTUAL env dict stamped into the `claude mcp add -e K=V ...` argv — the
    same captured-env idiom as test_install.py::test_cmd_install_federate_stamps_env.
    Never touches the real ~/.claude/settings.json (BRAINCELL_CLAUDE_SETTINGS
    redirects it to a tmp file, same as test_install.py's `_settings` helper).
    """
    from braincell import install as inst

    settings_path = tmp_path / "settings.json"
    monkeypatch.setenv("BRAINCELL_CLAUDE_SETTINGS", str(settings_path))
    monkeypatch.setattr(
        inst.shutil, "which",
        lambda n: "/fake/claude" if n == "claude" else None,
    )
    calls: list[list[str]] = []

    def fake_run(argv, cwd=None, capture_output=True, text=True):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(inst.subprocess, "run", fake_run)

    main(["install", str(repo), "--federate"])

    add_argv = calls[1]  # remove-then-add
    env: dict[str, str] = {}
    for i, tok in enumerate(add_argv):
        if tok == "-e":
            key, _, val = add_argv[i + 1].partition("=")
            env[key] = val
    return env


# ── The E2E flow ────────────────────────────────────────────────────────────────

class TestAddRepoFamilyFederationE2E:
    def test_installer_stamped_flag_drives_sibling_recall(self, tmp_path, monkeypatch):
        """Steps 1-5 of the row-11 flow: build two repos, family them, install
        --federate against A, and prove the CAPTURED installer env (not a
        hand-typed 'on') makes recall(scope='family') surface B's note."""
        a = tmp_path / "repoA"
        b = tmp_path / "repoB"
        a.mkdir()
        b.mkdir()
        pid_a = _make_member(a, ["alpha specific fact about widgets"])
        pid_b = _make_member(b, ["beta specific fact about gadgets"])
        add_family_members("e2efam", [str(a), str(b)])
        _fail_embed(monkeypatch)

        captured_env = _capture_install_env(tmp_path, monkeypatch, a)
        assert captured_env.get("BRAINCELL_FEDERATE") == "on", (
            "the installer must stamp BRAINCELL_FEDERATE=on with --federate"
        )
        assert captured_env.get("BRAINCELL_PROJECT_ID") == pid_a, (
            "installer must mint/confirm the SAME ULID get_project_id already gave repo A"
        )

        # Simulate the post-install MCP launch env using EXACTLY what was captured —
        # this is the linkage proof: the flag the installer writes is the flag
        # build_federation_plan (via server.recall_notes) reads.
        monkeypatch.setenv("BRAINCELL_FEDERATE", captured_env["BRAINCELL_FEDERATE"])
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", captured_env["BRAINCELL_PROJECT_ID"])
        monkeypatch.delenv("BRAINCELL_MODE", raising=False)  # project mode (default)

        store_a = SqliteStore(get_db_path(pid_a))
        try:
            notes = asyncio.run(
                server.recall_notes(store_a, "gadgets", scope="family")
            )
            assert any(n.project_id == pid_b for n in notes), (
                "sibling B's note must be surfaced via scope='family' fan-out"
            )
        finally:
            store_a.close()

    def test_federate_unset_raises_on_family_scope(self, tmp_path, monkeypatch):
        """Negative control: without BRAINCELL_FEDERATE, scope='family' in project
        mode must raise — the gap this spec closes (server.py:_resolve_scope)."""
        a = tmp_path / "repoA"
        b = tmp_path / "repoB"
        a.mkdir()
        b.mkdir()
        pid_a = _make_member(a, ["alpha fact"])
        _make_member(b, ["beta fact"])
        add_family_members("e2efam", [str(a), str(b)])
        _fail_embed(monkeypatch)

        monkeypatch.delenv("BRAINCELL_FEDERATE", raising=False)
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", pid_a)
        monkeypatch.delenv("BRAINCELL_MODE", raising=False)

        store_a = SqliteStore(get_db_path(pid_a))
        try:
            with pytest.raises(ValueError):
                asyncio.run(server.recall_notes(store_a, "fact", scope="family"))
        finally:
            store_a.close()

    def test_self_scope_isolated_to_seed(self, tmp_path, monkeypatch):
        """Even with federation ON, scope='self' (the default) must never surface
        a sibling's note — only the seed project's own brain."""
        a = tmp_path / "repoA"
        b = tmp_path / "repoB"
        a.mkdir()
        b.mkdir()
        pid_a = _make_member(a, ["alpha specific fact about widgets"])
        pid_b = _make_member(b, ["beta specific fact about widgets"])
        add_family_members("e2efam", [str(a), str(b)])
        _fail_embed(monkeypatch)

        monkeypatch.setenv("BRAINCELL_FEDERATE", "on")
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", pid_a)
        monkeypatch.delenv("BRAINCELL_MODE", raising=False)

        store_a = SqliteStore(get_db_path(pid_a))
        try:
            notes = asyncio.run(
                server.recall_notes(store_a, "widgets", scope="self")
            )
            assert notes, "the seed's own matching note must still come back"
            assert all(n.project_id == pid_a for n in notes), (
                "scope='self' must never include a sibling project's notes"
            )
            assert pid_b not in {n.project_id for n in notes}
        finally:
            store_a.close()

    def test_sibling_db_untouched_by_federated_recall(self, tmp_path, monkeypatch):
        """RO guarantee: federated recall opens siblings read-only — B's brain
        file must be byte-for-byte and mtime-identical after the fan-out read."""
        a = tmp_path / "repoA"
        b = tmp_path / "repoB"
        a.mkdir()
        b.mkdir()
        pid_a = _make_member(a, ["alpha fact about widgets"])
        pid_b = _make_member(b, ["beta fact about gadgets"])
        add_family_members("e2efam", [str(a), str(b)])
        _fail_embed(monkeypatch)

        monkeypatch.setenv("BRAINCELL_FEDERATE", "on")
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", pid_a)
        monkeypatch.delenv("BRAINCELL_MODE", raising=False)

        b_db = get_db_path(pid_b)
        bytes_before = b_db.read_bytes()
        mtime_before = b_db.stat().st_mtime_ns

        store_a = SqliteStore(get_db_path(pid_a))
        try:
            notes = asyncio.run(
                server.recall_notes(store_a, "gadgets", scope="family")
            )
            assert any(n.project_id == pid_b for n in notes), (
                "sanity: the fan-out must actually have read B for this to be a real RO proof"
            )
        finally:
            store_a.close()

        assert b_db.read_bytes() == bytes_before, "sibling db bytes must be unchanged"
        assert b_db.stat().st_mtime_ns == mtime_before, "sibling db mtime must be unchanged"
