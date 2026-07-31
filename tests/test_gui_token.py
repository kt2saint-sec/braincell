# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
test_gui_token.py — durable GUI auth token (_resolve_gui_token).

Pins the token-persistence contract:
- first resolve MINTS a token, persists it at get_gui_token_path() with 0600
  perms, and every later resolve returns the SAME token (restart-safe tabs);
- BRAINCELL_GUI_TOKEN env override wins and is NEVER written to disk;
- token files are per-namespace (no cross-namespace sharing);
- deleting the file (the ``--rotate-token`` CLI path) makes the next resolve
  mint a DIFFERENT token.

The conftest autouse fixture isolates XDG_DATA_HOME + BRAINCELL_DATA_NAMESPACE
per test, so nothing here touches the real ~/.local/share.
"""

import sys

import pytest


@pytest.fixture(autouse=True)
def _no_env_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear any ambient BRAINCELL_GUI_TOKEN so the mint path is exercised."""
    monkeypatch.delenv("BRAINCELL_GUI_TOKEN", raising=False)


class TestResolveGuiToken:
    @pytest.mark.xfail(sys.platform == "win32", strict=True, reason="no Windows ACL on GUI auth token — see BUGS.md cross-platform CI section")
    def test_mints_persists_0600_and_reuses(self):
        from braincell.config import get_gui_token_path
        from braincell.gui import _resolve_gui_token

        path = get_gui_token_path()
        assert not path.exists()

        first = _resolve_gui_token()
        assert first
        assert path.exists()
        assert path.read_text(encoding="utf-8").strip() == first
        assert oct(path.stat().st_mode)[-3:] == "600"
        # No stray tmp file left behind by the atomic write.
        assert not path.with_name(path.name + ".tmp").exists()

        second = _resolve_gui_token()
        assert second == first

    def test_env_override_wins_and_never_writes(self, monkeypatch):
        from braincell.config import get_gui_token_path
        from braincell.gui import _resolve_gui_token

        monkeypatch.setenv("BRAINCELL_GUI_TOKEN", "env-tok")
        assert _resolve_gui_token() == "env-tok"
        assert not get_gui_token_path().exists()

    def test_env_override_does_not_shadow_persisted_file(self, monkeypatch):
        """A persisted token survives an env-overridden launch untouched."""
        from braincell.config import get_gui_token_path
        from braincell.gui import _resolve_gui_token

        minted = _resolve_gui_token()
        monkeypatch.setenv("BRAINCELL_GUI_TOKEN", "env-tok")
        assert _resolve_gui_token() == "env-tok"
        monkeypatch.delenv("BRAINCELL_GUI_TOKEN")
        assert _resolve_gui_token() == minted
        assert get_gui_token_path().read_text(encoding="utf-8").strip() == minted

    def test_distinct_namespaces_get_distinct_tokens(self, monkeypatch):
        """No cross-namespace token sharing.

        DATA_NAMESPACE is frozen at config-import time, so per-test env changes
        do not reach it — patch the module global directly (the path helpers
        read it at call time).
        """
        import braincell.config as config
        from braincell.gui import _resolve_gui_token

        monkeypatch.setattr(config, "DATA_NAMESPACE", "ns_one")
        path_one = config.get_gui_token_path()
        tok_one = _resolve_gui_token()

        monkeypatch.setattr(config, "DATA_NAMESPACE", "ns_two")
        path_two = config.get_gui_token_path()
        tok_two = _resolve_gui_token()

        assert path_one != path_two
        assert path_one.exists() and path_two.exists()
        assert tok_one != tok_two
        assert path_one.read_text(encoding="utf-8").strip() == tok_one
        assert path_two.read_text(encoding="utf-8").strip() == tok_two

    def test_rotate_unlink_mints_a_different_token(self):
        """Simulates the ``braincell gui --rotate-token`` CLI path."""
        from braincell.config import get_gui_token_path
        from braincell.gui import _resolve_gui_token

        first = _resolve_gui_token()
        get_gui_token_path().unlink(missing_ok=True)
        second = _resolve_gui_token()
        assert second != first
        assert get_gui_token_path().read_text(encoding="utf-8").strip() == second
