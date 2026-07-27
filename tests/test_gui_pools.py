# SPDX-License-Identifier: AGPL-3.0-or-later
"""Native Memory Map live Pool API boundaries."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _app(tmp_path, *, writes=True, seed="01CONNECTED"):
    from braincell.gui import create_app

    return create_app(
        db_path=tmp_path / "braincell.db",
        allow_writes=writes,
        seed_project_id=seed,
    )


def test_pool_membership_api_changes_metadata_only(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        created = client.post("/api/pools", json={"action": "create", "name": "Release"})
        added = client.post(
            "/api/pools",
            json={"action": "add", "name": "Release", "project_ids": ["01CONNECTED", "01B"]},
        )
        decoupled = client.post(
            "/api/pools",
            json={"action": "decouple", "name": "Release", "project_id": "01B"},
        )

    assert created.status_code == 200
    assert added.status_code == 200
    assert decoupled.status_code == 200
    assert decoupled.json()["pools"]["Release"] == ["01CONNECTED"]


def test_pool_metadata_read_is_available_without_writes(tmp_path):
    with TestClient(_app(tmp_path, writes=False)) as client:
        response = client.get("/api/pools")
        write = client.post("/api/pools", json={"action": "create", "name": "Nope"})
    assert response.status_code == 200
    assert write.status_code in (404, 405)


def test_live_pool_queries_require_connected_session(tmp_path):
    with TestClient(_app(tmp_path, seed=None)) as client:
        search = client.post("/api/pools/search", json={"pool": "Release", "query": "x"})
        recall = client.post("/api/pools/recall", json={"pool": "Release", "query": "x"})
    assert search.status_code == 409
    assert recall.status_code == 409


def test_pool_queries_are_explicit_and_unknown_pool_is_404(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        response = client.post("/api/pools/search", json={"pool": "Unknown", "query": "x"})
    assert response.status_code == 404


def test_pool_membership_rejects_browser_path_or_memory_fields(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        response = client.post(
            "/api/pools",
            json={"action": "create", "name": "Release", "path": "/tmp", "db": "/tmp/x"},
        )
    assert response.status_code == 422
