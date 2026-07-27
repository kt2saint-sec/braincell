# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
server.py — BrainCell standalone FastMCP-stdio server.

Run directly: `braincell-mcp` (console script) or `python -m braincell.server`.

Tools are registered with short names (search, recall, remember, get_document,
ingest_status, list_documents, list_projects, list_families).
FastMCP exposes them as `mcp__braincell__<name>`
because the server key is "braincell" — no double-prefix.  JSON-schema-validated
at boundary.  All SQL parameterized; no raw-SQL / admin tool exposed.

Secret-scan before `remember` persist.
Schema-version refusal in lifespan.
Fails closed if store cannot be opened.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from .log import get as _get_log
from .embed import embed_query_async, embed_texts_async
from .mode import resolve_mode
from .project_registry import (
    load_families,
    load_path_registry,
    normalize_path,
)
from .store import open_store, Hit, Note, SqliteStore

log = _get_log("braincell.server")


# ── Lifespan (store open / schema check / close) ─────────────────────────────

@dataclass
class AppState:
    store: SqliteStore


@asynccontextmanager
async def _lifespan(_server: FastMCP):  # type: ignore[type-arg]
    """Open the store, verify schema, yield, close."""
    store = open_store()          # fails closed (sys.exit 1) if config absent
    store.assert_schema_version()  # refuses on mismatch
    log.info("BrainCell store opened: %s", store._db_path)
    try:
        yield AppState(store=store)
    finally:
        await store.aclose()
        log.info("BrainCell store closed")


mcp = FastMCP("braincell", lifespan=_lifespan)


# ── Helper: extract store from context ────────────────────────────────────────

def _store(ctx: Context) -> SqliteStore:  # type: ignore[type-arg]
    state: AppState = ctx.request_context.lifespan_context
    return state.store


def _resolve_scope(project: Optional[str], scope: str):
    """Resolve the project filter for a search or recall.

    The MCP connection owns one Project database. ``scope='self'`` is the only
    normal-runtime scope; a future explicit named-Pool MCP action must opt in to
    cross-Project reads rather than rely on a hidden fan-out flag.
    """
    if project is not None:
        return project
    if scope == "self":
        pid = os.environ.get("BRAINCELL_PROJECT_ID") or None
        return pid
    if scope in ("family", "all"):
        raise ValueError(
            f"scope={scope!r} is retired. Normal Recall and Search use only the "
            "connected project; use an explicit named Pool action for cross-project reads."
        )
    raise ValueError(
        f"scope={scope!r} is not valid. Use 'self' (default)."
    )


def _resolve_filter(
    projects: Optional[list[str]],
    project: Optional[str],
    scope: str,
) -> Any:
    """Resolve the project filter with G5 multi-project precedence.

    Precedence (applied in order):

    - Explicit ``projects`` list (non-None, non-empty) → that list IS the filter.
      In project mode, only a single-entry list whose sole entry matches the
      configured ``BRAINCELL_PROJECT_ID`` (the "self" project) is permitted.
      Any other list — multi-entry OR a single non-self entry — raises
      ``ValueError`` (cross-project selection requires global mode).
      In global mode, the list is passed straight through.
    - Falls through to ``_resolve_scope(project, scope)`` when ``projects`` is
      ``None`` or an empty list.

    Args:
        projects: Caller-supplied list of project ULIDs, or ``None``/empty list
                  to delegate to scope resolution.  Each entry must be a
                  non-empty string; the list is capped at 200 entries.
        project:  Single explicit project ULID (overrides ``scope``); forwarded
                  to ``_resolve_scope`` when ``projects`` is absent/empty.
        scope:    Scope string (``'self'``/``'family'``/``'all'``); forwarded to
                  ``_resolve_scope`` when ``projects`` is absent/empty.

    Returns:
        A ULID string, a list of ULID strings, or ``None`` — exactly the shapes
        ``store.search`` and ``store.recall`` accept as their project filter.

    Raises:
        ValueError: For invalid entries, oversized list, or cross-project
                    selection in project mode.
    """
    if projects:  # non-None and non-empty → G5 path
        if len(projects) > 200:
            raise ValueError(
                f"projects list must not exceed 200 entries (got {len(projects)})."
            )
        for p in projects:
            if not p or not p.strip():
                raise ValueError(
                    "projects entries must be non-empty strings; found an empty or "
                    "whitespace-only entry."
                )
        if resolve_mode() != "global":
            self_pid = os.environ.get("BRAINCELL_PROJECT_ID", "")
            if not (len(projects) == 1 and projects[0] == self_pid):
                raise ValueError(
                    "projects=[...] spanning multiple or non-self projects requires "
                    "global mode. Set BRAINCELL_MODE=global and open a global brain, "
                    "or use scope='self' (the default) for this project's brain."
                )
        return projects
    return _resolve_scope(project, scope)


