#!/usr/bin/env python3
"""BrainCell multi-OS smoke test — runs on Linux, macOS, and Windows locally.

Validates that all platform-specific paths resolve correctly for the host OS.
Does not require PySide6 (GUI) or Ollama (embedding) to pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

EXIT_OK = 0
EXIT_FAIL = 1
errors: list[str] = []


def check(name: str, got: object, expected: object) -> None:
    if got != expected:
        msg = f"FAIL {name}: expected {expected!r}, got {got!r}"
        print(f"  {msg}")
        errors.append(msg)
    else:
        print(f"  OK   {name}")


def main() -> int:
    print(f"BrainCell multi-OS smoke test — {sys.platform}")
    print()

    print("=== Path resolution ===")
    from braincell.platform import (
        get_data_home,
        get_config_home,
        get_claude_config_dir,
        get_codex_config_dir,
        get_opencode_config_dir,
        get_opencode_project_config_path,
        get_braincell_flag_path,
    )

    if sys.platform == "linux":
        # XDG overrides respect env vars
        home = Path.home()
        check("data_home (Linux)", get_data_home(), home / ".local" / "share")
        check("config_home (Linux)", get_config_home(), home / ".config")
    elif sys.platform == "darwin":
        home = Path.home()
        check("data_home (macOS)", get_data_home(), home / "Library" / "Application Support")
        check("config_home (macOS)", get_config_home(), home / "Library" / "Preferences")
    elif sys.platform == "win32":
        import os
        home = Path.home()
        local_appdata = os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))
        appdata = os.environ.get("APPDATA", str(home / "AppData" / "Roaming"))
        check("data_home (Windows)", str(get_data_home()), local_appdata)
        check("config_home (Windows)", str(get_config_home()), appdata)
    else:
        check(f"data_home ({sys.platform})", get_data_home(), Path.home() / ".local" / "share")

    home = Path.home()
    check("claude config dir", get_claude_config_dir(), home / ".claude")
    check("codex config dir", get_codex_config_dir(), home / ".codex")
    check("opencode config dir (exists)", get_opencode_config_dir().is_absolute(), True)
    check(
        "opencode project config",
        get_opencode_project_config_path(Path("/tmp/test-proj")),
        Path("/tmp/test-proj").resolve() / "opencode.json",
    )
    check("braincell flag path", str(get_braincell_flag_path()).endswith("family-hook.txt"), True)
    print()

    print("=== Module imports (no Qt/Ollama required) ===")
    try:
        import braincell.config
        import braincell.legacy_service
        import braincell.transcript_ingest
        import braincell.cli
        import braincell.pool
        import braincell.store
        import braincell.server
        check("core modules import", True, True)
    except ImportError as exc:
        check(f"core modules import: {exc}", False, True)

    # install.py needs tomlkit (installed in dev environment)
    try:
        import braincell.install
        check("install module imports", True, True)
        c = braincell.install.get_client("opencode")
        check("opencode client available", c.name == "opencode", True)
    except ImportError:
        print("  SKIP install.py (tomlkit not available)")
    print()

    if errors:
        print(f"\n{len(errors)} error(s):")
        for e in errors:
            print(f"  {e}")
        return EXIT_FAIL

    print("All checks passed.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
