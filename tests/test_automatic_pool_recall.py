# SPDX-License-Identifier: AGPL-3.0-or-later
"""Project-local Automatic Pool recall regression coverage."""

from __future__ import annotations

import json

import pytest

from braincell.automatic_pool_recall import (
    disable_automatic_pool_recall,
    enable_automatic_pool_recall,
    project_hook_settings_path,
    run_hook,
    status_automatic_pool_recall,
)
from braincell.cli import main
from braincell.project_registry import (
    add_to_pool,
    create_pool,
    decouple_from_pool,
    register_path,
)


def _project(tmp_path, project_id="01CONNECTED"):
    project = tmp_path / "selected"
    project.mkdir()
    (project / ".git").mkdir()
    register_path(project, project_id)
    create_pool("Release Work")
    add_to_pool("Release Work", [project_id])
    return project


def test_private_and_shareable_settings_are_project_local(tmp_path):
    project = _project(tmp_path)
    assert project_hook_settings_path(project, "local") == (
        project / ".claude" / "settings.local.json"
    )
    assert project_hook_settings_path(project, "project") == (
        project / ".claude" / "settings.json"
    )


def test_project_settings_symlink_cannot_escape_project(tmp_path):
    project = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / ".claude").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="outside the selected Project"):
        enable_automatic_pool_recall(project, scope="local")
    assert not list(outside.iterdir())


def test_enable_defaults_to_only_pool_and_preserves_other_hooks(tmp_path):
    project = _project(tmp_path)
    settings = project_hook_settings_path(project, "local")
    settings.parent.mkdir()
    settings.write_text(
        json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{"command": "keep-me"}]}]}}),
        encoding="utf-8",
    )

    result = enable_automatic_pool_recall(project, scope="local")

    assert result["changed"] is True
    assert result["pool"] == "Release Work"
    payload = json.loads(settings.read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for entry in payload["hooks"]["UserPromptSubmit"]
        for hook in entry["hooks"]
    ]
    assert "keep-me" in commands
    command = next(command for command in commands if "automatic-pool-recall run" in command)
    assert "--pool 'Release Work'" in command
    assert "--project-id 01CONNECTED" in command
    assert str(project) not in command
    assert result["backup_path"]


def test_enable_is_idempotent_and_conflict_safe(tmp_path):
    project = _project(tmp_path)
    first = enable_automatic_pool_recall(project, scope="local")
    before = project_hook_settings_path(project, "local").read_bytes()
    second = enable_automatic_pool_recall(project, scope="local")
    assert first["changed"] is True and second["changed"] is False
    assert project_hook_settings_path(project, "local").read_bytes() == before

    settings = project_hook_settings_path(project, "local")
    payload = json.loads(settings.read_text(encoding="utf-8"))
    payload["hooks"]["UserPromptSubmit"][-1]["hooks"][0]["command"] += " --changed"
    settings.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="conflicting"):
        enable_automatic_pool_recall(project, scope="local")


def test_multiple_pools_require_explicit_name(tmp_path):
    project = _project(tmp_path)
    create_pool("Second")
    add_to_pool("Second", ["01CONNECTED"])

    with pytest.raises(ValueError, match="Release Work.*Second|Second.*Release Work"):
        enable_automatic_pool_recall(project, scope="local")

    result = enable_automatic_pool_recall(project, scope="local", pool_name="Second")
    assert result["pool"] == "Second"


def test_disable_removes_only_braincell_hook_and_is_idempotent(tmp_path):
    project = _project(tmp_path)
    settings = project_hook_settings_path(project, "local")
    enable_automatic_pool_recall(project, scope="local")
    payload = json.loads(settings.read_text(encoding="utf-8"))
    payload["hooks"]["UserPromptSubmit"].append({"hooks": [{"command": "keep-me"}]})
    settings.write_text(json.dumps(payload), encoding="utf-8")

    first = disable_automatic_pool_recall(project, scope="local")
    second = disable_automatic_pool_recall(project, scope="local")

    assert first["changed"] is True and second["changed"] is False
    assert "keep-me" in settings.read_text(encoding="utf-8")
    assert "automatic-pool-recall run" not in settings.read_text(encoding="utf-8")


def test_status_is_project_and_scope_specific(tmp_path):
    project = _project(tmp_path)
    enable_automatic_pool_recall(project, scope="project")
    assert status_automatic_pool_recall(project, scope="project")["enabled"] is True
    assert status_automatic_pool_recall(project, scope="local")["enabled"] is False


def test_hook_noops_outside_connected_project(tmp_path, monkeypatch):
    _project(tmp_path)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.setattr(
        "braincell.automatic_pool_recall._recall_from_pool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not query")),
    )

    assert run_hook(
        {"prompt": "secret", "cwd": str(unrelated)},
        pool_name="Release Work",
        project_id="01CONNECTED",
    ) == {}


def test_hook_noops_without_cwd_or_after_decouple(tmp_path, monkeypatch):
    project = _project(tmp_path)
    enable_automatic_pool_recall(project, scope="local")
    monkeypatch.setattr(
        "braincell.automatic_pool_recall._recall_from_pool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not query")),
    )

    assert run_hook(
        {"prompt": "secret"}, pool_name="Release Work", project_id="01CONNECTED"
    ) == {}
    decouple_from_pool("Release Work", "01CONNECTED")
    assert run_hook(
        {"prompt": "secret", "cwd": str(project)},
        pool_name="Release Work",
        project_id="01CONNECTED",
    ) == {}


def test_hook_uses_explicit_pool_inside_connected_project(tmp_path, monkeypatch):
    project = _project(tmp_path)
    enable_automatic_pool_recall(project, scope="local")
    nested = project / "src"
    nested.mkdir()
    calls = []
    monkeypatch.setattr(
        "braincell.automatic_pool_recall._recall_from_pool",
        lambda pool, root, query, k: calls.append((pool, root, query, k))
        or [{"kind": "decision", "content": "Use the rollback guard", "project_id": "01OTHER"}],
    )

    result = run_hook(
        {"prompt": "how deploy?", "cwd": str(nested)},
        pool_name="Release Work",
        project_id="01CONNECTED",
    )

    assert calls == [("Release Work", project.resolve(), "how deploy?", 5)]
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "Automatic Pool recall" in context
    assert "Use the rollback guard" in context


def test_cli_enable_status_disable_roundtrip(tmp_path, capsys):
    project = _project(tmp_path)

    main(["automatic-pool-recall", "enable", str(project)])
    assert "Automatic Pool recall: Enabled" in capsys.readouterr().out

    main(["automatic-pool-recall", "status", str(project)])
    assert "Pool: Release Work" in capsys.readouterr().out

    main(["automatic-pool-recall", "disable", str(project)])
    assert "Automatic Pool recall: Disabled" in capsys.readouterr().out