def _pin_write_project(project: Optional[str]) -> str:
    """Resolve the project a WRITE (remember/forget/supersede) attributes to.

    Per-project v1 pins to BRAINCELL_PROJECT_ID: a caller may omit `project` (we
    use the configured one) or pass the matching ULID, but a DIFFERENT project is
    rejected — never silently swapped (F12). Falls back to the explicit `project`
    when no server project is configured (tests/direct runs).
    """
    configured = os.environ.get("BRAINCELL_PROJECT_ID")
    if configured:
        if project and project.strip() and project.strip() != configured:
            raise ValueError(
                f"This braincell instance is scoped to project {configured}. "
                f"Refusing to operate on a note attributed to {project!r}. Omit the "
                f"`project` argument, or pass the matching ULID."
            )
        return configured
    if not project or not project.strip():
        raise ValueError("project must be provided when BRAINCELL_PROJECT_ID is unset.")
    return project.strip()


def _pin_read_project(project: Optional[str]) -> Optional[str]:
    """Pin a read-only lookup to the self project in global mode.

    In global mode a missing ``project`` argument would otherwise aggregate
    across ALL projects in the shared DB — a cross-project leak.  This helper
    defaults ``project`` to ``BRAINCELL_PROJECT_ID`` when the server runs in
    global mode so ``get_document`` / ``list_documents`` / ``ingest_status``
    stay scoped to the configured 'self' project unless the caller passes an
    explicit project ULID.

    In project mode behaviour is unchanged — the per-project DB only holds one
    project's rows anyway, so no extra filter is injected.

    Args:
        project: The caller-supplied project ULID, or None when omitted.

    Returns:
        The explicit project (unchanged if given), ``BRAINCELL_PROJECT_ID`` in
        global mode when project is None (may itself be None if the env var is
        unset), or None in project mode when project is None.
    """
    if project is not None:
        return project
    if resolve_mode() == "global":
        return os.environ.get("BRAINCELL_PROJECT_ID") or None
    return None


# ── Structured-output models ─────────────────────────────────────────────────

class SearchHit(BaseModel):
    """One ranked search result chunk."""
    chunk_id: int
    doc_key: str
    title: str
    snippet: str
    score: float
    cosine: Optional[float]
    fts_matched: bool
    source_path: Optional[str]


class MemoryNote(BaseModel):
    """One curated memory note returned by recall."""
    id: int
    project_id: str
    scope: str
    kind: str
    content: str
    tags: Optional[list[str]]
    confidence: Optional[float]
    source_hint: Optional[str]
    superseded_by: Optional[int]
    created_at: str
    # Retrieval provenance — how this note reached the result, so authority can be
    # judged rather than assumed. 'direct' = matched the query; 'resolved' = the
    # current answer, reached by following a superseded note that matched
    # (`resolved_from` / `history` say what it replaced); 'linked' = an "also-see"
    # note pulled in over the note graph from `linked_from`; 'chunk' = a ranked
    # transcript EXCERPT (kind='excerpt', NEGATIVE id — never a real note id)
    # backfilled when curated notes are sparse. Corpus text, not curated memory.
    retrieval_origin: str = "direct"
    resolved_from: Optional[int] = None
    history: list[dict] = []
    linked_from: Optional[int] = None
    relation: Optional[str] = None
    relation_weight: Optional[float] = None


class ConflictHit(BaseModel):
    """An existing ACTIVE note the new note may contradict or duplicate.

    Advisory only — the note was persisted regardless. If the new note REPLACES
    one of these, call `supersede(note_id, ...)` on the old note; if it merely
    relates, no action is needed. Never auto-resolved: choosing the write target
    from recalled content is the memory-poisoning path this server rejects.
    """
    note_id: int
    kind: str
    cosine: float
    snippet: str


