# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""Project-only runtime mode resolution.

BrainCell has one operational mode: a selected project's own database.  Legacy
global data is handled only by the explicit migration workflow, never by normal
CLI, MCP, or Memory Map startup.
"""

import os

VALID_MODES = ("project",)


def resolve_mode(cli_mode: str | None = None) -> str:
    """Return the active mode string, or raise ValueError for unknown modes.

    Args:
        cli_mode: Explicit mode from a CLI ``--mode`` flag; overrides the env.

    Returns:
        ``"project"``.

    Raises:
        ValueError: If the resolved mode is not in ``VALID_MODES``.
    """
    mode = (cli_mode or os.environ.get("BRAINCELL_MODE", "project")).strip().lower()
    if mode not in VALID_MODES:
        if mode == "global":
            raise ValueError(
                "BRAINCELL_MODE=global is retired. BrainCell now opens only the selected "
                "project's memory; use the legacy migration workflow to recover old data."
            )
        raise ValueError(
            f"Unknown BRAINCELL_MODE={mode!r}. Valid modes: {', '.join(VALID_MODES)}."
        )
    return mode
