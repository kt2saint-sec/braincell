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
import unicodedata
from pathlib import Path

from .catalog_io import atomic_write_json, catalog_lock
from .config import get_families_path, get_path_registry_path, get_pools_path
from .log import get as _get_log

log = _get_log("braincell.project_registry")


def is_safe_project_id(value: object) -> bool:
    """Return whether *value* is a safe opaque on-disk Project identifier.

    Historical test/install catalogs contain pre-ULID identifiers, so this
    deliberately validates the filesystem security boundary rather than
    rejecting those existing catalogs: one non-empty path component, never an
    absolute path, traversal, or a value containing a path separator.
    """
    if not isinstance(value, str) or not value or value in {".", ".."}:
        return False
    return (
        not Path(value).is_absolute()
        and Path(value).name == value
        and "/" not in value
        and "\\" not in value
        and "\x00" not in value
    )


def _valid_registry(data: object) -> bool:
    return isinstance(data, dict) and all(
        isinstance(key, str)
        and Path(key).is_absolute()
        and is_safe_project_id(value)
        for key, value in data.items()
    )


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
        if _valid_registry(data):
            return data
        log.warning(
            "path-registry.json has unsafe or invalid entries — treating as empty"
        )
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("path-registry.json unreadable (%s) — treating as empty", exc)
        return {}


def _load_path_registry_for_mutation(path: Path) -> dict[str, str]:
    """Load a registry for RMW, refusing to reinterpret corruption as empty."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(
            f"Path registry is unreadable; refusing to mutate {path}: {exc}"
        ) from exc
    if not _valid_registry(data):
        raise RuntimeError(
            f"Path registry has an invalid format; refusing to mutate {path}"
        )
    return data


def save_path_registry(registry: dict[str, str]) -> None:
    """Persist a complete registry under the catalog lock."""
    if not _valid_registry(registry):
        raise ValueError("Refusing to save an unsafe or invalid path registry.")
    p = get_path_registry_path()
    with catalog_lock(p):
        _load_path_registry_for_mutation(p)
        atomic_write_json(p, registry, sort_keys=True)


def register_path(path: str | Path, ulid: str) -> None:
    """Upsert one `abs_path → ULID` mapping (no-op if already current)."""
    if not is_safe_project_id(ulid):
        raise ValueError(f"Unsafe Project identity: {ulid!r}")
    key = normalize_path(path)
    catalog = get_path_registry_path()
    with catalog_lock(catalog):
        registry = _load_path_registry_for_mutation(catalog)
        owner = registry.get(key)
        if owner == ulid:
            return
        if owner is not None:
            raise ValueError(
                f"Path {key} is already owned by Project {owner}; "
                "use Project reassociation to change identity."
            )
        registry[key] = ulid
        atomic_write_json(catalog, registry, sort_keys=True)


def get_or_create_project_id(path: str | Path, factory) -> str:
    """Atomically resolve or mint one stable identity for a normalized path."""
    key = normalize_path(path)
    catalog = get_path_registry_path()
    with catalog_lock(catalog):
        registry = _load_path_registry_for_mutation(catalog)
        existing = registry.get(key)
        if existing is not None:
            return existing
        project_id = str(factory())
        if not is_safe_project_id(project_id):
            raise ValueError("Project identity factory returned an unsafe value.")
        registry[key] = project_id
        atomic_write_json(catalog, registry, sort_keys=True)
        return project_id


def reassociate_project_path(ulid: str, path: str | Path) -> tuple[Path, Path]:
    """Move one stable ULID's path registration without touching memory or Pools."""
    key = normalize_path(path)
    catalog = get_path_registry_path()
    with catalog_lock(catalog):
        registry = _load_path_registry_for_mutation(catalog)
        old_paths = sorted(
            existing_path for existing_path, existing_id in registry.items()
            if existing_id == ulid
        )
        if not old_paths:
            raise KeyError(f"Project {ulid!r} is not registered.")
        owner = registry.get(key)
        if owner is not None and owner != ulid:
            raise ValueError(
                f"Destination {key} is already owned by Project {owner}."
            )
        updated = {
            existing_path: existing_id
            for existing_path, existing_id in registry.items()
            if existing_id != ulid
        }
        updated[key] = ulid
        atomic_write_json(catalog, updated, sort_keys=True)
        return Path(old_paths[0]), Path(key)


def resolve_ulid_to_path(ulid: str, registry: dict[str, str] | None = None) -> Path | None:
    """Resolve a stable ULID to its currently registered path, if any."""
    paths = [path for path, candidate in (registry or load_path_registry()).items() if candidate == ulid]
    return Path(min(paths)) if paths else None


# ── Orphan reconciliation (READ-ONLY inventory) ───────────────────────────────