class RememberResult(BaseModel):
    """Return value of remember — the persisted note id + embed status.

    potential_conflicts (contradiction guard): ACTIVE notes embedding-close
    to the new one (cosine ≥ BRAINCELL_CONFLICT_COS, default 0.85). Cosine
    cannot tell contradiction from paraphrase — review them, and `supersede`
    whichever this note genuinely replaces.
    """
    note_id: str
    embedded: bool = False
    potential_conflicts: list[ConflictHit] = []


class ForgetResult(BaseModel):
    """Return value of forget — whether a note was tombstoned or removed."""
    deleted: bool
    hard: bool = False


class SupersedeResult(BaseModel):
    """Return value of supersede — new note id, the old id, and embed status."""
    new_id: int
    superseded: int
    embedded: bool = False


class DocumentResult(BaseModel):
    """Full document record returned by get_document."""
    id: int
    doc_key: str
    title: str
    content_type: str
    commit_sha: Optional[str]
    created_at: str
    updated_at: Optional[str]
    chunks: list[Any]
    metadata: Optional[Any]


class IngestStatusResult(BaseModel):
    """Ingest-status snapshot returned by ingest_status."""
    indexed: bool
    doc_count: int
    chunk_count: int
    last_ingest_ts: Optional[str]
    head_sha: Optional[str]
    stale: bool


class DocumentSummary(BaseModel):
    """Per-document summary row returned by list_documents."""
    doc_key: str
    title: str
    content_type: str
    created_at: str
    updated_at: Optional[str]


class ProjectInfo(BaseModel):
    """One registered project from the path registry."""
    project_id: str
    path: str


class FamilyInfo(BaseModel):
    """One project family from families.json."""
    name: str
    members: list[str]      # all member abs paths (registered or not)
    project_ids: list[str]  # ULIDs of registered members only (lazy-link)


# ── Search core (shared by the MCP `search` tool + the CLI `braincell search`) ─

async def search_hits(
    store: SqliteStore,
    query: str,
    project: Optional[str] = None,
    k: int = 10,
    rank: str = "hybrid",
    scope: str = "self",
    projects: Optional[list[str]] = None,
) -> list[Hit]:
    """Core chunk-search orchestration shared by the MCP tool and the CLI.

    Embeds the query and searches the connected Project store. Returns raw store
    ``Hit`` objects; the caller adapts them (the MCP tool → ``SearchHit`` DTOs,
    the CLI → JSON / a table). Mirrors ``recall_notes`` below.

    ``rank`` is this layer's name for the ranking strategy; the ``Store`` protocol
    calls the equivalent argument ``mode``.
    """
    if rank not in ("hybrid", "semantic", "keyword"):
        raise ValueError(f"Invalid rank '{rank}'. Must be hybrid|semantic|keyword.")
    if k < 1 or k > 100:
        raise ValueError("k must be between 1 and 100.")

    qvec = await embed_query_async(query)
    proj_filter = _resolve_filter(projects, project, scope)
    return await store.search(qvec, query, proj_filter, k, rank)


