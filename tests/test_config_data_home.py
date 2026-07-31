# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
test_config_data_home.py — regression tests for BUGS.md "platform data
roots": `config._xdg_data_home()` picking a platform-appropriate default
(macOS: ~/Library/Application Support, Windows: %LOCALAPPDATA%) while never
silently orphaning data an older, Linux-style-only build already wrote to
~/.local/share on those platforms.

`_data_home_mismatch_warned` is process-global (warn-once, not per-call) —
every test that exercises the warning path resets it via monkeypatch first so
test order never matters.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


def _no_env(monkeypatch):
    """conftest's autouse isolate_xdg fixture always SETS XDG_DATA_HOME —
    the env override is the point of that fixture, but it also short-circuits
    every platform-default code path this suite exists to test."""
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)


class TestEnvOverrideAlwaysWins:
    @pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
    def test_xdg_data_home_env_wins_on_every_platform(self, tmp_path, monkeypatch, platform):
        from braincell import config

        monkeypatch.setattr(sys, "platform", platform)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "explicit"))
        assert config._xdg_data_home() == tmp_path / "explicit"


class TestLinuxUnchanged:
    def test_default_is_dot_local_share(self, tmp_path, monkeypatch):
        from braincell import config

        _no_env(monkeypatch)
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert config._xdg_data_home() == tmp_path / ".local" / "share"

    def test_never_consults_legacy_detection(self, tmp_path, monkeypatch):
        """Linux's own default already IS the 'legacy' path — no mismatch
        branch should even run (nothing to warn about, ever)."""
        from braincell import config

        _no_env(monkeypatch)
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        (tmp_path / ".local" / "share" / config.DATA_NAMESPACE).mkdir(parents=True)
        monkeypatch.setattr(config, "_data_home_mismatch_warned", False)
        config._xdg_data_home()
        assert config._data_home_mismatch_warned is False


class TestMacOSDataHome:
    def test_fresh_install_uses_application_support(self, tmp_path, monkeypatch):
        from braincell import config

        _no_env(monkeypatch)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert config._xdg_data_home() == tmp_path / "Library" / "Application Support"

    def test_prefers_existing_populated_legacy_root_and_warns_once(
        self, tmp_path, monkeypatch, caplog,
    ):
        import logging

        from braincell import config

        _no_env(monkeypatch)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(config, "_data_home_mismatch_warned", False)
        legacy_ns = tmp_path / ".local" / "share" / config.DATA_NAMESPACE
        legacy_ns.mkdir(parents=True)

        with caplog.at_level(logging.WARNING, logger="braincell.config"):
            first = config._xdg_data_home()
            second = config._xdg_data_home()  # second call must not re-warn

        assert first == tmp_path / ".local" / "share"
        assert second == first
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    def test_both_populated_prefers_legacy_and_warns_about_mismatch(
        self, tmp_path, monkeypatch, caplog,
    ):
        import logging

        from braincell import config

        _no_env(monkeypatch)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(config, "_data_home_mismatch_warned", False)
        legacy_root = tmp_path / ".local" / "share"
        platform_root = tmp_path / "Library" / "Application Support"
        (legacy_root / config.DATA_NAMESPACE).mkdir(parents=True)
        (platform_root / config.DATA_NAMESPACE).mkdir(parents=True)

        with caplog.at_level(logging.WARNING, logger="braincell.config"):
            result = config._xdg_data_home()

        assert result == legacy_root  # existing/in-use root wins, no silent pick
        assert any("Both" in r.message for r in caplog.records)
        # Neither root is touched — no migration, no deletion.
        assert (legacy_root / config.DATA_NAMESPACE).is_dir()
        assert (platform_root / config.DATA_NAMESPACE).is_dir()

    def test_only_platform_root_populated_no_warning(self, tmp_path, monkeypatch, caplog):
        import logging

        from braincell import config

        _no_env(monkeypatch)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(config, "_data_home_mismatch_warned", False)
        platform_root = tmp_path / "Library" / "Application Support"
        (platform_root / config.DATA_NAMESPACE).mkdir(parents=True)

        with caplog.at_level(logging.WARNING, logger="braincell.config"):
            result = config._xdg_data_home()

        assert result == platform_root
        assert not any(r.levelno == logging.WARNING for r in caplog.records)


class TestWindowsDataHome:
    def test_uses_localappdata_when_set(self, tmp_path, monkeypatch):
        from braincell import config

        _no_env(monkeypatch)
        monkeypatch.setattr(sys, "platform", "win32")
        local_app_data = tmp_path / "Local"
        monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
        assert config._xdg_data_home() == local_app_data

    def test_falls_back_to_appdata_local_when_localappdata_unset(self, tmp_path, monkeypatch):
        from braincell import config

        _no_env(monkeypatch)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert config._xdg_data_home() == tmp_path / "AppData" / "Local"

    def test_prefers_existing_populated_legacy_root(self, tmp_path, monkeypatch):
        from braincell import config

        _no_env(monkeypatch)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(config, "_data_home_mismatch_warned", False)
        (tmp_path / ".local" / "share" / config.DATA_NAMESPACE).mkdir(parents=True)

        assert config._xdg_data_home() == tmp_path / ".local" / "share"


class TestDownstreamPathsFollowDataHome:
    """get_db_path/get_local_state_dir derive from _xdg_data_home() — prove
    the platform default actually reaches the real per-project path, not
    just the helper function in isolation."""

    def test_get_local_state_dir_uses_macos_default(self, tmp_path, monkeypatch):
        from braincell import config

        _no_env(monkeypatch)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        state_dir = config.get_local_state_dir("01AAAAAAAAAAAAAAAAAAAAAAAA")
        assert state_dir == (
            tmp_path / "Library" / "Application Support" / config.DATA_NAMESPACE
            / "projects" / "01AAAAAAAAAAAAAAAAAAAAAAAA"
        )
