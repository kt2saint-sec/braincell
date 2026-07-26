# SPDX-License-Identifier: AGPL-3.0-or-later
"""Project-only runtime mode and legacy-scope guard coverage."""

from __future__ import annotations

import pytest

from braincell.mode import resolve_mode
from braincell.server import _resolve_scope


class TestResolveMode:
    def test_default_is_project(self, monkeypatch):
        monkeypatch.delenv("BRAINCELL_MODE", raising=False)
        assert resolve_mode() == "project"

    def test_cli_arg_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("BRAINCELL_MODE", "global")
        # CLI 'project' overrides the env's 'global'.
        assert resolve_mode("project") == "project"

    def test_env_project(self, monkeypatch):
        monkeypatch.setenv("BRAINCELL_MODE", "project")
        assert resolve_mode() == "project"

    def test_global_cli_arg_is_retired(self, monkeypatch):
        monkeypatch.delenv("BRAINCELL_MODE", raising=False)
        with pytest.raises(ValueError, match="retired"):
            resolve_mode("global")

    def test_global_env_is_retired(self, monkeypatch):
        monkeypatch.setenv("BRAINCELL_MODE", "global")
        with pytest.raises(ValueError, match="retired"):
            resolve_mode()

    def test_unknown_mode_raises(self, monkeypatch):
        monkeypatch.delenv("BRAINCELL_MODE", raising=False)
        with pytest.raises(ValueError, match="Unknown BRAINCELL_MODE"):
            resolve_mode("banana")


class TestResolveScope:
    def test_self_pins_to_configured_project(self, monkeypatch):
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", "01PROJECT0000000000000000A")
        assert _resolve_scope(None, "self") == "01PROJECT0000000000000000A"

    def test_explicit_project_overrides(self, monkeypatch):
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", "01PROJECT0000000000000000A")
        assert _resolve_scope("01OTHER00000000000000000XX", "self") == "01OTHER00000000000000000XX"

    def test_self_falls_back_to_none_when_unset(self, monkeypatch):
        monkeypatch.delenv("BRAINCELL_PROJECT_ID", raising=False)
        assert _resolve_scope(None, "self") is None

    def test_legacy_family_scope_is_rejected(self):
        with pytest.raises(ValueError, match="retired"):
            _resolve_scope(None, "family")

    def test_legacy_all_scope_is_rejected(self):
        with pytest.raises(ValueError, match="retired"):
            _resolve_scope(None, "all")