# ── Tool: search ─────────────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def search(
    query: str,
    project: Optional[str] = None,
    k: int = 10,
    rank: str = "hybrid",
    scope: str = "self",
    projects: Optional[list[str]] = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> list[SearchHit]:
    """Hybrid (vector + keyword) search over ingested documents & transcripts.

    Args:
        query:    Natural-language search query.
        project:  Explicit project ULID to scope to (overrides ``scope``);
                  None → use scope.  Ignored when ``projects`` is provided.
        k:        Maximum results to return (default 10).
        rank:     'hybrid' (default) | 'semantic' | 'keyword' — the ranking
                  strategy. (Named ``mode`` in earlier releases; the
                  CLI's ``--mode`` selects the project/global brain instead.)
        scope:    'self' (default — this project only). Cross-project scopes
                  ('family'/'all') require global mode.  Ignored when
                  ``projects`` is provided.
        projects: Optional explicit list of project ULIDs to search across.
                  Takes precedence over both ``project`` and ``scope``.  In
                  global mode, multiple ULIDs pool results from all listed
                  projects; in project mode only a single-entry list matching
                  the self project is permitted (raises otherwise).  Entries
                  must be non-empty strings; list capped at 200 entries.
                  ``None`` or ``[]`` falls through to ``project``/``scope``
                  resolution.

    Returns:
        Ranked list of SearchHit (chunk_id, doc_key, title, snippet, score, cosine,
        fts_matched, source_path). NOTE on scores: `score` is the RANKING signal — in hybrid mode it
        is a Reciprocal Rank Fusion value (~1/(60+rank)), so it is rank-only and its tiny
        magnitude (~0.016) carries NO match-quality signal; use it only for ordering.
        `cosine` is the interpretable vector relevance in [-1,1] (higher cosine = more
        on-topic; the exact on-topic threshold depends on the active embedding model;
        None when a hit came only from keyword/FTS),
        and `fts_matched` flags chunks that also matched full-text search. Judge a hit's
        relevance by `cosine` + `fts_matched`, not by `score`.
    """
    hits = await search_hits(
        _store(ctx), query, project=project, k=k, rank=rank, scope=scope,
        projects=projects,
    )
    return [
        SearchHit(
            chunk_id=h.chunk_id,
            doc_key=h.doc_key,
            title=h.title,
            snippet=h.snippet,
            score=round(h.score, 6),
            cosine=round(h.cosine, 4) if h.cosine is not None else None,
            fts_matched=h.fts_matched,
            source_path=h.source_path,
        )
        for h in hits
    ]


# ── Chunk fallback (cold-start recall degradation) ───────────────────────────

def _chunk_fallback_enabled() -> bool:
    """True unless BRAINCELL_RECALL_CHUNK_FALLBACK is 0/false/off (default ON).

    The fallback is the cold-start fix: a freshly built brain has thousands of
    searchable chunks and ~zero curated notes, so recall (and the proactive
    a machine consumer) delivered nothing on
    day one. Backfilling recall with provenance-marked transcript excerpts makes
    a fresh brain useful immediately; excerpts fade out as curated notes accrue.
    """
    val = os.environ.get("BRAINCELL_RECALL_CHUNK_FALLBACK", "").strip().lower()
    return val not in ("0", "false", "off")


def _chunk_fallback_min_cosine() -> float:
    """Cosine floor for fallback excerpts (BRAINCELL_RECALL_FALLBACK_COS).

    Vector-ranked chunks below this floor are dropped so the hook never injects
    off-topic transcript text. FTS-matched chunks (keyword hit on the user's own
    prompt words) are kept regardless — lexical match is its own relevance
    signal. Default 0.50 was measured on a real qwen3-embedding:4b@1024 brain
    (263-doc corpus): on-topic queries scored 0.60-0.70; off-topic queries
    ("sourdough bread starter hydration", "olympic swimming world records")
    topped out at 0.21-0.34 (nearest neighbors only, no true match in corpus).
    """
    try:
        return float(os.environ.get("BRAINCELL_RECALL_FALLBACK_COS", "0.50") or 0.50)
    except ValueError:
        return 0.50


def _looks_like_ulid(s: str) -> bool:
    return len(s) == 26 and s.isalnum()


def _hit_to_excerpt_note(h: Hit) -> Note:
    """Adapt a chunk-search Hit to a provenance-marked pseudo-note.

    Honest-provenance contract (do not blur):
      - ``retrieval_origin='chunk'`` — machine-retrieved transcript excerpt,
        never a curated note a user wrote via `remember`.
      - ``kind='excerpt'`` — deliberately NOT a valid `remember` kind, so an
        excerpt can never be mistaken for (or written back as) curated memory.
      - ``id=-chunk_id`` — negative, so it can never address a real note:
        `forget`/`supersede` on it are safe no-ops (note ids are positive).
    """
    pid = h.doc_key.split(":", 1)[0] if ":" in h.doc_key else ""
    return Note(
        id=-h.chunk_id,
        project_id=pid if _looks_like_ulid(pid) else "",
        scope="project",
        kind="excerpt",
        content=h.snippet,
        tags=[],
        confidence=None,
        source_hint=h.source_path or h.doc_key,
        superseded_by=None,
        created_at="",
        retrieval_origin="chunk",
    )


