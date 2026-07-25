# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_gui_ingest.py — regression tests for braincell/gui_ingest.py.

Covers the ingestion-management endpoints the Memory-Map GUI uses:
/api/fs (folder browser), /api/ingest (+status), /api/clear, /api/schedule,
plus the pure schedule_due() logic. All offline: ingest jobs run a trivial
python -c subprocess via the command_for() test seam — never a real build.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Optional

from fastapi.testclient import TestClient

from tests.conftest import _insert_doc_and_chunk, make_store


def _app(
    tmp_path: Path,
    *,
    allow_writes: bool = True,
    auth_token: Optional[str] = None,
    native_bridge=None,
):
    from braincell.gui import create_app
    return create_app(
        db_path=tmp_path / "braincell.db",
        allow_writes=allow_writes,
        auth_token=auth_token,
        native_bridge=native_bridge,
    )


def _wait_done(client: TestClient, timeout_s: float = 10.0) -> dict:
    """Poll /api/ingest/status until the job leaves 'running'."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        job = client.get("/api/ingest/status").json()["job"]
        if job and job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("ingest job did not finish in time")


# ── /api/fs ───────────────────────────────────────────────────────────────────

class TestFsBrowse:
    def test_lists_directories_only(self, tmp_path):
        (tmp_path / "proj_a").mkdir()
        (tmp_path / "proj_b").mkdir()
        (tmp_path / ".hidden").mkdir()
        (tmp_path / "file.txt").write_text("x")
        with TestClient(_app(tmp_path)) as client:
            r = client.get("/api/fs", params={"path": str(tmp_path)})
        assert r.status_code == 200
        names = [d["name"] for d in r.json()["dirs"]]
        assert "proj_a" in names and "proj_b" in names
        assert ".hidden" not in names and "file.txt" not in names

    def test_response_shape(self, tmp_path):
        with TestClient(_app(tmp_path)) as client:
            data = client.get("/api/fs", params={"path": str(tmp_path)}).json()
        assert data["path"] == str(tmp_path)
        assert data["parent"] == str(tmp_path.parent)
        assert "home" in data

    def test_404_on_file(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        with TestClient(_app(tmp_path)) as client:
            assert client.get("/api/fs", params={"path": str(f)}).status_code == 404

    def test_absent_in_read_only_mode(self, tmp_path):
        with TestClient(_app(tmp_path, allow_writes=False)) as client:
            assert client.get("/api/fs").status_code == 404


# ── /api/pick-folder (Qt bridge) ───────────────────────────────────────────────

class TestPickFolderNative:
    class Bridge:
        def __init__(self, result):
            self.result = result

        def activate(self):
            return True

        def pick_folder(self):
            return self.result

    def test_bridge_absent(self, tmp_path):
        with TestClient(_app(tmp_path)) as client:
            r = client.post("/api/pick-folder")
        assert r.status_code == 200
        assert r.json()["unavailable"] is True

    def test_qt_bridge_returns_path(self, tmp_path):
        picked = tmp_path / "picked-dir"
        picked.mkdir()
        bridge = self.Bridge({"path": str(picked.resolve())})
        with TestClient(_app(tmp_path, native_bridge=bridge)) as client:
            r = client.post("/api/pick-folder")
        assert r.status_code == 200
        assert r.json() == {"path": str(picked.resolve())}

    def test_cancel_returns_cancelled(self, tmp_path):
        with TestClient(
            _app(tmp_path, native_bridge=self.Bridge({"cancelled": True}))
        ) as client:
            r = client.post("/api/pick-folder")
        assert r.status_code == 200
        assert r.json() == {"cancelled": True}

    def test_absent_in_read_only_mode(self, tmp_path):
        with TestClient(_app(tmp_path, allow_writes=False)) as client:
            assert client.post("/api/pick-folder").status_code in (404, 405)

    def test_token_required(self, tmp_path):
        with TestClient(_app(tmp_path, auth_token="s3cret")) as client:
            assert client.post("/api/pick-folder").status_code == 401


# ── /api/ingest ───────────────────────────────────────────────────────────────

class TestIngest:
    def test_job_runs_and_completes(self, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        proj.mkdir()
        app = _app(tmp_path)
        with TestClient(app) as client:
            mgr = app.state.ingest_manager
            monkeypatch.setattr(
                mgr, "command_for",
                lambda path: [sys.executable, "-c", f"print('built {proj.name}')"],
            )
            r = client.post("/api/ingest", json={"path": str(proj)})
            assert r.status_code == 200
            job = _wait_done(client)
        assert job["state"] == "done"
        assert job["returncode"] == 0
        assert any("built proj" in ln for ln in job["log"])
        assert job["path"] == str(proj.resolve())

    def test_failed_job_reports_error(self, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        proj.mkdir()
        app = _app(tmp_path)
        with TestClient(app) as client:
            mgr = app.state.ingest_manager
            monkeypatch.setattr(
                mgr, "command_for",
                lambda path: [sys.executable, "-c", "import sys;print('boom');sys.exit(3)"],
            )
            client.post("/api/ingest", json={"path": str(proj)})
            job = _wait_done(client)
        assert job["state"] == "error"
        assert job["returncode"] == 3

    def test_400_on_non_directory(self, tmp_path):
        with TestClient(_app(tmp_path)) as client:
            r = client.post("/api/ingest", json={"path": str(tmp_path / "nope")})
        assert r.status_code == 400

    def test_409_when_busy(self, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        proj.mkdir()
        app = _app(tmp_path)
        with TestClient(app) as client:
            mgr = app.state.ingest_manager
            monkeypatch.setattr(
                mgr, "command_for",
                lambda path: [sys.executable, "-c", "import time;time.sleep(1.2)"],
            )
            assert client.post("/api/ingest", json={"path": str(proj)}).status_code == 200
            assert client.post("/api/ingest", json={"path": str(proj)}).status_code == 409
            _wait_done(client)

    def test_status_null_before_any_job(self, tmp_path):
        with TestClient(_app(tmp_path)) as client:
            assert client.get("/api/ingest/status").json()["job"] is None

    def test_default_command_is_cli_build(self, tmp_path):
        from braincell.gui_ingest import IngestManager
        cmd = IngestManager().command_for("/some/path")
        assert cmd[1:] == ["-m", "braincell.cli", "build", "/some/path"]


# ── /api/ingest build flags (mode=global / reembed) ────────────────────────────

class TestIngestBuildFlags:
    """The flags append server-side AFTER the command_for() seam, so an argv-echo
    fake sees exactly what the real `braincell build` subprocess would receive."""

    _ECHO = "import sys;print('ARGS::' + ' '.join(sys.argv[1:]))"

    def _run_with(self, tmp_path, monkeypatch, body_extra: dict):
        proj = tmp_path / "proj"
        proj.mkdir()
        app = _app(tmp_path)
        with TestClient(app) as client:
            mgr = app.state.ingest_manager
            monkeypatch.setattr(
                mgr, "command_for",
                lambda path: [sys.executable, "-c", self._ECHO],
            )
            r = client.post("/api/ingest", json={"path": str(proj), **body_extra})
            assert r.status_code == 200
            job = _wait_done(client)
        assert job["state"] == "done"
        argline = next(ln for ln in job["log"] if ln.startswith("ARGS::"))
        return argline[len("ARGS::"):], job

    def test_global_and_reembed_appended(self, tmp_path, monkeypatch):
        args, job = self._run_with(
            tmp_path, monkeypatch, {"mode": "global", "reembed": True}
        )
        assert args == "--mode global --reembed"
        assert job["mode"] == "global"
        assert job["reembed"] is True

    def test_default_appends_nothing(self, tmp_path, monkeypatch):
        args, job = self._run_with(tmp_path, monkeypatch, {})
        assert args == ""
        assert job["mode"] == "project"
        assert job["reembed"] is False

    def test_invalid_mode_422(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        with TestClient(_app(tmp_path)) as client:
            r = client.post("/api/ingest", json={"path": str(proj), "mode": "bogus"})
        assert r.status_code == 422


# ── /api/clear ────────────────────────────────────────────────────────────────

class TestClear:
    def _seed(self, tmp_path, project_id: str) -> None:
        from braincell.project_registry import register_path
        register_path(str(tmp_path / "repo"), project_id)
        store = make_store(tmp_path)

        async def _w():
            await _insert_doc_and_chunk(
                store, project=project_id, doc_key="d1", text="hello world"
            )
            await store.remember(text="a note", kind="note", project=project_id)
            await store.aclose()

        asyncio.run(_w())

    def test_clear_docs_keeps_notes(self, tmp_path):
        pid = "01TESTPROJECTAAAAAAAAAAAAA"
        self._seed(tmp_path, pid)
        with TestClient(_app(tmp_path)) as client:
            r = client.post("/api/clear", json={"project_id": pid})
            assert r.status_code == 200
            body = r.json()
            assert body["docs_removed"] == 1
            assert body["notes_removed"] == 0
            projs = client.get("/api/projects").json()
        me = [p for p in projs if p["project_id"] == pid][0]
        assert me["docs"] == 0 and me["chunks"] == 0 and me["notes"] == 1

    def test_clear_including_notes(self, tmp_path):
        pid = "01TESTPROJECTBBBBBBBBBBBBB"
        self._seed(tmp_path, pid)
        with TestClient(_app(tmp_path)) as client:
            r = client.post("/api/clear", json={"project_id": pid, "include_notes": True})
            assert r.status_code == 200
            assert r.json()["notes_removed"] == 1
            projs = client.get("/api/projects").json()
        me = [p for p in projs if p["project_id"] == pid][0]
        assert me["notes"] == 0

    def test_404_unknown_project(self, tmp_path):
        with TestClient(_app(tmp_path)) as client:
            r = client.post("/api/clear", json={"project_id": "01NOPE"})
        assert r.status_code == 404

    def test_clear_removes_ledger(self, tmp_path):
        pid = "01TESTPROJECTCCCCCCCCCCCCC"
        self._seed(tmp_path, pid)
        ledger = tmp_path / "transcript_ingest_ledger.json"
        ledger.write_text("{}")
        with TestClient(_app(tmp_path)) as client:
            r = client.post("/api/clear", json={"project_id": pid})
        assert r.status_code == 200
        assert not ledger.exists()


# ── /api/schedule ─────────────────────────────────────────────────────────────

class TestSchedule:
    def test_set_list_remove_roundtrip(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        with TestClient(_app(tmp_path)) as client:
            r = client.post(
                "/api/schedule", json={"path": str(proj), "interval_minutes": 60}
            )
            assert r.status_code == 200
            scheds = client.get("/api/schedule").json()["schedules"]
            assert len(scheds) == 1
            assert scheds[0]["path"] == str(proj.resolve())
            assert scheds[0]["interval_minutes"] == 60
            # interval 0 removes
            client.post("/api/schedule", json={"path": str(proj), "interval_minutes": 0})
            assert client.get("/api/schedule").json()["schedules"] == []

    def test_persisted_to_disk(self, tmp_path):
        from braincell.gui_ingest import load_schedules, schedules_path
        proj = tmp_path / "proj"
        proj.mkdir()
        with TestClient(_app(tmp_path)) as client:
            client.post("/api/schedule", json={"path": str(proj), "interval_minutes": 5})
        assert schedules_path().exists()
        assert load_schedules()[0]["interval_minutes"] == 5

    def test_400_negative_interval(self, tmp_path):
        with TestClient(_app(tmp_path)) as client:
            r = client.post("/api/schedule", json={"path": "/x", "interval_minutes": -1})
        assert r.status_code == 400

    def test_schedule_due_logic(self):
        from braincell.gui_ingest import schedule_due
        now = 1_000_000.0
        assert schedule_due({"interval_minutes": 10, "last_run": None}, now)
        assert schedule_due({"interval_minutes": 10, "last_run": now - 601}, now)
        assert not schedule_due({"interval_minutes": 10, "last_run": now - 599}, now)
        assert not schedule_due({"interval_minutes": 0, "last_run": None}, now)


# ── SPA carries the new UI ────────────────────────────────────────────────────

class TestTemplateHasIngestUi:
    def test_html_has_modal_and_ingest_controls(self, tmp_path):
        with TestClient(_app(tmp_path)) as client:
            html = client.get("/").text
        for needle in (
            'id="modal-root"', "openIngestModal", "/api/fs", "/api/ingest",
            "/api/clear", "/api/schedule", "Clear memory", "Auto-build",
        ):
            assert needle in html, f"missing {needle!r} in SPA"

    def test_prompt_gone_from_new_pool(self, tmp_path):
        with TestClient(_app(tmp_path)) as client:
            html = client.get("/").text
        assert 'prompt("New pool name' not in html


class TestIngestLogStreaming:
    def test_log_streams_while_job_still_running(self, tmp_path, monkeypatch):
        """Child stdout must land in job.log DURING the run, not only at exit.

        The build child is spawned with PYTHONUNBUFFERED=1: with stdout piped
        (no tty) a Python child block-buffers, so pre-fix the GUI's live build
        log stayed empty for the entire run and only filled at completion —
        the owner-reported "I don't see the ingest happening" (2026-07-25).
        """
        proj = tmp_path / "proj"
        proj.mkdir()
        app = _app(tmp_path)
        with TestClient(app) as client:
            mgr = app.state.ingest_manager
            monkeypatch.setattr(
                mgr, "command_for",
                lambda path: [
                    sys.executable, "-c",
                    "import time; print('early-line'); time.sleep(30)",
                ],
            )
            client.post("/api/ingest", json={"path": str(proj)})
            deadline = time.time() + 8.0
            seen_early = False
            while time.time() < deadline:
                job = client.get("/api/ingest/status").json()["job"]
                if job["state"] != "running":
                    break
                if any("early-line" in ln for ln in job["log"]):
                    seen_early = True
                    break
                time.sleep(0.1)
            assert seen_early, (
                "child stdout must stream into job.log while the job runs "
                "(PYTHONUNBUFFERED in the child env) — not arrive only at exit"
            )
            if mgr._proc is not None:  # tear down the sleeping child
                mgr._proc.terminate()
            _wait_done(client)
