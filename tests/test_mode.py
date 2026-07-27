# SPDX-License-Identifier: AGPL-3.0-or-later
"""Project-only runtime mode rejection coverage."""

from __future__ import annotations

import pytest

from braincell.mode import resolve_mode


class TestResolveMode:
    def test_default_is_project(self, monkeypatch):
        monkeypatch.delenv("BRAINCELL_MODE", raising=False)
        assert resolve_mode() == "project"

    def test_explicit_project_overrides_retired_environment_value(self, monkeypatch):
        monkeypatch.setenv("BRAINCELL_MODE", "global")
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
