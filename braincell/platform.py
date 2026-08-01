# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
platform.py — Single Source of Truth for cross-platform paths and desktop integration.

All platform-specific logic lives here. Other modules delegate to this module
rather than scattering ``sys.platform`` checks and hardcoded home-directory
paths throughout the codebase.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .log import get as _get_log

log = _get_log("braincell.platform")

if sys.platform == "win32":
    _SYSTEMCTL_AVAILABLE = False
else:
    _SYSTEMCTL_AVAILABLE = shutil.which("systemctl") is not None


# ── Path resolution ─────────────────────────────────────────────────────────────


def _platform_data_home_default() -> Path:
    """The platform-appropriate default data root when ``XDG_DATA_HOME`` is
    unset: macOS ``~/Library/Application Support``, Windows ``%LOCALAPPDATA%``
    (``~/AppData/Local`` if that variable is somehow unset), everything else
    (Linux and other POSIX systems) ``~/.local/share``.
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        return Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return Path.home() / ".local" / "share"


def get_data_home() -> Path:
    """Return the platform-appropriate data directory.

    Windows: %LOCALAPPDATA% (e.g., C:\\Users\\user\\AppData\\Local)
    macOS: ~/Library/Application Support
    Linux: $XDG_DATA_HOME or ~/.local/share
    """
    env = os.environ.get("XDG_DATA_HOME")
    if env:
        return Path(env)
    return _platform_data_home_default()


def _legacy_linux_style_data_home() -> Path:
    """``~/.local/share`` — every platform's default BEFORE platform-aware
    defaults were introduced. Needed only to detect pre-existing data left there
    by an older install on macOS/Windows.
    """
    return Path.home() / ".local" / "share"


def get_config_home() -> Path:
    """Return the platform-appropriate config directory.

    Windows: %APPDATA% (e.g., C:\\Users\\user\\AppData\\Roaming)
    macOS: ~/Library/Preferences
    Linux: $XDG_CONFIG_HOME or ~/.config
    """
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Preferences"
    else:
        return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def get_claude_config_dir() -> Path:
    """Return the platform-appropriate Claude Code config directory.

    Claude Code uses ``~/.claude`` on all platforms (cross-platform dotfile convention).
    """
    return Path.home() / ".claude"


def get_codex_config_dir() -> Path:
    """Return the platform-appropriate Codex config directory.

    Codex uses ``~/.codex`` on all platforms (cross-platform dotfile convention).
    """
    return Path.home() / ".codex"


def get_opencode_config_dir() -> Path:
    """Return the platform-appropriate OpenCode config directory.

    OpenCode uses ``~/.config/opencode`` on Linux/macOS and
    ``%APPDATA%/opencode`` (or ``~/.config/opencode``) on Windows.
    Respects ``XDG_CONFIG_HOME`` on non-Windows platforms.
    """
    return get_config_home() / "opencode"


def get_opencode_project_config_path(project_root: Path) -> Path:
    """Return the project-local OpenCode config path (``<project>/opencode.json``)."""
    return project_root.resolve() / "opencode.json"


def get_braincell_flag_path() -> Path:
    """Return the platform-appropriate flag file path."""
    return get_data_home() / "braincell" / "family-hook.txt"


# ── Launcher installation ───────────────────────────────────────────────────────

_ICON_PNG_SIZES = (48, 128, 256, 512)


def _resolve_cli_exec() -> str:
    """Absolute path to the ``braincell`` console script.

    Desktop environments launch entries with the *login/session* PATH, which
    almost never includes a project virtualenv's ``bin`` dir. Resolve an
    absolute path so the launcher works regardless of the session PATH.
    """
    found = shutil.which("braincell")
    if found:
        return found
    sibling = Path(sys.executable).with_name("braincell")
    if sibling.exists():
        return str(sibling)
    return "braincell"


# ── Linux launcher (XDG .desktop + hicolor icons) ──────────────────────────────

_DESKTOP_ENTRY_TEMPLATE = """\
[Desktop Entry]
Type=Application
Name=BrainCell Map
Comment=Local memory map — projects and pools
Exec={exec}
Icon=braincell
Terminal=false
Categories=Development;Utility;
StartupNotify=true
"""


def _install_launcher_linux(project_path: Path | None = None) -> tuple[Path, Path]:
    """Install the desktop icon + .desktop entry (idempotent). Returns (icon, desktop).

    ``project_path`` is the project folder the icon launches (default: cwd).
    """
    from importlib.resources import files

    root = (project_path or Path.cwd()).resolve()
    data_home = get_data_home()
    icons_dir = data_home / "icons"
    hicolor = icons_dir / "hicolor"
    apps_dir = data_home / "applications"
    apps_dir.mkdir(parents=True, exist_ok=True)

    assets = files("braincell").joinpath("assets")
    svg_bytes = assets.joinpath("braincell.svg").read_bytes()

    scalable = hicolor / "scalable" / "apps"
    scalable.mkdir(parents=True, exist_ok=True)
    (scalable / "braincell.svg").write_bytes(svg_bytes)
    for size in _ICON_PNG_SIZES:
        png = assets.joinpath(f"braincell-{size}.png")
        try:
            png_bytes = png.read_bytes()
        except FileNotFoundError:
            continue
        size_dir = hicolor / f"{size}x{size}" / "apps"
        size_dir.mkdir(parents=True, exist_ok=True)
        (size_dir / "braincell.png").write_bytes(png_bytes)

    icons_dir.mkdir(parents=True, exist_ok=True)
    icon_dst = icons_dir / "braincell.svg"
    icon_dst.write_bytes(svg_bytes)

    desktop_dst = apps_dir / "braincell-map.desktop"
    exec_line = f'"{_resolve_cli_exec()}" start "{root}"'
    desktop_dst.write_text(
        _DESKTOP_ENTRY_TEMPLATE.format(exec=exec_line), encoding="utf-8"
    )

    for cmd in (
        ["update-desktop-database", str(apps_dir)],
        ["gtk-update-icon-cache", "-f", "-t", str(hicolor)],
    ):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, check=False, capture_output=True)
            except OSError as exc:
                log.warning("%s failed (non-fatal): %s", cmd[0], exc)
        else:
            log.warning(
                "%s not found — launcher installed; the app menu/icon may need "
                "a manual refresh or re-login.", cmd[0],
            )
    return icon_dst, desktop_dst


# ── macOS launcher (.app wrapper under ~/Applications) ──────────────────────────

_MACOS_INFO_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>BrainCell</string>
    <key>CFBundleDisplayName</key><string>BrainCell Map</string>
    <key>CFBundleIdentifier</key><string>com.braincell.map</string>
    <key>CFBundleExecutable</key><string>braincell-launch</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>LSMinimumSystemVersion</key><string>11.0</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
"""

