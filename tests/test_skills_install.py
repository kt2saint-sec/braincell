# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_skills_install.py — packaged BrainCell skills → selected project.

The skills ship inside the wheel (`braincell/skills/<name>/SKILL.md`, declared in
pyproject package-data); `braincell install --skills` is what actually places them
on a user's machine. These tests pin the placement contract:

  absent      → written inside the selected project
  identical   → no-op (idempotent re-run)
  DIFFERENT   → refused, left untouched
  remove      → removes only an unchanged BrainCell-managed copy

That last case is the important one: a user may have their own skill named
`braincell-init`, and silently overwriting it would destroy work no backup covers.

No test touches a user-level skills directory.
"""

from __future__ import annotations

from braincell.install import (
    install_project_skills,
    packaged_skills,
    project_skills_dir,
    remove_project_skills,
)


def _project(tmp_path):
    project = tmp_path / "selected-project"
    project.mkdir()
    return project


# ── Discovery ──────────────────────────────────────────────────────────────────

def test_packaged_skills_are_discoverable():
    """Both skills must be readable from the installed package, not just the repo."""
    names = packaged_skills()
    assert "braincell-init" in names
    assert "braincell-sync" in names


def test_skills_dirs_are_client_specific_and_project_local(tmp_path):
    project = _project(tmp_path)
    assert project_skills_dir(project, "claude") == project.resolve() / ".claude" / "skills"
    assert project_skills_dir(project, "codex") == project.resolve() / ".agents" / "skills"


# ── Placement ──────────────────────────────────────────────────────────────────

def test_fresh_install_writes_every_skill_inside_selected_project(tmp_path):
    project = _project(tmp_path)
    target = project / ".claude" / "skills"
    results = install_project_skills(project, "claude")

    assert results, "no skills were installed"
    assert {r[1] for r in results} == {"installed"}
    for name, _status, path in results:
        assert path == target / name / "SKILL.md"
        assert path.exists()
        assert path.read_text(encoding="utf-8").lstrip().startswith("---"), "SKILL.md lost its frontmatter"


def test_reinstall_is_idempotent(tmp_path):
    project = _project(tmp_path)
    target = project / ".claude" / "skills"
    install_project_skills(project, "claude")
    before = {
        p: p.read_text(encoding="utf-8") for p in target.rglob("SKILL.md")
    }

    results = install_project_skills(project, "claude")

    assert {r[1] for r in results} == {"current"}, "re-run should report no-op, not rewrite"
    after = {p: p.read_text(encoding="utf-8") for p in target.rglob("SKILL.md")}
    assert after == before, "idempotent re-run modified a file"


def test_existing_different_skill_is_refused_not_clobbered(tmp_path):
    """The no-clobber guarantee: a user's own same-named skill survives untouched."""
    project = _project(tmp_path)
    target = project / ".claude" / "skills"
    mine = target / "braincell-init" / "SKILL.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("---\nname: braincell-init\n---\n\nMY OWN SKILL, DO NOT TOUCH\n", encoding="utf-8")

    results = install_project_skills(project, "claude")

    by_name = {name: status for name, status, _ in results}
    assert by_name["braincell-init"] == "conflict"
    assert "MY OWN SKILL, DO NOT TOUCH" in mine.read_text(encoding="utf-8"), (
        "install_project_skills overwrote a user-authored skill"
    )
    # The non-conflicting one still installs — one conflict must not block the rest.
    assert by_name["braincell-sync"] == "installed"


def test_conflict_then_resolution_installs(tmp_path):
    """After the user clears their version, a re-run installs braincell's copy."""
    project = _project(tmp_path)
    mine = project / ".claude" / "skills" / "braincell-init" / "SKILL.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("different content\n", encoding="utf-8")
    result = install_project_skills(project, "claude")
    assert {n: s for n, s, _ in result}["braincell-init"] == "conflict"

    mine.unlink()
    result = install_project_skills(project, "claude")
    assert {n: s for n, s, _ in result}["braincell-init"] == "installed"


def test_remove_deletes_only_unchanged_managed_skills(tmp_path):
    project = _project(tmp_path)
    installed = install_project_skills(project, "codex")
    edited = next(path for name, _status, path in installed if name == "braincell-init")
    edited.write_text("user changed this\n", encoding="utf-8")

    results = remove_project_skills(project, "codex")
    by_name = {name: status for name, status, _path in results}

    assert by_name["braincell-init"] == "conflict"
    assert edited.read_text(encoding="utf-8") == "user changed this\n"
    assert by_name["braincell-sync"] == "removed"
    assert not next(path for name, _status, path in installed if name == "braincell-sync").exists()


def test_unknown_skill_client_is_rejected(tmp_path):
    project = _project(tmp_path)
    try:
        project_skills_dir(project, "vscode")
    except ValueError as exc:
        assert "Claude or Codex" in str(exc)
    else:
        raise AssertionError("VS Code must not receive unsupported BrainCell skills")


def test_project_skill_directory_symlink_cannot_escape_project(tmp_path):
    project = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / ".claude").symlink_to(outside, target_is_directory=True)

    try:
        install_project_skills(project, "claude")
    except RuntimeError as exc:
        assert "outside the selected project" in str(exc)
    else:
        raise AssertionError("project-local skill installation followed an escaping symlink")
    assert not list(outside.rglob("SKILL.md"))


# ── Content ────────────────────────────────────────────────────────────────────

def test_installed_skills_carry_no_maintainer_path(tmp_path):
    """Regression guard: the shipped copies must not reference the maintainer's clone.

    `$HOME/braincell` is meaningless on a user's machine, and it evades
    tests/test_repo_hygiene.py (which matches repo-relative tokens and the literal
    home path, not an env var), so it has to be asserted here.
    """
    project = _project(tmp_path)
    install_project_skills(project, "claude")
    for path in (project / ".claude" / "skills").rglob("SKILL.md"):
        text = path.read_text(encoding="utf-8")
        assert "HOME/braincell" not in text, f"{path} references the maintainer's clone"
        assert "/home/" not in text, f"{path} contains an absolute home path"