async def _chunk_fallback(
    store: SqliteStore,
    notes: list[Note],
    query: str,
    qvec,
    k: int,
    plan,
    proj_filter,
) -> list[Note]:
    """Backfill a sparse note recall with ranked transcript-chunk excerpts.

    Curated notes always rank first and are returned unchanged; excerpts only
    fill the remaining slots up to ``k``. Any failure returns the notes as-is —
    the fallback must never break recall (mirrors the hook's fail-quiet rule).
    """
    try:
        deficit = k - len(notes)
        rank = "hybrid" if qvec is not None else "keyword"
        from .federate import federated_search
        if plan is not None:
            hits = await federated_search(store, plan, qvec, query, k, rank)
        else:
            hits = await store.search(qvec, query, proj_filter, k, rank)
        floor = _chunk_fallback_min_cosine()
        # Seeded with curated-note text so an excerpt never repeats a note; grows
        # as excerpts are kept so identical chunk text (e.g. a duplicated
        # transcript snippet across two chunks) is only ever surfaced once.
        seen_texts = {n.content for n in notes}
        kept: list[Note] = []
        for h in hits:
            relevant = (h.cosine is not None and h.cosine >= floor) or h.fts_matched
            if not relevant or h.snippet in seen_texts:
                continue
            seen_texts.add(h.snippet)
            kept.append(_hit_to_excerpt_note(h))
            if len(kept) == deficit:
                break
        return notes + kept
    except Exception as exc:
        log.warning("recall chunk fallback failed (non-fatal): %s", exc)
        return notes


# ── Recall core (shared by the MCP `recall` tool + the CLI `braincell recall`) ─

async def recall_notes(
    store: SqliteStore,
    query: str,
    project: Optional[str] = None,
    k: int = 5,
    min_cosine: Optional[float] = None,
    dedup: bool = True,
    scope: str = "self",
    projects: Optional[list[str]] = None,
    include_superseded: bool = False,
) -> list[Note]:
    """Core recall orchestration shared by the MCP tool and the CLI.

    Embeds the query (falling back to keyword/recency when the embedder is down)
    and recalls from the connected Project store. Returns raw store ``Note``
    objects; the caller adapts them (the MCP tool → ``MemoryNote`` DTOs, the CLI
    → JSON / a table).

    Validation, embedding fallback, and filter resolution are shared by the MCP
    and CLI paths.
    """
    if k < 1 or k > 50:
        raise ValueError("k must be between 1 and 50.")
    if min_cosine is not None and not (0.0 <= min_cosine <= 1.0):
        raise ValueError("min_cosine must be between 0.0 and 1.0.")

    qvec = None
    if query and query.strip():
        try:
            qvec = await embed_query_async(query)
        except Exception as exc:
            log.warning(
                "BRAINCELL_EMBED_UNAVAILABLE: embedding is down — recall falls back to "
                "keyword/recency (no vector ranking). Restart when the embedder is "
                "reachable to restore semantic recall. %s", exc,
            )
    proj_filter = _resolve_filter(projects, project, scope)
    notes = await store.recall(
        qvec, proj_filter, k, qtext=query, min_cosine=min_cosine, dedup=dedup,
        include_superseded=include_superseded,
    )
    # Cold-start graceful degradation: when fewer than k curated notes came back
    # and there is a query to rank against, fill the remainder with transcript
    # excerpts from chunk search, provenance-marked (retrieval_origin='chunk',
    # kind='excerpt', negative ids). Gated off for history/audit views
    # (include_superseded) and for empty queries (recency listing stays notes-only).
    if (
        _chunk_fallback_enabled()
        and not include_superseded
        and query and query.strip()
        and len(notes) < k
    ):
            notes = await _chunk_fallback(store, notes, query, qvec, k, None, proj_filter)
    return notes


