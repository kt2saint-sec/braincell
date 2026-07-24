# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
family_hook.py — proactive family-recall UserPromptSubmit hook (A2).

Packaged module (run as ``python -m braincell.family_hook``) so it ships with the
pip install and needs no loose script path. `braincell install` registers exactly
this command in the client's UserPromptSubmit hooks.

Surfaces relevant *family* memory automatically at the start of a turn, instead of
waiting for the model to choose to call the recall tool. On each user prompt it runs
`braincell recall --scope family --json` for the working directory and injects the
top-k sibling notes as a compact "Family memory" context block.

Contract (Claude Code UserPromptSubmit hook):
  - stdin:  JSON payload with at least ``prompt`` and ``cwd``.
  - stdout: ``{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
            "additionalContext": "<block>"}}`` to inject context, or ``{}`` for no-op.
  - exit:   always 0. FAIL-QUIET — any error, timeout, or empty result emits ``{}``
            and never blocks the turn (a memory hook must never break the loop).

Opt-in (disabled by default): the hook no-ops unless the arm flag file exists
(``braincell hook on`` creates it; ``braincell hook off`` removes it — same path).
Arming implies family fan-out, so the recall subprocess is invoked with
BRAINCELL_FEDERATE=on regardless of ambient env.

Env knobs (all optional):
  BRAINCELL_FAMILY_HOOK_FLAG     arm-flag path (default ~/.claude/state/braincell-family-hook.txt)
  BRAINCELL_HOOK_PYTHON          python that can import braincell (default: this interpreter)
  BRAINCELL_FAMILY_HOOK_K        max notes to inject (default 5)
  BRAINCELL_FAMILY_HOOK_MAXCHARS per-note content cap (default 500)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


# The arm-flag path is shared with `braincell hook` (cli.cmd_hook) — keep in sync.
def default_flag_path() -> Path:
    return Path.home() / ".claude" / "state" / "braincell-family-hook.txt"


def _noop() -> None:
    """Emit the no-op form and exit successfully (never block a turn)."""
    print("{}")
    sys.exit(0)


def main() -> None:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        payload = json.loads(raw or "{}")
    except Exception:
        _noop()

    flag = os.environ.get("BRAINCELL_FAMILY_HOOK_FLAG", str(default_flag_path()))
    if not Path(flag).is_file():  # disarmed → transparent no-op
        _noop()

    prompt = (payload.get("prompt") or "").strip()
    cwd = payload.get("cwd") or os.getcwd()
    if not prompt:
        _noop()

    # Prefer an explicit interpreter; else the one running this hook (the venv that
    # installed braincell) — never a bare "python" that may miss the package.
    py = os.environ.get("BRAINCELL_HOOK_PYTHON") or sys.executable
    k = os.environ.get("BRAINCELL_FAMILY_HOOK_K", "5")
    maxchars = int(os.environ.get("BRAINCELL_FAMILY_HOOK_MAXCHARS", "500") or 500)

    # Arming the hook implies family fan-out — force federation on for THIS call so
    # the hook works without depending on the ambient env carrying BRAINCELL_FEDERATE.
    env = {**os.environ, "BRAINCELL_FEDERATE": "on"}
    try:
        res = subprocess.run(
            [py, "-m", "braincell.cli", "recall", prompt,
             "--path", cwd, "--scope", "family", "--json", "-k", k],
            capture_output=True, text=True, timeout=20, env=env,
        )
    except Exception:
        _noop()

    if res.returncode != 0:
        # No brain, federation-unresolvable, family raise, etc. — degrade silently.
        _noop()
    try:
        notes = json.loads(res.stdout or "[]")
    except Exception:
        _noop()
    if not notes:
        _noop()

    lines = ["Family memory (braincell — related notes from sibling projects in this family):"]
    for n in notes:
        kind = n.get("kind", "note")
        content = " ".join((n.get("content") or "").split())
        if len(content) > maxchars:
            content = content[: maxchars - 3] + "..."
        proj = (n.get("project_id") or "")[:8]
        lines.append(f"- [{kind}] {content}  ({proj})")
    ctx = "\n".join(lines)

    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": ctx,
    }}))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Last-resort fail-quiet: no hook error may ever surface to the user.
        print("{}")
