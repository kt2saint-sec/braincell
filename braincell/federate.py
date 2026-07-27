# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
federate.py — opt-in federated family recall (``BRAINCELL_FEDERATE``).

Answers a cross-project ``scope='family'`` recall by FANNING OUT across each
family member's per-project ``braincell.db`` at query time and RRF-merging the
ranked lists — instead of physically merging them into a global brain (``pool``).
The per-project brains stay physically separate and are opened READ-ONLY, so this
strengthens (never erodes) the isolation posture.

Design:
  - **Opt-in / default-off.** ``BRAINCELL_FEDERATE=on`` + PROJECT mode + the
    default ``scope='family'`` path; otherwise ``build_federation_plan`` returns
    None and the caller runs the normal single-store recall unchanged.
  - **Source selection is mandatory** — federation runs over a *family* only
    (``scope='all'`` is intentionally NOT federated: research shows indiscriminate
    fan-out lowers quality; families are the selection primitive — RAGRoute
    arXiv:2502.19280 §4.3, "The Power of Noise" SIGIR 2024).
  - **Corroboration-aware rank-only fusion** (RRF, Cormack SIGIR 2009, k=60,
    1-based) → insensitive to cross-store score/vector-space scale, so
    heterogeneous-embedder siblings can be included. The fusion accumulator is keyed
    by CONTENT (a sha256 of the note/chunk text), not by per-store row id — so the
    SAME lesson independently recorded in N family members SUMS its
    ``weight/(k+rank)`` contributions and ranks ABOVE an equal-rank note that only
    one project holds. (Keying by ``(project_id, id)`` — disjoint across per-project
    stores — made the RRF sum never accumulate: pure weighted interleaving that then
    *dropped* the corroborating copies; content-keying turns duplication into the
    fusion signal it should be.) Optional per-store weighting (LangChain
    ``EnsembleRetriever`` pattern) gives the active project a tunable prior
    (``BRAINCELL_RRF_WEIGHT_ACTIVE``).
  - **Never mix vector spaces.** A sibling whose ``embed_fingerprint`` ≠
    the query embedder's contributes LEXICAL hits only (recall called with
    ``qvec=None``); ``BRAINCELL_FEDERATE_STRICT=on`` skips it entirely.
  - **Fail-degrade.** Each member is isolated in its own coroutine;
    a missing / locked / schema-incompatible sibling is skipped with a warning and
    the merge proceeds with the rest — one bad member never fails the query.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import embed_spec
from .config import get_db_path
from .log import get as _get_log
from .mode import resolve_mode  # legacy compatibility path; ordinary Pool queries use plan_for_pool
from .project_registry import resolve_family_ulids, resolve_pool, resolve_ulid_to_path
from .schema import MEMORY_SCHEMA_VERSION
from .store import Hit, Note, SqliteStore

log = _get_log("braincell.federate")


# ── Env knobs (read at call time so tests can monkeypatch os.environ) ──────────

def federation_enabled() -> bool:
    """True when ``BRAINCELL_FEDERATE=on`` (default off = byte-identical today)."""
    return os.environ.get("BRAINCELL_FEDERATE", "off").strip().lower() == "on"


def _strict() -> bool:
    """True when fingerprint-mismatched siblings should be SKIPPED, not lexical-only."""
    return os.environ.get("BRAINCELL_FEDERATE_STRICT", "off").strip().lower() == "on"


def _rrf_k() -> int:
    """Cross-store RRF constant (shares BRAINCELL_RRF_K with intra-store fusion)."""
    try:
        return int(os.environ.get("BRAINCELL_RRF_K", "60") or 60)
    except ValueError:
        return 60


def _active_weight() -> float:
    """RRF weight multiplier for the active (seed) project's list. Default 1.0
    (uniform). >1.0 gives the working directory a tunable prior — a small thumb on
    the scale, not a filter (LangChain EnsembleRetriever weighting pattern)."""
    try:
        return float(os.environ.get("BRAINCELL_RRF_WEIGHT_ACTIVE", "1.0") or 1.0)
    except ValueError:
        return 1.0


# Max sibling brains opened concurrently (bounds aiosqlite thread churn).
_MAX_CONCURRENCY = 8


# ── Resolved targets / plan ────────────────────────────────────────────────────

