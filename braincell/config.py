# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
config.py — platform-appropriate path resolution for the standalone BrainCell MCP.

Path layout for the default ``braincell`` namespace:

    <data home>/braincell/projects/<ULID>/
        └── braincell.db      (bc_documents / bc_chunks / bc_chunks_fts +
                               memory_notes / memory_fts + schema_version)

``<data home>`` is platform-appropriate by default (see `_xdg_data_home`):
Linux ``~/.local/share``, macOS ``~/Library/Application Support``, Windows
``%LOCALAPPDATA%``. It is always overridable via ``XDG_DATA_HOME`` regardless
of platform (the env var name is a holdover from the Linux-only original —
kept as-is so an existing override on any platform keeps working unchanged).

The data-dir namespace is configurable via ``BRAINCELL_DATA_NAMESPACE`` (default
``braincell``). A consumer that wants a FULLY ISOLATED store sets this to its own
name — e.g. a second tool exports ``BRAINCELL_DATA_NAMESPACE=mytool`` so
its brain lives at ``<data home>/mytool/projects/<ULID>/``.

This is the single source of truth for the namespace within this package — both
the data dir and the per-project identity filename derive from it.
"""

import os
import sys
from pathlib import Path

from .log import get as _get_log

log = _get_log("braincell.config")

# ── Data-dir namespace (single source of truth) ───────────────────────────────
# Default ``braincell``; a standalone consumer overrides it for an isolated store
# (see module docstring).
DATA_NAMESPACE = os.environ.get("BRAINCELL_DATA_NAMESPACE", "braincell")

# ── XDG / platform base directories ───────────────────────────────────────────

# Warn about a legacy-vs-platform data-home mismatch at most once per process
# (see `_xdg_data_home`) — every project-path resolution calls this function,
# and re-warning on every call would flood the log for no added information.
_data_home_mismatch_warned = False


# Delegated to braincell.platform (single source of truth).
from .platform import _legacy_linux_style_data_home, _platform_data_home_default


def _xdg_data_home() -> Path:
    """Resolve the data-home root.

    Precedence: explicit ``XDG_DATA_HOME`` env override (any platform, always
    wins, unconditionally) > an EXISTING populated legacy
    ``~/.local/share/<namespace>`` root on macOS/Windows (versions before
    platform-aware defaults used the Linux-style path unconditionally, so a
    real user's brain can already live there) > the platform-appropriate
    default from `_platform_data_home_default`.

    No silent migration, ever: an existing legacy root is used AS-IS, never
    copied or moved. If BOTH the legacy and the new platform-default root
    already hold this namespace's data, the legacy root wins (it is the one
    actually in use) and a warning is logged once per process — the owner
    decides if/when to consolidate; this function never picks one over the
    other by deleting or merging anything.
    """
    env = os.environ.get("XDG_DATA_HOME")
    if env:
        return Path(env)

    platform_default = _platform_data_home_default()
    if sys.platform not in ("darwin", "win32"):
        return platform_default  # Linux: always was this default; nothing to detect

    legacy = _legacy_linux_style_data_home()
    if legacy == platform_default:
        return platform_default  # never happens for darwin/win32; defensive only

    legacy_populated = (legacy / DATA_NAMESPACE).is_dir()
    if not legacy_populated:
        return platform_default

    global _data_home_mismatch_warned
    platform_populated = (platform_default / DATA_NAMESPACE).is_dir()
    if not _data_home_mismatch_warned:
        if platform_populated:
            log.warning(
                "Both %s and %s hold existing '%s' data — using the legacy "
                "%s root (no silent migration). Consolidate manually if this "
                "is unintended.",
                legacy, platform_default, DATA_NAMESPACE, legacy,
            )
        else:
            log.warning(
                "Using the legacy %s data root ('%s' predates platform-"
                "appropriate defaults) instead of the current default %s. "
                "No data was moved.",
                legacy, DATA_NAMESPACE, platform_default,
            )
        _data_home_mismatch_warned = True
    return legacy


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
    from .project_registry import get_or_create_project_id, resolve_path_to_ulid

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
    return get_or_create_project_id(project_root, ULID)


def resolve_project_id_readonly(project_root: Path) -> str | None:
    """Return the project ULID if the path is registered, else None — NEVER mints.
    For label-only callers that must not write into a target folder."""
    try:
        return get_project_id(project_root, create=False)
    except ProjectIdentityMissing:
        return None


# ── Per-project paths ─────────────────────────────────────────────────────────


def get_local_state_dir(project_id: str) -> Path:
    """Return ``~/.local/share/<namespace>/projects/<id>/`` — never committed."""
    from .project_registry import is_safe_project_id

    if not is_safe_project_id(project_id):
        raise ValueError(f"Unsafe Project identity: {project_id!r}")
    return _xdg_data_home() / DATA_NAMESPACE / "projects" / project_id


def get_db_path(project_id: str) -> Path:
    """Return ``~/.local/share/<namespace>/projects/<id>/braincell.db`` — XDG.

    The single per-project store: documents, chunks, memory notes, FTS indexes,
    schema version, and embedding fingerprint all live in this one file.
    """
    return get_local_state_dir(project_id) / "braincell.db"


def get_maintenance_preferences_path(project_id: str) -> Path:
    """Return the crash-safe per-Project maintenance-preferences catalog.

    This is intentionally separate from the Project database: it controls a
    human confirmation preference, never memory content or deletion state.
    """
    return get_local_state_dir(project_id) / "maintenance-preferences.json"


def get_maintenance_audit_path(project_id: str) -> Path:
    """Return the durable per-Project hard-prune audit catalog.

    It remains outside the SQLite database so a permanent history-row purge
    cannot erase evidence of a maintenance attempt.
    """
    return get_local_state_dir(project_id) / "maintenance-audit.json"


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
    """Return ``~/.local/share/<namespace>/global/braincell.db`` — the RETIRED shared global brain.

    Parallel to the workspace-level registry/families paths: NOT under ``projects/``.
    Retired surface: ``braincell build --mode global`` no longer exists
    (``--mode`` accepts only ``project``), so nothing creates this DB anymore.
    The path is retained for ``legacy_recovery.py`` and the retired pooling
    tests; normal runtime never opens it.
    """
    return _xdg_data_home() / DATA_NAMESPACE / "global" / "braincell.db"
