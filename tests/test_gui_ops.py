# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_gui_ops.py — regression tests for braincell/gui_ops.py.

Covers the maintenance-command endpoints the Memory-Map GUI uses:
/api/ops/{consolidate,reflect,contradictions,reembed-notes} (+status),
/api/backup, /api/memory (log) and /api/memory/undo — plus the SPA markup
for the ⌘ Commands panel and the forget/uninstall orphan wirings.

All offline: no Ollama (contradictions runs no_llm; consolidate uses the
deterministic merge; reembed patches gui_ops.embed_texts). Jobs run in a
worker thread against the same tmp db, polled via /api/ops/status exactly
like the SPA does.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import fake_vec, make_store


def test_ingest_and_maintenance_share_one_mutation_coordinator():
    from braincell.gui_ingest import IngestManager
    from braincell.gui_mutation import GuiMutationCoordinator
    from braincell.gui_ops import OpsJobManager

    coordinator = GuiMutationCoordinator()
    ingest = IngestManager(coordinator)
    ops = OpsJobManager(coordinator)

    async def _run():
        ingest.command_for = lambda _path: [
            sys.executable,
            "-c",
            "import time; time.sleep(0.2)",
        ]
        await ingest.start("/tmp/project")
        with pytest.raises(RuntimeError, match="already running"):
            await ops.start("reembed-notes", lambda: None)
        await ingest.wait()

    asyncio.run(_run())


def _app(tmp_path: Path, *, allow_writes: bool = True):
    from braincell.gui import create_app
    return create_app(db_path=tmp_path / "braincell.db", allow_writes=allow_writes)


def _register(tmp_path: Path, pid: str) -> Path:
    from braincell.project_registry import register_path
    root = tmp_path / f"repo-{pid}"
    root.mkdir(exist_ok=True)
    register_path(str(root), pid)
    return root


def _seed_notes(tmp_path: Path, pid: str, texts: list[str], *, seed=None) -> list[int]:
    """Seed notes; seed=None → NULL embedding, int → identical fake vectors."""
    store = make_store(tmp_path)

    async def _w():
        ids = []
        for t in texts:
            emb = fake_vec(seed) if seed is not None else None
            nid = await store.remember(text=t, kind="note", project=pid, embedding=emb)
            ids.append(int(nid))
        await store.aclose()
        return ids

    return asyncio.run(_w())