@dataclass
class FederationTarget:
    """One resolved family member to federate over."""
    project_id: str
    db_path: Path
    fingerprint_ok: bool  # store's embed space == the query embedder's → vectors usable


@dataclass(frozen=True)
class PoolMemberStatus:
    """One Pool member's explicit read-only query eligibility/result."""

    project_id: str
    status: str
    detail: str


@dataclass
class FederationPlan:
    """A resolved federated-recall plan: the seed project + the members to query."""
    seed_pid: str
    targets: list[FederationTarget]
    member_status: list[PoolMemberStatus] = field(default_factory=list)


def resolve_pool_targets(
    pool_name: str, connected_project_id: str
) -> tuple[str, list[FederationTarget], list[PoolMemberStatus]]:
    """Resolve one explicit Pool to readable member brains.

    Pool membership stores ULIDs, never paths or copied memory.  A current path
    is resolved through the registry for every query; missing, inaccessible,
    corrupt, and incompatible members are returned as structured skipped status
    records. Membership is checked before any member database is opened.
    """
    display_name, ulids = resolve_pool(pool_name)
    if connected_project_id not in ulids:
        raise ValueError(
            f"Connected Project {connected_project_id!r} is not a member of Pool {display_name!r}."
        )
    ordered = sorted(ulids, key=lambda project_id: (project_id != connected_project_id, project_id))
    targets: list[FederationTarget] = []
    member_status: list[PoolMemberStatus] = []
    for project_id in ordered:
        try:
            current_path = resolve_ulid_to_path(project_id)
            if current_path is None:
                log.warning("pool %s: skip %s (no current registered project path)", display_name, project_id)
                member_status.append(PoolMemberStatus(project_id, "missing", "No current registered Project path."))
                continue
            if not current_path.is_dir():
                log.warning("pool %s: skip %s (project path unavailable: %s)", display_name, project_id, current_path)
                member_status.append(PoolMemberStatus(project_id, "unavailable", "Registered Project path is unavailable."))
                continue
            db = get_db_path(project_id)
            if not db.exists():
                log.info("pool %s: skip %s (no built project memory at %s)", display_name, project_id, db)
                member_status.append(PoolMemberStatus(project_id, "missing", "Project memory has not been built."))
                continue
            probe = _read_fingerprint_and_version_ro(db)
            if probe is None:
                member_status.append(PoolMemberStatus(project_id, "corrupt", "Project memory is inaccessible or corrupt."))
                continue
            fingerprint, version = probe
            if version is not None and version != MEMORY_SCHEMA_VERSION:
                log.warning(
                    "pool %s: skip %s (schema v%s != engine v%s)",
                    display_name, project_id, version, MEMORY_SCHEMA_VERSION,
                )
                member_status.append(PoolMemberStatus(project_id, "incompatible", "Project memory schema is incompatible."))
                continue
            fingerprint_ok = fingerprint == embed_spec.FINGERPRINT
            if not fingerprint_ok and _strict():
                log.warning("pool %s: skip %s (embedding fingerprint mismatch)", display_name, project_id)
                member_status.append(PoolMemberStatus(project_id, "incompatible", "Project memory uses a different embedding model."))
                continue
            targets.append(FederationTarget(project_id, db, fingerprint_ok))
            member_status.append(PoolMemberStatus(project_id, "ready", "Opened read-only for this Pool query."))
        except Exception as exc:  # one malformed registry member must not fail the Pool
            log.warning("pool %s: skip %s (%r)", display_name, project_id, exc)
            member_status.append(PoolMemberStatus(project_id, "unavailable", str(exc)))
    return display_name, targets, member_status


def plan_for_pool(pool_name: str, connected_project_id: str) -> FederationPlan:
    """Build an explicit live read-only query plan for exactly one named Pool."""
    _display_name, targets, member_status = resolve_pool_targets(pool_name, connected_project_id)
    return FederationPlan(
        seed_pid=connected_project_id, targets=targets, member_status=member_status
    )


def _mark_pool_member(plan: FederationPlan, project_id: str, status: str, detail: str) -> None:
    """Replace a planned member's status when its actual read fails."""
    for index, item in enumerate(plan.member_status):
        if item.project_id == project_id:
            plan.member_status[index] = PoolMemberStatus(project_id, status, detail)
            return
    plan.member_status.append(PoolMemberStatus(project_id, status, detail))


