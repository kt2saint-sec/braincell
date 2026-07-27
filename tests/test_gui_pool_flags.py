# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for retired materialized-Pool GUI routes."""

from fastapi.testclient import TestClient


def test_normal_gui_never_mounts_materialized_pool_or_family_writes(tmp_path):
    from braincell.gui import create_app

    app = create_app(db_path=tmp_path / "braincell.db", allow_writes=True)
    with TestClient(app) as client:
        assert client.post("/api/pool", json={}).status_code == 404
        assert client.post("/api/family", json={}).status_code == 404
