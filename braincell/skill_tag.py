# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
skill_tag.py — Pure skill-body detection and name extraction for BrainCell.

Skill SKILL.md bodies are injected into Claude Code conversations as single
messages containing the rendered skill content.  They are cross-session
boilerplate and should be embedded ONCE per skill name (deduped across runs),
NOT once per transcript.

Pure: no I/O, no DB, no network.  All helpers are module-level functions
operating on plain strings.  Skill docs are a separate
concern from transcript docs; nothing here touches storage.
"""

from __future__ import annotations

import re

# ── Detection ──────────────────────────────────────────────────────────────────

# Unambiguous marker emitted by Claude Code's skill loader when it injects a
# SKILL.md body into the conversation.  The line format is:
#   "Base directory for this skill: /path/to/skill-dir"
_BASE_DIR_LINE = re.compile(r"Base directory for this skill:\s*(\S+)", re.IGNORECASE)

# SKILL.md frontmatter signature: the file must contain BOTH "name:" and
# "description:" near the top (within the first 600 chars) to qualify as a
# frontmatter-style skill body.  The longer token goes first to avoid partial
# matches (Rule per regex alternation best-practice).
_FRONTMATTER_NAME = re.compile(r"(?m)^\s*name\s*:", re.IGNORECASE)
_FRONTMATTER_DESC = re.compile(r"(?m)^\s*description\s*:", re.IGNORECASE)
_FRONTMATTER_WINDOW = 600  # chars — enough for a typical YAML header


def is_skill_body(page: str) -> bool:
    """Return True iff *page* is a rendered SKILL.md injection.

    Conservative: only two unambiguous markers qualify:
      1. The Claude Code skill-loader emits "Base directory for this skill: <path>".
      2. A SKILL.md frontmatter block (name: + description: both in the first
         600 chars).

    False-positive prevention:
    - A page that merely *mentions* a skill (e.g. prose about some /skill-name)
      does NOT match either marker and returns False.
    - A `<command-name>` invocation page (carrying user args) does NOT contain
      the skill-loader base-dir marker or a YAML frontmatter block in its header,
      so it also returns False.
    """
    if _BASE_DIR_LINE.search(page):
        return True
    # Frontmatter check: require BOTH name: and description: in the opening window.
    head = page[:_FRONTMATTER_WINDOW]
    if _FRONTMATTER_NAME.search(head) and _FRONTMATTER_DESC.search(head):
        return True
    return False


def skill_name_from_body(page: str) -> str | None:
    """Extract the skill name from a skill-body page.

    Priority order:
    1. Basename of the "Base directory for this skill: <path>" path — the most
       unambiguous marker (the skill-dir basename IS the skill name by convention).
    2. Frontmatter ``name:`` value (stripped of leading/trailing whitespace and
       YAML delimiters such as quotes).

    Returns None if neither marker is found (caller should treat as unnameable).
    """
    m = _BASE_DIR_LINE.search(page)
    if m:
        raw_path = m.group(1).strip().rstrip("/")
        # Take the last path component as the skill name.
        name = raw_path.split("/")[-1] if "/" in raw_path else raw_path
        return name or None

    # Frontmatter name: value
    m2 = re.search(r"(?m)^\s*name\s*:\s*(.+)$", page[:_FRONTMATTER_WINDOW], re.IGNORECASE)
    if m2:
        return m2.group(1).strip().strip("'\"") or None

    return None
