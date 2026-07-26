# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
config.py — XDG path resolution for the standalone BrainCell MCP.

Path layout for the default ``braincell`` namespace:

    ~/.local/share/braincell/projects/<ULID>/
        └── braincell.db      (bc_documents / bc_chunks / bc_chunks_fts +
                               memory_notes / memory_fts + schema_version)

The data-dir namespace is configurable via ``BRAINCELL_DATA_NAMESPACE`` (default
``braincell``). A consumer that wants a FULLY ISOLATED store sets this to its own
name — e.g. a second tool exports ``BRAINCELL_DATA_NAMESPACE=mytool`` so
its brain lives at ``~/.local/share/mytool/projects/<ULID>/``.
Override the XDG base separately via ``XDG_DATA_HOME``.

This is the single source of truth for the namespace within this package — both
the data dir and the per-project identity filename derive from it.
"""

import os
from pathlib import Path
from typing import Optional

# ── Data-dir namespace (single source of truth) ───────────────────────────────
# Default ``braincell``; a standalone consumer overrides it for an isolated store
# (see module docstring).
DATA_NAMESPACE = os.environ.get("BRAINCELL_DATA_NAMESPACE", "braincell")

# ── XDG base directories ──────────────────────────────────────────────────────


def _xdg_data_home() -> Path:
    """XDG_DATA_HOME (defaults to ~/.local/share)."""
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


# ── Project identity ──────────────────────────────────────────────────────────


class ProjectIdentityMissing(RuntimeError):
    """Raised by ``get_project_id(create=False)`` when the path is not yet in the
    central path-registry. Lets read-only callers avoid MINTING an identity for a
    folder that may have nothing to ingest."""


def get_project_id(project_root: Path, *, create: bool = True) -> str:
    """Return the project's ULID from the CENTRAL path registry — no in-repo file.

    Clean break: identity lives ONLY in the central XDG path-registry. There is no
    legacy in-repo ``*.project.json`` adoption — the registry is the sole source.

    Resolution order:
      1. Path already in path-registry.json   -> return its ULID.
      2. Unregistered + create                -> mint, register, return.
      3. Unregistered + not create            -> ProjectIdentityMissing.
    """
    # Lazy import: project_registry imports config, so a module-level import here
    # would be circular.
    from .project_registry import register_path, resolve_path_to_ulid

    project_root = Path(project_root).resolve()

    # 1. central registry hit (the source of truth)
    pid = resolve_path_to_ulid(project_root)
    if pid:
        return pid

    if not create:
        raise ProjectIdentityMissing(
            f"{project_root} not in path-registry (create=False)"
        )

    # 2. fresh identity — stored ONLY in the central XDG registry, nothing in the repo
    from ulid import ULID  # lazy: only minting needs it
    new_id = str(ULID())
    register_path(project_root, new_id)
    return new_id


def resolve_project_id_readonly(project_root: Path) -> Optional[str]:
    """Return the project ULID if the path is registered, else None — NEVER mints.
    For label-only callers that must not write into a target folder."""
    try:
        return get_project_id(project_root, create=False)
    except ProjectIdentityMissing:
        return None


# ── Per-project paths ─────────────────────────────────────────────────────────


def get_local_state_dir(project_id: str) -> Path:
    """Return ``~/.local/share/<namespace>/projects/<id>/`` — never committed."""
    return _xdg_data_home() / DATA_NAMESPACE / "projects" / project_id


def get_structure_dir(project_root: Path) -> Path:
    """Return ``~/.local/share/<namespace>/projects/<id>/structure/`` — XDG, read-only ingest vault."""
    return get_local_state_dir(get_project_id(project_root)) / "structure"


def get_db_path(project_id: str) -> Path:
    """Return ``~/.local/share/<namespace>/projects/<id>/braincell.db`` — XDG.

    The single per-project store: documents, chunks, memory notes, FTS indexes,
    schema version, and embedding fingerprint all live in this one file.
    """
    return get_local_state_dir(project_id) / "braincell.db"


# ── Workspace-level project registry + families (BrainCell project model) ──────


def get_path_registry_path() -> Path:
    """``~/.local/share/<namespace>/path-registry.json`` — workspace-level map of
    absolute repo path → project ULID (ENCODE direction)."""
    return _xdg_data_home() / DATA_NAMESPACE / "path-registry.json"


def get_families_path() -> Path:
    """``~/.local/share/<namespace>/families.json`` — workspace-level project families
    (name → [member abs paths]) for cross-project (family-scoped) recall."""
    return _xdg_data_home() / DATA_NAMESPACE / "families.json"


def get_pools_path() -> Path:
    """``~/.local/share/<namespace>/pools.json`` — ULID-only Pool membership.

    This is non-memory bookkeeping.  It never contains copied notes, documents,
    chunks, or absolute paths.
    """
    return _xdg_data_home() / DATA_NAMESPACE / "pools.json"


def get_gui_token_path() -> Path:
    """``~/.local/share/<namespace>/gui-token`` — the persisted GUI auth token (0600)."""
    return _xdg_data_home() / DATA_NAMESPACE / "gui-token"


def get_tour_seen_path() -> Path:
    """``~/.local/share/<namespace>/gui-tour-seen`` — flag file marking that the
    GUI's guided tour has been completed or skipped once on this machine.

    Server-side on purpose: the native window's renderer profile has no
    persistent localStorage, so a profile-local flag would re-ambush the user
    on every native launch. Namespace-level (like the token), not per-project —
    onboarding is a person-level event, not a brain-level one."""
    return _xdg_data_home() / DATA_NAMESPACE / "gui-tour-seen"


def get_global_db_path() -> Path:
    """Return ``~/.local/share/<namespace>/global/braincell.db`` — the shared global brain.

    Parallel to the workspace-level registry/families paths: NOT under ``projects/``.
    The global brain must be created explicitly (``braincell build --mode global``)
    before any tool or ``open_store`` can open it.
    """
    return _xdg_data_home() / DATA_NAMESPACE / "global" / "braincell.db"
