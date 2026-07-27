# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
test_gui_active_memory.py — active-project-memory backend (plan Phases B + D).

Covers, per the active-project-memory plan §8:
  - B1  RO sibling views: /api/notes + /api/search with projects=<sibling>
        serve the sibling's own db read-only; 404 "not built" when
        unregistered / db missing; sibling file byte-untouched; a write via
        the RO path raises.
  - Write pinning: POST /api/forget with a sibling's (note_id, project)
        against the launch store is a structural no-op (deleted=false).
  - B2  Honest sibling counts in /api/projects (project-mode launches);
        corrupt sibling → zeros + 200, never a 500.
  - B3  Active-seeded federation: federate=true&seed=<ulid> builds the plan
        for the SEED and reads the seed's db as self.  Decoy trap per the
        test_pool_sync remap lesson: A and B carry DIFFERENT note content so
        a wrong-db self read is detectable, never coincidentally right.
  - D   /api/config exposes launch_project_id (alias of seed_project_id).
  - Invariant: server.py never imports gui modules; BRAINCELL_MODE is never
        assigned inside the package.

All offline (TestClient; embedder patched down where a query embed would
fire).  Per-project brains are created at config.get_db_path(pid) — the same
path the GUI resolves — under the conftest-isolated XDG tree.  Stores are
seeded and aclosed inside a single asyncio.run() each (never reused across
two runs).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import _insert_doc_and_chunk

PID_A = "ACTMEMLAUNCHA01"  # launch project (owns the opened store)
PID_B = "ACTMEMSIBLINGB2"  # built sibling
PID_C = "ACTMEMCORRUPTC3"  # corrupt sibling (B2)
PID_D = "ACTMEMUNBUILTD4"  # registered, never built


# ── Helpers ───────────────────────────────────────────────────────────────────

def _register(tmp_path: Path, name: str, pid: str) -> Path:
    """Register tmp_path/name → pid in the path registry; return the dir."""
    from braincell.project_registry import register_path
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    register_path(str(root), pid)
    return root


def _brain_db(pid: str) -> Path:
    """The per-project db path the GUI itself resolves for pid."""
    from braincell.config import get_db_path
    return get_db_path(pid)


def _seed_brain(pid: str, *, notes: list[str] = (), docs: list[tuple[str, str]] = ()) -> list[int]:
    """Create pid's brain at its registry-resolved path; seed notes/docs.

    docs is a list of (doc_key, text).  Returns the inserted note ids.  The
    store is opened and aclosed inside ONE asyncio.run (per conftest policy).
    """
    from braincell.store import SqliteStore
    db = _brain_db(pid)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.assert_schema_version()

    async def _write() -> list[int]:
        ids = []
        for text in notes:
            ids.append(int(await store.remember(text, "note", pid)))
        for i, (doc_key, text) in enumerate(docs):
            await _insert_doc_and_chunk(
                store, project=pid, doc_key=doc_key, text=text, seed=i + 1
            )
        await store.aclose()
        return ids

    return asyncio.run(_write())


def _launch_app(pid: str, *, allow_writes: bool = False):
    """A GUI app launched project-mode on pid's own brain (seed = pid)."""
    from braincell.gui import create_app
    return create_app(
        db_path=_brain_db(pid), allow_writes=allow_writes, seed_project_id=pid
    )


def _embedder_down():
    """Patch the GUI's embedder to be unavailable (keyword/recency fallback)."""
    return patch(
        "braincell.gui.embed_query_async", side_effect=RuntimeError("Ollama down")
    )


# ── Connected-Project reads and retired selector boundaries ─────────────────

