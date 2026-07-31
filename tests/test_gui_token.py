# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
test_gui_token.py — durable GUI auth token (_resolve_gui_token).

Pins the token-persistence contract:
- first resolve MINTS a token, persists it at get_gui_token_path() restricted
  to the current user only (POSIX: 0600; Windows: icacls — see
  `_windows_restrict_token_acl`, BUGS.md "token ACL parity"), and every later
  resolve returns the SAME token (restart-safe tabs);
- BRAINCELL_GUI_TOKEN env override wins and is NEVER written to disk;
- token files are per-namespace (no cross-namespace sharing);
- deleting the file (the ``--rotate-token`` CLI path) makes the next resolve
  mint a DIFFERENT token.

The conftest autouse fixture isolates XDG_DATA_HOME + BRAINCELL_DATA_NAMESPACE
per test, so nothing here touches the real ~/.local/share.

The ci/windows-macos-matrix branch marked the old
``test_mints_persists_0600_and_reuses`` ``xfail(win32, strict=True)`` because
Windows had NO ACL restriction at the time. `_windows_restrict_token_acl` now
supplies one, so that marker is gone: the mint/persist/reuse contract is
asserted platform-agnostically below, the 0600 bit-check is a POSIX-only
test, and Windows ACL behaviour has its own class. Do not reintroduce the
marker — under ``strict=True`` the now-passing test would fail the run.
"""

import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_env_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear any ambient BRAINCELL_GUI_TOKEN so the mint path is exercised."""
    monkeypatch.delenv("BRAINCELL_GUI_TOKEN", raising=False)


class TestResolveGuiToken:
    def test_mints_persists_and_reuses(self):
        from braincell.config import get_gui_token_path
        from braincell.gui import _resolve_gui_token

        path = get_gui_token_path()
        assert not path.exists()

        first = _resolve_gui_token()
        assert first
        assert path.exists()
        assert path.read_text(encoding="utf-8").strip() == first
        # No stray tmp file left behind by the atomic write.
        assert not path.with_name(path.name + ".tmp").exists()

        second = _resolve_gui_token()
        assert second == first

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX file-mode bits; Windows ACL parity is "
               "TestWindowsGuiTokenAcl below (icacls has no chmod-bit analog).",
    )
    def test_posix_mode_is_0600(self):
        from braincell.config import get_gui_token_path
        from braincell.gui import _resolve_gui_token

        _resolve_gui_token()
        assert oct(get_gui_token_path().stat().st_mode)[-3:] == "600"

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
        from braincell import config
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


# ── Windows ACL parity (mocked: this suite runs on Linux) ──────────────────────
#
# icacls has no in-process seam the way `os.chmod` does, so these mock
# `subprocess.run` directly rather than exercising a real Windows ACL.

class TestWindowsGuiTokenAcl:
    def test_resolve_invokes_icacls_instead_of_chmod_on_windows(self, monkeypatch):
        from braincell.config import get_gui_token_path
        from braincell.gui import _resolve_gui_token

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("USERNAME", "alice")
        monkeypatch.delenv("USERDOMAIN", raising=False)
        calls = []

        class _Result:
            returncode = 0
            stderr = ""

        monkeypatch.setattr(
            "subprocess.run", lambda cmd, **kw: calls.append(cmd) or _Result(),
        )
        chmod_calls = []
        monkeypatch.setattr("os.chmod", lambda *a, **k: chmod_calls.append(a))

        token = _resolve_gui_token()
        assert token
        assert get_gui_token_path().read_text(encoding="utf-8").strip() == token
        assert chmod_calls == []  # Windows never calls chmod for the ACL
        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[0] == "icacls"
        assert "/inheritance:r" in cmd
        assert "alice:F" in cmd

    def test_domain_qualified_account_when_userdomain_is_set(self, monkeypatch):
        import braincell.gui as gui_module

        monkeypatch.setenv("USERDOMAIN", "CORP")
        monkeypatch.setenv("USERNAME", "alice")
        calls = []

        class _Result:
            returncode = 0
            stderr = ""

        monkeypatch.setattr(
            "subprocess.run", lambda cmd, **kw: calls.append(cmd) or _Result(),
        )
        gui_module._windows_restrict_token_acl(Path("C:/token"))
        assert "CORP\\alice:F" in calls[0]

    def test_missing_username_warns_and_skips_icacls(self, monkeypatch, caplog):
        import logging

        import braincell.gui as gui_module

        monkeypatch.delenv("USERNAME", raising=False)
        calls = []
        monkeypatch.setattr("subprocess.run", lambda cmd, **kw: calls.append(cmd))

        with caplog.at_level(logging.WARNING, logger="braincell.gui"):
            gui_module._windows_restrict_token_acl(Path("C:/token"))
        assert calls == []
        assert any("USERNAME" in record.message for record in caplog.records)

    def test_icacls_failure_warns_but_does_not_raise(self, monkeypatch, caplog):
        import logging

        import braincell.gui as gui_module

        monkeypatch.setenv("USERNAME", "alice")
        monkeypatch.delenv("USERDOMAIN", raising=False)

        class _Result:
            returncode = 5
            stderr = "Access is denied."

        monkeypatch.setattr("subprocess.run", lambda cmd, **kw: _Result())

        with caplog.at_level(logging.WARNING, logger="braincell.gui"):
            gui_module._windows_restrict_token_acl(Path("C:/token"))  # must not raise
        assert any("icacls failed" in record.message for record in caplog.records)