def _read_fingerprint_and_version_ro(
    db_path: Path,
) -> Optional[tuple[Optional[str], Optional[int]]]:
    """Read a sibling's embed fingerprint + schema version READ-ONLY (no writes).

    Uses ``file:…?mode=ro`` so the probe never migrates or checkpoints the sibling.
    Returns ``(fingerprint, version)`` when the file is a readable database (either
    field may be None if that table is absent), or ``None`` when the file cannot be
    read at all — missing, NOT a database (corrupt / half-written), or a hot WAL with
    no live writer — signalling the caller to SKIP that member. Never raises: a
    corrupt file surfaces as ``sqlite3.DatabaseError`` (the PARENT of
    ``OperationalError``), so we catch ``sqlite3.Error`` — the whole family — not just
    ``OperationalError``.
    """
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    con = None
    try:
        con = sqlite3.connect(uri, uri=True)
        con.execute("PRAGMA query_only=ON")
        con.execute("PRAGMA busy_timeout=5000")
        try:
            frow = con.execute("SELECT fingerprint FROM embed_fingerprint LIMIT 1").fetchone()
            fp = frow[0] if frow else None
        except sqlite3.OperationalError:
            fp = None
        try:
            vrow = con.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            ver = int(vrow[0]) if vrow and vrow[0] is not None else None
        except sqlite3.OperationalError:
            ver = None
        return fp, ver
    except sqlite3.Error as exc:  # corrupt / not-a-db / cannot-open (hot WAL, no writer)
        log.warning("federate: skip %s (unreadable read-only: %s)", db_path, exc)
        return None
    finally:
        if con is not None:
            con.close()


def resolve_federation_targets(seed_pid: str) -> list[FederationTarget]:
    """Resolve a family's members into federated targets (seed first, then sorted).

    Includes only members with a built brain whose schema matches this engine's
    (``MEMORY_SCHEMA_VERSION``) — an incompatible sibling is skipped (logged) rather
    than crashing recall deep inside a mismatched query. A member whose embed
    fingerprint differs is kept but marked ``fingerprint_ok=False`` (lexical-only)
    unless ``BRAINCELL_FEDERATE_STRICT=on``, which drops it.
    """
    ulids = resolve_family_ulids(seed_pid)  # always includes seed_pid
    ordered = sorted(ulids, key=lambda p: (p != seed_pid, p))  # seed first
    targets: list[FederationTarget] = []
    for pid in ordered:
        # Never let one member's resolution failure be fatal:
        # a corrupt / locked / unreadable sibling — or any raise inside
        # resolution — skips that member and the query proceeds with the rest.
        try:
            db = get_db_path(pid)
            if pid == seed_pid:
                # The caller already holds a validated open store for the seed (the
                # server lifespan / GUI opened it and passed the schema + fingerprint
                # gates), so include it unconditionally as a vector-capable target —
                # a transient RO-probe hiccup must never drop the active project.
                targets.append(FederationTarget(project_id=pid, db_path=db, fingerprint_ok=True))
                continue
            if not db.exists():
                log.info("federate: skip %s (no brain built at %s)", pid, db)
                continue
            probe = _read_fingerprint_and_version_ro(db)
            if probe is None:
                continue  # unreadable / corrupt sibling — skip (already logged)
            fp, ver = probe
            if ver is not None and ver != MEMORY_SCHEMA_VERSION:
                log.warning(
                    "federate: skip %s (schema v%s != engine v%s)", pid, ver, MEMORY_SCHEMA_VERSION
                )
                continue
            fp_ok = fp == embed_spec.FINGERPRINT
            if not fp_ok and _strict():
                log.warning("federate: skip %s (embed fingerprint mismatch, STRICT)", pid)
                continue
            targets.append(FederationTarget(project_id=pid, db_path=db, fingerprint_ok=fp_ok))
        except Exception as exc:
            log.warning("federate: skip %s (resolution error: %r)", pid, exc)
            continue
    return targets


