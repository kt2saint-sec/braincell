# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression coverage for the dry-run-first Project setup command."""

from __future__ import annotations

from argparse import Namespace

import pytest


def _args(path, **overrides):
    values = {
        "path": str(path), "client": "claude", "claude_scope": "local",
        "with_skills": False, "automatic_pool_recall": None,
        "skip_transcripts": True, "yes": False, "dry_run": False,
        "acknowledge_home": False, "acknowledge_non_git": True,
        "allow_privileged": False,
    }
    values.update(overrides)
    return Namespace(**values)


def test_setup_default_is_a_no_write_plan(tmp_path, monkeypatch, capsys):
    from braincell import cli

    project = tmp_path / "project"
    project.mkdir()
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    before = sorted(path.relative_to(xdg) for path in xdg.rglob("*") if xdg.exists())
    cli.cmd_setup(_args(project))

    output = capsys.readouterr().out
    assert f"Project: {project.resolve()}" in output
    assert "Planned writes:" in output
    assert "No changes applied" in output
    after = sorted(path.relative_to(xdg) for path in xdg.rglob("*") if xdg.exists())
    assert after == before


def test_setup_applies_existing_commands_in_order(tmp_path, monkeypatch):
    from braincell import cli

    project = tmp_path / "project"
    project.mkdir()
    calls = []
    monkeypatch.setattr(cli, "cmd_build", lambda args: calls.append(("build", args.path)))
    monkeypatch.setattr(cli, "cmd_install", lambda args: calls.append(("connect", args.path)))
    monkeypatch.setattr(cli, "cmd_skills", lambda args: calls.append(("skills", args.path)))
    cli.cmd_setup(_args(project, yes=True, with_skills=True))
    assert calls == [("build", str(project.resolve())), ("connect", str(project.resolve())), ("skills", str(project.resolve()))]


def test_setup_refuses_filesystem_root_even_with_yes():
    from braincell import cli

    with pytest.raises(SystemExit, match="filesystem root"):
        cli.cmd_setup(_args("/", yes=True, acknowledge_non_git=True))


class TestSetupPlansTheModelDownload:
    """Setup previews the embedding-model download and applies it on --yes."""

    @staticmethod
    def _status(**overrides):
        base = {
            "provider": "ollama", "model": "test-model:1b", "dim": 1024,
            "reachable": True, "model_present": True, "ok": True, "detail": "",
        }
        base.update(overrides)
        if "ok" not in overrides:
            base["ok"] = base["reachable"] and base["model_present"]
        return base

    def test_plan_lists_the_download_when_the_model_is_missing(
        self, tmp_path, monkeypatch, capsys
    ):
        from braincell import cli, embed

        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        monkeypatch.setattr(
            embed, "embedder_status",
            lambda **_k: self._status(model_present=False),
        )
        cli.cmd_setup(_args(project, skip_transcripts=False))
        output = capsys.readouterr().out
        assert "Download embedding model test-model:1b" in output
        assert "No changes applied" in output

    def test_plan_stays_quiet_when_the_model_is_present(
        self, tmp_path, monkeypatch, capsys
    ):
        from braincell import cli, embed

        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        monkeypatch.setattr(embed, "embedder_status", lambda **_k: self._status())
        cli.cmd_setup(_args(project, skip_transcripts=False))
        assert "Download embedding model" not in capsys.readouterr().out

    def test_apply_pulls_before_building(self, tmp_path, monkeypatch):
        from braincell import cli, embed

        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        monkeypatch.setattr(
            embed, "embedder_status",
            lambda **_k: self._status(model_present=False),
        )
        order = []
        monkeypatch.setattr(
            embed, "ensure_embed_model",
            lambda **_k: order.append("pull") or self._status(),
        )
        monkeypatch.setattr(
            cli, "cmd_build", lambda _a: order.append("build")
        )
        monkeypatch.setattr(cli, "cmd_install", lambda _a: order.append("install"))
        cli.cmd_setup(_args(project, skip_transcripts=False, yes=True))
        assert order == ["pull", "build", "install"]

    def test_apply_fails_closed_when_the_download_cannot_succeed(
        self, tmp_path, monkeypatch
    ):
        from braincell import cli, embed

        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        unready = self._status(model_present=False, detail="still not ready")
        monkeypatch.setattr(embed, "embedder_status", lambda **_k: unready)
        monkeypatch.setattr(embed, "ensure_embed_model", lambda **_k: unready)
        monkeypatch.setattr(
            cli, "cmd_build",
            lambda _a: pytest.fail("built despite an unready embedder"),
        )
        with pytest.raises(SystemExit, match="still not ready"):
            cli.cmd_setup(_args(project, skip_transcripts=False, yes=True))