# ── Tool: recall ─────────────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def recall(
    query: str,
    project: Optional[str] = None,
    k: int = 5,
    min_cosine: Optional[float] = None,
    dedup: bool = True,
    scope: str = "self",
    projects: Optional[list[str]] = None,
    include_superseded: bool = False,
    ctx: Context = None,  # type: ignore[assignment]
) -> list[MemoryNote]:
    """Recall curated memory notes (decisions, bug lessons, observations).

    Args:
        query:      Natural-language query. When non-empty and embedding is
                    available, uses hybrid recall (vector cosine + FTS5 RRF-fused).
                    Falls back to keyword/recency when embedding is unavailable.
        project:    Explicit project ULID to scope notes (overrides ``scope``).
                    Ignored when ``projects`` is provided.
        k:          Maximum notes to return (default 5).
        min_cosine: Optional cosine similarity floor [0, 1]. When set, notes whose
                    stored-vector cosine to the query falls below this threshold are
                    dropped before ranking. Only applies to vector-ranked hits;
                    FTS-only hits (no cosine) are unaffected. Ignored when embedding
                    is unavailable or query is empty.
        dedup:      When True (default), near-duplicate notes (stored-vector cosine >
                    0.95 to an already-returned note) are suppressed. Greedy walk in
                    fused-score order: keeps the higher-ranked note, drops later dups.
                    Notes with no embedding (FTS-only) are never dropped by dedup.
                    Only active in the hybrid path (qvec is not None).
        scope:      'self' (default — this project's notes only). In global mode,
                    'family' (this project's family) and 'all' (every project in
                    the global brain) are also supported.  Cross-project scopes
                    require BRAINCELL_MODE=global; using them in project mode raises
                    an error.  Ignored when ``projects`` is provided.
        projects:   Optional explicit list of project ULIDs to recall across.
                    Takes precedence over both ``project`` and ``scope``.  In
                    global mode, multiple ULIDs pool notes from all listed
                    projects; in project mode only a single-entry list matching
                    the self project is permitted (raises otherwise).  Entries
                    must be non-empty strings; list capped at 200 entries.
                    ``None`` or ``[]`` falls through to ``project``/``scope``
                    resolution.
        include_superseded:
                    False (default) returns CURRENT truth: a note that has been
                    superseded never comes back as the answer — if the query matches
                    its wording, the note that REPLACED it is returned instead, with
                    `retrieval_origin='resolved'` and the retired note in `history`.
                    True returns the raw historical set (superseded notes ranked on
                    their own merits) — for auditing what a project used to believe.

    Returns:
        List of memory notes ranked by relevance (recency- and confidence-weighted
        in the hybrid path), with kind/content/confidence/superseded_by and the
        retrieval provenance fields. At most k results; may be fewer after
        min_cosine, dedup, or supersession resolution. When fewer than k curated
        notes match a non-empty query, remaining slots are backfilled with ranked
        transcript excerpts (retrieval_origin='chunk', kind='excerpt', negative
        id) so a freshly built brain is useful before notes accumulate — these
        are machine-retrieved corpus text, not curated notes; never pass their
        negative ids to forget/supersede. Disable via
        BRAINCELL_RECALL_CHUNK_FALLBACK=off.
    """
    notes = await recall_notes(
        _store(ctx), query, project=project, k=k, min_cosine=min_cosine,
        dedup=dedup, scope=scope, projects=projects,
        include_superseded=include_superseded,
    )
    return [
        MemoryNote(
            id=n.id,
            project_id=n.project_id,
            scope=n.scope,
            kind=n.kind,
            content=n.content,
            tags=n.tags,
            confidence=n.confidence,
            source_hint=n.source_hint,
            superseded_by=n.superseded_by,
            created_at=n.created_at,
            retrieval_origin=n.retrieval_origin,
            resolved_from=n.resolved_from,
            history=n.history,
            linked_from=n.linked_from,
            relation=n.relation,
            relation_weight=n.relation_weight,
        )
        for n in notes
    ]


# ── Tool: remember ───────────────────────────────────────────────────────────

