# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""project_registry.py — workspace-level path↔ULID registry + project families.

Implements the BrainCell project-model foundation:

- **Path→ULID registry** (`path-registry.json`): maps an ABSOLUTE repo path →
  project ULID. Built in the ENCODE direction — the abs path is captured at
  register/ingest time and stored verbatim; we NEVER reverse a `~/.claude/projects`
  dirname (`path.replace('/','-')` is ambiguous to decode). Lookup is a dict hit.
- **Families** (`families.json`): `{family_name: [member_abs_path, ...]}`. The
  authoritative family declaration; members are listed by PATH so an un-ingested
  repo (no ULID yet) can still be declared. Family→ULID resolution goes through the
  path registry; un-registered members contribute nothing (lazy-link).

Both files live at the workspace level (`config.get_*_path()`, call-time XDG so
tests isolate via `_isolate_xdg`). Writes are atomic (tmp + os.replace). Both
fail-safe to empty on a missing/corrupt file (a bad config must not crash ingest
or search); a malformed families.json logs loud and degrades to "no
families" rather than aborting.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from .config import get_families_path, get_path_registry_path
from .log import get as _get_log

log = _get_log("braincell.project_registry")


# ── Path normalisation (the registry KEY) ─────────────────────────────────────

def normalize_path(path: str | Path) -> str:
    """Canonical registry key for a repo path: absolute, normpath'd, no symlink
    resolution (so a moved/renamed repo stays comparable, and we never touch the
    filesystem). Inputs are already absolute (an ingest root or a transcript cwd)."""
    return os.path.normpath(str(Path(path).expanduser()))


# ── path → ULID registry ──────────────────────────────────────────────────────

def load_path_registry() -> dict[str, str]:
    """Return `{abs_path: ULID}`. Empty dict on missing/corrupt (fail-safe)."""
    p = get_path_registry_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("path-registry.json unreadable (%s) — treating as empty", exc)
        return {}


def save_path_registry(registry: dict[str, str]) -> None:
    """Persist the registry atomically (tmp + os.replace)."""
    p = get_path_registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def register_path(path: str | Path, ulid: str) -> None:
    """Upsert one `abs_path → ULID` mapping (no-op if already current)."""
    key = normalize_path(path)
    registry = load_path_registry()
    if registry.get(key) == ulid:
        return
    registry[key] = ulid
    save_path_registry(registry)


def resolve_path_to_ulid(
    path: str | Path, registry: Optional[dict[str, str]] = None
) -> Optional[str]:
    """Look up a repo path's ULID (None if not registered — lazy-link)."""
    reg = registry if registry is not None else load_path_registry()
    return reg.get(normalize_path(path))


# ── families ──────────────────────────────────────────────────────────────────

def load_families() -> dict[str, list[str]]:
    """Return `{family_name: [member_abs_path, ...]}`. Empty on missing; a
    malformed file logs loud and degrades to empty (no families) — a bad config
    never crashes ingest/search."""
    p = get_families_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("families.json unreadable (%s) — treating as no families", exc)
        return {}
    if not isinstance(data, dict):
        log.warning("families.json is not an object — treating as no families")
        return {}
    # Coerce to {str: [str]}; drop malformed entries loudly.
    out: dict[str, list[str]] = {}
    for name, members in data.items():
        if isinstance(members, list) and all(isinstance(m, str) for m in members):
            out[name] = members
        else:
            log.warning("families.json: family %r has non-list members — skipped", name)
    return out


def save_families(families: dict[str, list[str]]) -> None:
    """Persist families.json atomically (tmp + os.replace).

    Args:
        families: Mapping of family name → list of absolute member paths.

    Raises:
        TypeError: If ``families`` is not a dict.
    """
    if not isinstance(families, dict):
        raise TypeError(f"families must be a dict, got {type(families).__name__!r}")
    p = get_families_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(families, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def add_family_members(name: str, paths: list[str]) -> dict[str, list[str]]:
    """Add member paths to a family, creating it if absent.

    Each path is normalized via ``normalize_path``. Existing members are
    preserved; duplicates after normalization are removed. Members are stored
    sorted. Persists the result to families.json.

    Args:
        name:  Family name.
        paths: Absolute (or normalizable) paths to add.

    Returns:
        The updated families dict (all families, not just ``name``).
    """
    families = load_families()
    existing: set[str] = {normalize_path(m) for m in families.get(name, [])}
    for p in paths:
        existing.add(normalize_path(p))
    families[name] = sorted(existing)
    save_families(families)
    return families


def remove_family(name: str, paths: Optional[list[str]] = None) -> bool:
    """Remove a family or specific members from it.

    Args:
        name:  Family name.
        paths: When ``None``, remove the entire family. When a list, remove
               only those normalized member paths; if the family becomes empty
               it is dropped entirely.

    Returns:
        ``True`` if anything changed, ``False`` if the family or the
        specified members were not found.
    """
    families = load_families()
    if name not in families:
        return False
    if paths is None:
        del families[name]
        save_families(families)
        return True
    normalized_to_remove = {normalize_path(p) for p in paths}
    current = families[name]
    after = [m for m in current if normalize_path(m) not in normalized_to_remove]
    if len(after) == len(current):
        return False  # nothing matched
    if after:
        families[name] = after
    else:
        del families[name]
    save_families(families)
    return True


def resolve_family_ulids(
    ulid: str,
    *,
    families: Optional[dict[str, list[str]]] = None,
    registry: Optional[dict[str, str]] = None,
) -> set[str]:
    """Return the set of project ULIDs in `ulid`'s family (always includes `ulid`).

    A project is in a family if one of its registered paths is listed in that
    family. Member paths that aren't registered yet (no ULID) are skipped
    (lazy-link). A project in no family resolves to just `{ulid}`.
    """
    fams = families if families is not None else load_families()
    reg = registry if registry is not None else load_path_registry()

    own_paths = {p for p, u in reg.items() if u == ulid}
    result: set[str] = {ulid}
    if not own_paths:
        return result

    for member_paths in fams.values():
        member_keys = {normalize_path(m) for m in member_paths}
        if own_paths & member_keys:  # this project is a member of this family
            for mk in member_keys:
                mu = reg.get(mk)
                if mu:  # lazy-link: un-registered members contribute nothing
                    result.add(mu)
    return result


# ── Claude-Code projects dirname mapping (encode-direction) ────────────────────

def claude_encode(path: str | Path) -> str:
    """Encode an absolute path the way Claude Code names a `~/.claude/projects`
    dir — a flat `/` → `-` substitution (e.g. `/home/user/my-project` →
    `-home-user-my-project`). Used to MATCH a project dirname against registered paths
    in the ENCODE direction; we never reverse a dirname (that decode is ambiguous —
    `osint-toolkit` vs `osint/toolkit`)."""
    return normalize_path(path).replace("/", "-")


def resolve_claude_dir_to_ulid(
    dirname: str, registry: Optional[dict[str, str]] = None
) -> Optional[str]:
    """Map a `~/.claude/projects/<encoded-cwd>` dirname → project ULID by encoding
    each registered abs path and matching. None if no registered path encodes to
    this dirname (lazy-link — the source project isn't registered yet)."""
    reg = registry if registry is not None else load_path_registry()
    for abs_path, ulid in reg.items():
        if claude_encode(abs_path) == dirname:
            return ulid
    return None