class TestConnectedProjectViews:
    def _setup_two_brains(self, tmp_path: Path) -> None:
        _register(tmp_path, "projA", PID_A)
        _register(tmp_path, "projB", PID_B)
        _seed_brain(PID_A, notes=["launch-only alpha note"],
                    docs=[("doc-a", "alpha launch chunk")])
        _seed_brain(PID_B, notes=["sibling-only beta note"],
                    docs=[("doc-b", "beta sibling chunk")])

    def test_default_notes_and_search_use_connected_store(self, tmp_path):
        self._setup_two_brains(tmp_path)
        with _embedder_down(), TestClient(_launch_app(PID_A)) as client:
            notes = client.get("/api/notes").json()["notes"]
            hits = client.get("/api/search?q=alpha").json()["hits"]
        assert [note["content"] for note in notes] == ["launch-only alpha note"]
        assert [hit["doc_key"] for hit in hits] == ["doc-a"]

    @pytest.mark.parametrize(
        "url",
        [
            f"/api/notes?projects={PID_B}",
            f"/api/search?q=beta&projects={PID_B}",
            f"/api/notes?projects={PID_A},{PID_B}",
            "/api/notes?federate=true",
            f"/api/search?q=beta&seed={PID_B}",
        ],
    )
    def test_retired_cross_project_selectors_are_rejected(self, tmp_path, url):
        self._setup_two_brains(tmp_path)
        with TestClient(_launch_app(PID_A)) as client:
            response = client.get(url)
        assert response.status_code == 400
        assert "Pool" in response.json()["detail"]


# ── Write pinning ─────────────────────────────────────────────────────────────

class TestWritePinning:
    def test_forget_sibling_project_is_rejected_before_mutation(self, tmp_path):
        _register(tmp_path, "projA", PID_A)
        _register(tmp_path, "projB", PID_B)
        ids_a = _seed_brain(PID_A, notes=["launch decoy note"])
        ids_b = _seed_brain(PID_B, notes=["sibling protected note"])
        assert ids_a[0] == ids_b[0]
        before_a = _brain_db(PID_A).read_bytes()
        before_b = _brain_db(PID_B).read_bytes()

        with TestClient(_launch_app(PID_A, allow_writes=True)) as client:
            r = client.post(
                "/api/forget", json={"note_id": ids_b[0], "project": PID_B}
            )
        assert r.status_code == 409
        assert "connected Project" in r.json()["detail"]
        assert _brain_db(PID_A).read_bytes() == before_a
        assert _brain_db(PID_B).read_bytes() == before_b


# ── Metadata-only Project catalog ────────────────────────────────────────────

class TestProjectCatalogMetadata:
    def test_sibling_rows_do_not_read_sibling_counts(self, tmp_path):
        _register(tmp_path, "projA", PID_A)
        _register(tmp_path, "projB", PID_B)
        _seed_brain(PID_A, notes=["a note"])
        _seed_brain(
            PID_B,
            notes=["b note 1", "b note 2"],
            docs=[("doc-b", "sibling doc text")],
        )
        with TestClient(_launch_app(PID_A)) as client:
            data = client.get("/api/projects").json()
        by_pid = {p["project_id"]: p for p in data}
        assert by_pid[PID_A]["notes"] == 1
        assert (
            by_pid[PID_B]["docs"],
            by_pid[PID_B]["chunks"],
            by_pid[PID_B]["notes"],
        ) == (0, 0, 0)


# ── Project-only bootstrap config ────────────────────────────────────────────

class TestConfigConnectedProject:
    def test_config_exposes_only_connected_project_binding(self, tmp_path):
        from braincell.gui import create_app
        app = create_app(
            db_path=tmp_path / "braincell.db", seed_project_id="SEEDPID00001"
        )
        with TestClient(app) as client:
            data = client.get("/api/config").json()
        assert data["seed_project_id"] == "SEEDPID00001"
        assert data["connected_project_id"] == "SEEDPID00001"
        assert "launch_project_id" not in data
        assert "federate_available" not in data


# ── Invariant regression ──────────────────────────────────────────────────────

class TestScopeInvariants:
    def test_server_module_never_imports_gui(self):
        """server.py stays byte-independent of the GUI feature (plan invariant)."""
        import braincell
        src = (Path(braincell.__file__).parent / "server.py").read_text(
            encoding="utf-8"
        )
        assert "from .gui" not in src
        assert "import gui" not in src
        assert "braincell.gui" not in src

    def test_braincell_mode_is_never_assigned_in_package(self):
        """BRAINCELL_MODE is only ever read — no code path promotes to global."""
        import re

        import braincell
        pattern = re.compile(
            r"environ\[\s*['\"]BRAINCELL_MODE['\"]\s*\]\s*=|putenv\(\s*['\"]BRAINCELL_MODE"
        )
        pkg = Path(braincell.__file__).parent
        offenders = [
            p.name
            for p in pkg.rglob("*.py")
            if pattern.search(p.read_text(encoding="utf-8"))
        ]
        assert offenders == [], f"BRAINCELL_MODE assigned in: {offenders}"