_MACOS_LAUNCH_SCRIPT = """\
#!/bin/sh
exec {exec} start {root}
"""


def _install_launcher_macos(project_path: Path | None = None) -> tuple[Path, Path]:
    """Install a minimal .app wrapper under ``~/Applications`` (idempotent).
    Returns (icon, app bundle).
    """
    import shlex
    from importlib.resources import files

    root = (project_path or Path.cwd()).resolve()
    app_dir = Path.home() / "Applications" / "BrainCell.app"
    contents = app_dir / "Contents"
    macos_dir = contents / "MacOS"
    resources_dir = contents / "Resources"
    macos_dir.mkdir(parents=True, exist_ok=True)
    resources_dir.mkdir(parents=True, exist_ok=True)

    (contents / "Info.plist").write_text(_MACOS_INFO_PLIST, encoding="utf-8")

    svg_bytes = files("braincell").joinpath("assets", "braincell.svg").read_bytes()
    icon_dst = resources_dir / "braincell.svg"
    icon_dst.write_bytes(svg_bytes)

    launch_script = macos_dir / "braincell-launch"
    launch_script.write_text(
        _MACOS_LAUNCH_SCRIPT.format(
            exec=shlex.quote(_resolve_cli_exec()), root=shlex.quote(str(root)),
        ),
        encoding="utf-8",
    )
    os.chmod(launch_script, 0o755)

    return icon_dst, app_dir


