# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for explicit BrainCell project-target selection."""

from __future__ import annotations

import os

import pytest

from braincell.project_target import ProjectTargetError, validate_project_target


def test_resolves_symlink_and_marks_git_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(project, target_is_directory=True)

    target = validate_project_target(linked)

    assert target.path == project.resolve()
    assert target.has_project_marker is True
    assert target.warnings == ()


def test_refuses_filesystem_root():
    with pytest.raises(ProjectTargetError, match="filesystem root"):
        validate_project_target("/", allow_privileged=True)


def test_home_requires_explicit_acknowledgement(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".git").mkdir()
    monkeypatch.setattr("braincell.project_target.Path.home", lambda: home)

    with pytest.raises(ProjectTargetError, match="--acknowledge-home"):
        validate_project_target(home)

    target = validate_project_target(home, acknowledge_home=True)
    assert "home directory" in " ".join(target.warnings)


def test_non_git_directory_requires_explicit_acknowledgement(tmp_path):
    project = tmp_path / "project without git"
    project.mkdir()

    with pytest.raises(ProjectTargetError, match="--acknowledge-non-git"):
        validate_project_target(project)

    target = validate_project_target(project, acknowledge_non_git=True)
    assert target.has_project_marker is False


def test_codex_target_requires_git_marker_even_when_non_git_is_acknowledged(tmp_path):
    project = tmp_path / "plain-project"
    project.mkdir()

    with pytest.raises(ProjectTargetError, match="requires a Git project"):
        validate_project_target(project, acknowledge_non_git=True, require_git=True)


def test_privileged_execution_requires_explicit_override(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    # raising=False: os.geteuid does not exist on Windows; production reads it
    # via getattr(os, "geteuid", None), so injecting it still exercises the
    # privileged path there.
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.delenv("SUDO_USER", raising=False)

    with pytest.raises(ProjectTargetError, match="--allow-privileged"):
        validate_project_target(project)

    target = validate_project_target(project, allow_privileged=True)
    assert "privileged" in " ".join(target.warnings)


def test_register_rejects_unsafe_target_before_minting(tmp_path):
    from braincell.cli import main
    from braincell.project_registry import load_path_registry

    non_git = tmp_path / "plain-directory"
    non_git.mkdir()

    with pytest.raises(SystemExit, match="acknowledge-non-git"):
        main(["register", str(non_git)])
    assert load_path_registry() == {}