def build_federation_plan(
    project: Optional[str],
    scope: str,
    projects: Optional[list[str]],
) -> Optional[FederationPlan]:
    """Return a FederationPlan when this recall should federate, else None.

    Federates ONLY when: the flag is on, the engine is in PROJECT mode, the caller
    used the plain ``scope='family'`` path (no explicit ``project`` / ``projects``
    override), and a seed project is configured. Global mode returns None (the
    global DB already holds every project — use the in-DB filter). Any other case
    returns None → the caller runs the normal single-store recall unchanged.

    Raises:
        ValueError: federation requested but ``BRAINCELL_PROJECT_ID`` is unset —
                    family recall needs a reference project (mirrors the edge-#3
                    guard in server._resolve_scope).
    """
    if not federation_enabled():
        return None
    if project is not None or projects:
        return None
    if scope != "family":
        return None
    if resolve_mode() != "project":
        return None
    seed_pid = os.environ.get("BRAINCELL_PROJECT_ID", "").strip()
    if not seed_pid:
        raise ValueError(
            "Federated family recall requires BRAINCELL_PROJECT_ID — the server "
            "needs a reference project to resolve its family."
        )
    return FederationPlan(seed_pid=seed_pid, targets=resolve_federation_targets(seed_pid))


def plan_for_seed(seed_pid: str) -> FederationPlan:
    """Build a federation plan for an explicit seed project (bypasses the env gate).

    Used by callers that already know their seed project — e.g. the GUI, which
    resolves it from the launched path rather than ``BRAINCELL_PROJECT_ID``.
    """
    return FederationPlan(seed_pid=seed_pid, targets=resolve_federation_targets(seed_pid))


# ── Cross-store RRF merge + dedup ──────────────────────────────────────────────

def _rrf_merge_notes(
    ranked_lists: list[tuple[str, list[Note]]],
    *,
    seed_pid: str,
    active_weight: float,
    k_rrf: int,
) -> list[Note]:
    """Corroboration-aware weighted Reciprocal Rank Fusion across N ranked lists.

    ``ranked_lists`` is ``[(project_id, [Note, …]), …]`` in target order (seed
    first). Each note is keyed by a **content hash** (sha256 of ``note.content``),
    NOT ``(project_id, id)``: per-project row ids are disjoint across the separate
    stores, so keying by id would make the RRF sum never accumulate — pure weighted
    interleaving. Content-keying instead SUMS ``Σ weight / (k_rrf + rank)`` (1-based
    rank; Cormack 2009; LangChain EnsembleRetriever formula) whenever the same text
    recurs across members, so a corroborated note fuses to a strictly higher score
    than an equal-rank note only one project holds — and this fold makes a separate
    content-dedup pass redundant (each content appears once in the output). The seed
    (active) project's list is scaled by ``active_weight`` (default 1.0 = uniform).
    The representative Note kept per content is the first seen in (target, rank)
    order — the seed's instance when the seed holds that content, else the earliest
    member's. Deterministic total order:
    ``(fused DESC, best_rank ASC, (representative project_id, id) ASC)`` — the
    explicit tie-break every reference implementation omits.
    """
    scores: dict[str, float] = {}
    best_rank: dict[str, int] = {}
    note_of: dict[str, Note] = {}
    for pid, notes in ranked_lists:
        weight = active_weight if pid == seed_pid else 1.0
        for rank, note in enumerate(notes, start=1):  # 1-based (Elastic/Cormack)
            key = hashlib.sha256(note.content.encode("utf-8")).hexdigest()
            scores[key] = scores.get(key, 0.0) + weight / (k_rrf + rank)
            if key not in best_rank or rank < best_rank[key]:
                best_rank[key] = rank
            note_of.setdefault(key, note)  # first-seen = seed's instance if present
    ordered_keys = sorted(
        scores.keys(),
        key=lambda key: (-scores[key], best_rank[key], (note_of[key].project_id, note_of[key].id)),
    )
    return [note_of[key] for key in ordered_keys]


def _dedup_by_content(notes: list[Note]) -> list[Note]:
    """Collapse notes with identical content, keeping the first (highest-ranked).

    NO LONGER on the federated-recall merge path — ``_rrf_merge_notes`` now folds
    byte-identical cross-project content into one fused key itself (corroboration is
    the fusion signal), so a separate dedup pass would be redundant. Retained as a
    standalone utility (and unit-tested) for callers that post-process an
    already-fused list. Content-hash dedup matches the merge dedup in LangChain
    (``page_content``) and LlamaIndex (``node.hash``). Cross-store *semantic*
    (near-duplicate) dedup is intentionally NOT done here — the Note contract carries
    no embedding, and cosine across heterogeneous embedders is meaningless; intra-store
    cosine dedup already ran inside each recall().
    """
    seen: set[str] = set()
    out: list[Note] = []
    for note in notes:
        h = hashlib.sha256(note.content.encode("utf-8")).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        out.append(note)
    return out