# ── Windows launcher (Start Menu .lnk via PowerShell COM) ───────────────────────

_WINDOWS_SHORTCUT_POWERSHELL = """\
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut('{lnk_path}')
$shortcut.TargetPath = '{target}'
$shortcut.Arguments = 'start "{root}"'
$shortcut.IconLocation = '{icon}'
$shortcut.WorkingDirectory = '{root}'
$shortcut.Description = 'BrainCell Map'
$shortcut.Save()
"""


def _windows_start_menu_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def _windows_restrict_token_acl(path: Path) -> None:
    """Best-effort ACL restriction for the auth token on Windows.

    The token inherits the per-user ACL of the config directory by default;
    this function attempts a tighter restriction but is not required for
    functional safety — the config directory is already user-scoped.
    """
    if sys.platform != "win32":
        return
    try:
        import ntsecuritycon  # noqa: F401 — needed by win32security
        import win32security
    except ImportError:
        return
    try:
        sd = win32security.GetFileSecurity(
            str(path), win32security.DACL_SECURITY_INFORMATION,
        )
        dacl = sd.GetSecurityDescriptorDacl()
        if dacl is not None:
            current_user, _domain, _ = win32security.LookupAccountName("", os.environ.get("USERNAME", ""))
            for i in range(dacl.GetAceCount()):
                ace = dacl.GetAce(i)
                if ace and len(ace) >= 3 and ace[2] == current_user:
                    return  # user ACE already present; nothing to add
        sd.SetSecurityDescriptorDacl(1, dacl, 0)  # protected, no inheritance
        win32security.SetFileSecurity(
            str(path), win32security.DACL_SECURITY_INFORMATION, sd,
        )
    except Exception:
        log.debug("Windows ACL restriction skipped (non-fatal)", exc_info=True)


