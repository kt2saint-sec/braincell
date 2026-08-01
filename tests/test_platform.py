# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""Tests for braincell.platform — cross-platform SSoT module."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestGetDataHome:
    def test_linux_uses_xdg_data_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("XDG_DATA_HOME", "/custom/data")
        from braincell.platform import get_data_home

        assert get_data_home() == Path("/custom/data")

    def test_linux_fallback_to_local_share(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        from braincell.platform import get_data_home

        assert get_data_home() == Path.home() / ".local" / "share"

    def test_macos_uses_application_support(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        from braincell.platform import get_data_home

        assert get_data_home() == Path.home() / "Library" / "Application Support"

    def test_windows_uses_localappdata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
        from braincell.platform import get_data_home

        assert get_data_home() == Path(r"C:\Users\test\AppData\Local")

    def test_windows_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        from braincell.platform import get_data_home

        assert get_data_home() == Path.home() / "AppData" / "Local"


class TestGetConfigHome:
    def test_linux_uses_xdg_config_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("XDG_CONFIG_HOME", "/custom/config")
        from braincell.platform import get_config_home

        assert get_config_home() == Path("/custom/config")

    def test_linux_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        from braincell.platform import get_config_home

        assert get_config_home() == Path.home() / ".config"

    def test_macos_uses_preferences(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        from braincell.platform import get_config_home

        assert get_config_home() == Path.home() / "Library" / "Preferences"

    def test_windows_uses_appdata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("APPDATA", r"C:\Users\test\AppData\Roaming")
        from braincell.platform import get_config_home

        assert get_config_home() == Path(r"C:\Users\test\AppData\Roaming")

    def test_windows_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.delenv("APPDATA", raising=False)
        from braincell.platform import get_config_home

        assert get_config_home() == Path.home() / "AppData" / "Roaming"


class TestGetClientConfigDirs:
    def test_claude_uses_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        from braincell.platform import get_claude_config_dir

        assert get_claude_config_dir() == Path.home() / ".claude"

    def test_codex_uses_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        from braincell.platform import get_codex_config_dir

        assert get_codex_config_dir() == Path.home() / ".codex"

    def test_opencode_linux_uses_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("XDG_CONFIG_HOME", "/custom/config")
        from braincell.platform import get_opencode_config_dir

        assert get_opencode_config_dir() == Path("/custom/config") / "opencode"

    def test_opencode_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        from braincell.platform import get_opencode_config_dir

        assert get_opencode_config_dir() == Path.home() / "Library" / "Preferences" / "opencode"

    def test_opencode_project_config_path(self, tmp_path: Path) -> None:
        proj = tmp_path / "my-project"
        from braincell.platform import get_opencode_project_config_path

        assert get_opencode_project_config_path(proj) == proj.resolve() / "opencode.json"

    def test_braincell_flag_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("XDG_DATA_HOME", "/custom/data")
        from braincell.platform import get_braincell_flag_path

        assert get_braincell_flag_path() == Path("/custom/data") / "braincell" / "family-hook.txt"


class TestInstallLauncher:
    def test_dispatches_to_windows(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        mock_windows = MagicMock(return_value=(tmp_path / "icon.ico", tmp_path / "shortcut.lnk"))
        monkeypatch.setattr("braincell.platform._install_launcher_windows", mock_windows)

        from braincell.platform import install_launcher
        result = install_launcher(tmp_path)

        mock_windows.assert_called_once_with(tmp_path)
        assert result == (tmp_path / "icon.ico", tmp_path / "shortcut.lnk")

    def test_dispatches_to_macos(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        mock_macos = MagicMock(return_value=(tmp_path / "icon.svg", tmp_path / "App.app"))
        monkeypatch.setattr("braincell.platform._install_launcher_macos", mock_macos)

        from braincell.platform import install_launcher
        result = install_launcher(tmp_path)

        mock_macos.assert_called_once_with(tmp_path)
        assert result == (tmp_path / "icon.svg", tmp_path / "App.app")

    def test_dispatches_to_linux(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        mock_linux = MagicMock(return_value=(tmp_path / "icon.svg", tmp_path / "desktop.desktop"))
        monkeypatch.setattr("braincell.platform._install_launcher_linux", mock_linux)

        from braincell.platform import install_launcher
        result = install_launcher(tmp_path)

        mock_linux.assert_called_once_with(tmp_path)
        assert result == (tmp_path / "icon.svg", tmp_path / "desktop.desktop")


class TestRemoveLegacyService:
    def test_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        mock_remove = MagicMock(return_value={"removed": True, "detail": "ok"})
        monkeypatch.setattr("braincell.platform._remove_linux_legacy_service", mock_remove)

        from braincell.platform import remove_legacy_service
        result = remove_legacy_service()

        mock_remove.assert_called_once()
        assert result == {"removed": True, "detail": "ok"}

    def test_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        mock_remove = MagicMock(return_value={"removed": True, "detail": "ok"})
        monkeypatch.setattr("braincell.platform._remove_macos_legacy_service", mock_remove)

        from braincell.platform import remove_legacy_service
        result = remove_legacy_service()

        mock_remove.assert_called_once()
        assert result == {"removed": True, "detail": "ok"}

    def test_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        mock_remove = MagicMock(return_value={"removed": False, "detail": "not found"})
        monkeypatch.setattr("braincell.platform._remove_windows_legacy_service", mock_remove)

        from braincell.platform import remove_legacy_service
        result = remove_legacy_service()

        mock_remove.assert_called_once()
        assert result == {"removed": False, "detail": "not found"}
