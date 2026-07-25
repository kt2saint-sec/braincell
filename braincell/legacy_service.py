# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""One-release cleanup bridge for the retired braincell-map.service."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

UNIT_NAME = "braincell-map.service"


def unit_path() -> Path:
    override = os.environ.get("BRAINCELL_SYSTEMD_USER_DIR")
    if override:
        return Path(override) / UNIT_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "systemd" / "user" / UNIT_NAME


def _systemctl(args: list[str]) -> tuple[int, str]:
    executable = shutil.which("systemctl")
    if executable is None:
        return 127, "systemctl not found"
    proc = subprocess.run(
        [executable, "--user", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def status() -> dict:
    path = unit_path()
    return {
        "unit_path": str(path),
        "installed": path.exists() or path.is_symlink(),
        "active": _systemctl(["is-active", UNIT_NAME])[0] == 0,
        "enabled": _systemctl(["is-enabled", UNIT_NAME])[0] == 0,
    }


def remove() -> dict:
    """Disable, stop, and remove only the retired GUI unit."""
    path = unit_path()
    was_present = path.exists()
    details: list[str] = []

    rc, output = _systemctl(["disable", "--now", UNIT_NAME])
    low = output.lower()
    if rc != 0 and not any(
        marker in low
        for marker in ("not loaded", "not found", "no such file", "does not exist")
    ):
        current = status()
        return {
            **current,
            "removed": False,
            "detail": f"disable --now failed; unit left intact: {output}",
        }

    path.unlink(missing_ok=True)
    _systemctl(["daemon-reload"])
    _systemctl(["reset-failed", UNIT_NAME])
    current = status()
    return {
        **current,
        "removed": was_present and not current["installed"],
        "detail": "; ".join(details),
    }
