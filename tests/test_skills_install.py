# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_skills_install.py — packaged Claude Code skills → ~/.claude/skills.

The skills ship inside the wheel (`braincell/skills/<name>/SKILL.md`, declared in
pyproject package-data); `braincell install --skills` is what actually places them
on a user's machine. These tests pin the placement contract:

  absent      → written
  identical   → no-op (idempotent re-run)
  DIFFERENT   → refused, left untouched

That last case is the important one: a user may have their own skill named
`braincell-init`, and silently overwriting it would destroy work no backup covers.

Everything is redirected to tmp_path via BRAINCELL_CLAUDE_SKILLS_DIR — no test
touches the real ~/.claude/skills.
"""

from __future__ import annotations

import pytest

from braincell.install import claude_skills_dir, install_skills, packaged_skills


@pytest.fixture(autouse=True)
def _isolated_skills_dir(tmp_path, monkeypatch):
    """Redirect the skills dir so the real ~/.claude/skills is never touched."""
    target = tmp_path / "claude-skills"
    monkeypatch.setenv("BRAINCELL_CLAUDE_SKILLS_DIR", str(target))
    return target


# ── Discovery ──────────────────────────────────────────────────────────────────

def test_packaged_skills_are_discoverable():
    """Both skills must be readable from the installed package, not just the repo."""
    names = packaged_skills()
    assert "braincell-init" in names
    assert "braincell-sync" in names


def test_skills_dir_honours_env_override(_isolated_skills_dir):
    assert claude_skills_dir() == _isolated_skills_dir


# ── Placement ──────────────────────────────────────────────────────────────────

def test_fresh_install_writes_every_skill(_isolated_skills_dir):
    results = install_skills()

    assert results, "no skills were installed"
    assert {r[1] for r in results} == {"installed"}
    for name, _status, path in results:
        assert path == _isolated_skills_dir / name / "SKILL.md"
        assert path.exists()
        assert path.read_text().lstrip().startswith("---"), "SKILL.md lost its frontmatter"


def test_reinstall_is_idempotent(_isolated_skills_dir):
    install_skills()
    before = {
        p: p.read_text() for p in _isolated_skills_dir.rglob("SKILL.md")
    }

    results = install_skills()

    assert {r[1] for r in results} == {"current"}, "re-run should report no-op, not rewrite"
    after = {p: p.read_text() for p in _isolated_skills_dir.rglob("SKILL.md")}
    assert after == before, "idempotent re-run modified a file"


def test_existing_different_skill_is_refused_not_clobbered(_isolated_skills_dir):
    """The no-clobber guarantee: a user's own same-named skill survives untouched."""
    mine = _isolated_skills_dir / "braincell-init" / "SKILL.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("---\nname: braincell-init\n---\n\nMY OWN SKILL, DO NOT TOUCH\n")

    results = install_skills()

    by_name = {name: status for name, status, _ in results}
    assert by_name["braincell-init"] == "conflict"
    assert "MY OWN SKILL, DO NOT TOUCH" in mine.read_text(), (
        "install_skills overwrote a user-authored skill"
    )
    # The non-conflicting one still installs — one conflict must not block the rest.
    assert by_name["braincell-sync"] == "installed"


def test_conflict_then_resolution_installs(_isolated_skills_dir):
    """After the user clears their version, a re-run installs braincell's copy."""
    mine = _isolated_skills_dir / "braincell-init" / "SKILL.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("different content\n")
    assert dict((n, s) for n, s, _ in install_skills())["braincell-init"] == "conflict"

    mine.unlink()
    assert dict((n, s) for n, s, _ in install_skills())["braincell-init"] == "installed"


def test_explicit_target_dir_overrides_env(tmp_path):
    other = tmp_path / "elsewhere"
    results = install_skills(target_dir=other)
    for _name, _status, path in results:
        assert other in path.parents


# ── Content ────────────────────────────────────────────────────────────────────

def test_installed_skills_carry_no_maintainer_path(_isolated_skills_dir):
    """Regression guard: the shipped copies must not reference the maintainer's clone.

    `$HOME/braincell` is meaningless on a user's machine, and it evades
    tests/test_repo_hygiene.py (which matches repo-relative tokens and the literal
    home path, not an env var), so it has to be asserted here.
    """
    install_skills()
    for path in _isolated_skills_dir.rglob("SKILL.md"):
        text = path.read_text()
        assert "HOME/braincell" not in text, f"{path} references the maintainer's clone"
        assert "/home/" not in text, f"{path} contains an absolute home path"
