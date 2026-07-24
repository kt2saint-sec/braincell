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
import hashlib
import sqlite3
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


# ── B1: read-only sibling views ───────────────────────────────────────────────

class TestRoSiblingViews:
    def _setup_two_brains(self, tmp_path: Path) -> None:
        _register(tmp_path, "projA", PID_A)
        _register(tmp_path, "projB", PID_B)
        _seed_brain(PID_A, notes=["launch-only alpha note"],
                    docs=[("doc-a", "alpha launch chunk")])
        _seed_brain(PID_B, notes=["sibling-only beta note"],
                    docs=[("doc-b", "beta sibling chunk")])

    def test_notes_sibling_filter_returns_siblings_real_rows(self, tmp_path):
        """projects=<sibling> serves the SIBLING's rows, not the launch db's."""
        self._setup_two_brains(tmp_path)
        with TestClient(_launch_app(PID_A)) as client:
            data = client.get(f"/api/notes?projects={PID_B}").json()
        contents = [n["content"] for n in data["notes"]]
        assert "sibling-only beta note" in contents
        assert "launch-only alpha note" not in contents

    def test_notes_launch_filter_uses_opened_store(self, tmp_path):
        self._setup_two_brains(tmp_path)
        with TestClient(_launch_app(PID_A)) as client:
            data = client.get(f"/api/notes?projects={PID_A}").json()
        contents = [n["content"] for n in data["notes"]]
        assert contents == ["launch-only alpha note"]

    def test_search_sibling_filter_returns_siblings_chunks(self, tmp_path):
        self._setup_two_brains(tmp_path)
        with _embedder_down():
            with TestClient(_launch_app(PID_A)) as client:
                data = client.get(f"/api/search?q=beta&projects={PID_B}").json()
        assert any(h["doc_key"] == "doc-b" for h in data["hits"])
        assert not any(h["doc_key"] == "doc-a" for h in data["hits"])

    def test_sibling_db_file_is_byte_untouched_by_views(self, tmp_path):
        """RO open — the sibling's braincell.db must not change by a single byte."""
        self._setup_two_brains(tmp_path)
        db_b = _brain_db(PID_B)
        before = hashlib.sha256(db_b.read_bytes()).hexdigest()
        with _embedder_down():
            with TestClient(_launch_app(PID_A)) as client:
                client.get(f"/api/notes?projects={PID_B}")
                client.get(f"/api/search?q=beta&projects={PID_B}")
        after = hashlib.sha256(db_b.read_bytes()).hexdigest()
        assert before == after, "sibling db mutated by a read-only view"

    def test_ro_open_rejects_writes(self, tmp_path):
        """A write attempt through the RO path must raise (query_only + mode=ro)."""
        self._setup_two_brains(tmp_path)
        from braincell.store import SqliteStore

        async def _attempt():
            store = SqliteStore(_brain_db(PID_B), read_only=True)
            try:
                with pytest.raises(sqlite3.OperationalError):
                    await store.remember("write into sibling", "note", PID_B)
            finally:
                await store.aclose()

        asyncio.run(_attempt())

    def test_unregistered_ulid_404_not_built(self, tmp_path):
        self._setup_two_brains(tmp_path)
        with TestClient(_launch_app(PID_A)) as client:
            r = client.get("/api/notes?projects=NOSUCHULID000")
        assert r.status_code == 404
        assert "not built" in r.json()["detail"]

    def test_registered_but_missing_db_404_not_built(self, tmp_path):
        self._setup_two_brains(tmp_path)
        _register(tmp_path, "projD", PID_D)  # registered, never built
        with _embedder_down():
            with TestClient(_launch_app(PID_A)) as client:
                r_notes = client.get(f"/api/notes?projects={PID_D}")
                r_search = client.get(f"/api/search?q=x&projects={PID_D}")
        assert r_notes.status_code == 404
        assert "not built" in r_notes.json()["detail"]
        assert r_search.status_code == 404
        assert "not built" in r_search.json()["detail"]

    def test_multi_ulid_filter_keeps_opened_db_behavior(self, tmp_path):
        """A multi-ULID list filters the opened db (today's behavior, unchanged)."""
        self._setup_two_brains(tmp_path)
        with TestClient(_launch_app(PID_A)) as client:
            data = client.get(f"/api/notes?projects={PID_A},{PID_B}").json()
        contents = [n["content"] for n in data["notes"]]
        # The opened (launch) db holds only A's rows.
        assert contents == ["launch-only alpha note"]

    def test_unseeded_app_keeps_filtering_opened_db(self, tmp_path):
        """Without a launch seed (global-mode / test app) the filter stays in-db."""
        from braincell.gui import create_app
        _register(tmp_path, "projA", PID_A)
        _register(tmp_path, "projB", PID_B)
        # One multi-project db (the global-brain shape), served without a seed.
        _seed_brain(PID_A, notes=["multi-db note A"])
        db = _brain_db(PID_A)
        from braincell.store import SqliteStore

        async def _add_b():
            store = SqliteStore(db)
            await store.remember("multi-db note B", "note", PID_B)
            await store.aclose()

        asyncio.run(_add_b())
        app = create_app(db_path=db, seed_project_id=None)
        with TestClient(app) as client:
            data = client.get(f"/api/notes?projects={PID_B}").json()
        assert [n["content"] for n in data["notes"]] == ["multi-db note B"]