def find_orphans() -> dict[str, list[dict[str, str]]]:
    """Preview-only inventory of two independent orphan classes.

    - ``orphaned_registry_entries``: path-registry rows whose registered path
      no longer exists on disk — the repo was deleted or moved without
      ``braincell project reassociate``. Memory for that ULID is untouched and
      still reachable by ULID; only the path mapping is stale.
    - ``orphaned_project_databases``: a ``projects/<ulid>/braincell.db`` on
      disk with no path-registry row naming that ULID — the registry entry was
      lost or never written for an existing brain.

    Detection only: this never deletes a registry row, a database, or any
    memory, and never reassociates a path (that is
    ``reassociate_project_path``, already live via `braincell project
    reassociate`). Callers decide what — if anything — to do with the list.
    """
    from . import config

    registry = load_path_registry()
    registered_ulids: set[str] = set(registry.values())
    orphaned_paths = sorted(
        (
            {"path": path_str, "project_id": ulid}
            for path_str, ulid in registry.items()
            if not Path(path_str).exists()
        ),
        key=lambda item: item["path"],
    )

    projects_root = config._xdg_data_home() / config.DATA_NAMESPACE / "projects"
    orphaned_databases: list[dict[str, str]] = []
    if projects_root.is_dir():
        for entry in projects_root.iterdir():
            if not entry.is_dir() or entry.name in registered_ulids:
                continue
            db_path = entry / "braincell.db"
            if db_path.is_file():
                orphaned_databases.append(
                    {"project_id": entry.name, "database": str(db_path)}
                )
    orphaned_databases.sort(key=lambda item: item["project_id"])

    return {
        "orphaned_registry_entries": orphaned_paths,
        "orphaned_project_databases": orphaned_databases,
    }


# ── Pools (ULID membership only) ────────────────────────────────────────────

def normalize_pool_name(name: str) -> str:
    """Canonical Pool-name key while preserving the caller's display spelling."""
    if not isinstance(name, str):
        raise TypeError("Pool name must be a string.")
    normalized = " ".join(unicodedata.normalize("NFKC", name).strip().split()).casefold()
    if not normalized:
        raise ValueError("Pool name cannot be empty.")
    return normalized