# ── Orchestrator ───────────────────────────────────────────────────────────────

async def federated_recall(
    self_store: SqliteStore | None,
    plan: FederationPlan,
    qvec,
    k: int,
    *,
    qtext: str = "",
    min_cosine: Optional[float] = None,
    dedup: bool = True,
    include_superseded: bool = False,
) -> list[Note]:
    """Fan out recall across a family's brains and RRF-merge the ranked lists.

    Reuses the already-open ``self_store`` for the seed project and opens every
    other member READ-ONLY (``mode=ro`` — never written/migrated). Each member runs
    the existing ``recall`` (``rerank=False`` so per-store rerank never double-fires)
    with ``qvec`` only when its embed space matches; mismatched members get
    ``qvec=None`` (lexical/recency contribution). Members are isolated: a failure
    yields ``[]`` for that member and is logged, never propagated. Results are
    content-keyed RRF-merged (weighted; identical cross-project notes corroborate
    into one fused entry), optionally reranked over a bounded window, then truncated
    to ``k``.

    Supersession is resolved INSIDE each member's ``recall``, never after the merge:
    ``superseded_by`` is an id local to one database file, and ``_rrf_merge_notes``
    keys on content hash — post-merge the local ids no longer identify anything.
    Each member therefore contributes its own current truth, and those compete.
    """
    fetch_k = max(k * 3, 30)
    sem = asyncio.Semaphore(_MAX_CONCURRENCY)

    async def _recall_one(target: FederationTarget) -> tuple[str, list[Note]]:
        async with sem:
            is_self = target.project_id == plan.seed_pid and self_store is not None
            store = self_store if is_self else SqliteStore(target.db_path, read_only=True)
            try:
                use_vec = qvec if target.fingerprint_ok else None
                notes = await store.recall(
                    use_vec,
                    target.project_id,
                    fetch_k,
                    qtext=qtext,
                    min_cosine=min_cosine,
                    dedup=dedup,
                    rerank=False,
                    include_superseded=include_superseded,
                )
                # Strip graph-expansion "also-see" notes: second-degree hits should
                # not compete with direct hits from other members in the merge.
                return target.project_id, [n for n in notes if not n.expansion]
            except Exception as exc:  # fail-degrade: one bad member never kills recall
                log.warning("federate: member %s skipped (%r)", target.project_id, exc)
                _mark_pool_member(plan, target.project_id, "query-failed", str(exc))
                return target.project_id, []
            finally:
                if not is_self:
                    await store.aclose()

    ranked_lists = await asyncio.gather(*(_recall_one(t) for t in plan.targets))

    # RRF merge is corroboration-aware and content-keyed, so it already folds
    # byte-identical cross-project notes into one fused entry — no separate
    # _dedup_by_content pass needed (that dropped the corroborating copies).
    merged = _rrf_merge_notes(
        list(ranked_lists),
        seed_pid=plan.seed_pid,
        active_weight=_active_weight(),
        k_rrf=_rrf_k(),
    )

    # Optional single rerank over a bounded window (mirrors store.search: never
    # rerank the full N×fetch_k merge — that would be a latency foot-gun).
    from .rerank import rerank_enabled, rerank_notes, rerank_window
    if rerank_enabled() and qtext and merged:
        window = max(k, rerank_window())
        return await rerank_notes(qtext, merged[:window], top_k=k)
    return merged[:k]


# ── Search (documents/chunks) federation — Phase 2 ─────────────────────────────