@mcp.tool()
async def remember(
    text: str,
    kind: str,
    project: Optional[str] = None,
    tags: Optional[list[str]] = None,
    confidence: Optional[float] = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> RememberResult:
    """Persist a curated memory note.

    Secret-scans the text before persisting — rejects on pattern match.

    Args:
        text:       The note content.
        kind:       'decision' | 'bug_lesson' | 'note' | 'observation'.
        project:    Project ULID. Optional — defaults to this server's project.
                    If given, it MUST match the server's project (this instance
                    serves exactly one brain); a mismatch is rejected, never
                    silently re-filed.
        tags:       Optional JSON array of string tags.
        confidence: Optional float 0.0–1.0 confidence score.

    Returns:
        {note_id, embedded, potential_conflicts} on success. Raises ValueError on
        secret hit or bad kind. potential_conflicts lists ACTIVE notes this one
        may contradict or duplicate (advisory — the note was persisted): review
        them, and if this note REPLACES one, call supersede on the old id so
        recall returns one truth instead of two competing ones.
    """
    if not text or not text.strip():
        raise ValueError("text must not be empty.")
    if confidence is not None and not (0.0 <= confidence <= 1.0):
        raise ValueError("confidence must be between 0.0 and 1.0.")

    vec = None
    try:
        _vecs = await embed_texts_async([text])
        vec = _vecs[0]
    except Exception as exc:
        log.warning(
            "BRAINCELL_EMBED_UNAVAILABLE: embedding is down — note saved FTS-only, "
            "backfill later via `braincell reembed-notes` (see embed.py _embed_ollama). %s",
            exc,
        )

    project = _pin_write_project(project)
    store = _store(ctx)
    # Contradiction guard — scan BEFORE the insert so the new note never
    # matches itself. Warn-only: the note persists regardless; a failure to scan
    # must never block a write.
    conflicts = []
    try:
        conflicts = await store.find_conflicts(project, vec)
    except Exception as exc:
        log.warning("conflict scan failed (non-fatal, note still persisted): %s", exc)
    note_id = await store.remember(
        text=text,
        kind=kind,
        project=project,
        tags=tags,
        confidence=confidence,
        embedding=vec,
    )
    return RememberResult(
        note_id=note_id,
        embedded=vec is not None,
        potential_conflicts=[
            ConflictHit(
                note_id=c.id, kind=c.kind, cosine=round(c.cosine, 4),
                snippet=c.content[:200],
            )
            for c in conflicts
        ],
    )


# ── Tool: forget ─────────────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def forget(
    note_id: int,
    project: Optional[str] = None,
    hard: bool = False,
    ctx: Context = None,  # type: ignore[assignment]
) -> ForgetResult:
    """Retract a memory note by its numeric ID.

    Soft (default, hard=false): tombstones the note (sets deleted_at). The note
    is hidden from recall immediately but the row is retained — safe for audit
    trails and future undelete. Returns deleted=true when a live note was
    tombstoned; returns deleted=false if the note was already tombstoned, not
    found, or owned by a different project (idempotent).

    Hard (hard=true): permanently removes the note and its FTS index entry.
    Cannot be undone. Use for GDPR/scrub requests or explicit purge.

    Only operates on notes belonging to this server's project — cross-project
    retraction is rejected on both paths.

    Args:
        note_id: Integer ID of the note to retract (from recall results).
        project: Project ULID (defaults to this server's project; a mismatch is
                 rejected).
        hard:    When true, permanently delete instead of soft-tombstoning.

    Returns:
        {"deleted": true/false, "hard": true/false} — deleted indicates whether
        a note was acted on; hard echoes which path ran.
    """
    project = _pin_write_project(project)
    deleted = await _store(ctx).forget(note_id, project, hard=hard)
    return ForgetResult(deleted=deleted, hard=hard)


