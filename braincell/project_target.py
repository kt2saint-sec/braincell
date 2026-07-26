# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""Explicit, shared safety checks for a selected BrainCell project.

This module deliberately does not prompt.  CLI and GUI callers must collect the
required acknowledgement before calling it, which keeps noninteractive use
safe and makes every selected target testable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ProjectTargetError(ValueError):
    """The requested project target is unsafe or needs acknowledgement."""


@dataclass(frozen=True)
class ProjectTarget:
    """A resolved target and the warnings the caller must display."""

    path: Path
    has_project_marker: bool
    warnings: tuple[str, ...]


def _is_privileged() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return bool(os.environ.get("SUDO_USER")) or bool(geteuid and geteuid() == 0)


def validate_project_target(
    path: str | Path,
    *,
    acknowledge_home: bool = False,
    acknowledge_non_git: bool = False,
    allow_privileged: bool = False,
    require_git: bool = False,
) -> ProjectTarget:
    """Resolve and validate one explicit project directory.

    ``/`` is never a valid project.  Selecting a home directory, a non-Git
    directory, or a privileged execution context requires an explicit caller
    acknowledgement.  Non-Git projects remain supported unless the client
    itself requires Git discovery (currently Codex project configuration).
    """
    resolved = Path(path).expanduser().resolve()
    if resolved == Path(resolved.anchor):
        raise ProjectTargetError("BrainCell refuses the filesystem root as a project target.")
    if not resolved.is_dir():
        raise ProjectTargetError(f"Project target is not a directory: {resolved}")

    warnings: list[str] = []
    if resolved == Path.home().resolve():
        if not acknowledge_home:
            raise ProjectTargetError(
                f"{resolved} is your home directory. Re-run with --acknowledge-home "
                "only if you intentionally want it as a BrainCell project."
            )
        warnings.append("Selected project is the home directory.")

    has_project_marker = (resolved / ".git").exists()
    if require_git and not has_project_marker:
        raise ProjectTargetError(
            f"{resolved} is not a Git project. This client requires a Git project "
            "for its project-local configuration."
        )
    if not has_project_marker:
        if not acknowledge_non_git:
            raise ProjectTargetError(
                f"{resolved} has no .git marker. Re-run with --acknowledge-non-git "
                "to use this non-Git project intentionally."
            )
        warnings.append("Selected project has no Git marker.")

    if _is_privileged():
        if not allow_privileged:
            raise ProjectTargetError(
                "BrainCell is running as root or through sudo. Re-run with "
                "--allow-privileged only if you intentionally want configuration "
                "and project state owned by that account."
            )
        warnings.append("BrainCell is running in a privileged account context.")

    return ProjectTarget(resolved, has_project_marker, tuple(warnings))