# ── Write pinning ─────────────────────────────────────────────────────────────

class TestWritePinning:
    def test_forget_sibling_note_is_structural_noop(self, tmp_path):
        """POST /api/forget acts on the OPENED (launch) store only.

        With a sibling's (note_id, project) pair the launch db holds no such
        row → store.forget no-ops → deleted=false, and the sibling's note (and
        the launch note sharing the same integer id — the decoy) both survive.
        Pinned so a future refactor cannot make cross-project forget succeed.
        """
        _register(tmp_path, "projA", PID_A)
        _register(tmp_path, "projB", PID_B)
        ids_a = _seed_brain(PID_A, notes=["launch decoy note"])
        ids_b = _seed_brain(PID_B, notes=["sibling protected note"])
        # Fresh brains assign the same first id — exactly the decoy alignment
        # that makes an id-only (project-ignoring) forget detectable.
        assert ids_a[0] == ids_b[0]

        with TestClient(_launch_app(PID_A, allow_writes=True)) as client:
            r = client.post(
                "/api/forget", json={"note_id": ids_b[0], "project": PID_B}
            )
            assert r.status_code == 200
            assert r.json()["deleted"] is False

            sibling = client.get(f"/api/notes?projects={PID_B}").json()
            launch = client.get(f"/api/notes?projects={PID_A}").json()
        assert any(
            n["content"] == "sibling protected note" for n in sibling["notes"]
        ), "sibling note was affected by a launch-store forget"
        assert any(
            n["content"] == "launch decoy note" for n in launch["notes"]
        ), "launch decoy note (same integer id) was wrongly forgotten"


# ── B2: honest sibling counts in /api/projects ────────────────────────────────

class TestHonestSiblingCounts:
    def test_sibling_rows_carry_real_counts_in_project_mode(self, tmp_path):
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
        b = by_pid[PID_B]
        assert b["docs"] == 1
        assert b["chunks"] == 1
        assert b["notes"] == 2

    def test_corrupt_sibling_yields_zeros_not_500(self, tmp_path):
        _register(tmp_path, "projA", PID_A)
        _register(tmp_path, "projC", PID_C)
        _seed_brain(PID_A, notes=["a note"])
        db_c = _brain_db(PID_C)
        db_c.parent.mkdir(parents=True, exist_ok=True)
        db_c.write_bytes(b"this is not a sqlite database at all")
        with TestClient(_launch_app(PID_A)) as client:
            r = client.get("/api/projects")
        assert r.status_code == 200, "a corrupt sibling must never 500 the map"
        row = next(p for p in r.json() if p["project_id"] == PID_C)
        assert (row["docs"], row["chunks"], row["notes"]) == (0, 0, 0)

    def test_unbuilt_sibling_yields_zeros(self, tmp_path):
        _register(tmp_path, "projA", PID_A)
        _register(tmp_path, "projD", PID_D)  # registered, no db
        _seed_brain(PID_A, notes=["a note"])
        with TestClient(_launch_app(PID_A)) as client:
            data = client.get("/api/projects").json()
        row = next(p for p in data if p["project_id"] == PID_D)
        assert (row["docs"], row["chunks"], row["notes"]) == (0, 0, 0)

    def test_unseeded_app_does_not_enrich(self, tmp_path):
        """Sibling enrichment is project-mode only (no seed → no RO fan-out)."""
        from braincell.gui import create_app
        _register(tmp_path, "projA", PID_A)
        _register(tmp_path, "projB", PID_B)
        _seed_brain(PID_A, notes=["a note"])
        _seed_brain(PID_B, notes=["b note"])
        app = create_app(db_path=_brain_db(PID_A), seed_project_id=None)
        with TestClient(app) as client:
            data = client.get("/api/projects").json()
        row = next(p for p in data if p["project_id"] == PID_B)
        assert row["notes"] == 0


# ── B3: active-seeded federation ──────────────────────────────────────────────