# ── Tool: supersede ──────────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(idempotentHint=True))
async def supersede(
    note_id: int,
    new_content: str,
    project: Optional[str] = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> SupersedeResult:
    """Supersede an existing memory note with updated content.

    Creates a new note with `new_content` and marks the old note as superseded
    (sets its superseded_by field). The old note is retained for audit. Inherits
    the old note's kind/tags/confidence. Secret-scans `new_content` before write.

    Concurrency: the replacement is written and the old note is marked in ONE
    transaction, guarded by a compare-and-set. If another writer superseded or
    tombstoned the same note first, nothing is written and the call fails with
    `supersede_conflict` — re-recall the note and retry against the current one.

    Args:
        note_id:     ID of the note to supersede.
        new_content: The replacement content.
        project:     Project ULID (defaults to this server's project; a mismatch
                     is rejected).

    Returns:
        {"new_id": <int>, "superseded": <int>} on success.
    """
    if not new_content or not new_content.strip():
        raise ValueError("new_content must not be empty.")

    vec = None
    try:
        _vecs = await embed_texts_async([new_content])
        vec = _vecs[0]
    except Exception as exc:
        log.warning(
            "BRAINCELL_EMBED_UNAVAILABLE: embedding is down — supersede note saved "
            "FTS-only, backfill later via `braincell reembed-notes`. %s", exc,
        )

    project = _pin_write_project(project)
    new_id = await _store(ctx).supersede(note_id, new_content, project, embedding=vec)
    return SupersedeResult(new_id=new_id, superseded=note_id, embedded=vec is not None)


# ── Tool: get_document ───────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_document(
    doc_key: str,
    project: Optional[str] = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> Optional[DocumentResult]:
    """Retrieve a full document (chunks + provenance) by its key.

    Args:
        doc_key: The document key (e.g. a transcript session id or doc name).
        project: Project ULID for scoped lookup.

    Returns:
        Document dict or null if not found.
    """
    if not doc_key or not doc_key.strip():
        raise ValueError("doc_key must not be empty.")

    doc = await _store(ctx).get_document(doc_key.strip(), _pin_read_project(project))
    if doc is None:
        return None
    return DocumentResult(
        id=doc.id,
        doc_key=doc.doc_key,
        title=doc.title,
        content_type=doc.content_type,
        commit_sha=doc.commit_sha,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        chunks=doc.chunks,
        metadata=doc.metadata,
    )


# ── Tool: ingest_status ──────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def ingest_status(
    project: Optional[str] = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> IngestStatusResult:
    """Report whether this project has been indexed and basic counts.

    Args:
        project: Project ULID (or None for aggregate across all projects).

    Returns:
        {indexed, doc_count, chunk_count, last_ingest_ts, head_sha, stale}.
    """
    status = await _store(ctx).ingest_status(_pin_read_project(project))
    return IngestStatusResult(
        indexed=status.indexed,
        doc_count=status.doc_count,
        chunk_count=status.chunk_count,
        last_ingest_ts=status.last_ingest_ts,
        head_sha=status.head_sha,
        stale=status.stale,
    )


# ── Tool: list_documents ──────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_documents(
    project: Optional[str] = None,
    filter: Optional[str] = None,
    limit: int = 200,
    ctx: Context = None,  # type: ignore[assignment]
) -> list[DocumentSummary]:
    """List ingested documents & transcripts with name/title/content_type summary.

    Args:
        project: Project ULID to scope the listing.
        filter:  Optional substring filter on doc_key or title (case-insensitive).
        limit:   Maximum rows to return (default 200, hard cap 200).

    Returns:
        List of {doc_key, title, content_type, created_at, updated_at}.
    """
    if limit < 1 or limit > 200:
        raise ValueError("limit must be between 1 and 200.")
    rows = await _store(ctx).list_documents(_pin_read_project(project), filter, limit)
    return [DocumentSummary(**r) for r in rows]


# ── Tool: list_projects ──────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_projects() -> list[ProjectInfo]:
    """List all registered projects from the path registry.

    Reads the workspace-level ``path-registry.json`` (no store required).

    Returns:
        List of ``{project_id, path}`` sorted by path, one entry per registered
        project. Empty list when no projects are registered.
    """
    registry = load_path_registry()
    return [
        ProjectInfo(project_id=ulid, path=path)
        for path, ulid in sorted(registry.items())
    ]


# ── Tool: list_families ───────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_families() -> list[FamilyInfo]:
    """List all project families and resolve member ULIDs.

    Reads ``families.json`` and cross-references the path registry. Unregistered
    member paths (no ULID yet) are included in ``members`` but contribute nothing
    to ``project_ids`` (lazy-link — a member can be declared before it is
    ingested). Families are returned sorted by name.

    Returns:
        List of ``{name, members, project_ids}`` — one entry per defined family.
        ``members`` contains all declared paths; ``project_ids`` contains only
        the ULIDs of currently-registered members.
    """
    families = load_families()
    registry = load_path_registry()
    result: list[FamilyInfo] = []
    for fname, members in sorted(families.items()):
        pids = [
            registry[normalize_path(m)]
            for m in members
            if normalize_path(m) in registry
        ]
        result.append(FamilyInfo(name=fname, members=members, project_ids=pids))
    return result


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    """Run the FastMCP server on stdio (standard transport for `claude mcp add`)."""
    mcp.run()


if __name__ == "__main__":
    main()
