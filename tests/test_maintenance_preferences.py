# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""Per-Project destructive-maintenance preference regressions."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

PROJECT_ID = "01MAINTPREF000000000000000"
ACKNOWLEDGEMENT = (
    "ENABLING THIS FEATURE MEANS I AGREE BRAINCELL IS NOT RESPONSIBLE "
    "SINCE I WAS ADVISED OF RISKS"
)


def test_preferences_default_to_typed_delete_without_writing_state():
    from braincell.config import get_maintenance_preferences_path
    from braincell.maintenance_preferences import load_preferences

    path = get_maintenance_preferences_path(PROJECT_ID)
    assert load_preferences(PROJECT_ID) == {"bypass_delete_confirmation": False}
    assert not path.exists(), "reading defaults must not create state"


def test_enabling_bypass_requires_exact_acknowledgement():
    from braincell.maintenance_preferences import (
        MaintenancePreferencesError,
        set_bypass_delete_confirmation,
    )

    with pytest.raises(MaintenancePreferencesError, match="acknowledgement"):
        set_bypass_delete_confirmation(PROJECT_ID, True, acknowledgement="DELETE")

    saved = set_bypass_delete_confirmation(
        PROJECT_ID, True, acknowledgement=ACKNOWLEDGEMENT
    )
    assert saved == {"bypass_delete_confirmation": True}


def test_preferences_are_per_project_and_disabling_needs_no_acknowledgement():
    from braincell.maintenance_preferences import (
        load_preferences,
        set_bypass_delete_confirmation,
    )

    other_project = "01MAINTPREF111111111111111"
    set_bypass_delete_confirmation(
        PROJECT_ID, True, acknowledgement=ACKNOWLEDGEMENT
    )

    assert load_preferences(other_project) == {"bypass_delete_confirmation": False}
    assert set_bypass_delete_confirmation(PROJECT_ID, False) == {
        "bypass_delete_confirmation": False
    }


def test_corrupt_preferences_fail_closed_and_are_not_overwritten():
    from braincell.config import get_maintenance_preferences_path
    from braincell.maintenance_preferences import (
        MaintenancePreferencesError,
        load_preferences,
        set_bypass_delete_confirmation,
    )

    path = get_maintenance_preferences_path(PROJECT_ID)
    path.parent.mkdir(parents=True)
    original = b"{ definitely not valid json"
    path.write_bytes(original)

    with pytest.raises(MaintenancePreferencesError, match="unreadable"):
        load_preferences(PROJECT_ID)
    with pytest.raises(MaintenancePreferencesError, match="unreadable"):
        set_bypass_delete_confirmation(
            PROJECT_ID, True, acknowledgement=ACKNOWLEDGEMENT
        )
    assert path.read_bytes() == original


def test_gui_preference_api_is_connected_project_scoped(tmp_path):
    from braincell.gui import create_app
    from braincell.project_registry import register_path

    root = tmp_path / "project"
    root.mkdir()
    register_path(root, PROJECT_ID)
    app = create_app(
        db_path=tmp_path / "braincell.db",
        allow_writes=True,
        seed_project_id=PROJECT_ID,
    )

    with TestClient(app) as client:
        assert client.get("/api/preferences/maintenance").json() == {
            "bypass_delete_confirmation": False
        }
        refused = client.put(
            "/api/preferences/maintenance",
            json={"bypass_delete_confirmation": True, "acknowledgement": "DELETE"},
        )
        assert refused.status_code == 409
        enabled = client.put(
            "/api/preferences/maintenance",
            json={
                "bypass_delete_confirmation": True,
                "acknowledgement": ACKNOWLEDGEMENT,
            },
        )
        assert enabled.status_code == 200
        assert enabled.json() == {"bypass_delete_confirmation": True}
        disabled = client.put(
            "/api/preferences/maintenance",
            json={"bypass_delete_confirmation": False},
        )
        assert disabled.status_code == 200
        assert disabled.json() == {"bypass_delete_confirmation": False}


def test_gui_preference_api_is_not_available_read_only(tmp_path):
    from braincell.gui import create_app

    app = create_app(db_path=tmp_path / "braincell.db", allow_writes=False)
    with TestClient(app) as client:
        assert client.put(
            "/api/preferences/maintenance",
            json={"bypass_delete_confirmation": False},
        ).status_code in (404, 405)


def test_gui_maintenance_overview_is_connected_project_read_only(tmp_path):
    """The Memory Map may inspect only its Connected Project's health."""
    from braincell.gui import create_app
    from braincell.project_registry import register_path

    root = tmp_path / "project"
    root.mkdir()
    register_path(root, PROJECT_ID)
    app = create_app(
        db_path=tmp_path / "braincell.db",
        allow_writes=False,
        seed_project_id=PROJECT_ID,
    )

    with TestClient(app) as client:
        response = client.get("/api/maintenance/overview")

    assert response.status_code == 200
    overview = response.json()
    assert overview["connected_project_id"] == PROJECT_ID
    assert overview["preferences"] == {"bypass_delete_confirmation": False}
    assert overview["storage_impact"]["memory_estimate_bytes"] is None


def test_gui_maintenance_overview_requires_a_connected_project(tmp_path):
    from braincell.gui import create_app

    app = create_app(db_path=tmp_path / "braincell.db", allow_writes=False)
    with TestClient(app) as client:
        assert client.get("/api/maintenance/overview").status_code == 409