class TestActiveSeededFederation:
    def _setup_family(self, tmp_path: Path) -> None:
        from braincell.project_registry import add_family_members
        root_a = _register(tmp_path, "projA", PID_A)
        root_b = _register(tmp_path, "projB", PID_B)
        add_family_members("actfam", [str(root_a), str(root_b)])
        # DIFFERENT content per member — the decoy trap: if the launch store
        # were wrongly passed as self for a seed=B plan, target B would query
        # A's db (which holds no B rows) and B's note would vanish from the
        # merge, failing loudly instead of passing by coincidence.
        _seed_brain(PID_A, notes=["family note only in launch A"],
                    docs=[("doc-a", "alphaword launch chunk")])
        _seed_brain(PID_B, notes=["family note only in sibling B"],
                    docs=[("doc-b", "betaword sibling chunk")])

    def test_seeded_family_notes_read_the_seed_db_as_self(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BRAINCELL_FEDERATE", "on")
        self._setup_family(tmp_path)
        with TestClient(_launch_app(PID_A)) as client:
            data = client.get(f"/api/notes?federate=true&seed={PID_B}").json()
        contents = [n["content"] for n in data["notes"]]
        assert "family note only in sibling B" in contents, (
            "seed=B must read B's own db as self — its rows are missing"
        )
        assert "family note only in launch A" in contents, (
            "family fan-out must still include the launch member"
        )

    def test_seeded_family_ranks_seed_first_under_active_weight(self, tmp_path, monkeypatch):
        """RRF seed preference must apply to the ?seed= project, not the launch one."""
        monkeypatch.setenv("BRAINCELL_FEDERATE", "on")
        monkeypatch.setenv("BRAINCELL_RRF_WEIGHT_ACTIVE", "2.0")
        self._setup_family(tmp_path)
        with TestClient(_launch_app(PID_A)) as client:
            data = client.get(f"/api/notes?federate=true&seed={PID_B}").json()
        assert data["notes"][0]["content"] == "family note only in sibling B", (
            "with an active-weight prior, the SEED's (B's) list must rank first"
        )

    def test_seeded_family_search_reads_the_seed_db_as_self(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BRAINCELL_FEDERATE", "on")
        self._setup_family(tmp_path)
        with _embedder_down():
            with TestClient(_launch_app(PID_A)) as client:
                data = client.get(
                    f"/api/search?q=betaword&federate=true&seed={PID_B}"
                ).json()
        assert any(h["doc_key"] == "doc-b" for h in data["hits"]), (
            "seed=B federated search must surface B's chunk from B's own db"
        )

    def test_launch_seed_param_matches_default_behavior(self, tmp_path, monkeypatch):
        """seed=<launch pid> is the same view as no seed at all."""
        monkeypatch.setenv("BRAINCELL_FEDERATE", "on")
        self._setup_family(tmp_path)
        with TestClient(_launch_app(PID_A)) as client:
            explicit = client.get(f"/api/notes?federate=true&seed={PID_A}").json()
            implicit = client.get("/api/notes?federate=true").json()
        assert explicit["notes"] == implicit["notes"]

    def test_unknown_seed_404(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BRAINCELL_FEDERATE", "on")
        self._setup_family(tmp_path)
        with TestClient(_launch_app(PID_A)) as client:
            r = client.get("/api/notes?federate=true&seed=NOSUCHULID000")
        assert r.status_code == 404

    def test_registered_unbuilt_seed_404_not_built(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BRAINCELL_FEDERATE", "on")
        self._setup_family(tmp_path)
        _register(tmp_path, "projD", PID_D)  # registered, never built
        with TestClient(_launch_app(PID_A)) as client:
            r = client.get(f"/api/notes?federate=true&seed={PID_D}")
        assert r.status_code == 404
        assert "not built" in r.json()["detail"]

    def test_seed_without_federate_does_not_switch_view(self, tmp_path, monkeypatch):
        """seed= is a federation re-seed only; the plain path stays the opened db."""
        monkeypatch.setenv("BRAINCELL_FEDERATE", "on")
        self._setup_family(tmp_path)
        with TestClient(_launch_app(PID_A)) as client:
            data = client.get(f"/api/notes?seed={PID_B}").json()
        contents = [n["content"] for n in data["notes"]]
        assert contents == ["family note only in launch A"]

    def test_seed_db_untouched_by_federated_view(self, tmp_path, monkeypatch):
        """The seed's db is opened read-only — byte-identical after the query."""
        monkeypatch.setenv("BRAINCELL_FEDERATE", "on")
        self._setup_family(tmp_path)
        db_b = _brain_db(PID_B)
        before = hashlib.sha256(db_b.read_bytes()).hexdigest()
        with TestClient(_launch_app(PID_A)) as client:
            client.get(f"/api/notes?federate=true&seed={PID_B}")
        after = hashlib.sha256(db_b.read_bytes()).hexdigest()
        assert before == after


# ── D: /api/config launch_project_id alias ────────────────────────────────────

class TestConfigLaunchProjectId:
    def test_seeded_config_exposes_launch_alias(self, tmp_path):
        from braincell.gui import create_app
        app = create_app(
            db_path=tmp_path / "braincell.db", seed_project_id="SEEDPID00001"
        )
        with TestClient(app) as client:
            data = client.get("/api/config").json()
        assert data["seed_project_id"] == "SEEDPID00001"
        assert data["launch_project_id"] == "SEEDPID00001"
        assert data["federate_available"] is True

    def test_unseeded_config_has_null_launch_alias(self, tmp_path):
        from braincell.gui import create_app
        app = create_app(db_path=tmp_path / "braincell.db")
        with TestClient(app) as client:
            data = client.get("/api/config").json()
        assert data["seed_project_id"] is None
        assert data["launch_project_id"] is None
        assert data["federate_available"] is False


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