def _install_launcher_windows(project_path: Path | None = None) -> tuple[Path, Path]:
    """Create a Start Menu shortcut (.lnk) (idempotent). Returns (icon, .lnk).

    Uses PowerShell's WScript.Shell COM object — no third-party dependency.
    """
    from importlib.resources import files

    root = (project_path or Path.cwd()).resolve()
    start_menu = _windows_start_menu_dir()
    start_menu.mkdir(parents=True, exist_ok=True)

    icon_bytes = files("braincell").joinpath("assets", "braincell.ico").read_bytes()
    icon_dst = start_menu / "braincell.ico"
    icon_dst.write_bytes(icon_bytes)

    lnk_path = start_menu / "BrainCell Map.lnk"
    script = _WINDOWS_SHORTCUT_POWERSHELL.format(
        lnk_path=str(lnk_path), target=_resolve_cli_exec(),
        root=str(root), icon=str(icon_dst),
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        log.warning(
            "PowerShell shortcut creation failed (exit %s); the Start Menu "
            "entry was not installed: %s",
            result.returncode, result.stderr.strip(),
        )
    return icon_dst, lnk_path


def install_launcher(project_path: Path | None = None) -> tuple[Path, Path]:
    """Install the platform-appropriate desktop launcher (idempotent).
    Returns (icon, launcher entry).

    Linux: XDG ``.desktop`` entry + hicolor theme icons.
    macOS: an .app wrapper under ``~/Applications``.
    Windows: a Start Menu ``.lnk`` via PowerShell.
    """
    project_path = project_path or Path.cwd()
    if sys.platform == "darwin":
        return _install_launcher_macos(project_path)
    if sys.platform == "win32":
        return _install_launcher_windows(project_path)
    return _install_launcher_linux(project_path)


# ── Legacy service removal ──────────────────────────────────────────────────────

UNIT_NAME = "braincell-map.service"


def remove_legacy_service() -> dict:
    """Remove the retired braincell-map service on the current platform.

    Returns a dict with keys: removed (bool), detail (str), and platform-specific
    status fields where applicable.
    """
    if sys.platform == "linux":
        return _remove_linux_legacy_service()
    if sys.platform == "win32":
        return _remove_windows_legacy_service()
    if sys.platform == "darwin":
        return _remove_macos_legacy_service()
    return {"removed": False, "detail": "No legacy service on this platform"}


def legacy_service_status() -> dict:
    """Check the status of the retired braincell-map service on this platform."""
    if sys.platform == "linux":
        return _linux_service_status()
    if sys.platform == "darwin":
        return _macos_service_status()
    if sys.platform == "win32":
        return _windows_service_status()
    return {
        "installed": False,
        "active": False,
        "enabled": False,
        "detail": "No legacy service on this platform",
    }


def _linux_service_status() -> dict:
    path = _linux_unit_path()
    rc, _output = _systemctl(["is-active", UNIT_NAME])
    active = rc == 0
    rc, _output = _systemctl(["is-enabled", UNIT_NAME])
    enabled = rc == 0
    return {
        "unit_path": str(path),
        "installed": path.exists() or path.is_symlink(),
        "active": active,
        "enabled": enabled,
    }


def _macos_service_status() -> dict:
    plist = _macos_plist_path()
    installed = plist.exists()
    active = False  # cannot check without launchctl
    return {
        "unit_path": str(plist),
        "installed": installed,
        "active": active,
        "enabled": installed,
    }


def _windows_service_status() -> dict:
    return {
        "installed": False,
        "active": False,
        "enabled": False,
        "detail": "Windows legacy service detection not implemented",
    }


def _linux_unit_path() -> Path:
    override = os.environ.get("BRAINCELL_SYSTEMD_USER_DIR")
    if override:
        return Path(override) / UNIT_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "systemd" / "user" / UNIT_NAME


def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "com.braincell.map.plist"


def _real_systemctl(args: list[str]) -> tuple[int, str]:
    """Internal implementation.  Use ``_systemctl`` (module-level attr) so
    tests can monkeypatch it without reaching into the closure."""
    executable = shutil.which("systemctl")
    if executable is None:
        return 127, "systemctl not found"
    proc = subprocess.run(
        [executable, "--user", *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


_systemctl = _real_systemctl  # tests may monkeypatch this


def _remove_linux_legacy_service() -> dict:
    path = _linux_unit_path()
    was_present = path.exists() or path.is_symlink()

    rc, output = _systemctl(["disable", "--now", UNIT_NAME])
    low = output.lower()
    if rc != 0 and not any(
        marker in low
        for marker in ("not loaded", "not found", "no such file", "does not exist")
    ):
        return {
            "removed": False,
            "installed": path.exists() or path.is_symlink(),
            "detail": f"disable --now failed; unit left intact: {output}",
        }

    path.unlink(missing_ok=True)
    _systemctl(["daemon-reload"])
    _systemctl(["reset-failed", UNIT_NAME])

    return {
        "removed": was_present and not (path.exists() or path.is_symlink()),
        "detail": "Removed legacy systemd service",
    }


def _remove_macos_legacy_service() -> dict:
    plist_path = _macos_plist_path()
    if plist_path.exists():
        plist_path.unlink()
        return {"removed": True, "detail": f"Removed LaunchAgent {plist_path}"}
    return {"removed": False, "detail": "No legacy macOS LaunchAgent found"}


def _remove_windows_legacy_service() -> dict:
    """Check for and remove legacy Windows Task Scheduler task (defensive)."""
    task_name = "BraincellMap"
    try:
        rc = subprocess.run(
            ["schtasks", "/Query", "/TN", task_name],
            capture_output=True,
            check=False,
        ).returncode
        if rc == 0:
            subprocess.run(
                ["schtasks", "/Delete", "/TN", task_name, "/F"],
                capture_output=True,
                check=False,
            )
            return {"removed": True, "detail": f"Removed scheduled task {task_name}"}
    except Exception:  # noqa: BLE001,S110 — defensive best-effort; schtasks may not exist
        pass
    return {"removed": False, "detail": "No legacy Windows scheduled task found"}