def _wait_op(client: TestClient, timeout_s: float = 15.0) -> dict:
    """Poll /api/ops/status until the job leaves 'running' (the SPA's loop)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        job = client.get("/api/ops/status").json()["job"]
        if job and job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("ops job did not finish in time")


# ── Read-only gating: every route absent without --allow-writes ───────────────

class TestOpsGating:
    def test_all_ops_routes_absent_read_only(self, tmp_path):
        with TestClient(_app(tmp_path, allow_writes=False)) as client:
            for path, body in (
                ("/api/ops/consolidate", {"project_id": "X"}),
                ("/api/ops/reflect", {"project_id": "X"}),
                ("/api/ops/contradictions", {"project_id": "X"}),
                ("/api/ops/reembed-notes", {"project_id": "X"}),
                ("/api/backup", {}),
                ("/api/memory/undo", {"op_id": 1, "project_id": "X"}),
            ):
                assert client.post(path, json=body).status_code in (404, 405), path
            assert client.get("/api/ops/status").status_code in (404, 405)
            assert client.get("/api/memory?project_id=X").status_code in (404, 405)

    def test_unknown_project_404(self, tmp_path):
        with TestClient(_app(tmp_path)) as client:
            for path in ("/api/ops/consolidate", "/api/ops/reflect",
                         "/api/ops/contradictions", "/api/ops/reembed-notes"):
                r = client.post(path, json={"project_id": "01NOPE"})
                assert r.status_code == 404, path
            assert client.get("/api/memory?project_id=01NOPE").status_code == 404
            r = client.post("/api/memory/undo", json={"op_id": 1, "project_id": "01NOPE"})
            assert r.status_code == 404

    def test_extra_body_field_rejected(self, tmp_path):
        """extra=forbid — a smuggled field is a 422, mirroring gui_install."""
        with TestClient(_app(tmp_path)) as client:
            r = client.post("/api/ops/consolidate",
                            json={"project_id": "X", "command": "evil"})
        assert r.status_code == 422

    def test_busy_409(self, tmp_path, monkeypatch):
        pid = "01OPSBUSYAAAAAAAAAAAAAAAAA"
        _register(tmp_path, pid)
        from braincell import gui_ops
        monkeypatch.setattr(
            gui_ops, "run_consolidate",
            lambda *a, **k: time.sleep(0.8) or {"slow": True},
        )
        with TestClient(_app(tmp_path)) as client:
            assert client.post("/api/ops/consolidate",
                               json={"project_id": pid}).status_code == 200
            assert client.post("/api/ops/consolidate",
                               json={"project_id": pid}).status_code == 409
            _wait_op(client)


# ── consolidate ────────────────────────────────────────────────────────────────

class TestOpsConsolidate:
    def test_dry_run_reports_clusters_and_writes_nothing(self, tmp_path):
        pid = "01OPSCONSAAAAAAAAAAAAAAAAA"
        _register(tmp_path, pid)
        _seed_notes(tmp_path, pid, ["dup one", "dup two"], seed=5)  # cosine 1.0
        with TestClient(_app(tmp_path)) as client:
            r = client.post("/api/ops/consolidate",
                            json={"project_id": pid, "apply": False})
            assert r.status_code == 200
            job = _wait_op(client)
            assert job["state"] == "done", "\n".join(job["log"])
            assert any("cluster" in ln for ln in job["log"])
            # nothing written: both notes still live, no operation recorded
            notes = client.get(f"/api/notes?projects={pid}").json()["notes"]
            assert len(notes) == 2
            ops = client.get(f"/api/memory?project_id={pid}").json()["operations"]
            assert ops == []

    def test_apply_merges_backs_up_and_is_undoable(self, tmp_path):
        pid = "01OPSCONSBBBBBBBBBBBBBBBBB"
        _register(tmp_path, pid)
        _seed_notes(tmp_path, pid, ["dup one", "dup two"], seed=6)
        with TestClient(_app(tmp_path)) as client:
            r = client.post("/api/ops/consolidate",
                            json={"project_id": pid, "apply": True})
            assert r.status_code == 200
            job = _wait_op(client)
            assert job["state"] == "done", "\n".join(job["log"])
            assert job["result"]["applied"] is True
            # pre-merge backup written beside the db (the CLI's discipline)
            backups = list(tmp_path.glob("braincell-preconsolidate-*.db"))
            assert backups, "expected an auto pre-merge backup"
            assert job["result"]["backup"] in [str(b) for b in backups]
            # one note tombstoned → only one live note left
            notes = client.get(f"/api/notes?projects={pid}").json()["notes"]
            assert len(notes) == 1
            # operation recorded in the memory log
            ops = client.get(f"/api/memory?project_id={pid}").json()["operations"]
            assert len(ops) == 1
            op = ops[0]
            assert op["kind"] == "consolidate"
            assert op["undone_at"] is None
            # undo via the endpoint restores the tombstoned note
            r = client.post("/api/memory/undo",
                            json={"op_id": op["id"], "project_id": pid})
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is True
            assert len(body["restored"]) == 1
            notes = client.get(f"/api/notes?projects={pid}").json()["notes"]
            assert len(notes) == 2
            # log now shows it undone; re-undo refuses (409)
            ops = client.get(f"/api/memory?project_id={pid}").json()["operations"]
            assert ops[0]["undone_at"] is not None
            r = client.post("/api/memory/undo",
                            json={"op_id": op["id"], "project_id": pid})
            assert r.status_code == 409


# ── reflect (dry-run only — apply needs an LLM; core is covered elsewhere) ────

class TestOpsReflect:
    def test_dry_run_previews_clusters(self, tmp_path):
        pid = "01OPSREFLAAAAAAAAAAAAAAAAA"
        _register(tmp_path, pid)
        _seed_notes(tmp_path, pid, ["related a", "related b"], seed=9)
        with TestClient(_app(tmp_path)) as client:
            r = client.post("/api/ops/reflect",
                            json={"project_id": pid, "apply": False})
            assert r.status_code == 200
            job = _wait_op(client)
            assert job["state"] == "done", "\n".join(job["log"])
            assert job["result"]["clusters_considered"] == 1
            assert job["result"]["applied"] is False
            ops = client.get(f"/api/memory?project_id={pid}").json()["operations"]
            assert ops == []


# ── contradictions ─────────────────────────────────────────────────────────────

class TestOpsContradictions:
    def test_no_llm_lists_pairs_unjudged(self, tmp_path):
        pid = "01OPSCTRDAAAAAAAAAAAAAAAAA"
        _register(tmp_path, pid)
        _seed_notes(tmp_path, pid, ["close claim one", "close claim two"], seed=11)
        with TestClient(_app(tmp_path)) as client:
            r = client.post("/api/ops/contradictions",
                            json={"project_id": pid, "no_llm": True})
            assert r.status_code == 200
            job = _wait_op(client)
        assert job["state"] == "done", "\n".join(job["log"])
        res = job["result"]
        assert res["notes_scanned"] == 2
        assert res["pairs_over_threshold"] == 1
        assert res["pairs"][0]["verdict"] == "unjudged"
        assert res["pairs_judged"] == 0


# ── reembed-notes ──────────────────────────────────────────────────────────────

class TestOpsReembed:
    def test_backfills_null_embeddings(self, tmp_path, monkeypatch):
        pid = "01OPSREEMAAAAAAAAAAAAAAAAA"
        _register(tmp_path, pid)
        _seed_notes(tmp_path, pid, ["no vector 1", "no vector 2"])  # NULL embeddings
        from braincell import gui_ops
        monkeypatch.setattr(
            gui_ops, "embed_texts", lambda texts: [fake_vec(3) for _ in texts]
        )
        with TestClient(_app(tmp_path)) as client:
            r = client.post("/api/ops/reembed-notes", json={"project_id": pid})
            assert r.status_code == 200
            job = _wait_op(client)
        assert job["state"] == "done", "\n".join(job["log"])
        assert job["result"]["reembedded"] == 2


# ── /api/backup ────────────────────────────────────────────────────────────────

class TestBackup:
    def test_backup_writes_snapshot(self, tmp_path):
        pid = "01OPSBKUPAAAAAAAAAAAAAAAAA"
        _seed_notes(tmp_path, pid, ["worth keeping"])
        with TestClient(_app(tmp_path)) as client:
            r = client.post("/api/backup", json={})
        assert r.status_code == 200
        dest = Path(r.json()["path"])
        assert dest.exists() and dest.stat().st_size > 0
        assert dest.parent == tmp_path
        assert dest.name.startswith("braincell-backup-")

    def test_backup_snapshot_is_a_readable_brain(self, tmp_path):
        pid = "01OPSBKUPBBBBBBBBBBBBBBBBB"
        _seed_notes(tmp_path, pid, ["survives the copy"])
        with TestClient(_app(tmp_path)) as client:
            dest = Path(client.post("/api/backup", json={}).json()["path"])
        import sqlite3
        con = sqlite3.connect(str(dest))
        try:
            n = con.execute("SELECT COUNT(*) FROM memory_notes").fetchone()[0]
        finally:
            con.close()
        assert n == 1


# ── /api/memory (log) ──────────────────────────────────────────────────────────

class TestMemoryLog:
    def test_empty_log(self, tmp_path):
        pid = "01OPSMLOGAAAAAAAAAAAAAAAAA"
        _register(tmp_path, pid)
        with TestClient(_app(tmp_path)) as client:
            data = client.get(f"/api/memory?project_id={pid}").json()
        assert data == {"operations": []}

    def test_undo_unknown_op_409(self, tmp_path):
        pid = "01OPSMLOGBBBBBBBBBBBBBBBBB"
        _register(tmp_path, pid)
        with TestClient(_app(tmp_path)) as client:
            r = client.post("/api/memory/undo",
                            json={"op_id": 999, "project_id": pid})
        assert r.status_code == 409


# ── SPA carries the Commands panel + the two orphan wirings ───────────────────

class TestTemplateCommandsPanel:
    def test_html_has_commands_button_and_endpoints(self, tmp_path):
        with TestClient(_app(tmp_path)) as client:
            html = client.get("/").text
        for needle in (
            'id="cmd-btn"', "openCommandsModal",
            "/api/ops/", "/api/ops/status", "/api/backup",
            "/api/memory", "/api/memory/undo",
            "cmdConsolidate", "cmdReflect", "cmdContradictions",
            "cmdReembed", "cmdBackup", "cmdMemLog", "cmdMemUndo",
        ):
            assert needle in html, f"missing {needle!r} in SPA"

    def test_html_accounts_for_cli_only_commands(self, tmp_path):
        """serve/gui/register must be listed with a run-from-CLI note, not omitted."""
        with TestClient(_app(tmp_path)) as client:
            html = client.get("/").text
        assert "Run from the CLI" in html
        for cmd in ("serve", "register"):
            assert f'<div class="k">{cmd}</div>' in html, f"missing CLI note for {cmd}"

    def test_forget_orphan_now_wired(self, tmp_path):
        """POST /api/forget existed with zero frontend refs — now wired + confirmed."""
        with TestClient(_app(tmp_path)) as client:
            html = client.get("/").text
        assert "/api/forget" in html, "forget endpoint not referenced by the SPA"
        assert "confirmForgetNote" in html, "forget lacks its confirm step"
        assert "doForgetNote" in html

    def test_uninstall_orphan_now_wired(self, tmp_path):
        """POST /api/uninstall existed with zero frontend refs — now wired + confirmed."""
        with TestClient(_app(tmp_path)) as client:
            html = client.get("/").text
        assert "/api/uninstall" in html, "uninstall endpoint not referenced by the SPA"
        assert "cmdUninstall" in html
        assert "cmd-un-client" in html and "cmd-un-scope" in html

    def test_write_controls_disabled_not_hidden_read_only(self, tmp_path):
        """Convention: read-only DISABLES write controls with an explanatory title."""
        with TestClient(_app(tmp_path, allow_writes=False)) as client:
            html = client.get("/").text
        # The commands panel and its run handlers ship in every mode; the
        # gating string used by wdis()/requireWrites must be present verbatim.
        assert 'id="cmd-btn"' in html
        assert "read-only: launch with --allow-writes" in html
