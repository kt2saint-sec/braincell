# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""Crash-safe, per-Project confirmation preferences for future maintenance.

The only Phase-1 preference is deliberately narrow: whether a person has
chosen to bypass typing ``DELETE`` during a future permanent-maintenance run.
It never bypasses candidate review, evidence, approval-digest verification,
the final Apply action, or any execution safeguard.
"""

from __future__ import annotations

import json
from typing import Final

from . import config
from .catalog_io import atomic_write_json, catalog_lock

ENABLE_BYPASS_ACKNOWLEDGEMENT: Final = (
    "ENABLING THIS FEATURE MEANS I AGREE BRAINCELL IS NOT RESPONSIBLE "
    "SINCE I WAS ADVISED OF RISKS"
)
_DEFAULT_PREFERENCES: Final = {"bypass_delete_confirmation": False}


class MaintenancePreferencesError(RuntimeError):
    """Preferences are malformed or a requested safety acknowledgement failed."""


def _load(path) -> dict[str, bool]:  # type: ignore[no-untyped-def]
    """Read an exact valid document, or fail closed without modifying it."""
    if not path.exists():
        return dict(_DEFAULT_PREFERENCES)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise MaintenancePreferencesError(
            "maintenance preferences are unreadable; refusing to change them"
        ) from exc
    if (
        not isinstance(data, dict)
        or set(data) != {"bypass_delete_confirmation"}
        or not isinstance(data["bypass_delete_confirmation"], bool)
    ):
        raise MaintenancePreferencesError(
            "maintenance preferences are invalid; refusing to change them"
        )
    return {"bypass_delete_confirmation": data["bypass_delete_confirmation"]}


def load_preferences(project_id: str) -> dict[str, bool]:
    """Return a Project's preference without creating state for defaults."""
    return _load(config.get_maintenance_preferences_path(project_id))


def set_bypass_delete_confirmation(
    project_id: str,
    enabled: bool,
    *,
    acknowledgement: str | None = None,
) -> dict[str, bool]:
    """Set the typed-delete bypass after its exact serious acknowledgement."""
    if not isinstance(enabled, bool):
        raise MaintenancePreferencesError("bypass_delete_confirmation must be boolean")
    if enabled and acknowledgement != ENABLE_BYPASS_ACKNOWLEDGEMENT:
        raise MaintenancePreferencesError(
            "the exact acknowledgement is required before enabling the bypass"
        )

    path = config.get_maintenance_preferences_path(project_id)
    with catalog_lock(path):
        _load(path)  # Refuse corruption; never overwrite an unprovable setting.
        preferences = {"bypass_delete_confirmation": enabled}
        atomic_write_json(path, preferences, sort_keys=True)
    return preferences
