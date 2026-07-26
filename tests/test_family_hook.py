# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression coverage for the retired user-level recall hook."""

from __future__ import annotations

import io

from braincell import family_hook


def test_legacy_hook_entry_point_is_always_a_noop(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"prompt":"secret","cwd":"/tmp"}'))

    family_hook.main()

    assert capsys.readouterr().out == "{}\n"


def test_legacy_hook_does_not_spawn_or_read_project_state(monkeypatch, capsys):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("retired global hook attempted operational work")

    monkeypatch.setattr("subprocess.run", forbidden)
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))

    family_hook.main()

    assert capsys.readouterr().out == "{}\n"
