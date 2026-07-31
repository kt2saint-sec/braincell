# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_repo_hygiene.py — tracked files must not depend on untracked ones.

Two failure modes, both of which only bite AFTER publication, when nobody is
looking at this repo the way the maintainer does:

1. **Dangling internal references.** `CLAUDE.md`, `docs/STAGE_LOG.md` and
   `.claude/rules/*.md` are deliberately gitignored — internal maintainer notes
   that must never enter git history. A tracked file that says "see CLAUDE.md § X"
   reads fine on this machine and is a dead pointer for everyone else.
2. **Absolute home paths.** `/home/<user>/...` baked into a tracked file leaks the
   maintainer's username and is wrong on every other machine.

Both are cheap to introduce and invisible locally, which is exactly why they need
a test rather than vigilance. Ignore status is resolved by asking `git
check-ignore`, so this stays correct automatically when `.gitignore` changes.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# A reference is REPO-relative only when it is not rooted at a home directory:
# braincell legitimately reads the user's own `~/.claude/projects` transcripts, and
# `.claude/settings.json` in prose means the user's Claude Code config, not a file
# in this repo.
_PATH_TOKEN = re.compile(r"(?<![\w~/.-])((?:\.claude|docs|evals|tests|braincell)/[\w./-]+|CLAUDE\.md)")

_BINARY_SUFFIXES = {".png", ".ico", ".svg", ".db", ".jsonl"}

# These are documented output locations inside a user's selected Project, not
# dependencies expected to exist in this source checkout.
_ALLOWED_GENERATED_PATHS = {
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".claude/skills",
}


def _tracked_text_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    return [f for f in out if Path(f).suffix not in _BINARY_SUFFIXES]


def _unquote(name: str) -> str:
    """Undo git's C-style quoting (`"a\\r"` → `a\r`) so names match our tokens."""
    if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
        try:
            name = ast.literal_eval(name)
        except (SyntaxError, ValueError):
            name = name[1:-1]
    return name


def _ignored(paths: set[str]) -> set[str]:
    """Return the subset of *paths* that git ignores (authoritative, batched)."""
    if not paths:
        return set()
    ordered = sorted(paths)
    # Bytes stdin, not text=True: text mode translates "\n" to os.linesep, and a
    # CRLF-fed git on Windows echoes back C-quoted names with a literal \r.
    proc = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        cwd=REPO, input="\n".join(ordered).encode("utf-8"), capture_output=True,
    )
    stdout = proc.stdout.decode("utf-8", errors="replace")
    return {_unquote(line.strip()) for line in stdout.splitlines() if line.strip()}


def test_no_tracked_file_references_a_gitignored_path():
    """A published file must not point at a file that was never published."""
    candidates: dict[str, list[tuple[str, int, str]]] = {}
    for rel in _tracked_text_files():
        if rel == ".gitignore":
            continue  # .gitignore must name ignored paths
        if rel == Path(__file__).name or rel.endswith("test_repo_hygiene.py"):
            continue  # this file names the paths it polices
        try:
            text = (REPO / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for match in _PATH_TOKEN.finditer(line):
                token = match.group(1).rstrip(".,;:)`\"'")
                candidates.setdefault(token, []).append((rel, lineno, line.strip()))

    violations = [
        f"{rel}:{lineno} references gitignored {token!r}\n      {line[:110]}"
        for token in _ignored(set(candidates)) - _ALLOWED_GENERATED_PATHS
        # .get, not [..]: a name git quoted in a way _unquote cannot reverse
        # must not crash the test with a KeyError.
        for rel, lineno, line in candidates.get(token, [(token, 0, "")])
    ]
    assert not violations, (
        "Tracked files reference gitignored (never-published) paths — these are dead "
        "pointers for anyone who clones this repo:\n  - " + "\n  - ".join(sorted(violations))
    )


def test_no_tracked_file_contains_the_maintainers_home_path():
    """A real home path leaks the maintainer's username and is wrong elsewhere.

    Matched against the CURRENT user's home directory rather than a hardcoded
    name — so it catches whoever is committing, and so this test does not itself
    have to name anybody. Synthetic fixture paths like `/home/user/proj-a` in the
    registry tests are deliberately unaffected: they are test data, not a leak.
    """
    home = str(Path.home()).rstrip("/")
    pattern = re.compile(re.escape(home) + r"/")
    violations = []
    for rel in _tracked_text_files():
        if rel.endswith("test_repo_hygiene.py"):
            continue
        try:
            text = (REPO / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                violations.append(f"{rel}:{lineno}  {line.strip()[:110]}")
    assert not violations, (
        "Tracked files contain absolute home paths (use `~/` or a relative path):\n  - "
        + "\n  - ".join(violations)
    )


def test_runtime_has_no_retired_external_viewer_paths():
    """The Memory Map is native-only; retired launch paths must not regrow."""
    forbidden = (
        "webbrowser",
        "open_browser",
        "_schedule_browser_open",
        "port_serves_gui",
        "--no-browser",
        "zenity",
    )
    violations = []
    for path in sorted((REPO / "braincell").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                violations.append(f"{path.relative_to(REPO)} contains {token!r}")
    assert not violations, (
        "Retired external-viewer runtime paths returned:\n  - "
        + "\n  - ".join(violations)
    )