def _rrf_merge_hits(
    ranked_lists: list[tuple[str, list[Hit]]],
    *,
    seed_pid: str,
    active_weight: float,
    k_rrf: int,
) -> list[Hit]:
    """Corroboration-aware weighted RRF across N ranked Hit lists (chunk search).

    A ``Hit`` carries no ``project_id`` and per-store ``chunk_id``s are disjoint
    across the separate brains, so — mirroring ``_rrf_merge_notes`` — a hit is keyed
    by a **content hash** (sha256 of ``doc_key`` + ``snippet``), NOT ``(member_pid,
    chunk_id)``. Identical chunk text surfacing from multiple members then SUMS its
    ``weight/(k_rrf+rank)`` contributions (true fusion) rather than staying disjoint
    and being deduped away — so a corroborated chunk outranks an equal-rank chunk
    only one member holds, and the fold makes a separate dedup pass redundant. The
    representative Hit kept per content is the first seen in (target, rank) order;
    its originating member pid is tracked for the deterministic tie-break. Same
    rank-only formula and weighting as ``_rrf_merge_notes``. Deterministic total
    order: ``(fused DESC, best_rank ASC, (representative member_pid, chunk_id) ASC)``.
    """
    scores: dict[str, float] = {}
    best_rank: dict[str, int] = {}
    hit_of: dict[str, Hit] = {}
    rep_pid: dict[str, str] = {}
    for pid, hits in ranked_lists:
        weight = active_weight if pid == seed_pid else 1.0
        for rank, hit in enumerate(hits, start=1):
            key = hashlib.sha256(f"{hit.doc_key}\n{hit.snippet}".encode("utf-8")).hexdigest()
            scores[key] = scores.get(key, 0.0) + weight / (k_rrf + rank)
            if key not in best_rank or rank < best_rank[key]:
                best_rank[key] = rank
            if key not in hit_of:  # first-seen = seed's instance if present
                hit_of[key] = hit
                rep_pid[key] = pid
    ordered_keys = sorted(
        scores.keys(),
        key=lambda key: (-scores[key], best_rank[key], (rep_pid[key], hit_of[key].chunk_id)),
    )
    return [hit_of[key] for key in ordered_keys]


def _dedup_hits(hits: list[Hit]) -> list[Hit]:
    """Collapse chunk hits with identical (doc_key + snippet), keeping the first.

    NO LONGER on the federated-search merge path — ``_rrf_merge_hits`` now folds
    identical cross-project chunk text into one fused key itself (corroboration is
    the fusion signal). Retained as a standalone utility (and unit-tested) for
    callers that post-process an already-fused list (parity with the notes-side
    ``_dedup_by_content``).
    """
    seen: set[str] = set()
    out: list[Hit] = []
    for hit in hits:
        h = hashlib.sha256(f"{hit.doc_key}\n{hit.snippet}".encode("utf-8")).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        out.append(hit)
    return out


async def federated_search(
    self_store: SqliteStore | None,
    plan: FederationPlan,
    qvec,
    qtext: str,
    k: int,
    mode: str,
) -> list[Hit]:
    """Fan out chunk search across a family's brains and RRF-merge the Hit lists.

    Mirrors ``federated_recall``: seed reuses ``self_store``; other members open
    READ-ONLY. A fingerprint-mismatched member is searched in ``keyword`` mode
    (never scoring foreign vectors); members are isolated (a failure yields ``[]``);
    per-store rerank is disabled and a single bounded rerank runs at the merge.
    """
    fetch_k = max(k * 3, 30)
    sem = asyncio.Semaphore(_MAX_CONCURRENCY)

    async def _search_one(target: FederationTarget) -> tuple[str, list[Hit]]:
        async with sem:
            is_self = target.project_id == plan.seed_pid and self_store is not None
            store = self_store if is_self else SqliteStore(target.db_path, read_only=True)
            try:
                use_mode = mode if target.fingerprint_ok else "keyword"
                hits = await store.search(
                    qvec, qtext, target.project_id, fetch_k, use_mode, rerank=False
                )
                return target.project_id, hits
            except Exception as exc:  # fail-degrade
                log.warning("federate(search): member %s skipped (%r)", target.project_id, exc)
                _mark_pool_member(plan, target.project_id, "query-failed", str(exc))
                return target.project_id, []
            finally:
                if not is_self:
                    await store.aclose()

    ranked_lists = await asyncio.gather(*(_search_one(t) for t in plan.targets))
    # Content-keyed RRF already folds identical cross-project chunks — no separate
    # _dedup_hits pass needed.
    merged = _rrf_merge_hits(
        list(ranked_lists), seed_pid=plan.seed_pid, active_weight=_active_weight(), k_rrf=_rrf_k()
    )

    from .rerank import rerank_enabled, rerank_hits, rerank_window
    if rerank_enabled() and qtext and merged:
        window = max(k, rerank_window())
        return await rerank_hits(qtext, merged[:window], top_k=k)
    return merged[:k]
