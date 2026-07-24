# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
mode.py — BrainCell mode resolution.

Two modes are supported:

- ``project`` (default): an isolated per-repo brain (one braincell.db per project
  ULID under ``~/.local/share/<namespace>/projects/<id>/``).
- ``global``: one shared brain across all projects (``braincell.db`` under
  ``~/.local/share/<namespace>/global/``).  The global brain must be created
  explicitly via ``braincell build --mode global`` before it can be opened.

Precedence: CLI ``--mode`` arg > ``BRAINCELL_MODE`` env > default ``project``.
"""

import os

VALID_MODES = ("project", "global")


def resolve_mode(cli_mode: str | None = None) -> str:
    """Return the active mode string, or raise ValueError for unknown modes.

    Args:
        cli_mode: Explicit mode from a CLI ``--mode`` flag; overrides the env.

    Returns:
        ``"project"`` or ``"global"``.

    Raises:
        ValueError: If the resolved mode is not in ``VALID_MODES``.
    """
    mode = (cli_mode or os.environ.get("BRAINCELL_MODE", "project")).strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(
            f"Unknown BRAINCELL_MODE={mode!r}. Valid modes: {', '.join(VALID_MODES)}."
        )
    return mode