def _load_pools_document() -> dict[str, object]:
    path = get_pools_path()
    if not path.exists():
        return {"version": 1, "pools": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Pool membership metadata is unreadable: {path} ({exc})") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("pools"), list):
        raise RuntimeError(f"Pool membership metadata has an unsupported format: {path}")
    return raw


def _save_pools_document(document: dict[str, object]) -> None:
    path = get_pools_path()
    atomic_write_json(path, document)


def _pool_records(document: dict[str, object]) -> list[dict[str, object]]:
    records = document["pools"]
    if not isinstance(records, list):  # defensive, checked by _load_pools_document
        raise RuntimeError("Pool membership metadata has invalid records.")  # noqa: TRY004  # Corrupt persisted metadata is not caller type input.
    return records  # type: ignore[return-value]


def load_pools() -> dict[str, tuple[str, ...]]:
    """Return display Pool names mapped to sorted, de-duplicated project ULIDs."""
    out: dict[str, tuple[str, ...]] = {}
    for record in _pool_records(_load_pools_document()):
        name = record.get("name")
        members = record.get("members")
        if isinstance(name, str) and isinstance(members, list) and all(isinstance(member, str) for member in members):
            out[name] = tuple(sorted(set(members)))
    return out


def resolve_pool(name: str) -> tuple[str, tuple[str, ...]]:
    """Resolve a display/name-normalized Pool selector to its display name and members."""
    document = _load_pools_document()
    record = _find_pool(_pool_records(document), name)
    if record is None:
        raise KeyError(f"Pool {name!r} does not exist.")
    display = record.get("name")
    members = record.get("members")
    if not isinstance(display, str) or not isinstance(members, list) or not all(
        isinstance(member, str) for member in members
    ):
        raise RuntimeError("Pool membership metadata has invalid members.")
    return display, tuple(sorted(set(members)))


def _find_pool(records: list[dict[str, object]], name: str) -> dict[str, object] | None:
    key = normalize_pool_name(name)
    for record in records:
        if record.get("normalized_name") == key:
            return record
    return None


def create_pool(name: str) -> dict[str, tuple[str, ...]]:
    """Create an empty named Pool. Names collide by documented normalization."""
    catalog = get_pools_path()
    with catalog_lock(catalog):
        document = _load_pools_document()
        records = _pool_records(document)
        if _find_pool(records, name) is not None:
            raise ValueError(f"A Pool named {name!r} already exists after name normalization.")
        display = " ".join(unicodedata.normalize("NFKC", name).strip().split())
        records.append({"name": display, "normalized_name": normalize_pool_name(name), "members": []})
        records.sort(key=lambda item: str(item["normalized_name"]))
        _save_pools_document(document)
    return load_pools()


def add_to_pool(name: str, project_ids: list[str]) -> tuple[str, ...]:
    """Add stable ULIDs to one Pool without duplicating memberships."""
    if not project_ids or any(not isinstance(project_id, str) or not project_id.strip() for project_id in project_ids):
        raise ValueError("Add to Pool requires at least one non-empty project ULID.")
    catalog = get_pools_path()
    with catalog_lock(catalog):
        document = _load_pools_document()
        record = _find_pool(_pool_records(document), name)
        if record is None:
            raise KeyError(f"Pool {name!r} does not exist.")
        members = record.get("members")
        if not isinstance(members, list):
            raise RuntimeError("Pool membership metadata has invalid members.")  # noqa: TRY004  # Corrupt persisted metadata is not caller type input.
        record["members"] = sorted({str(member) for member in members} | {project_id.strip() for project_id in project_ids})
        _save_pools_document(document)
        return tuple(record["members"])  # type: ignore[return-value]


def decouple_from_pool(name: str, project_id: str) -> bool:
    """Remove one Pool membership only; project data and other Pools are untouched."""
    catalog = get_pools_path()
    with catalog_lock(catalog):
        document = _load_pools_document()
        record = _find_pool(_pool_records(document), name)
        if record is None:
            raise KeyError(f"Pool {name!r} does not exist.")
        members = record.get("members")
        if not isinstance(members, list) or project_id not in members:
            return False
        record["members"] = [member for member in members if member != project_id]
        _save_pools_document(document)
        return True


def delete_pool(name: str) -> bool:
    """Delete only the membership definition, never a Project or its memory."""
    catalog = get_pools_path()
    with catalog_lock(catalog):
        document = _load_pools_document()
        records = _pool_records(document)
        record = _find_pool(records, name)
        if record is None:
            return False
        records.remove(record)
        _save_pools_document(document)
        return True


def pools_for_project(project_id: str) -> tuple[str, ...]:
    """Return every Pool containing a Project ULID; memberships never union silently."""
    return tuple(sorted(name for name, members in load_pools().items() if project_id in members))


def resolve_path_to_ulid(
    path: str | Path, registry: dict[str, str] | None = None
) -> str | None:
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


def _load_families_for_mutation(path: Path) -> dict[str, list[str]]:
    """Load and fully validate family metadata before a read-modify-write."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(
            f"Family metadata is unreadable; refusing to mutate {path}: {exc}"
        ) from exc
    if not isinstance(data, dict) or not all(
        isinstance(name, str)
        and isinstance(members, list)
        and all(isinstance(member, str) for member in members)
        for name, members in data.items()
    ):
        raise RuntimeError(
            f"Family metadata has an invalid format; refusing to mutate {path}"
        )
    return data


def save_families(families: dict[str, list[str]]) -> None:
    """Persist families.json atomically (tmp + os.replace).

    Args:
        families: Mapping of family name → list of absolute member paths.

    Raises:
        TypeError: If ``families`` is not a dict.
    """
    if not isinstance(families, dict):
        raise TypeError(f"families must be a dict, got {type(families).__name__!r}")
    if not all(
        isinstance(name, str)
        and isinstance(members, list)
        and all(isinstance(member, str) for member in members)
        for name, members in families.items()
    ):
        raise ValueError("Refusing to save invalid family metadata.")
    p = get_families_path()
    with catalog_lock(p):
        _load_families_for_mutation(p)
        atomic_write_json(p, families, sort_keys=True)


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
    catalog = get_families_path()
    with catalog_lock(catalog):
        families = _load_families_for_mutation(catalog)
        existing: set[str] = {normalize_path(m) for m in families.get(name, [])}
        for p in paths:
            existing.add(normalize_path(p))
        families[name] = sorted(existing)
        atomic_write_json(catalog, families, sort_keys=True)
        return families


def remove_family(name: str, paths: list[str] | None = None) -> bool:
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
    catalog = get_families_path()
    with catalog_lock(catalog):
        families = _load_families_for_mutation(catalog)
        if name not in families:
            return False
        if paths is None:
            del families[name]
            atomic_write_json(catalog, families, sort_keys=True)
            return True
        normalized_to_remove = {normalize_path(p) for p in paths}
        current = families[name]
        after = [m for m in current if normalize_path(m) not in normalized_to_remove]
        if len(after) == len(current):
            return False
        if after:
            families[name] = after
        else:
            del families[name]
        atomic_write_json(catalog, families, sort_keys=True)
        return True


def resolve_family_ulids(
    ulid: str,
    *,
    families: dict[str, list[str]] | None = None,
    registry: dict[str, str] | None = None,
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
    dirname: str, registry: dict[str, str] | None = None
) -> str | None:
    """Map a `~/.claude/projects/<encoded-cwd>` dirname → project ULID by encoding
    each registered abs path and matching. None if no registered path encodes to
    this dirname (lazy-link — the source project isn't registered yet)."""
    reg = registry if registry is not None else load_path_registry()
    for abs_path, ulid in reg.items():
        if claude_encode(abs_path) == dirname:
            return ulid
    return None
