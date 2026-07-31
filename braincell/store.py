# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
store.py — Store Protocol + SqliteStore implementation for BrainCell.

Design constraints:
  - Store Protocol: single seam — MCP tools and pipeline depend ONLY on this.
  - SqliteStore: aiosqlite + NumPy brute-force cosine.
    Vectors are unit-length (embed.py L2-normalises every vector, any provider)
    so dot-product == cosine; no sqlite-vec needed for V0.
  - Hybrid search: RRF of NumPy cosine + FTS5 (falls back to LIKE scan
    if FTS5 is unavailable in the frozen sqlite3).
  - Fail closed: open_store() exits non-zero on missing BRAINCELL_STORE env.
  - Schema-version refusal: assert_schema_version() refuses on mismatch.
  - scope column (cross-project seam): inert in V0; `project=None` fallback
    deferred to V1.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

import aiosqlite
import numpy as np

from . import embed_spec
from .log import get as _get_log
from .schema import (
    BRAINCELL_INIT_STMTS,
    MEMORY_NOTES_UID_IDX_DDL,
    MEMORY_SCHEMA_VERSION,
    NOTE_LINKS_DDL,
    NOTE_LINKS_IDX_DDL,
    OPERATION_NOTES_DDL,
    OPERATION_NOTES_IDX_DDL,
    OPERATIONS_DDL,
)

log = _get_log("braincell.store")


# ── Tunable ranking constants (env-overridable, read once at import) ───────────
# Defaults preserve historical behaviour exactly; set the env var to retune
# without a code edit (the review's #7 — "hard-coded ranking constants").

def _env_float(name: str, default: float) -> float:
    """Read a float from env *name*, falling back to *default* on unset/invalid."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("%s=%r is not a float — using default %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    """Read an int from env *name*, falling back to *default* on unset/invalid."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("%s=%r is not an int — using default %s", name, raw, default)
        return default


# Near-duplicate cutoff for recall dedup: a candidate note whose stored-vector
# cosine to an already-kept note exceeds this is dropped (greedy, fused order).
_DEDUP_COSINE: float = _env_float("BRAINCELL_DEDUP_COS", 0.95)

# Contradiction guard (warn-only): `remember` surfaces active notes whose
# cosine to the incoming note is ≥ _CONFLICT_COS so the caller can decide to
# `supersede` instead of accumulating a contradiction. Threshold rationale
# (measured on qwen3-embedding:4b@1024, 2026-07-23): realistic note-shaped
# corrections score 0.96–0.98, short negated sentences ~0.86, paraphrases ~0.94,
# unrelated ~0.31 — so 0.85 catches the conflict-shaped band while staying far
# above unrelated content. Cosine CANNOT distinguish contradiction from
# paraphrase (both are "potential conflicts"; judging which is which is
# `braincell contradictions`' LLM pass). _CONFLICT_K=0 disables the scan.
_CONFLICT_COS: float = _env_float("BRAINCELL_CONFLICT_COS", 0.85)
_CONFLICT_K: int = _env_int("BRAINCELL_CONFLICT_K", 3)
# Recency half-life (days) for the decay factor — older notes fade.
_HALFLIFE_DAYS: float = _env_float("BRAINCELL_HALFLIFE_DAYS", 90.0)
# Reciprocal Rank Fusion constant k — larger k flattens the rank advantage.
_RRF_K: int = _env_int("BRAINCELL_RRF_K", 60)
# Hybrid fusion mode. 'rrf' (default, rank-only, tuning-free) | 'cc'
# (convex combination of min-max-normalized scores — needs a tuned alpha but can
# beat RRF; Bruch et al. arXiv:2210.11934). Default preserves today's ranking.
_FUSION: str = os.environ.get("BRAINCELL_FUSION", "rrf")
# CC weight on the semantic (vector) side vs lexical (FTS). alpha in [0,1]:
# 1.0 = pure semantic order, 0.0 = pure lexical order.
_FUSION_ALPHA: float = _env_float("BRAINCELL_FUSION_ALPHA", 0.5)
# Graph note-links. LINK_EXPAND = how many linked notes to pull into recall
# after the primary ranked set (0 = off = byte-identical recall). LINK_COS = the
# cosine threshold for auto-linking on write; LINK_RECENT_N bounds how many
# recent same-project notes a new note is compared against.
_LINK_EXPAND: int = _env_int("BRAINCELL_LINK_EXPAND", 0)
_LINK_COS: float = _env_float("BRAINCELL_LINK_COS", 0.6)
_LINK_RECENT_N: int = _env_int("BRAINCELL_LINK_RECENT_N", 20)
# Vector backend. 'bruteforce' (default, NumPy cosine over float32 BLOBs) is
# correct at braincell's scale (Lin et al. arXiv:2409.06464 — flat beats ANN
# below ~tens-of-thousands of vectors). 'sqlitevec' (vec0 ANN) is deferred:
# adopting it means taking on a compiled third-party
# dependency, which needs an explicit decision rather than a drive-by import.
# Adopt only when instrumented p95 crosses the trigger (~50 ms, see
# BRAINCELL_VEC_P95_TRIGGER_MS below). We instrument now so the decision is
# data-driven. Brute force stays behind the Store Protocol, so vec0 is a
# non-breaking later swap.
_BACKEND: str = os.environ.get("BRAINCELL_BACKEND", "bruteforce")
# Trigger threshold (ms) at which vec0 ANN starts to pay off on this hardware.
_VEC_P95_TRIGGER_MS: float = _env_float("BRAINCELL_VEC_P95_TRIGGER_MS", 50.0)


# ── Errors ────────────────────────────────────────────────────────────────────

class EmbedderMismatchError(RuntimeError):
    """Raised when a store's embed fingerprint differs from the configured one.

    This is a PERMANENT config-level failure (restarting cannot fix it) — the
    brain on disk was embedded under one model and the process is configured
    with another. Carries structured fields so the CLI and GUI can render one
    clean actionable line instead of a traceback. Subclasses RuntimeError so
    existing ``except RuntimeError`` /
    ``pytest.raises(RuntimeError)`` handlers keep working unchanged.
    """

    #: Stable machine-readable discriminator.
    error = "embedder_mismatch"

    def __init__(self, db_path: Path, built_with: str, configured: str):
        self.db_path = Path(db_path)
        self.built_with = built_with
        self.configured = configured
        # The global brain lives at <namespace>/global/braincell.db — its
        # rebuild needs --mode global; a per-project brain does not.
        self.rebuild_cmd = (
            "braincell build --mode global --reembed"
            if self.db_path.parent.name == "global"
            else "braincell build --reembed"
        )
        super().__init__(
            f"BrainCell embedding-space mismatch in {db_path}: "
            f"store was built with {built_with!r} but the configured embedder "
            f"is {configured!r}. Mixing vector spaces corrupts search. "
            f"Restore the original embedder env "
            f"(BRAINCELL_EMBED_PROVIDER/BRAINCELL_EMBED_MODEL/BRAINCELL_EMBED_DIM), "
            f"or rebuild with `{self.rebuild_cmd}` to re-embed under the new model."
        )


class SupersedeConflict(ValueError):
    """Raised when a supersede loses a race against a concurrent writer.

    SQLite serialises physical writes, but braincell is not a single logical
    writer: every MCP server process, the CLI, and the GUI open their own store.
    ``supersede`` therefore compare-and-sets — if the target note stopped being
    live-and-unsuperseded between the read and the write, the whole transaction
    rolls back and this is raised rather than silently producing two "current"
    replacements pointing at one stale note.

    Subclasses ValueError so existing ``except ValueError`` handlers (and the MCP
    error mapping) keep working unchanged.
    """

    #: Stable machine-readable discriminator for MCP clients.
    error = "supersede_conflict"


# ── Typed result containers ───────────────────────────────────────────────────

@dataclass
class Hit:
    """One search result from braincell_search."""
    chunk_id: int
    doc_key: str
    title: str
    snippet: str
    score: float
    source_path: str | None = None
    metadata: dict | None = None
    # Interpretable relevance, surfaced alongside `score` so an LLM consumer can judge
    # match quality. In hybrid mode `score` is an RRF rank-fusion value (~1/(60+rank),
    # rank-only — carries NO magnitude signal); `cosine` is the raw vector similarity
    # in [-1,1] (None when the hit came only from the keyword/FTS list, no vector rank),
    # and `fts_matched` flags chunks that also matched full-text search.
    cosine: float | None = None
    fts_matched: bool = False


@dataclass
class Doc:
    """Full document record from braincell_get_document."""
    id: int
    doc_key: str
    title: str
    content_type: str
    commit_sha: str | None
    created_at: str
    updated_at: str | None
    chunks: list[dict] = field(default_factory=list)
    metadata: dict | None = None


@dataclass
class Status:
    """Ingest status from braincell_ingest_status."""
    indexed: bool
    doc_count: int
    chunk_count: int
    last_ingest_ts: str | None
    head_sha: str | None
    stale: bool = False


@dataclass
class ConflictCandidate:
    """An ACTIVE note whose cosine to an incoming `remember` crosses the
    conflict threshold — a *potential* contradiction or duplicate. Warn-only
    signal: the caller decides whether to `supersede`; nothing is auto-resolved
    (a recalled note steering an automatic write is the memory-poisoning path
    this design explicitly rejects)."""
    id: int
    kind: str
    content: str
    cosine: float


@dataclass
class Note:
    """One memory note from braincell_recall."""
    id: int
    project_id: str
    scope: str
    kind: str
    content: str
    tags: list[str]
    confidence: float | None
    source_hint: str | None
    superseded_by: int | None
    created_at: str
    # Liveness authority ('active' | 'superseded' | 'tombstoned'). Recall
    # returns 'active' notes (or resolves to them); other values appear only in
    # include_superseded historical views. Default keeps merge/test constructors
    # (federate, fixtures) unchanged.
    status: str = "active"
    # True when this note was appended by graph-link expansion (an "also-see"
    # note pulled in via bc_note_links), not a direct hit. Default False keeps
    # every existing construction site + serializer unchanged.
    expansion: bool = False
    # Retrieval provenance — lets a consumer weigh authority instead of treating
    # every result as an equally-direct answer:
    #   'direct'   — the note itself matched the query
    #   'resolved' — a SUPERSEDED note matched and this is the current replacement
    #                it resolves to (``resolved_from`` / ``history`` say what it replaced)
    #   'linked'   — pulled in by graph expansion from ``linked_from``
    retrieval_origin: str = "direct"
    resolved_from: int | None = None
    history: list[dict] = field(default_factory=list)
    linked_from: int | None = None
    relation: str | None = None
    relation_weight: float | None = None


# ── Store Protocol ────────────────────────────────────────────────────────────

@runtime_checkable
class Store(Protocol):
    """Backend-agnostic storage interface.

    V0 impl: SqliteStore.
    V2+ impl: PostgresStore (pgvector + HNSW) — same signatures, no tool churn.
    """

    def assert_schema_version(self) -> None:
        """Refuse (raise RuntimeError) on schema mismatch. Never auto-migrates."""
        ...

    async def search(
        self,
        qvec: Optional[np.ndarray],
        qtext: str,
        project: str | None,
        k: int,
        mode: str,
    ) -> list[Hit]:
        """Hybrid (vector + keyword) search over ingested chunks.

        mode: 'semantic' | 'keyword' | 'hybrid' (RRF).
        project scoping: a single ULID or a sequence of ULIDs filters to those
        projects; project=None (or an empty sequence) applies NO filter and
        searches ALL projects in the store — this is what scope='all' uses
        (server._resolve_scope). An empty string '' filters to project_id=''
        and therefore matches no rows.
        """
        ...

    async def get_document(
        self, doc_id_or_key: str | int, project: str | None
    ) -> Doc | None:
        """Return full document with chunk texts."""
        ...

    async def ingest_status(self, project: str | None) -> Status:
        """Return indexed/doc_count/last_ingest_ts for this project."""
        ...

    async def remember(
        self,
        text: str,
        kind: str,
        project: str,
        tags: list[str] | None,
        confidence: float | None,
        embedding: np.ndarray | None = None,
    ) -> str:
        """Persist a curated memory note. Returns the note id as string.

        embedding: optional pre-computed unit float32 vector (embed_spec.DIM).
        When None the note is stored with NULL embedding (FTS-only recall until
        backfilled with `braincell reembed-notes`). Secret-scan before persist
        — raises ValueError on hit.
        """
        ...

    async def forget(self, note_id: int, project_id: str, hard: bool = False) -> bool:
        """Retract a memory note scoped to project_id.

        Soft (default): tombstone via deleted_at; returns True iff a live row was
        tombstoned. Hard: permanent DELETE; returns True iff a row was removed.
        """
        ...

    async def supersede(
        self,
        note_id: int,
        new_content: str,
        project_id: str,
        kind: str | None = None,
        tags: list[str] | None = None,
        confidence: float | None = None,
    ) -> int:
        """Supersede a note with new content; returns the new note id."""
        ...

    async def recall(
        self,
        qvec: np.ndarray | None,
        project: str | Sequence[str] | None,
        k: int,
        qtext: str = "",
        min_cosine: float | None = None,
        dedup: bool = True,
        include_superseded: bool = False,
    ) -> list[Note]:
        """Retrieve curated memory notes.

        Returns CURRENT truth by default: a superseded hit is resolved to the note
        that replaced it. ``include_superseded=True`` returns the raw historical set.

        When qvec is not None, uses hybrid RRF (vector cosine + FTS5 over
        memory_notes) — mirrors the chunk search() pipeline. NULL-embedding notes
        appear only via the FTS list (correct; semantically ranked once embedded).
        When qvec is None: keyword (FTS5 MATCH over memory_fts when qtext is
        non-empty) with recency as the tie-breaker, or pure recency when qtext is
        empty or FTS5 is unavailable.

        Hybrid-path params (qvec must be not None):
          min_cosine: drop vec_hits whose cosine < this threshold before RRF fusion.
          dedup: when True, drop notes whose stored-vector cosine to any already-kept
                 note exceeds 0.95 (greedy, fused-score order). FTS-only (NULL-
                 embedding) notes are never dropped by dedup.
        """
        ...

    async def list_documents(
        self,
        project: str | None,
        filter: str | None,
        limit: int,
    ) -> list[dict]:
        """List ingested documents, capped at 200 rows. SQL parameterized."""
        ...

    async def aclose(self) -> None:
        """Await-close all open DB connections. Preferred in async context."""
        ...

    def close(self) -> None:
        """Close all open DB connections (sync wrapper; safe after event loop exits)."""
        ...


# ── Secret scanning ───────────────────────────────────────────────────────────

# Max chars for a single remembered note (~25k tokens). Bounds the write +
# FTS-index cost and stops a runaway paste from being persisted.
_MAX_NOTE_CHARS = 100_000

# Patterns that suggest the text contains a secret. Prefix/structure-based — no
# entropy check, so this is a guardrail against accidental pastes, not a
# guarantee. Covers the common cloud / AI / VCS credential formats + PEM keys.
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"[A-Z_]{6,}=(?!false|true|none|null)[A-Za-z0-9+/]{16,}", re.IGNORECASE),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),                  # Anthropic
    re.compile(r"sk-(?:proj-|prod-|test-)?[A-Za-z0-9]{20,}"),  # OpenAI (incl. sk-proj-)
    re.compile(r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}"),   # Stripe secret/restricted
    re.compile(r"AKIA[0-9A-Z]{16}"),                           # AWS access key id
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),                      # Google API key
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),               # Slack token
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),                 # GitHub ghp_/gho_/ghu_/ghs_/ghr_
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),               # GitHub fine-grained PAT
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"ANTHROPIC_API_KEY\s*[:=]"),
    re.compile(r"OPENROUTER_API_KEY\s*[:=]"),
    re.compile(r"OPENAI_API_KEY\s*[:=]"),
]


def _secret_scan(text: str) -> str | None:
    """Return the first matching pattern description, or None if clean."""
    for pat in _SECRET_PATTERNS:
        m = pat.search(text)
        if m:
            return f"text matches secret pattern '{pat.pattern[:40]}'"
    return None


# ── NumPy vector helpers ──────────────────────────────────────────────────────

def _vec_to_blob(vec: np.ndarray) -> bytes:
    """Encode a float32 NumPy array as raw bytes for SQLite BLOB storage."""
    return vec.astype(np.float32).tobytes()


def _blob_to_vec(blob: bytes) -> np.ndarray:
    """Decode a BLOB back to a float32 NumPy array."""
    return np.frombuffer(blob, dtype=np.float32)


def _stack_blobs(blobs: list[bytes]) -> np.ndarray:
    """Decode a list of float32 BLOBs into one (N, DIM) matrix.

    Uses a single contiguous ``frombuffer`` over the joined bytes instead of N
    separate ``np.frombuffer`` calls + ``np.stack`` (which allocates N small
    arrays then copies them again). The store guards against mixed dimensions at
    write time, so every blob is the same length and the reshape is exact.
    """
    return np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(len(blobs), -1)


def _cosine_top_k_matrix(
    qvec: np.ndarray,
    ids: list[int],
    matrix: np.ndarray,
    k: int,
) -> list[tuple[int, float]]:
    """Top-k (id, score) by dot product over a pre-decoded (N, DIM) matrix.

    Caller supplies an already-decoded matrix so the blobs are decoded ONCE even
    when the same vectors are also needed for dedup (the recall path). Uses
    ``argpartition`` (O(N)) to find the top-k then sorts only those k, instead of
    a full O(N log N) ``argsort`` over every row.
    """
    n = matrix.shape[0]
    if n == 0:
        return []
    q = qvec.astype(np.float32)
    if matrix.shape[1] != q.shape[0]:
        # Cross-process vector-space mismatch (e.g. MCP server embedder != the
        # embedder this store was built with). Fail loud — never score across
        # mismatched dimensions.
        raise ValueError(
            f"BrainCell vector-space mismatch: store holds {matrix.shape[1]}-d "
            f"vectors but the query is {q.shape[0]}-d. The embed provider/model "
            f"changed since this store was built. Restart Claude so the MCP server "
            f"uses the same embedder, or rebuild with "
            f"`braincell build --reembed`."
        )
    scores = matrix @ q  # dot product per row
    if k < n:
        part = np.argpartition(scores, -k)[-k:]        # O(N) — unsorted top-k
        order = part[np.argsort(scores[part])[::-1]]   # sort only the k winners
    else:
        order = np.argsort(scores)[::-1]
    return [(ids[i], float(scores[i])) for i in order]


def _cosine_top_k(
    qvec: np.ndarray,
    ids: list[int],
    blobs: list[bytes],
    k: int,
) -> list[tuple[int, float]]:
    """Return top-k (chunk_id, score) by dot product (cosine for unit vectors).

    Both qvec and stored vectors are L2-normalised (embed.py normalises every vector
    after embedding, regardless of provider — Ollama qwen3-embedding:0.6b or OpenAI
    text-embedding-3-*), so dot product == cosine similarity without an explicit
    normalisation step here.
    """
    if not blobs:
        return []
    return _cosine_top_k_matrix(qvec, ids, _stack_blobs(blobs), k)


# ── RRF fusion helper ─────────────────────────────────────────────────────────

def _rrf_fuse(
    vec_hits: list[tuple[int, float]],
    fts_hits: list[tuple[int, float]],
    k_rrf: int = _RRF_K,
) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion of two ranked lists.

    Each list is (chunk_id, score). Returns merged list sorted by RRF score
    descending. chunk_ids not in either list get 0 contribution from that side.
    """
    scores: dict[int, float] = {}
    for rank, (cid, _) in enumerate(vec_hits, 1):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k_rrf + rank)
    for rank, (cid, _) in enumerate(fts_hits, 1):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k_rrf + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def _cc_fuse(
    vec_hits: list[tuple[int, float]],
    fts_hits: list[tuple[int, float]],
    alpha: float = _FUSION_ALPHA,
) -> list[tuple[int, float]]:
    """Convex-combination fusion. Both inputs are higher-is-better scores.

    Each list is min-max normalized to [0,1] independently, then fused as
    ``alpha*sem_norm + (1-alpha)*lex_norm``. An id absent from a list contributes
    0 from that side. Returns merged list sorted by fused score descending.

    Unlike RRF (rank-only), CC preserves score magnitude, so a strongly-agreeing
    doc can outrank a weakly-agreeing one that RRF would tie on rank. Needs a
    tuned ``alpha``; RRF stays the default (tuning-free).

    Edge cases: empty list → no contribution; a single item (or all-equal
    scores) normalizes to 1.0 (span 0 → treat every member as top of its list).
    """
    def _norm(hits: list[tuple[int, float]]) -> dict[int, float]:
        if not hits:
            return {}
        vals = [s for _, s in hits]
        lo, hi = min(vals), max(vals)
        span = hi - lo
        if span == 0.0:
            return {cid: 1.0 for cid, _ in hits}
        return {cid: (s - lo) / span for cid, s in hits}

    sem = _norm(vec_hits)
    lex = _norm(fts_hits)
    fused: dict[int, float] = {}
    for cid, v in sem.items():
        fused[cid] = fused.get(cid, 0.0) + alpha * v
    for cid, v in lex.items():
        fused[cid] = fused.get(cid, 0.0) + (1.0 - alpha) * v
    return sorted(fused.items(), key=lambda x: x[1], reverse=True)


def _fuse_hits(
    vec_hits: list[tuple[int, float]],
    fts_hits: list[tuple[int, float]],
) -> list[tuple[int, float]]:
    """Dispatch hybrid fusion by ``BRAINCELL_FUSION`` (default 'rrf').

    Reads the module-level ``_FUSION`` / ``_FUSION_ALPHA`` at call time so tests
    can monkeypatch them. 'rrf' → byte-identical to the previous ``_rrf_fuse``
    call; 'cc' → convex-combination fusion.
    """
    if _FUSION == "cc":
        return _cc_fuse(vec_hits, fts_hits, _FUSION_ALPHA)
    return _rrf_fuse(vec_hits, fts_hits)


# ── Recency decay + confidence blend ──────────────────────────────────────────

_DATETIME_FMT = "%Y-%m-%d %H:%M:%S"


def _recency_decay(age_days: float, half_life_days: float = _HALFLIFE_DAYS) -> float:
    """Recency decay factor: 1.0 at age 0, halving every half_life_days.

    Returns a value in (0, 1] — monotonically decreasing in age_days.
    Negative ages are clamped to 0.0 (no boost above 1.0).

    Args:
        age_days:       Age of the note in fractional days (non-negative after clamp).
        half_life_days: Days until the factor halves (default 90 — ~3 months).

    Returns:
        Decay multiplier in (0, 1].  Exactly 1.0 at age 0; approaches 0 asymptotically.
    """
    return 0.5 ** (max(age_days, 0.0) / half_life_days)


def _blend_score(
    fused_score: float,
    confidence: float | None,
    created_at: str,
    now: datetime,
) -> float:
    """Blend a fused RRF score with a confidence factor and a recency decay.

    Formula: fused_score * (0.5 + 0.5 * confidence) * _recency_decay(age_days).

    Args:
        fused_score: RRF-fused ranking score from ``_rrf_fuse``.
        confidence:  Note's stored confidence value (REAL column, may be None).
                     None → neutral factor 1.0 (unrated notes are not penalised).
        created_at:  Note's ``created_at`` string in ``"%Y-%m-%d %H:%M:%S"`` format.
                     Parse failures default to age_days=0.0 (treated as fresh).
        now:         Reference datetime for age computation (pass ``datetime.now()``
                     once per call-site to keep the batch consistent).

    Returns:
        Blended score ≥ 0.
    """
    conf_factor = (0.5 + 0.5 * confidence) if confidence is not None else 1.0
    try:
        created = datetime.strptime(created_at, _DATETIME_FMT)
        age_days = max((now - created).total_seconds() / 86400.0, 0.0)
    except (ValueError, TypeError):
        age_days = 0.0
    return fused_score * conf_factor * _recency_decay(age_days)


def _project_clause(project) -> tuple[str, list]:
    """Build a `d.project_id` filter that accepts a single ULID, a set/sequence of
    ULIDs (family-scoped search), or None (no filter → all projects).

    Returns ``(sql_fragment, params)`` where sql_fragment is '' (no filter) or
    'd.project_id IN (?,...)'. A str is treated as a single-element set, so existing
    single-project callers keep working unchanged.
    """
    if project is None:
        return "", []
    ids = [project] if isinstance(project, str) else list(project)
    if not ids:
        return "", []
    placeholders = ",".join("?" * len(ids))
    return f"d.project_id IN ({placeholders})", ids


def _notes_project_clause(project, prefix: str = "") -> tuple[str, list]:
    """Build a ``project_id`` filter for memory_notes queries (recall path).

    Mirror of ``_project_clause`` but WITHOUT the ``d.`` table alias — recall
    queries ``memory_notes`` directly (unaliased) in most branches; pass
    ``prefix='mn.'`` for the FTS JOIN variant where the table is aliased as
    ``mn``.

    Returns ``(sql_fragment, params)``:
      - ``('', [])`` when *project* is None or an empty sequence (no filter;
        all projects returned).
      - ``('{col} = ?', [ulid])`` for a single ULID *string*.
      - ``('{col} IN (?,…)', [ulid1, ulid2, …])`` for a non-empty sequence.
    """
    if project is None:
        return "", []
    if isinstance(project, str):
        return f"{prefix}project_id = ?", [project]
    ids = list(project)
    if not ids:
        return "", []
    return f"{prefix}project_id IN ({','.join('?' * len(ids))})", ids


# ── v4 migration helpers (stable note identity + FK-safe link graph) ──────────
# Every helper is IDEMPOTENT and re-entrant: the v3→v4 ladder applies its steps as
# individual DDL statements (sqlite3 autocommits DDL), so a crash mid-migration
# leaves a partially-migrated store that the NEXT open completes rather than
# corrupts — each step re-checks the state it is about to change.


def _new_note_uid() -> str:
    """Mint a stable, lexicographically-sortable cross-database note id (ULID).

    Lazy import mirrors ``config.get_project_id`` — only write paths need it.
    """
    from ulid import ULID
    return str(ULID())


def _backfill_note_uids(con: sqlite3.Connection) -> int:
    """Give every uid-less note a ULID. Returns the number of rows stamped."""
    rows = con.execute("SELECT id FROM memory_notes WHERE note_uid IS NULL").fetchall()
    for (note_id,) in rows:
        con.execute(
            "UPDATE memory_notes SET note_uid = ? WHERE id = ?", (_new_note_uid(), note_id)
        )
    return len(rows)


def _clear_dangling_supersessions(con: sqlite3.Connection) -> int:
    """NULL out ``superseded_by`` values pointing at rows that no longer exist.

    Pre-v4 hard deletes (which never enforced foreign keys) could leave a note
    pointing at a vanished replacement. Those rows must be cleaned BEFORE
    ``PRAGMA foreign_keys=ON``, or every later write to the table would fail.
    """
    cur = con.execute(
        "UPDATE memory_notes SET superseded_by = NULL "
        "WHERE superseded_by IS NOT NULL "
        "AND superseded_by NOT IN (SELECT id FROM memory_notes)"
    )
    return cur.rowcount or 0


def _rebuild_note_links_with_cascade(con: sqlite3.Connection) -> int:
    """Re-create ``bc_note_links`` with ``ON DELETE CASCADE`` on both foreign keys.

    SQLite cannot ALTER a constraint onto an existing table, so the v3 table is
    renamed aside, the canonical (v4) DDL is applied, surviving rows are copied,
    and the old table is dropped. Orphan edges — rows referencing a note that a
    pre-v4 hard delete removed — are dropped in the process (they would otherwise
    fail ``PRAGMA foreign_key_check``). No-op when the table is already v4-shaped.

    Returns the number of orphan edges removed.
    """
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='bc_note_links'"
    ).fetchone()
    if row is None:  # store predates the graph entirely — create it fresh
        con.execute(NOTE_LINKS_DDL)
        con.execute(NOTE_LINKS_IDX_DDL)
        return 0
    if row[0] and "ON DELETE CASCADE" in row[0].upper():
        return 0  # already rebuilt (idempotent re-entry)

    orphans = con.execute(
        "SELECT COUNT(*) FROM bc_note_links WHERE "
        "src_id NOT IN (SELECT id FROM memory_notes) OR "
        "dst_id NOT IN (SELECT id FROM memory_notes)"
    ).fetchone()[0]
    con.execute("DROP TABLE IF EXISTS bc_note_links_v3")
    con.execute("DROP INDEX IF EXISTS bc_note_links_src_idx")
    con.execute("ALTER TABLE bc_note_links RENAME TO bc_note_links_v3")
    con.execute(NOTE_LINKS_DDL)
    con.execute(
        "INSERT OR IGNORE INTO bc_note_links (src_id, dst_id, kind, weight, created_at) "
        "SELECT src_id, dst_id, kind, weight, created_at FROM bc_note_links_v3 "
        "WHERE src_id IN (SELECT id FROM memory_notes) "
        "AND dst_id IN (SELECT id FROM memory_notes)"
    )
    con.execute("DROP TABLE bc_note_links_v3")
    con.execute(NOTE_LINKS_IDX_DDL)
    return int(orphans)


def _live_note_predicate(prefix: str = "") -> str:
    """SQL predicate selecting notes that are CURRENT truth.

    The ``status`` column is the single liveness authority — this predicate is
    its one consumer for "current truth". ``deleted_at``/``superseded_by`` are
    provenance (when it died / what replaced it), kept in sync by write paths but
    never consulted for liveness. Every recall branch funnels through this helper,
    so a future lifecycle state is an enum value + this function, nothing else.

    Args:
        prefix: Table alias prefix (e.g. ``'mn.'``) for the FTS-JOIN variant.
    """
    return f"{prefix}status = 'active'"


def _apply_resolution(
    ranked: list[tuple[int, float]],
    mapping: dict[int, int],
) -> tuple[list[tuple[int, float]], dict[int, list[int]]]:
    """Rewrite a ranked candidate list so superseded hits point at current truth.

    Each superseded candidate is replaced by the note it resolves to, carrying the
    BEST score any of its stale ancestors earned — a retired note matching the
    query strongly is evidence FOR its replacement, which is the whole point:
    a query phrased in the old vocabulary ("use Redis") still surfaces the
    decision that replaced it, even though the replacement never says "Redis".

    Duplicates collapse (two stale notes can resolve to one current note) and the
    original best-first order is preserved.

    Returns:
        ``(rewritten_ranked, {current_id: [stale ids that resolved to it]})``.
    """
    out: list[tuple[int, float]] = []
    index_of: dict[int, int] = {}
    resolved_from: dict[int, list[int]] = {}
    for cid, score in ranked:
        target = mapping.get(cid, cid)
        if target != cid:
            resolved_from.setdefault(target, []).append(cid)
        pos = index_of.get(target)
        if pos is None:
            index_of[target] = len(out)
            out.append((target, score))
        elif score > out[pos][1]:
            out[pos] = (target, score)
    return out, resolved_from


# Maximum supersession hops walked when resolving a stale note to current truth.
# Chains this long do not occur naturally (supersede refuses an already-superseded
# note, and a replacement is always a fresh row, so cycles cannot be created
# through the API); the cap exists so a hand-edited or pool-corrupted cycle
# terminates instead of spinning.
_SUPERSEDE_MAX_HOPS: int = 16


# ── SqliteStore implementation ────────────────────────────────────────────────

class SqliteStore:
    """SqliteStore — V0 BrainCell storage backend.

    One per-project `braincell.db` holds every table: bc_documents / bc_chunks /
    bc_chunks_fts (documents & transcripts) plus memory_notes / memory_fts /
    schema_version / embed_fingerprint (curated memory). A single connection means
    cross-table transactions and atomic backup.

    Vector search: NumPy brute-force cosine over float32 BLOBs.
    Full-text search: FTS5 (with LIKE fallback if FTS5 absent in frozen build).
    """

    def __init__(self, db_path: Path, *, read_only: bool = False) -> None:
        self._db_path = db_path
        # read_only=True opens a federated sibling brain non-mutating (mode=ro).
        self._read_only = read_only
        # single aiosqlite connection (opened lazily on first async call)
        self._conn: aiosqlite.Connection | None = None
        # Writes use a separate connection. One task owns it from BEGIN through
        # commit/rollback, while reads continue on _conn with SQLite isolation.
        self._write_conn: aiosqlite.Connection | None = None
        self._conn_init_lock = asyncio.Lock()
        self._write_conn_init_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        # FTS5 availability (probed in assert_schema_version)
        self._fts5_ok: bool = True
        # Instrumentation: rolling window of _vector_search decode+matmul times
        # (milliseconds) for this session, used by `braincell stats` to decide
        # whether the vec0 ANN backend is worth adopting.
        self._vec_search_ms: list[float] = []

    # ── Schema bootstrap / version gate ──────────────────────────────────────

    def assert_schema_version(self) -> None:
        """Bootstrap the single braincell.db and verify it. Refuses on mismatch.

        Called synchronously (before the event loop starts) from the MCP
        lifespan and from open_store().  Uses stdlib sqlite3 (not aiosqlite)
        so it works in a purely sync context too. Gates BOTH the whole-store
        schema_version AND the embedding-space fingerprint (fail loud rather than
        silently mixing vector spaces — F16).
        """
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self._db_path))
        try:
            con.execute("PRAGMA busy_timeout=30000")
            con.execute("PRAGMA journal_mode=WAL")
            # Probe FTS5 availability against a throwaway in-memory db: FTS5 is
            # a property of the sqlite LIBRARY, not of this file. Probing in the
            # store itself was a REAL write on EVERY open, which serialized
            # behind any concurrent writer's lock (busy_timeout 30 s) — so a
            # running `braincell build` made GUI startup hang past serve_native's
            # 20 s budget and the desktop icon (Terminal=false) dead-clicked.
            # On an already-current db every remaining statement below is a
            # no-op `IF NOT EXISTS` / read, so opening never needs the write
            # lock and stays instant while a build is writing.
            probe = sqlite3.connect(":memory:")
            try:
                probe.execute("CREATE VIRTUAL TABLE _bc_fts5_probe USING fts5(x)")
                self._fts5_ok = True
            except sqlite3.OperationalError:
                self._fts5_ok = False
                log.warning("FTS5 not available in this sqlite3 build — falling back to LIKE scan")
            finally:
                probe.close()
            # Run init DDL (all idempotent CREATE IF NOT EXISTS).
            for stmt in BRAINCELL_INIT_STMTS:
                if "fts5" in stmt.lower() and not self._fts5_ok:
                    log.warning("Skipping FTS5 DDL (unavailable): %s", stmt[:60])
                    continue
                con.execute(stmt)
            con.commit()

            # Check or stamp schema_version — with forward migration for older DBs.
            row = con.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            stored = row[0] if row is not None else None

            # ── Forward migrations (idempotent; additive ALTERs on external-content FTS5 are safe) ──
            if stored is not None and stored < MEMORY_SCHEMA_VERSION:
                if stored < 2:  # v1 → v2: add embedding + deleted_at to memory_notes
                    cols = {r[1] for r in con.execute("PRAGMA table_info(memory_notes)").fetchall()}
                    if "embedding" not in cols:
                        con.execute("ALTER TABLE memory_notes ADD COLUMN embedding BLOB")
                    if "deleted_at" not in cols:
                        con.execute("ALTER TABLE memory_notes ADD COLUMN deleted_at TEXT")
                if stored < 3:  # v2 → v3: add the bc_note_links graph table
                    con.execute(NOTE_LINKS_DDL)
                    con.execute(NOTE_LINKS_IDX_DDL)
                if stored < 4:  # v3 → v4: stable note_uid + revision + FK-safe graph
                    cols = {r[1] for r in con.execute("PRAGMA table_info(memory_notes)").fetchall()}
                    if "note_uid" not in cols:
                        con.execute("ALTER TABLE memory_notes ADD COLUMN note_uid TEXT")
                    if "revision" not in cols:
                        con.execute(
                            "ALTER TABLE memory_notes ADD COLUMN revision "
                            "INTEGER NOT NULL DEFAULT 1"
                        )
                    if "pooled_from" not in cols:
                        con.execute("ALTER TABLE memory_notes ADD COLUMN pooled_from TEXT")
                    dcols = {r[1] for r in con.execute(
                        "PRAGMA table_info(bc_documents)").fetchall()}
                    if "pooled_from" not in dcols:
                        con.execute("ALTER TABLE bc_documents ADD COLUMN pooled_from TEXT")
                    stamped = _backfill_note_uids(con)
                    dangling = _clear_dangling_supersessions(con)
                    orphans = _rebuild_note_links_with_cascade(con)
                    log.info(
                        "braincell v3→v4 migration on %s: %d note_uid stamped, "
                        "%d dangling supersession(s) cleared, %d orphan link(s) dropped.",
                        self._db_path, stamped, dangling, orphans,
                    )
                if stored < 5:  # v4 → v5: merge operation log (consolidate/reflect undo)
                    con.execute(OPERATIONS_DDL)
                    con.execute(OPERATION_NOTES_DDL)
                    con.execute(OPERATION_NOTES_IDX_DDL)
                    # Purely additive: pre-v5 merges have no recorded prior state and
                    # stay un-undoable. `memory log` is simply empty until the first
                    # --apply under v5 — no backfill is possible or attempted.
                if stored < 6:  # v5 → v6: `status` becomes the liveness authority
                    cols = {r[1] for r in con.execute(
                        "PRAGMA table_info(memory_notes)").fetchall()}
                    if "status" not in cols:
                        con.execute(
                            "ALTER TABLE memory_notes ADD COLUMN status TEXT NOT NULL "
                            "DEFAULT 'active' CHECK (status IN "
                            "('active','superseded','tombstoned'))"
                        )
                    # Backfill from the demoted provenance columns. Precedence:
                    # tombstoned dominates (a reflect source is superseded AND
                    # tombstoned — it must not resurface as merely 'superseded').
                    # This derivation is exact because pre-v6 liveness was DEFINED
                    # by these two columns; post-v6 they are provenance only.
                    cur = con.execute(
                        "UPDATE memory_notes SET status = CASE "
                        "WHEN deleted_at IS NOT NULL THEN 'tombstoned' "
                        "WHEN superseded_by IS NOT NULL THEN 'superseded' "
                        "ELSE 'active' END"
                    )
                    log.info(
                        "braincell v5→v6 migration on %s: status stamped on %d note(s).",
                        self._db_path, cur.rowcount or 0,
                    )
                con.execute("UPDATE schema_version SET version = ?", (MEMORY_SCHEMA_VERSION,))
                con.commit()
                stored = MEMORY_SCHEMA_VERSION

            if stored is None:
                con.execute(
                    "INSERT INTO schema_version(version) VALUES (?)",
                    (MEMORY_SCHEMA_VERSION,),
                )
                con.commit()
            elif stored != MEMORY_SCHEMA_VERSION:
                raise RuntimeError(
                    f"BrainCell schema_version mismatch in {self._db_path}: "
                    f"expected {MEMORY_SCHEMA_VERSION}, found {row[0]}. "
                    f"Run `braincell build` to migrate, or delete braincell.db "
                    f"to start fresh (you will lose curated memory notes)."
                )

            # v4: the note_uid unique index is applied HERE, never from
            # BRAINCELL_INIT_STMTS — on a v3 store the column does not exist until
            # the ladder above has run. Idempotent (IF NOT EXISTS) for both a fresh
            # store and one that just migrated.
            con.execute(MEMORY_NOTES_UID_IDX_DDL)
            con.commit()

            # v4: foreign keys are enforced from now on (they never were before, so
            # the graph could accumulate orphan edges). The ladder cleans the two
            # known violation classes first; anything still outstanding is reported
            # rather than raised — a legacy inconsistency must not make an existing
            # brain unopenable.
            con.execute("PRAGMA foreign_keys=ON")
            violations = con.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                log.warning(
                    "%s: %d foreign-key violation(s) remain after migration (first: %r). "
                    "Writes touching those rows may fail; run `braincell backup` and report this.",
                    self._db_path, len(violations), violations[0],
                )

            # Check or stamp the embedding-space fingerprint (F16). A store built
            # with one embedder must never be silently reused with another.
            frow = con.execute(
                "SELECT fingerprint FROM embed_fingerprint LIMIT 1"
            ).fetchone()
            if frow is None:
                con.execute(
                    "INSERT INTO embed_fingerprint(fingerprint) VALUES (?)",
                    (embed_spec.FINGERPRINT,),
                )
                con.commit()
            elif frow[0] != embed_spec.FINGERPRINT:
                raise EmbedderMismatchError(
                    self._db_path, frow[0], embed_spec.FINGERPRINT
                )
        finally:
            con.close()

    # ── Re-embed support (dimension-change safety) ────────────────────────────

    def wipe_project_embeddings(self, project_id: str) -> int:
        """Delete all braincell documents + chunks for a project (sync sqlite3).

        Used by `braincell build --reembed` to prevent silently mixing
        embedding dimensions when the embed provider/model changes:
        the store is emptied so the rebuild writes a single, consistent vector
        space. Returns the number of documents removed. The per-project DB scopes
        this to one project by construction; the project_id filter is
        belt-and-suspenders.
        """
        cf = sqlite3.connect(str(self._db_path))
        try:
            cf.execute("PRAGMA busy_timeout=30000")
            n = cf.execute(
                "SELECT COUNT(*) FROM bc_documents WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
            cf.execute(
                "DELETE FROM bc_chunks WHERE document_id IN "
                "(SELECT id FROM bc_documents WHERE project_id = ?)",
                (project_id,),
            )
            cf.execute("DELETE FROM bc_documents WHERE project_id = ?", (project_id,))
            # Rebuild the external-content FTS index to drop now-stale rows.
            if self._fts5_ok:
                try:
                    cf.execute(
                        "INSERT INTO bc_chunks_fts(bc_chunks_fts) VALUES('rebuild')"
                    )
                except sqlite3.OperationalError:
                    pass
            cf.commit()
            return n
        finally:
            cf.close()

    def reset_embedding_space(self) -> dict:
        """Reset the embedding space fingerprint and clear all vectors (sync sqlite3).

        The ONE sanctioned escape from EmbedderMismatchError. When a fingerprint
        mismatch prevents opening the store, this wipes ALL documents/chunks
        (fingerprint switch invalidates every vector in the DB across all projects)
        and clears note embeddings (old-space vectors must not survive into the
        new space — NULL is honest; FTS keyword recall still works and
        `braincell reembed-notes` backfills). Then restamps the fingerprint so
        `assert_schema_version()` passes on next call.

        Only `build --reembed` may call this; mixing vector spaces corrupts search.

        Returns {"docs_wiped": int, "note_embeddings_cleared": int,
                 "fingerprint": str}.
        """
        cf = sqlite3.connect(str(self._db_path))
        try:
            cf.execute("PRAGMA busy_timeout=30000")
            # Count docs before wiping
            docs_count = cf.execute("SELECT COUNT(*) FROM bc_documents").fetchone()[0]
            # Count notes with embeddings before clearing
            notes_count = cf.execute(
                "SELECT COUNT(*) FROM memory_notes WHERE embedding IS NOT NULL"
            ).fetchone()[0]
            # Delete all chunks (fingerprint switch invalidates every vector)
            cf.execute("DELETE FROM bc_chunks")
            # Delete all documents
            cf.execute("DELETE FROM bc_documents")
            # Rebuild the external-content FTS index to drop now-stale rows.
            if self._fts5_ok:
                try:
                    cf.execute(
                        "INSERT INTO bc_chunks_fts(bc_chunks_fts) VALUES('rebuild')"
                    )
                except sqlite3.OperationalError:
                    pass
            # Clear note embeddings (old-space vectors must not survive)
            cf.execute("UPDATE memory_notes SET embedding = NULL")
            # Restamp the fingerprint
            cf.execute("DELETE FROM embed_fingerprint")
            cf.execute(
                "INSERT INTO embed_fingerprint(fingerprint) VALUES (?)",
                (embed_spec.FINGERPRINT,),
            )
            cf.commit()
            return {
                "docs_wiped": docs_count,
                "note_embeddings_cleared": notes_count,
                "fingerprint": embed_spec.FINGERPRINT,
            }
        finally:
            cf.close()

    # ── Connection accessor (lazy open) ───────────────────────────────────────

    async def _conn_get(self) -> aiosqlite.Connection:
        """Return the (lazily-opened) single aiosqlite connection to braincell.db.

        When ``read_only`` (federated sibling brains), open via a ``file:…?mode=ro``
        URI — an OS-level read-only handle that is the HARD guarantee the sibling
        is never written, checkpointed, or migrated. SQLite ≥ 3.22.0 reads a LIVE
        WAL database this way (the owner process keeps -shm/-wal present + readable,
        so reads see the latest committed frames, not a stale checkpoint —
        sqlite.org/wal.html#readonly). ``PRAGMA query_only=ON`` rejects any
        accidental write early; ``journal_mode`` is never set on a RO handle.
        """
        if self._conn is None:
            async with self._conn_init_lock:
                if self._conn is None:
                    self._conn = await self._open_async_connection(read_only=self._read_only)
        return self._conn

    async def _open_async_connection(
        self, *, read_only: bool,
    ) -> aiosqlite.Connection:
        """Open and configure one async connection without publishing it."""
        if read_only:
            uri = f"file:{Path(self._db_path).resolve().as_posix()}?mode=ro"
            connection = await aiosqlite.connect(uri, uri=True)
            await connection.execute("PRAGMA query_only=ON")
            await connection.execute("PRAGMA busy_timeout=5000")
        else:
            connection = await aiosqlite.connect(str(self._db_path))
            await connection.execute("PRAGMA busy_timeout=30000")
            await connection.execute("PRAGMA journal_mode=WAL")
        # FK enforcement is per-connection and off by default.
        await connection.execute("PRAGMA foreign_keys=ON")
        connection.row_factory = aiosqlite.Row
        return connection

    async def _write_conn_get(self) -> aiosqlite.Connection:
        """Return the dedicated writer connection; read-only stores cannot write."""
        if self._read_only:
            raise PermissionError(f"BrainCell store is read-only: {self._db_path}")
        if self._write_conn is None:
            async with self._write_conn_init_lock:
                if self._write_conn is None:
                    self._write_conn = await self._open_async_connection(read_only=False)
        return self._write_conn

    @asynccontextmanager
    async def _write_transaction(self, *, immediate: bool = True):
        """Give one coroutine exclusive ownership of the writer transaction.

        The dedicated writer connection prevents normal reads from seeing
        uncommitted state. The task lock prevents another writer from entering,
        committing, or rolling back this task's transaction.
        """
        async with self._write_lock:
            connection = await self._write_conn_get()
            await connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except BaseException:
                await connection.rollback()
                raise
            else:
                try:
                    await connection.commit()
                except BaseException:
                    # SQLite can leave a transaction active when COMMIT fails
                    # (for example SQLITE_BUSY). Never release ownership with
                    # the shared writer connection still inside that transaction.
                    await connection.rollback()
                    raise

    # ── search ────────────────────────────────────────────────────────────────

    async def search(
        self,
        qvec: Optional[np.ndarray],
        qtext: str,
        project: str | None,
        k: int,
        mode: str,
        rerank: bool = True,
    ) -> list[Hit]:
        """Hybrid (RRF of vector + FTS5) chunk search.

        mode: 'semantic' | 'keyword' | 'hybrid'.
        project: None → all projects (no filter); a single ULID, or a sequence of
        ULIDs (family/pool scoped) → `d.project_id IN (…)`. The filtering itself is
        delegated to `_project_clause`.
        """
        cf = await self._conn_get()
        fetch_k = max(k * 3, 30)  # over-fetch for fusion quality

        vec_hits: list[tuple[int, float]] = []
        fts_hits: list[tuple[int, float]] = []

        if mode in ("semantic", "hybrid"):
            if qvec is None:
                raise ValueError(f"{mode} search requires a query embedding.")
            vec_hits = await self._vector_search(cf, qvec, project, fetch_k)

        if mode in ("keyword", "hybrid"):
            fts_hits = await self._fts_search(cf, qtext, project, fetch_k)

        if mode == "semantic":
            ranked = [(cid, score) for cid, score in vec_hits]
        elif mode == "keyword":
            ranked = fts_hits
        else:
            ranked = _fuse_hits(vec_hits, fts_hits)

        # When reranking is on, hydrate a wider top-M window so the reranker
        # has candidates to reorder; otherwise keep exactly k (byte-identical).
        from .rerank import rerank_enabled, rerank_hits, rerank_window
        rerank_on = rerank_enabled() and rerank
        limit = max(k, rerank_window()) if rerank_on else k
        ranked = ranked[:limit]
        # Attach interpretable relevance to every hit regardless of mode: the raw
        # cosine (from the vector list) + an FTS-match flag. The mode-dependent
        # `score` stays the ranking signal (RRF in hybrid); `cosine` is what a
        # consumer reasons about (RRF magnitude is rank-only — see Hit docstring).
        vec_cosine = dict(vec_hits)                 # chunk_id -> raw cosine similarity
        fts_ids = {cid for cid, _ in fts_hits}      # chunks that also matched FTS
        hits = await self._hydrate_hits(cf, ranked, vec_cosine, fts_ids)
        if rerank_on:
            hits = await rerank_hits(qtext, hits, top_k=k)
        return hits

    async def _vector_search(
        self,
        cf: aiosqlite.Connection,
        qvec: np.ndarray,
        project: str | None,
        k: int,
    ) -> list[tuple[int, float]]:
        """Fetch all chunks with embeddings, compute NumPy cosine, return top-k."""
        clause, pp = _project_clause(project)
        if clause:
            sql = (
                "SELECT c.id, c.embedding "
                "FROM bc_chunks c "
                "JOIN bc_documents d ON c.document_id = d.id "
                f"WHERE c.embedding IS NOT NULL AND {clause}"
            )
            rows = await (await cf.execute(sql, pp)).fetchall()
        else:
            sql = "SELECT c.id, c.embedding FROM bc_chunks c WHERE c.embedding IS NOT NULL"
            rows = await (await cf.execute(sql)).fetchall()

        if not rows:
            return []

        # Time the decode+matmul (the part vec0 would replace) and record it
        # in a rolling window so `braincell stats` can report p95 for the adopt
        # decision. Instrumentation only — the algorithm is unchanged.
        t0 = time.perf_counter()
        ids = [r[0] for r in rows]
        blobs = [bytes(r[1]) for r in rows]
        out = _cosine_top_k(qvec, ids, blobs, k)
        self._record_vec_search_ms((time.perf_counter() - t0) * 1000.0)
        return out

    def _record_vec_search_ms(self, ms: float) -> None:
        """Append a vector-search timing sample (bounded rolling window)."""
        self._vec_search_ms.append(ms)
        if len(self._vec_search_ms) > 1000:
            self._vec_search_ms = self._vec_search_ms[-1000:]

    def vec_search_p95_ms(self) -> float | None:
        """p95 of this session's vector-search timings (ms), or None if unused."""
        samples = self._vec_search_ms
        if not samples:
            return None
        ordered = sorted(samples)
        idx = min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))
        return ordered[idx]

    async def _fts_search(
        self,
        cf: aiosqlite.Connection,
        qtext: str,
        project: str | None,
        k: int,
    ) -> list[tuple[int, float]]:
        """FTS5 full-text search; LIKE fallback when FTS5 is unavailable."""
        results: list[tuple[int, float]] = []
        if self._fts5_ok:
            try:
                clause, pp = _project_clause(project)
                if clause:
                    sql = (
                        "SELECT c.id, fts.rank "
                        "FROM bc_chunks_fts fts "
                        "JOIN bc_chunks c ON c.id = fts.rowid "
                        "JOIN bc_documents d ON c.document_id = d.id "
                        f"WHERE bc_chunks_fts MATCH ? AND {clause} "
                        "ORDER BY fts.rank LIMIT ?"
                    )
                    rows = await (await cf.execute(sql, (qtext, *pp, k))).fetchall()
                else:
                    sql = (
                        "SELECT c.id, fts.rank "
                        "FROM bc_chunks_fts fts "
                        "JOIN bc_chunks c ON c.id = fts.rowid "
                        "WHERE bc_chunks_fts MATCH ? "
                        "ORDER BY fts.rank LIMIT ?"
                    )
                    rows = await (await cf.execute(sql, (qtext, k))).fetchall()
                # FTS5 rank is negative (lower = more relevant) → invert
                results = [(r[0], -float(r[1])) for r in rows]
            except sqlite3.OperationalError as exc:
                # OperationalError covers: FTS5 syntax error in the MATCH term,
                # FTS5 table absent at runtime, or SQLite constraint violation.
                # Genuine programming errors (e.g. wrong arg types) are NOT caught
                # here and will propagate so they aren't silently masked.
                log.warning("FTS5 search failed, falling back to LIKE: %s", exc)
                results = await self._like_search(cf, qtext, project, k)
        else:
            results = await self._like_search(cf, qtext, project, k)
        return results

    async def _like_search(
        self,
        cf: aiosqlite.Connection,
        qtext: str,
        project: str | None,
        k: int,
    ) -> list[tuple[int, float]]:
        """LIKE-based fallback when FTS5 is absent."""
        pattern = f"%{qtext}%"
        clause, pp = _project_clause(project)
        if clause:
            sql = (
                "SELECT c.id "
                "FROM bc_chunks c "
                "JOIN bc_documents d ON c.document_id = d.id "
                f"WHERE c.chunk_text LIKE ? AND {clause} "
                "LIMIT ?"
            )
            rows = await (await cf.execute(sql, (pattern, *pp, k))).fetchall()
        else:
            sql = "SELECT c.id FROM bc_chunks c WHERE c.chunk_text LIKE ? LIMIT ?"
            rows = await (await cf.execute(sql, (pattern, k))).fetchall()
        return [(r[0], 1.0) for r in rows]

    async def _hydrate_hits(
        self,
        cf: aiosqlite.Connection,
        ranked: list[tuple[int, float]],
        vec_cosine: dict[int, float] | None = None,
        fts_ids: set[int] | None = None,
    ) -> list[Hit]:
        """Fetch doc metadata for top-k chunk ids and build Hit objects.

        vec_cosine maps chunk_id -> raw cosine similarity so a hybrid/keyword hit can
        still expose its interpretable vector relevance; fts_ids is the set of chunks
        that also matched FTS. Both default to empty so direct callers keep working.
        """
        if not ranked:
            return []
        vec_cosine = vec_cosine or {}
        fts_ids = fts_ids or set()
        id_to_score = {cid: score for cid, score in ranked}
        placeholders = ",".join("?" * len(id_to_score))
        sql = (
            f"SELECT c.id, c.chunk_text, d.doc_key, d.title, d.metadata "
            f"FROM bc_chunks c "
            f"JOIN bc_documents d ON c.document_id = d.id "
            f"WHERE c.id IN ({placeholders})"
        )
        rows = await (await cf.execute(sql, list(id_to_score.keys()))).fetchall()
        hits: list[Hit] = []
        for row in rows:
            cid, text, doc_key, title, metadata_raw = row
            meta = json.loads(metadata_raw) if metadata_raw else {}
            hits.append(Hit(
                chunk_id=cid,
                doc_key=doc_key,
                title=title or doc_key,
                snippet=text[:400],
                score=id_to_score.get(cid, 0.0),
                source_path=meta.get("source_path"),
                metadata=meta,
                cosine=vec_cosine.get(cid),
                fts_matched=cid in fts_ids,
            ))
        # Re-sort by original RRF score (hydration may re-order).
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits

    # ── get_document ──────────────────────────────────────────────────────────

    async def get_document(
        self, doc_id_or_key: str | int, project: str | None
    ) -> Doc | None:
        cf = await self._conn_get()
        if isinstance(doc_id_or_key, int):
            row = await (await cf.execute(
                "SELECT id, doc_key, title, content_type, commit_sha, "
                "created_at, updated_at, metadata "
                "FROM bc_documents WHERE id = ?",
                (doc_id_or_key,),
            )).fetchone()
        else:
            if project:
                row = await (await cf.execute(
                    "SELECT id, doc_key, title, content_type, commit_sha, "
                    "created_at, updated_at, metadata "
                    "FROM bc_documents WHERE doc_key = ? AND project_id = ?",
                    (doc_id_or_key, project),
                )).fetchone()
            else:
                row = await (await cf.execute(
                    "SELECT id, doc_key, title, content_type, commit_sha, "
                    "created_at, updated_at, metadata "
                    "FROM bc_documents WHERE doc_key = ? LIMIT 1",
                    (doc_id_or_key,),
                )).fetchone()

        if row is None:
            return None

        doc_id = row[0]
        chunk_rows = await (await cf.execute(
            "SELECT chunk_index, chunk_text FROM bc_chunks "
            "WHERE document_id = ? ORDER BY chunk_index",
            (doc_id,),
        )).fetchall()
        meta = json.loads(row[7]) if row[7] else {}
        return Doc(
            id=row[0],
            doc_key=row[1],
            title=row[2],
            content_type=row[3],
            commit_sha=row[4],
            created_at=row[5],
            updated_at=row[6],
            chunks=[{"index": r[0], "text": r[1]} for r in chunk_rows],
            metadata=meta,
        )

    # ── ingest_status ─────────────────────────────────────────────────────────

    async def ingest_status(self, project: str | None) -> Status:
        cf = await self._conn_get()
        if project:
            doc_row = await (await cf.execute(
                "SELECT COUNT(*), MAX(updated_at) "
                "FROM bc_documents WHERE project_id = ?",
                (project,),
            )).fetchone()
            chunk_row = await (await cf.execute(
                "SELECT COUNT(*) FROM bc_chunks c "
                "JOIN bc_documents d ON c.document_id = d.id "
                "WHERE d.project_id = ?",
                (project,),
            )).fetchone()
        else:
            doc_row = await (await cf.execute(
                "SELECT COUNT(*), MAX(updated_at) FROM bc_documents",
            )).fetchone()
            chunk_row = await (await cf.execute(
                "SELECT COUNT(*) FROM bc_chunks",
            )).fetchone()

        doc_count = doc_row[0] if doc_row else 0
        last_ts = doc_row[1] if doc_row else None
        chunk_count = chunk_row[0] if chunk_row else 0
        return Status(
            indexed=doc_count > 0,
            doc_count=doc_count,
            chunk_count=chunk_count,
            last_ingest_ts=last_ts,
            head_sha=None,  # populated by pipeline when available
            stale=False,
        )

    # ── project_counts ────────────────────────────────────────────────────────

    async def project_counts(self) -> dict[str, dict[str, int]]:
        """{project_id: {"docs": int, "chunks": int, "notes": int}} across the opened DB.

        Notes count LIVE notes only (deleted_at IS NULL).
        """
        from collections import defaultdict

        cf = await self._conn_get()
        result: dict[str, dict[str, int]] = defaultdict(lambda: {"docs": 0, "chunks": 0, "notes": 0})

        doc_rows = await (await cf.execute(
            "SELECT project_id, COUNT(*) FROM bc_documents GROUP BY project_id"
        )).fetchall()
        for pid, cnt in doc_rows:
            result[pid]["docs"] = cnt

        chunk_rows = await (await cf.execute(
            "SELECT d.project_id, COUNT(*) FROM bc_chunks c "
            "JOIN bc_documents d ON c.document_id = d.id GROUP BY d.project_id"
        )).fetchall()
        for pid, cnt in chunk_rows:
            result[pid]["chunks"] = cnt

        note_rows = await (await cf.execute(
            "SELECT project_id, COUNT(*) FROM memory_notes "
            "WHERE status != 'tombstoned' GROUP BY project_id"
        )).fetchall()
        for pid, cnt in note_rows:
            result[pid]["notes"] = cnt

        return dict(result)

    # ── tail_since (GUI activity feed) ────────────────────────────────────────

    async def tail_since(
        self,
        *,
        note_after: int,
        doc_after: int,
        projects: list[str] | None = None,
        limit: int = 30,
    ) -> dict:
        """Rows newer than a per-table id cursor, for the GUI activity feed.

        Read-only. Each SELECT runs per-statement (no explicit BEGIN) so every
        call observes the latest committed rows — the feed must see writes made
        by other connections (MCP server, CLI build) between polls.

        Returns ``{"notes": [...], "documents": [...], "cursors": {"note", "doc"}}``
        where cursors are the current ``max(id)`` per table (0 when empty) —
        the client resets its cursor when these shrink after a clear. Each
        document carries a ``preview``: its first chunk's text clipped to
        ~280 chars, so the feed can show actual memory text instead of the
        transcript's ``.jsonl`` doc key ("" when the doc has no chunks).
        """
        cf = await self._conn_get()

        def _clip(text: str | None, limit: int = 280) -> str:
            if not text:
                return ""
            return text if len(text) <= limit else text[:limit] + "…"

        note_sql = (
            "SELECT id, project_id, kind, content, created_at, status "
            "FROM memory_notes WHERE id > ?"
        )
        note_params: list = [note_after]
        doc_sql = (
            "SELECT d.id, d.project_id, d.title, d.created_at, "
            "(SELECT COUNT(*) FROM bc_chunks c WHERE c.document_id = d.id) AS chunks, "
            "(SELECT c2.chunk_text FROM bc_chunks c2 WHERE c2.document_id = d.id "
            " ORDER BY c2.chunk_index ASC LIMIT 1) AS first_chunk "
            "FROM bc_documents d WHERE d.id > ?"
        )
        doc_params: list = [doc_after]
        if projects:
            ph = ",".join("?" * len(projects))
            note_sql += f" AND project_id IN ({ph})"
            note_params += list(projects)
            doc_sql += f" AND d.project_id IN ({ph})"
            doc_params += list(projects)
        note_sql += " ORDER BY id DESC LIMIT ?"
        note_params.append(limit)
        doc_sql += " ORDER BY d.id DESC LIMIT ?"
        doc_params.append(limit)

        note_rows = await (await cf.execute(note_sql, note_params)).fetchall()
        doc_rows = await (await cf.execute(doc_sql, doc_params)).fetchall()
        note_max = (await (await cf.execute(
            "SELECT max(id) FROM memory_notes"
        )).fetchone())[0]
        doc_max = (await (await cf.execute(
            "SELECT max(id) FROM bc_documents"
        )).fetchone())[0]

        return {
            "notes": [
                {
                    "id": r["id"],
                    "project": r["project_id"],
                    "kind": r["kind"],
                    "content": r["content"],
                    "created_at": r["created_at"],
                    "status": r["status"],
                }
                for r in note_rows
            ],
            "documents": [
                {
                    "id": r["id"],
                    "project": r["project_id"],
                    "title": r["title"],
                    "chunks": r["chunks"],
                    "created_at": r["created_at"],
                    "preview": _clip(r["first_chunk"]),
                }
                for r in doc_rows
            ],
            "cursors": {"note": note_max or 0, "doc": doc_max or 0},
        }

    # ── remember ──────────────────────────────────────────────────────────────

    async def remember(
        self,
        text: str,
        kind: str,
        project: str,
        tags: list[str] | None = None,
        confidence: float | None = None,
        embedding: np.ndarray | None = None,
    ) -> str:
        """Persist a curated memory note. Secret-scans text AND tags before write.

        embedding: optional pre-computed unit float32 vector (embed_spec.DIM).
        When None the note is stored with NULL embedding (FTS-only recall until
        backfilled). A wrong-dim embedding raises ValueError immediately (fail loud
        — never mix vector spaces silently).
        """
        for label, candidate in [("text", text), *(("tag", t) for t in (tags or []))]:
            hit = _secret_scan(candidate)
            if hit:
                raise ValueError(
                    f"braincell_remember refused: {label} {hit}. "
                    f"Do not persist secrets — remove the sensitive content and retry."
                )

        valid_kinds = {"decision", "bug_lesson", "note", "observation"}
        if kind not in valid_kinds:
            raise ValueError(f"Invalid kind '{kind}'. Must be one of: {valid_kinds}")

        if len(text) > _MAX_NOTE_CHARS:
            raise ValueError(
                f"braincell_remember refused: note is {len(text)} chars, over the "
                f"{_MAX_NOTE_CHARS}-char limit. Summarise before persisting."
            )

        if embedding is not None and embedding.shape[0] != embed_spec.DIM:
            raise ValueError(
                f"BrainCell write refused: note embedding is {embedding.shape[0]}-d but "
                f"embed_spec.DIM={embed_spec.DIM} ({embed_spec.FINGERPRINT}). Refusing to "
                f"mix vector spaces — after a provider/model/dim change, rebuild with "
                f"`braincell build --reembed`."
            )

        tags_json = json.dumps(tags or [])
        emb_blob = _vec_to_blob(embedding) if embedding is not None else None
        async with self._write_transaction() as mem:
            # Single transaction: the note row and its FTS index entry commit
            # TOGETHER, so a crash can never leave a durable note that is
            # permanently keyword-unsearchable (previously separate commits).
            note_id = await self._insert_note_tx(
                mem, project, kind, text, tags_json, confidence, emb_blob,
            )
            # Auto-link this note to similar recent notes (no-op when NULL vec
            # or links unavailable); commits in the same transaction as the note.
            await self._autolink_note(mem, note_id, project, embedding)
        return str(note_id)

    # ── shared non-committing write primitives (atomic merges) ────────────────
    # Every ``*_tx`` helper mutates WITHOUT committing — the caller owns the
    # transaction. This is what lets `remember`/`forget` stay single-write
    # methods while `consolidate_cluster_atomic`/`reflect_cluster_atomic`
    # compose the same statements into one all-or-nothing cluster merge.

    async def _insert_note_tx(
        self,
        mem: aiosqlite.Connection,
        project_id: str,
        kind: str,
        content: str,
        tags_json: str,
        confidence: float | None,
        emb_blob: bytes | None,
    ) -> int:
        """INSERT a note row + its FTS entry. No commit; returns the new id."""
        cursor = await mem.execute(
            "INSERT INTO memory_notes "
            "(project_id, scope, kind, content, tags, confidence, embedding, note_uid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, "project", kind, content, tags_json, confidence, emb_blob,
             _new_note_uid()),
        )
        note_id = cursor.lastrowid
        if self._fts5_ok:
            try:
                await mem.execute(
                    "INSERT INTO memory_fts(rowid, content) VALUES (?, ?)",
                    (note_id, content),
                )
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if (
                    "no such table: memory_fts" in message
                    or "no such module: fts5" in message
                ):
                    # A verified capability loss is recoverable: mark FTS
                    # unavailable so recall takes its recency path.
                    self._fts5_ok = False
                    log.warning(
                        "FTS5 became unavailable; using recency recall: %s", exc
                    )
                else:
                    # An individual indexing failure is not capability loss.
                    # Propagate so _write_transaction rolls the base note back.
                    raise
        return note_id

    async def _tombstone_note_tx(
        self, mem: aiosqlite.Connection, note_id: int, project_id: str,
    ) -> int:
        """Soft-delete one owned, not-yet-tombstoned note. No commit; rowcount."""
        cursor = await mem.execute(
            "UPDATE memory_notes SET deleted_at = datetime('now'), "
            "status = 'tombstoned' "
            "WHERE id = ? AND project_id = ? AND status != 'tombstoned'",
            (note_id, project_id),
        )
        return cursor.rowcount

    # ── forget / supersede (retraction) ───────────────────────────────────────

    async def forget(
        self,
        note_id: int,
        project_id: str,
        hard: bool = False,
    ) -> bool:
        """Retract a memory note, scoped to project_id.

        Soft (default, hard=False): sets deleted_at = datetime('now') on the
        memory_notes row; the FTS index entry is kept in place (recall filters
        tombstones at query time). Returns True iff a live (non-tombstoned) row
        was found and tombstoned; returns False if not found, wrong project, or
        already tombstoned (idempotent).

        Hard (hard=True): permanently DELETEs the memory_notes row AND the FTS
        index entry. Returns True iff a row was removed. Use for GDPR/scrub
        requests or explicit purge; cannot be undone.

        In both cases the ownership check (project_id match) is enforced —
        a note owned by another project is never touched.
        """
        async with self._write_transaction() as mem:
            if not hard:
                # Soft-delete: stamp deleted_at only when the row is live (owned + not yet
                # tombstoned). rowcount == 0 means not-found / wrong-project / already-dead.
                changed = await self._tombstone_note_tx(mem, note_id, project_id)
                return changed > 0

            # Hard-delete: verify ownership first, then remove FTS entry then the row.
            row = await (await mem.execute(
                "SELECT id FROM memory_notes WHERE id = ? AND project_id = ?",
                (note_id, project_id),
            )).fetchone()
            if row is None:
                return False
            # External-content FTS5: delete the index entry FIRST (while the content
            # row still holds the text to un-index), then the note row.
            if self._fts5_ok:
                try:
                    await mem.execute("DELETE FROM memory_fts WHERE rowid = ?", (note_id,))
                except sqlite3.OperationalError as exc:
                    log.warning("FTS5 memory_fts delete failed (non-fatal): %s", exc)
            await mem.execute(
                "UPDATE memory_notes SET superseded_by = NULL, "
                "status = CASE WHEN status = 'superseded' THEN 'active' ELSE status END "
                "WHERE superseded_by = ?",
                (note_id,),
            )
            await mem.execute("DELETE FROM memory_notes WHERE id = ?", (note_id,))
            return True

    async def supersede(
        self,
        note_id: int,
        new_content: str,
        project_id: str,
        kind: str | None = None,
        tags: list[str] | None = None,
        confidence: float | None = None,
        embedding: np.ndarray | None = None,
    ) -> int:
        """Replace a note's content with a new note and mark the old one superseded.

        Creates a new memory_notes row (new_content) and sets the OLD note's
        superseded_by to the new id. The old note is retained for audit. Inherits
        the old note's kind/tags/confidence unless explicitly overridden. Scoped to
        project_id; raises ValueError if the note is absent or owned by another
        project. Secret-scans new_content before persisting.

        Concurrency: the read, the insert and the pointer update run in one
        ``BEGIN IMMEDIATE`` transaction and the update is a compare-and-set on
        ``status = 'active'`` (the liveness authority). If another writer
        superseded or tombstoned the note first, nothing is written and
        ``SupersedeConflict`` is raised. ``revision`` is bumped on the superseded
        row, giving optimistic-concurrency ground truth for future editors.

        embedding: optional pre-computed unit float32 vector for the NEW note.
        When None the new note is stored with NULL embedding (FTS-only recall until
        backfilled). Wrong-dim raises ValueError immediately.
        """
        hit = _secret_scan(new_content)
        if hit:
            raise ValueError(
                f"braincell_supersede refused: new content {hit}. "
                f"Do not persist secrets — remove the sensitive content and retry."
            )
        if len(new_content) > _MAX_NOTE_CHARS:
            raise ValueError(
                f"braincell_supersede refused: note is {len(new_content)} chars, over "
                f"the {_MAX_NOTE_CHARS}-char limit. Summarise before persisting."
            )

        if embedding is not None and embedding.shape[0] != embed_spec.DIM:
            raise ValueError(
                f"BrainCell write refused: note embedding is {embedding.shape[0]}-d but "
                f"embed_spec.DIM={embed_spec.DIM} ({embed_spec.FINGERPRINT}). Refusing to "
                f"mix vector spaces — after a provider/model/dim change, rebuild with "
                f"`braincell build --reembed`."
            )

        # The whole read-modify-write runs in ONE immediate transaction so two
        # writers racing to supersede the same note cannot both win. BEGIN IMMEDIATE
        # takes SQLite's write lock up front (busy_timeout applies), and the final
        # UPDATE is a compare-and-set: it only matches while the note is still live
        # and unsuperseded, so the loser's rowcount is 0 and it rolls back.
        async with self._write_transaction() as mem:
            old = await (await mem.execute(
                "SELECT kind, tags, confidence, deleted_at, superseded_by FROM memory_notes "
                "WHERE id = ? AND project_id = ?",
                (note_id, project_id),
            )).fetchone()
            if old is None:
                raise ValueError(
                    f"Note {note_id} not found for project {project_id} — cannot supersede."
                )
            if old[3] is not None:
                raise SupersedeConflict(
                    f"Note {note_id} was tombstoned by another writer — nothing to supersede."
                )
            if old[4] is not None:
                raise SupersedeConflict(
                    f"Note {note_id} was already superseded by note {old[4]} "
                    f"(another writer got there first). Re-read the current note and retry."
                )

            new_kind = kind or old[0]
            new_tags = tags if tags is not None else (json.loads(old[1]) if old[1] else [])
            new_conf = confidence if confidence is not None else old[2]
            emb_blob = _vec_to_blob(embedding) if embedding is not None else None

            new_id = await self._insert_note_tx(
                mem, project_id, new_kind, new_content, json.dumps(new_tags),
                new_conf, emb_blob,
            )
            upd = await mem.execute(
                "UPDATE memory_notes SET superseded_by = ?, status = 'superseded', "
                "revision = revision + 1 "
                "WHERE id = ? AND status = 'active'",
                (new_id, note_id),
            )
            if upd.rowcount != 1:
                raise SupersedeConflict(
                    f"Note {note_id} changed underneath this supersede "
                    f"(another writer superseded or tombstoned it). Nothing was written."
                )
            # Auto-link the new note to similar recent notes (no-op when NULL vec).
            await self._autolink_note(mem, new_id, project_id, embedding)
        return new_id

    # ── Merge operation log (undo for consolidate/reflect) ────────────────────
    # Recording is deliberately split from mutating: callers open an operation,
    # record each note's PRIOR state immediately before changing it, then finalize.
    # That ordering is what makes undo exact rather than a guess — see schema.py.

    async def begin_operation(
        self, kind: str, project_id: str, backup_path: str | None = None,
    ) -> int:
        """Open a merge operation and return its id (the `<op#>` users pass to undo)."""
        if kind not in ("consolidate", "reflect"):
            raise ValueError(f"Invalid operation kind '{kind}'.")
        async with self._write_transaction() as mem:
            cur = await mem.execute(
                "INSERT INTO bc_operations(kind, project_id, backup_path) VALUES (?, ?, ?)",
                (kind, project_id, backup_path),
            )
            return int(cur.lastrowid)

    async def _record_operation_note_tx(
        self, mem: aiosqlite.Connection, op_id: int, note_id: int, action: str,
    ) -> None:
        """Snapshot a note's prior state under `op_id`. No commit — caller owns it."""
        row = await (await mem.execute(
            "SELECT deleted_at, superseded_by FROM memory_notes WHERE id = ?",
            (note_id,),
        )).fetchone()
        prev_deleted, prev_superseded = (row[0], row[1]) if row else (None, None)
        await mem.execute(
            "INSERT OR IGNORE INTO bc_operation_notes"
            "(op_id, note_id, action, prev_deleted_at, prev_superseded_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (op_id, note_id, action, prev_deleted, prev_superseded),
        )

    async def record_operation_note(
        self, op_id: int, note_id: int, action: str,
    ) -> None:
        """Snapshot a note's current deleted_at/superseded_by under `op_id`.

        MUST be called BEFORE the mutation — it reads the live row to capture the
        values undo will restore. Idempotent per (op_id, note_id, action): a repeat
        keeps the FIRST snapshot, so re-recording after a partial mutation cannot
        overwrite the true prior state with an already-mutated one.
        """
        async with self._write_transaction() as mem:
            await self._record_operation_note_tx(mem, op_id, note_id, action)

    # ── Atomic cluster merges (all-or-nothing per cluster) ────────────────────
    # Previously, `consolidate --apply` / `reflect --apply` issued each snapshot,
    # supersede and tombstone as its OWN committed transaction — a crash mid-
    # cluster could leave a live synthesis with live sources (duplicate truth, no
    # supersession chain) or a synthesis absent from the op-log (un-undoable).
    # These two methods run one whole cluster inside a single BEGIN IMMEDIATE:
    # snapshots, insert, pointers and tombstones commit together or not at all.
    # Anything expensive (LLM synthesis, embedding) happens BEFORE the call, so
    # the write lock is never held across a model invocation.

    async def consolidate_cluster_atomic(
        self,
        op_id: int,
        project_id: str,
        cluster_ids: list[int],
        representative_id: int,
        merged_content: str | None = None,
    ) -> int | None:
        """Atomically merge one near-duplicate cluster under operation `op_id`.

        Deterministic path (merged_content=None): snapshot + tombstone every
        member except the representative, which stays live untouched.

        LLM path (merged_content given): snapshot every member, supersede the
        representative with the merged note (inherits its kind/tags/confidence;
        CAS on ``status='active'`` — a concurrent writer raises
        ``SupersedeConflict`` and the WHOLE cluster rolls back), record the new
        note as 'created', then tombstone all members. Returns the new note id,
        or None on the deterministic path.
        """
        if merged_content is not None:
            hit = _secret_scan(merged_content)
            if hit:
                raise ValueError(
                    f"braincell consolidate refused: merged content {hit}. "
                    f"Do not persist secrets."
                )
            if len(merged_content) > _MAX_NOTE_CHARS:
                raise ValueError(
                    f"braincell consolidate refused: merged note is "
                    f"{len(merged_content)} chars, over the {_MAX_NOTE_CHARS}-char limit."
                )

        new_id: int | None = None
        async with self._write_transaction() as mem:
            if merged_content is None:
                for nid in cluster_ids:
                    if nid == representative_id:
                        continue
                    # Snapshot BEFORE the mutation — same discipline as `supersede`.
                    await self._record_operation_note_tx(mem, op_id, nid, "tombstoned")
                    await self._tombstone_note_tx(mem, nid, project_id)
            else:
                for nid in cluster_ids:
                    await self._record_operation_note_tx(mem, op_id, nid, "tombstoned")
                old = await (await mem.execute(
                    "SELECT kind, tags, confidence FROM memory_notes "
                    "WHERE id = ? AND project_id = ?",
                    (representative_id, project_id),
                )).fetchone()
                if old is None:
                    raise ValueError(
                        f"Note {representative_id} not found for project {project_id} "
                        f"— cannot merge cluster."
                    )
                new_id = await self._insert_note_tx(
                    mem, project_id, old[0], merged_content, old[1] or "[]",
                    old[2], None,
                )
                await self._record_operation_note_tx(mem, op_id, new_id, "created")
                upd = await mem.execute(
                    "UPDATE memory_notes SET superseded_by = ?, status = 'superseded', "
                    "revision = revision + 1 "
                    "WHERE id = ? AND status = 'active'",
                    (new_id, representative_id),
                )
                if upd.rowcount != 1:
                    raise SupersedeConflict(
                        f"Note {representative_id} changed underneath this merge "
                        f"(another writer superseded or tombstoned it). "
                        f"Nothing was written for this cluster."
                    )
                for nid in cluster_ids:
                    await self._tombstone_note_tx(mem, nid, project_id)
        return new_id

    async def reflect_cluster_atomic(
        self,
        op_id: int,
        project_id: str,
        cluster_ids: list[int],
        content: str,
        embedding: np.ndarray | None = None,
        confidence: float | None = 0.8,
        kind: str = "note",
    ) -> int:
        """Atomically apply one reflect synthesis under operation `op_id`.

        Inside one transaction: insert the synthesized note ('created'), then for
        each source — snapshot ('superseded', BEFORE mutation), point its
        ``superseded_by`` at the synthesis, tombstone it. The synthesis is
        auto-linked AFTER the sources are retired, so it never links to the notes
        it just replaced. Returns the synthesis note id.
        """
        hit = _secret_scan(content)
        if hit:
            raise ValueError(
                f"braincell reflect refused: synthesized content {hit}. "
                f"Do not persist secrets."
            )
        if len(content) > _MAX_NOTE_CHARS:
            raise ValueError(
                f"braincell reflect refused: synthesized note is {len(content)} chars, "
                f"over the {_MAX_NOTE_CHARS}-char limit."
            )
        if embedding is not None and embedding.shape[0] != embed_spec.DIM:
            raise ValueError(
                f"BrainCell write refused: note embedding is {embedding.shape[0]}-d but "
                f"embed_spec.DIM={embed_spec.DIM} ({embed_spec.FINGERPRINT})."
            )

        emb_blob = _vec_to_blob(embedding) if embedding is not None else None
        async with self._write_transaction() as mem:
            synth_id = await self._insert_note_tx(
                mem, project_id, kind, content, "[]", confidence, emb_blob,
            )
            # Undo tombstones the synthesis; without this record, restoring the
            # sources would leave both them AND their replacement live.
            await self._record_operation_note_tx(mem, op_id, synth_id, "created")
            for nid in cluster_ids:
                # Snapshot BEFORE mutating — this is what makes undo exact.
                await self._record_operation_note_tx(mem, op_id, nid, "superseded")
                await mem.execute(
                    "UPDATE memory_notes SET superseded_by = ?, "
                    "status = CASE WHEN status = 'tombstoned' THEN status "
                    "ELSE 'superseded' END "
                    "WHERE id = ? AND project_id = ?",
                    (synth_id, nid, project_id),
                )
                await self._tombstone_note_tx(mem, nid, project_id)
            await self._autolink_note(mem, synth_id, project_id, embedding)
        return synth_id

    async def finalize_operation(self, op_id: int) -> int:
        """Stamp the operation's note_count. Returns it. Drops an empty operation so
        a dry-run-like no-op never litters `memory log`."""
        async with self._write_transaction() as mem:
            row = await (await mem.execute(
                "SELECT COUNT(*) FROM bc_operation_notes WHERE op_id = ?", (op_id,),
            )).fetchone()
            count = int(row[0]) if row else 0
            if count == 0:
                await mem.execute("DELETE FROM bc_operations WHERE id = ?", (op_id,))
            else:
                await mem.execute(
                    "UPDATE bc_operations SET note_count = ? WHERE id = ?", (count, op_id),
                )
        return count

    async def list_operations(
        self, project_id: str | None = None, limit: int = 20,
    ) -> list[dict]:
        """Most-recent-first merge operations, for `braincell memory log`."""
        mem = await self._conn_get()
        sql = (
            "SELECT id, kind, project_id, created_at, note_count, backup_path, undone_at "
            "FROM bc_operations"
        )
        params: list = []
        if project_id is not None:
            sql += " WHERE project_id = ?"
            params.append(project_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = await (await mem.execute(sql, tuple(params))).fetchall()
        return [
            {
                "id": r[0], "kind": r[1], "project_id": r[2], "created_at": r[3],
                "note_count": r[4], "backup_path": r[5], "undone_at": r[6],
            }
            for r in rows
        ]

    async def undo_operation(self, op_id: int, project_id: str) -> dict:
        """Reverse a recorded merge operation. Returns a per-note outcome summary.

        Restores each note's snapshotted deleted_at/superseded_by, and tombstones
        any note the operation CREATED (reflect's synthesis) — without that, undoing
        a reflect resurrects the sources while leaving their replacement live, so
        recall would return both.

        Refuses (raises ValueError) on an unknown/foreign/already-undone operation.
        Per-note it REFUSES rather than forces: a note whose current state no longer
        matches what the operation left behind was changed by someone else since,
        and is reported as 'skipped_changed' instead of being clobbered. Runs in one
        BEGIN IMMEDIATE transaction, mirroring `supersede`'s discipline.
        """
        async with self._write_transaction() as mem:
            op = await (await mem.execute(
                "SELECT kind, project_id, undone_at FROM bc_operations WHERE id = ?",
                (op_id,),
            )).fetchone()
            if op is None:
                raise ValueError(f"No operation {op_id} in this brain.")
            if op[1] != project_id:
                raise ValueError(
                    f"Operation {op_id} belongs to project {op[1]}, not {project_id}."
                )
            if op[2] is not None:
                raise ValueError(f"Operation {op_id} was already undone at {op[2]}.")

            rows = await (await mem.execute(
                "SELECT note_id, action, prev_deleted_at, prev_superseded_by "
                "FROM bc_operation_notes WHERE op_id = ?",
                (op_id,),
            )).fetchall()

            restored, skipped, missing = [], [], []
            for note_id, action, prev_deleted, prev_superseded in rows:
                cur = await (await mem.execute(
                    "SELECT deleted_at, superseded_by FROM memory_notes "
                    "WHERE id = ? AND project_id = ?",
                    (note_id, project_id),
                )).fetchone()
                if cur is None:
                    missing.append(note_id)   # hard-deleted since; nothing to restore
                    continue
                if action == "created":
                    # Reflect's synthesis: tombstone it so the restored sources are
                    # the only current truth again. Already tombstoned → nothing to do.
                    if cur[0] is None:
                        await mem.execute(
                            "UPDATE memory_notes SET deleted_at = datetime('now'), "
                            "status = 'tombstoned' "
                            "WHERE id = ? AND project_id = ? AND status != 'tombstoned'",
                            (note_id, project_id),
                        )
                    restored.append(note_id)
                    continue
                # Compare-and-set: only reverse a note STILL in the merged-away state
                # this operation left it in. 'tombstoned' must still be tombstoned,
                # 'superseded' must still be superseded. Anything else means a later
                # writer (or an earlier undo) already owns this row — skip, never
                # clobber. rowcount==0 is the whole signal.
                # NOTE: these guards stay on the PROVENANCE columns on purpose —
                # a reflect source is superseded AND tombstoned (status='tombstoned'),
                # so a status='superseded' guard would wrongly skip it. Provenance is
                # exact here; only the restored liveness is re-derived as status.
                guard = ("deleted_at IS NOT NULL" if action == "tombstoned"
                         else "superseded_by IS NOT NULL")
                prev_status = ("tombstoned" if prev_deleted is not None
                               else "superseded" if prev_superseded is not None
                               else "active")
                upd = await mem.execute(
                    f"UPDATE memory_notes SET deleted_at = ?, superseded_by = ?, "
                    f"status = ? "
                    f"WHERE id = ? AND project_id = ? AND {guard}",
                    (prev_deleted, prev_superseded, prev_status, note_id, project_id),
                )
                (restored if upd.rowcount == 1 else skipped).append(note_id)

            await mem.execute(
                "UPDATE bc_operations SET undone_at = datetime('now') WHERE id = ?",
                (op_id,),
            )
        return {
            "op_id": op_id, "kind": op[0], "restored": restored,
            "skipped_changed": skipped, "missing": missing,
        }

    # ── Optional reranker over the recalled notes ─────────────────────────────

    async def _maybe_rerank_notes(self, qtext: str, notes: list[Note]) -> list[Note]:
        """Reorder recalled notes with the local reranker when enabled (else no-op)."""
        from .rerank import rerank_enabled, rerank_notes
        if not rerank_enabled() or not qtext or not notes:
            return notes
        return await rerank_notes(qtext, notes, top_k=len(notes))

    # ── Graph note-links (auto-link on write, expand on read) ─────────────────

    async def _autolink_note(
        self,
        mem: aiosqlite.Connection,
        note_id: int,
        project_id: str,
        vec: np.ndarray | None,
    ) -> None:
        """Auto-create ``related`` links from a freshly-written note to similar ones.

        Compares *vec* against the embeddings of up to ``_LINK_RECENT_N`` recent
        live same-project notes; for each whose cosine ≥ ``_LINK_COS`` a
        bidirectional ``related`` link is inserted (INSERT OR IGNORE — idempotent
        against the UNIQUE(src,dst,kind) constraint). No-op when the note has no
        embedding (NULL-embedding notes cannot be compared). Does not commit —
        the caller's transaction commits it together with the note write.
        """
        if vec is None:
            return
        try:
            rows = await (await mem.execute(
                "SELECT id, embedding FROM memory_notes "
                "WHERE embedding IS NOT NULL AND status != 'tombstoned' "
                "AND project_id = ? AND id != ? "
                "ORDER BY id DESC LIMIT ?",
                (project_id, note_id, _LINK_RECENT_N),
            )).fetchall()
        except sqlite3.OperationalError as exc:
            log.warning("autolink query failed (non-fatal): %s", exc)
            return
        if not rows:
            return
        ids = [r[0] for r in rows]
        mat = np.stack([_blob_to_vec(bytes(r[1])) for r in rows])
        sims = mat @ vec.astype(np.float32)
        for other_id, sim in zip(ids, sims):
            if float(sim) >= _LINK_COS:
                w = float(sim)
                for a, b in ((note_id, other_id), (other_id, note_id)):
                    try:
                        await mem.execute(
                            "INSERT OR IGNORE INTO bc_note_links "
                            "(src_id, dst_id, kind, weight) VALUES (?, ?, 'related', ?)",
                            (a, b, w),
                        )
                    except sqlite3.OperationalError as exc:
                        log.warning("autolink insert failed (non-fatal): %s", exc)
                        return

    async def _maybe_expand_links(
        self,
        mem: aiosqlite.Connection,
        notes: list[Note],
        project: str | Sequence[str] | None,
    ) -> list[Note]:
        """Append up to ``_LINK_EXPAND`` graph-linked notes after the ranked set.

        When ``BRAINCELL_LINK_EXPAND`` is 0 (default) this returns *notes*
        unchanged — recall is byte-identical to no-expansion. Otherwise it pulls notes
        linked FROM the current result (highest link weight first), skips any
        already present, and appends them tagged ``expansion=True`` — never
        displacing a direct hit.
        """
        if _LINK_EXPAND <= 0 or not notes:
            return notes
        present = {n.id for n in notes}
        src_ids = list(present)
        ph = ",".join("?" * len(src_ids))
        try:
            link_rows = await (await mem.execute(
                f"SELECT src_id, dst_id, kind, weight FROM bc_note_links "
                f"WHERE src_id IN ({ph}) ORDER BY weight DESC",
                src_ids,
            )).fetchall()
        except sqlite3.OperationalError as exc:
            log.warning("link expansion query failed (non-fatal): %s", exc)
            return notes
        seen = set(present)
        want: list[int] = []
        edge: dict[int, tuple[int, str, float | None]] = {}  # dst -> (src, kind, weight)
        for src, dst, kind, weight in link_rows:
            if dst not in seen:
                seen.add(dst)
                want.append(dst)
                edge[dst] = (src, kind, weight)
            if len(want) >= _LINK_EXPAND:
                break
        if not want:
            return notes
        wph = ",".join("?" * len(want))
        # Scope + liveness are enforced HERE, not just on the primary ranked set:
        # auto-links are same-project today, but a manual or pooled cross-project
        # link must never let expansion leak past a scope='self' recall, and an
        # "also-see" note that has been retired is not something to also see.
        _ec, _ep = _notes_project_clause(project)
        _e_and = f" AND {_ec}" if _ec else ""
        rows = await (await mem.execute(
            "SELECT id, project_id, scope, kind, content, tags, confidence, "
            "source_hint, superseded_by, created_at, status FROM memory_notes "
            f"WHERE id IN ({wph}) AND {_live_note_predicate()}{_e_and}",
            [*want, *_ep],
        )).fetchall()
        by_id = {r[0]: r for r in rows}
        for dst in want:  # preserve link-weight order
            row = by_id.get(dst)
            if row is None:
                continue
            src_id, rel_kind, rel_weight = edge.get(dst, (None, None, None))
            notes.append(Note(
                id=row[0],
                project_id=row[1],
                scope=row[2],
                kind=row[3],
                content=row[4],
                tags=json.loads(row[5] or "[]"),
                confidence=row[6],
                source_hint=row[7],
                superseded_by=row[8],
                created_at=row[9],
                status=row[10],
                expansion=True,
                retrieval_origin="linked",
                linked_from=src_id,
                relation=rel_kind,
                relation_weight=rel_weight,
            ))
        return notes

    # ── Supersession resolution (stale hit → current truth) ───────────────────

    async def _resolve_supersession(
        self,
        mem: aiosqlite.Connection,
        ids: Sequence[int],
    ) -> dict[int, int]:
        """Map every superseded id in *ids* to the CURRENT note it resolves to.

        One depth-capped recursive walk over ``superseded_by``. Ids that are
        already current are absent from the result. Never raises: a missing table
        or malformed chain degrades to "resolve nothing" (no resolution at all).
        """
        wanted = [int(i) for i in ids]
        if not wanted:
            return {}
        ph = ",".join("?" * len(wanted))
        try:
            rows = await (await mem.execute(
                f"WITH RECURSIVE chain(root, node, depth) AS ("
                f"  SELECT id, superseded_by, 1 FROM memory_notes "
                f"   WHERE id IN ({ph}) AND superseded_by IS NOT NULL "
                f"  UNION ALL "
                f"  SELECT c.root, n.superseded_by, c.depth + 1 "
                f"    FROM chain c JOIN memory_notes n ON n.id = c.node "
                f"   WHERE n.superseded_by IS NOT NULL AND c.depth < ?"
                f") SELECT root, node, depth FROM chain",
                [*wanted, _SUPERSEDE_MAX_HOPS],
            )).fetchall()
        except sqlite3.OperationalError as exc:
            log.warning("supersession resolution failed (non-fatal): %s", exc)
            return {}

        deepest: dict[int, tuple[int, int]] = {}  # root -> (depth, terminal id)
        for root, node, depth in rows:
            if node is None:
                continue
            seen = deepest.get(root)
            if seen is None or depth > seen[0]:
                deepest[root] = (depth, node)
        resolved: dict[int, int] = {}
        for root, (depth, node) in deepest.items():
            if depth >= _SUPERSEDE_MAX_HOPS:
                log.warning(
                    "note %s: supersession chain hit the %d-hop cap — resolving to note %s "
                    "(a cycle or a corrupted chain; check `superseded_by`).",
                    root, _SUPERSEDE_MAX_HOPS, node,
                )
            resolved[root] = node
        return resolved

    async def _note_history(
        self,
        mem: aiosqlite.Connection,
        stale_ids: Sequence[int],
    ) -> dict[int, dict]:
        """Fetch compact records of superseded notes, for the ``history`` field."""
        ids = sorted({int(i) for i in stale_ids})
        if not ids:
            return {}
        ph = ",".join("?" * len(ids))
        rows = await (await mem.execute(
            f"SELECT id, kind, content, created_at FROM memory_notes WHERE id IN ({ph})",
            ids,
        )).fetchall()
        return {
            r[0]: {"id": r[0], "kind": r[1], "content": r[2],
                   "created_at": r[3], "status": "superseded"}
            for r in rows
        }

    async def _rows_to_notes(
        self,
        mem: aiosqlite.Connection,
        rows: Sequence,
        resolved_from: dict[int, list[int]] | None = None,
    ) -> list[Note]:
        """Build ``Note`` objects from ``_SELECT``-shaped rows.

        Rows reached by resolving a superseded hit are tagged
        ``retrieval_origin='resolved'`` and carry the retired note(s) in
        ``history``, so a consumer can see WHAT the current answer replaced
        instead of being told only the conclusion.
        """
        resolved_from = resolved_from or {}
        present = {row[0] for row in rows}
        stale_ids = [
            sid for nid, sids in resolved_from.items() if nid in present for sid in sids
        ]
        history = await self._note_history(mem, stale_ids) if stale_ids else {}
        notes: list[Note] = []
        for row in rows:
            stale = resolved_from.get(row[0], [])
            notes.append(Note(
                id=row[0],
                project_id=row[1],
                scope=row[2],
                kind=row[3],
                content=row[4],
                tags=json.loads(row[5] or "[]"),
                confidence=row[6],
                source_hint=row[7],
                superseded_by=row[8],
                created_at=row[9],
                status=row[10],
                retrieval_origin="resolved" if stale else "direct",
                resolved_from=stale[0] if stale else None,
                history=[history[s] for s in stale if s in history],
            ))
        return notes

    # ── recall ────────────────────────────────────────────────────────────────

    async def recall(
        self,
        qvec: np.ndarray | None,
        project: str | Sequence[str] | None,
        k: int,
        qtext: str = "",
        min_cosine: float | None = None,
        dedup: bool = True,
        rerank: bool = True,
        include_superseded: bool = False,
    ) -> list[Note]:
        """Recall curated memory notes.

        When qvec is not None, uses hybrid RRF (vector cosine over embedded notes +
        FTS5 MATCH over memory_fts when qtext is given) — mirrors the chunk search()
        pipeline. NULL-embedding notes appear only via the FTS list (correct;
        semantically ranked once backfilled with `braincell reembed-notes`). Falls
        back to recency when neither vector hits nor FTS hits are available.

        When qvec is None: keyword (FTS5 MATCH over memory_fts, recency tie-breaker) when qtext is
        non-empty and FTS5 is available, else pure recency (newest-first).
        project: a ULID scopes to that project; project=None returns notes across ALL
        projects.

        Hybrid-path params (ignored when qvec is None):
          min_cosine: when not None, drop vec_hits whose cosine < min_cosine BEFORE
                      RRF fusion. FTS-only hits (no cosine) are unaffected.
          dedup: when True (default), drop notes whose stored-vector cosine to any
                 already-kept note exceeds 0.95 (greedy walk in fused-score order).
                 FTS-only (NULL-embedding) notes are never dropped by dedup.
                 Hydrates fetch_k candidates before dedup so the output can backfill
                 up to k distinct notes even after duplicates are discarded.

        Current-truth resolution (default):
          include_superseded=False (default) returns CURRENT truth. Superseded
          notes still take part in matching — the old wording is often what the
          query rhymes with — but a superseded hit is then RESOLVED: the chain of
          ``superseded_by`` is followed to the note that replaced it, that note is
          returned in its place (``retrieval_origin='resolved'``, with the retired
          note in ``history``), and scoring is re-blended on the REPLACEMENT's
          confidence and recency. A chain ending in a tombstone yields nothing —
          there is no current truth to report.

          include_superseded=True disables resolution entirely: no
          resolution, no filtering beyond tombstones, superseded notes ranked on
          their own merits. Use it for history/audit views (the Memory Map GUI does).

        Note: a resolved replacement is not in the candidate vector map, so the
        near-duplicate dedup pass never discards it.
        """
        mem = await self._conn_get()

        # Shared SELECT projection (no embedding/deleted_at — those are internal).
        _SELECT = (
            "SELECT id, project_id, scope, kind, content, tags, confidence, "
            "source_hint, superseded_by, created_at, status FROM memory_notes"
        )
        # What counts as a returnable note. Candidate SQL below stays deliberately
        # UNFILTERED (superseded notes must still match); this gates hydration.
        _live = "status != 'tombstoned'" if include_superseded else _live_note_predicate()

        # ── Hybrid semantic path (qvec is not None) ───────────────────────────
        if qvec is not None:
            fetch_k = max(k * 3, 30)

            # Vector candidates: all live (non-tombstoned) notes with non-NULL embedding.
            _vc, _vp = _notes_project_clause(project)
            _v_and = f" AND {_vc}" if _vc else ""
            vec_rows = await (await mem.execute(
                f"SELECT id, embedding FROM memory_notes "
                f"WHERE embedding IS NOT NULL AND status != 'tombstoned'{_v_and}",
                _vp,
            )).fetchall()

            # Decode each stored vector ONCE: the same vectors feed both the
            # cosine ranking and (when enabled) the dedup map below.
            vec_hits: list[tuple[int, float]] = []
            id_to_vec: dict[int, np.ndarray] = {}
            if vec_rows:
                ids = [r[0] for r in vec_rows]
                vecs = [_blob_to_vec(bytes(r[1])) for r in vec_rows]
                vec_hits = _cosine_top_k_matrix(qvec, ids, np.stack(vecs), fetch_k)
                if dedup:
                    id_to_vec = dict(zip(ids, vecs))

            # Min-cosine cutoff — drop vec_hits below threshold before fusion.
            if min_cosine is not None:
                vec_hits = [(cid, cos) for cid, cos in vec_hits if cos >= min_cosine]

            # FTS candidates: memory_fts MATCH when qtext is given and FTS5 available.
            fts_hits: list[tuple[int, float]] = []
            if qtext and self._fts5_ok:
                try:
                    _fc, _fp = _notes_project_clause(project, prefix="mn.")
                    _f_and = f" AND {_fc}" if _fc else ""
                    fts_rows = await (await mem.execute(
                        "SELECT mn.id, fts.rank "
                        "FROM memory_fts fts "
                        "JOIN memory_notes mn ON mn.id = fts.rowid "
                        f"WHERE memory_fts MATCH ?{_f_and} "
                        "AND mn.status != 'tombstoned' "
                        "ORDER BY fts.rank LIMIT ?",
                        [qtext, *_fp, fetch_k],
                    )).fetchall()
                    # FTS5 rank is negative (lower = more relevant) → invert.
                    fts_hits = [(r[0], -float(r[1])) for r in fts_rows]
                except sqlite3.OperationalError as exc:
                    log.warning("memory_fts MATCH failed in hybrid recall: %s", exc)

            # Fuse the ranked lists.
            if vec_hits and fts_hits:
                ranked: list[tuple[int, float]] = _fuse_hits(vec_hits, fts_hits)
            elif vec_hits:
                ranked = list(vec_hits)
            elif fts_hits:
                ranked = list(fts_hits)
            else:
                ranked = []

            # Rewrite superseded hits to the notes that replaced them, BEFORE
            # hydration — so the blend below scores the replacement's own
            # confidence and recency, which is the authority the caller wants.
            resolved_from: dict[int, list[int]] = {}
            if ranked and not include_superseded:
                mapping = await self._resolve_supersession(mem, [cid for cid, _ in ranked])
                if mapping:
                    ranked, resolved_from = _apply_resolution(ranked, mapping)

            if ranked:
                # Hydrate fetch_k candidates (not just k) to allow dedup backfill.
                cands = ranked[:fetch_k]
                id_to_score = {cid: score for cid, score in cands}
                placeholders = ",".join("?" * len(id_to_score))
                rows = await (await mem.execute(
                    f"{_SELECT} WHERE id IN ({placeholders}) AND {_live}",
                    list(id_to_score.keys()),
                )).fetchall()
                # Blend fused score with confidence factor and recency decay, then
                # re-sort.  Hydration already gives row[6]=confidence and
                # row[9]=created_at from _SELECT, so no second query is needed.
                _now = datetime.now()
                id_to_blended: dict[int, float] = {
                    row[0]: _blend_score(
                        id_to_score.get(row[0], 0.0), row[6], row[9], _now
                    )
                    for row in rows
                }
                rows = sorted(rows, key=lambda r: id_to_blended.get(r[0], 0.0), reverse=True)

                # Near-duplicate dedup (greedy, preserves fused-score order).
                # Notes without embeddings (FTS-only) are never dropped.
                if dedup:
                    kept_rows = []
                    kept_vecs: list[np.ndarray] = []
                    for row in rows:
                        row_vec = id_to_vec.get(row[0])
                        if row_vec is not None:
                            # Check cosine to every already-kept embedded note.
                            if any(float(np.dot(row_vec, kv)) > _DEDUP_COSINE for kv in kept_vecs):
                                continue  # near-duplicate — discard
                            kept_vecs.append(row_vec)
                        kept_rows.append(row)
                        if len(kept_rows) == k:
                            break
                    rows = kept_rows
                else:
                    rows = list(rows[:k])
            else:
                # No embeddings and no FTS hits: fall back to recency (live notes only).
                # Nothing was matched here, so there is no stale wording to rescue —
                # filtering in SQL is correct AND keeps superseded rows from eating
                # the LIMIT.
                _rc, _rp = _notes_project_clause(project)
                _r_where = f"WHERE {_rc} AND {_live}" if _rc else f"WHERE {_live}"
                rows = await (await mem.execute(
                    f"{_SELECT} {_r_where} ORDER BY created_at DESC LIMIT ?",
                    [*_rp, k],
                )).fetchall()

            notes = await self._rows_to_notes(mem, rows, resolved_from)
            if rerank:
                notes = await self._maybe_rerank_notes(qtext, notes)
            return await self._maybe_expand_links(mem, notes, project)

        # ── Keyword / recency path (qvec is None) ─────────────────────────────
        rows = None
        kw_resolved: dict[int, list[int]] = {}
        if qtext and self._fts5_ok:
            # Keyword path: FTS5 MATCH on memory_fts, joined back to memory_notes
            # via rowid. Recency is the tie-breaker within matched results.
            try:
                _kc, _kp = _notes_project_clause(project)
                _k_and = f" AND {_kc}" if _kc else ""
                _match = (
                    f"WHERE id IN (SELECT rowid FROM memory_fts WHERE memory_fts MATCH ?)"
                    f"{_k_and} AND status != 'tombstoned'"
                )
                if include_superseded:
                    rows = await (await mem.execute(
                        f"{_SELECT} {_match} ORDER BY created_at DESC LIMIT ?",
                        [qtext, *_kp, k],
                    )).fetchall()
                else:
                    # Match on the stale wording too, then resolve to current truth.
                    # Extra headroom: several retired notes can collapse onto one
                    # replacement, and chains ending in a tombstone drop out.
                    id_rows = await (await mem.execute(
                        f"SELECT id FROM memory_notes {_match} "
                        f"ORDER BY created_at DESC LIMIT ?",
                        [qtext, *_kp, max(k * 3, 30)],
                    )).fetchall()
                    cand_ids = [r[0] for r in id_rows]
                    rows = []
                    if cand_ids:
                        mapping = await self._resolve_supersession(mem, cand_ids)
                        ranked_ids, kw_resolved = _apply_resolution(
                            [(cid, 0.0) for cid in cand_ids], mapping
                        )
                        ids = [cid for cid, _ in ranked_ids]
                        ph = ",".join("?" * len(ids))
                        rows = await (await mem.execute(
                            f"{_SELECT} WHERE id IN ({ph}) AND {_live} "
                            f"ORDER BY created_at DESC LIMIT ?",
                            [*ids, k],
                        )).fetchall()
            except sqlite3.OperationalError as exc:
                log.warning("memory_fts MATCH failed, falling back to recency: %s", exc)
                rows = None  # fall through to recency path below

        if rows is None:
            # Recency path: no qtext, FTS5 unavailable, or FTS5 query failed (live only).
            _rp2_c, _rp2_p = _notes_project_clause(project)
            _rp2_where = f"WHERE {_rp2_c} AND {_live}" if _rp2_c else f"WHERE {_live}"
            rows = await (await mem.execute(
                f"{_SELECT} {_rp2_where} ORDER BY created_at DESC LIMIT ?",
                [*_rp2_p, k],
            )).fetchall()

        notes = await self._rows_to_notes(mem, rows, kw_resolved)
        if rerank:
            notes = await self._maybe_rerank_notes(qtext, notes)
        return await self._maybe_expand_links(mem, notes, project)

    # ── reembed_notes (backfill) ──────────────────────────────────────────────

    async def reembed_notes(
        self,
        project: str | None,
        embed_fn,
        batch_size: int = 32,
    ) -> int:
        """Backfill embeddings for memory_notes that have NULL embedding.

        Selects all notes with NULL embedding (optionally scoped to project),
        calls embed_fn in batches to compute vectors, and UPDATEs the rows.

        Args:
            project:    Project ULID to scope (or None for all projects).
            embed_fn:   Sync callable ``(texts: list[str]) -> list[np.ndarray]``
                        — e.g. ``braincell.embed.embed_texts``.
            batch_size: How many notes to embed per call (default 32).

        Returns:
            Count of notes whose embedding was populated.
        """
        mem = await self._conn_get()
        if project:
            null_rows = await (await mem.execute(
                "SELECT id, content FROM memory_notes "
                "WHERE embedding IS NULL AND project_id = ?",
                (project,),
            )).fetchall()
        else:
            null_rows = await (await mem.execute(
                "SELECT id, content FROM memory_notes WHERE embedding IS NULL",
            )).fetchall()

        if not null_rows:
            return 0

        count = 0
        for i in range(0, len(null_rows), batch_size):
            batch = null_rows[i : i + batch_size]
            texts = [r[1] for r in batch]
            embeddings = embed_fn(texts)
            if len(embeddings) != len(batch):
                raise ValueError(
                    "BrainCell reembed_notes: provider returned "
                    f"{len(embeddings)} embeddings for {len(batch)} inputs."
                )
            async with self._write_transaction() as writer:
                for (note_id, _), vec in zip(batch, embeddings, strict=True):
                    if vec.shape[0] != embed_spec.DIM:
                        raise ValueError(
                            f"BrainCell reembed_notes: embedding is {vec.shape[0]}-d but "
                            f"embed_spec.DIM={embed_spec.DIM} ({embed_spec.FINGERPRINT}). "
                            f"Aborting backfill — provider/model mismatch."
                        )
                    await writer.execute(
                        "UPDATE memory_notes SET embedding = ? "
                        "WHERE id = ? AND embedding IS NULL",
                        (_vec_to_blob(vec), note_id),
                    )
                    count += 1
        return count

    # ── Cluster detection + deterministic merge ───────────────────────────────

    async def find_conflicts(
        self,
        project: str,
        embedding: np.ndarray | None,
        k: int | None = None,
        threshold: float | None = None,
    ) -> list[ConflictCandidate]:
        """ACTIVE same-project notes whose cosine to *embedding* ≥ threshold.

        The warn-only contradiction guard behind `remember`: run BEFORE the new
        note persists (so it never matches itself), surfaced so the caller can
        choose `supersede` over silently accumulating a contradiction. Only
        ``status='active'`` notes are candidates — a conflict with retired truth
        is not a conflict. Returns up to *k* candidates, highest cosine first;
        empty when the embedding is None (FTS-only note) or k ≤ 0 (disabled via
        BRAINCELL_CONFLICT_K=0). Read-only — never blocks or mutates anything.
        """
        k = _CONFLICT_K if k is None else k
        threshold = _CONFLICT_COS if threshold is None else threshold
        if embedding is None or k <= 0:
            return []
        mem = await self._conn_get()
        rows = await (await mem.execute(
            "SELECT id, kind, content, embedding FROM memory_notes "
            "WHERE embedding IS NOT NULL AND status = 'active' AND project_id = ?",
            (project,),
        )).fetchall()
        if not rows:
            return []
        mat = np.stack([_blob_to_vec(bytes(r[3])) for r in rows])
        sims = mat @ embedding.astype(np.float32)
        hits = [
            ConflictCandidate(id=r[0], kind=r[1], content=r[2], cosine=float(s))
            for r, s in zip(rows, sims) if float(s) >= threshold
        ]
        hits.sort(key=lambda c: c.cosine, reverse=True)
        return hits[:k]

    async def find_note_clusters(
        self,
        project: str | None,
        threshold: float = 0.9,
    ) -> list[list[int]]:
        """Find clusters of near-duplicate memory notes by embedding similarity.

        Selects all live (non-tombstoned) notes with non-NULL embeddings for
        project (or all projects when project is None). Applies a greedy seed
        algorithm (O(n²), fine for note-count scale):

        - Notes are sorted newest-first (ORDER BY created_at DESC) so cluster[0]
          is the most-recent note — the deterministic representative.
        - Each unassigned note seeds a new cluster and collects every later
          unassigned note whose cosine to the seed ≥ threshold.
        - Stored vectors are L2-normalised; cosine = dot product (same convention
          as _cosine_top_k — no extra step required).
        - Clusters of size 1 (singletons with no near-duplicate) are excluded.

        All SQL is parameterised; tombstones are excluded via deleted_at IS NULL.

        Args:
            project:   Project ULID to scope results, or None for all projects.
            threshold: Cosine similarity required to join a cluster (default 0.9).

        Returns:
            A list of clusters. Each cluster is a list of note ids ordered
            newest-first; cluster[0] is the representative (the note to keep
            during consolidation). Only clusters of size ≥ 2 are returned.
        """
        mem = await self._conn_get()
        if project:
            rows = await (await mem.execute(
                "SELECT id, embedding FROM memory_notes "
                "WHERE embedding IS NOT NULL AND status != 'tombstoned' AND project_id = ? "
                "ORDER BY created_at DESC",
                (project,),
            )).fetchall()
        else:
            rows = await (await mem.execute(
                "SELECT id, embedding FROM memory_notes "
                "WHERE embedding IS NOT NULL AND status != 'tombstoned' "
                "ORDER BY created_at DESC",
            )).fetchall()

        if not rows:
            return []

        ids = [r[0] for r in rows]
        vecs = [_blob_to_vec(bytes(r[1])) for r in rows]

        assigned = [False] * len(ids)
        clusters: list[list[int]] = []

        for i in range(len(ids)):
            if assigned[i]:
                continue
            # Seed a new cluster with the current (newest unassigned) note.
            cluster: list[int] = [ids[i]]
            seed_vec = vecs[i]
            assigned[i] = True
            for j in range(i + 1, len(ids)):
                if assigned[j]:
                    continue
                cos = float(np.dot(seed_vec, vecs[j]))
                if cos >= threshold:
                    cluster.append(ids[j])
                    assigned[j] = True
            if len(cluster) >= 2:
                clusters.append(cluster)

        return clusters

    async def consolidate_cluster(
        self,
        cluster_ids: list[int],
        project: str,
        representative_id: int,
    ) -> None:
        """Deterministic cluster merge: keep representative, soft-forget the rest.

        Tombstones every member of cluster_ids except representative_id via the
        existing soft-delete (forget, hard=False) path. Ownership is enforced
        through forget() — notes owned by a different project are silently skipped
        (forget returns False). The representative note is left untouched (live).

        Args:
            cluster_ids:       All note ids in the cluster (includes representative).
            project:           Project ULID that owns the notes.
            representative_id: The note id to keep live. Must be in cluster_ids.
        """
        for note_id in cluster_ids:
            if note_id == representative_id:
                continue
            await self.forget(note_id, project, hard=False)

    # ── atomic document replacement ───────────────────────────────────────────

    async def document_is_current(
        self,
        project_id: str,
        doc_key: str,
        content_hash: bytes,
        *,
        expected_chunks: int,
    ) -> bool:
        """Return whether a document has exactly the completed requested state.

        A matching document hash alone is not a successful ingest checkpoint:
        every expected chunk must exist and have a non-null embedding, and no
        trailing chunk from an older, longer version may remain.
        """
        cf = await self._conn_get()
        row = await (
            await cf.execute(
                "SELECT d.content_hash, COUNT(c.id), "
                "COALESCE(SUM(c.embedding IS NOT NULL), 0) "
                "FROM bc_documents d "
                "LEFT JOIN bc_chunks c ON c.document_id = d.id "
                "WHERE d.project_id=? AND d.doc_key=? "
                "GROUP BY d.id",
                (project_id, doc_key),
            )
        ).fetchone()
        if row is None:
            return False
        stored_hash = bytes(row[0]) if row[0] is not None else None
        return (
            stored_hash == content_hash
            and int(row[1]) == expected_chunks
            and int(row[2]) == expected_chunks
        )

    async def document_metadata(
        self, project_id: str, doc_key: str,
    ) -> Optional[dict]:
        """Return one document's stored content hash and parsed metadata.

        ``{"content_hash": bytes | None, "metadata": dict}``; None when the
        document does not exist. Malformed stored metadata comes back as ``{}``
        so callers can apply their own policy without crashing on legacy rows.
        """
        cf = await self._conn_get()
        row = await (
            await cf.execute(
                "SELECT content_hash, metadata FROM bc_documents "
                "WHERE project_id=? AND doc_key=?",
                (project_id, doc_key),
            )
        ).fetchone()
        if row is None:
            return None
        try:
            metadata = json.loads(row[1]) if row[1] else {}
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return {
            "content_hash": bytes(row[0]) if row[0] is not None else None,
            "metadata": metadata,
        }

    async def update_document_metadata(
        self, project_id: str, doc_key: str, metadata: dict,
    ) -> bool:
        """Rewrite one document's metadata JSON without touching content state.

        Returns True iff the document existed. Chunks, FTS rows, and the
        content hash are untouched, so ingest checkpoints stay valid.
        """
        meta_json = json.dumps(metadata)
        async with self._write_transaction() as cf:
            cursor = await cf.execute(
                "UPDATE bc_documents SET metadata=?, updated_at=? "
                "WHERE project_id=? AND doc_key=?",
                (meta_json, datetime.now().isoformat(), project_id, doc_key),
            )
            return cursor.rowcount > 0

    async def replace_document(
        self,
        *,
        project_id: str,
        doc_key: str,
        title: str,
        content_hash: bytes,
        content_type: str,
        chunks: Sequence[tuple[str, np.ndarray]],
        commit_sha: Optional[str] = None,
        run_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> tuple[int, bool]:
        """Atomically replace one document and its exact chunk/FTS set.

        All embeddings are validated and serialized before the write lock is
        acquired. A validation error or any database failure therefore leaves
        the prior hash, chunks, and FTS entries intact.
        """
        prepared: list[tuple[int, str, bytes, bytes]] = []
        for chunk_index, (chunk_text, embedding) in enumerate(chunks):
            vector = np.asarray(embedding, dtype=np.float32)
            if vector.ndim != 1 or vector.shape[0] != embed_spec.DIM:
                actual = (
                    f"{vector.shape[0]}-d"
                    if vector.ndim == 1
                    else f"shape {vector.shape}"
                )
                raise ValueError(
                    f"BrainCell write refused: chunk embedding is {actual} but "
                    f"embed_spec.DIM={embed_spec.DIM} ({embed_spec.FINGERPRINT}). "
                    "Refusing to mix vector spaces in one store."
                )
            if not np.all(np.isfinite(vector)):
                raise ValueError(
                    "BrainCell write refused: chunk embedding contains non-finite values."
                )
            norm = float(np.linalg.norm(vector))
            if not np.isfinite(norm) or norm == 0.0:
                raise ValueError(
                    "BrainCell write refused: chunk embedding has zero or non-finite norm."
                )
            prepared.append(
                (
                    chunk_index,
                    chunk_text,
                    hashlib.sha256(chunk_text.encode()).digest(),
                    _vec_to_blob(vector),
                )
            )

        expected_chunks = len(prepared)
        if await self.document_is_current(
            project_id,
            doc_key,
            content_hash,
            expected_chunks=expected_chunks,
        ):
            cf = await self._conn_get()
            row = await (
                await cf.execute(
                    "SELECT id FROM bc_documents WHERE project_id=? AND doc_key=?",
                    (project_id, doc_key),
                )
            ).fetchone()
            if row is not None:
                return int(row[0]), False

        now = datetime.now().isoformat()
        meta_json = json.dumps(metadata or {})
        async with self._write_transaction() as cf:
            row = await (
                await cf.execute(
                    "SELECT id FROM bc_documents WHERE project_id=? AND doc_key=?",
                    (project_id, doc_key),
                )
            ).fetchone()
            if row is None:
                cursor = await cf.execute(
                    "INSERT INTO bc_documents "
                    "(project_id, doc_key, title, content_hash, content_type, "
                    "commit_sha, run_id, created_at, updated_at, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        project_id,
                        doc_key,
                        title,
                        content_hash,
                        content_type,
                        commit_sha,
                        run_id,
                        now,
                        now,
                        meta_json,
                    ),
                )
                document_id = int(cursor.lastrowid)
            else:
                document_id = int(row[0])
                old_chunks = await (
                    await cf.execute(
                        "SELECT id FROM bc_chunks WHERE document_id=?",
                        (document_id,),
                    )
                ).fetchall()
                if self._fts5_ok:
                    for old_chunk in old_chunks:
                        await cf.execute(
                            "DELETE FROM bc_chunks_fts WHERE rowid=?",
                            (int(old_chunk[0]),),
                        )
                await cf.execute(
                    "DELETE FROM bc_chunks WHERE document_id=?",
                    (document_id,),
                )
                await cf.execute(
                    "UPDATE bc_documents SET title=?, content_hash=?, "
                    "content_type=?, commit_sha=?, run_id=?, updated_at=?, metadata=? "
                    "WHERE id=?",
                    (
                        title,
                        content_hash,
                        content_type,
                        commit_sha,
                        run_id,
                        now,
                        meta_json,
                        document_id,
                    ),
                )

            for chunk_index, chunk_text, chunk_hash, emb_blob in prepared:
                cursor = await cf.execute(
                    "INSERT INTO bc_chunks "
                    "(document_id, chunk_index, chunk_text, chunk_hash, embedding, run_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        document_id,
                        chunk_index,
                        chunk_text,
                        chunk_hash,
                        emb_blob,
                        run_id,
                    ),
                )
                if self._fts5_ok:
                    await cf.execute(
                        "INSERT INTO bc_chunks_fts(rowid, chunk_text) VALUES (?, ?)",
                        (int(cursor.lastrowid), chunk_text),
                    )

        return document_id, True

    # ── list_documents ──────────────────────────────────────────────────────────

    _LIST_DOCUMENTS_MAX_LIMIT: int = 200

    async def list_documents(
        self,
        project: str | None = None,
        filter: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """List ingested documents with name/title/content_type summary.

        limit is silently capped at 200 (hard max) to prevent unbounded reads.
        filter is an optional case-insensitive substring match on doc_key or title.
        All SQL parameterized — no raw user input interpolated into the query string.
        """
        limit = min(max(1, limit), self._LIST_DOCUMENTS_MAX_LIMIT)
        cf = await self._conn_get()

        where_parts: list[str] = []
        params: list = []

        if project:
            where_parts.append("project_id = ?")
            params.append(project)
        if filter:
            pattern = f"%{filter}%"
            where_parts.append("(doc_key LIKE ? OR title LIKE ?)")
            params.extend([pattern, pattern])

        where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        params.append(limit)

        sql = (
            f"SELECT doc_key, title, content_type, created_at, updated_at "
            f"FROM bc_documents {where} ORDER BY doc_key LIMIT ?"
        )
        rows = await (await cf.execute(sql, params)).fetchall()
        return [
            {
                "doc_key": r[0],
                "title": r[1],
                "content_type": r[2],
                "created_at": r[3],
                "updated_at": r[4],
            }
            for r in rows
        ]

    # ── close / aclose ────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        """Await-close all open aiosqlite connections.

        Preferred in async context (e.g. server lifespan finally block).
        Safe to call multiple times; subsequent calls are no-ops.
        """
        async with self._write_lock:
            for attribute in ("_write_conn", "_conn"):
                connection = getattr(self, attribute)
                if connection is None:
                    continue
                try:
                    await connection.close()
                except Exception as exc:  # noqa: BLE001 — close is best-effort teardown; the handle is dropped regardless
                    log.warning("Error closing braincell.db connection: %s", exc)
                setattr(self, attribute, None)

    def close(self) -> None:
        """Close all open DB connections from a sync context.

        Safe to call after asyncio.run() returns (the event loop is gone).
        If called while an event loop IS running (which means the caller should
        have used `await store.aclose()` instead), logs a warning and skips —
        this avoids blocking the running loop, but connections will not be closed
        until the process exits.
        """
        import asyncio
        try:
            asyncio.get_running_loop()
            # There is a running loop — we cannot block it with asyncio.run().
            log.warning(
                "SqliteStore.close() called while an event loop is running; "
                "use `await store.aclose()` from async context instead. "
                "DB connections will not be closed until process exit."
            )
        except RuntimeError:
            # No running loop — safe to drive aclose() to completion.
            asyncio.run(self.aclose())


# ── open_store() factory ──────────────────────────────────────────────────────

def open_store(
    *,
    project_id: str | None = None,
    db_path: Path | None = None,
) -> SqliteStore:
    """Open the BrainCell SqliteStore.

    Resolution order:
    1. Explicit db_path (testing / pipeline use).
    2. project_id → XDG path via config.get_db_path.
    3. Project-only env fallback: BRAINCELL_STORE=sqlite +
       BRAINCELL_PROJECT_ID.

    Fails closed (sys.exit(1)) if:
    - BRAINCELL_STORE is unset AND no explicit db_path AND no project_id (project mode).
    - BRAINCELL_STORE is not 'sqlite'.
    """
    if db_path is not None:
        return SqliteStore(db_path)

    if project_id is not None:
        from .config import get_db_path
        return SqliteStore(get_db_path(project_id))

    # Project-only env fallback: reject retired global mode before opening anything.
    from .mode import resolve_mode
    resolve_mode()
    store_type = os.environ.get("BRAINCELL_STORE", "").strip().lower()
    if not store_type:
        print(
            "ERROR: BRAINCELL_STORE env var is not set and no project_id was given.\n"
            "  Set BRAINCELL_STORE=sqlite and provide a project_id, or use the\n"
            "  braincell CLI which passes paths directly.\n"
            "  Example: BRAINCELL_STORE=sqlite braincell build .",
            file=sys.stderr,
        )
        sys.exit(1)

    if store_type != "sqlite":
        print(
            f"ERROR: BRAINCELL_STORE='{store_type}' is not supported in V0.\n"
            f"  V0 only supports 'sqlite'. Postgres support is planned for V2+.",
            file=sys.stderr,
        )
        sys.exit(1)

    # BRAINCELL_STORE=sqlite requires PROJECT_ID env.
    pid = os.environ.get("BRAINCELL_PROJECT_ID", "").strip()
    if not pid:
        print(
            "ERROR: BRAINCELL_STORE=sqlite requires BRAINCELL_PROJECT_ID env var.\n"
            "  Run `braincell build <path>` to ingest a project first,\n"
            "  then export BRAINCELL_PROJECT_ID=<ulid-from-path-registry>.",
            file=sys.stderr,
        )
        sys.exit(1)

    from .config import get_db_path
    cf_path = get_db_path(pid)
    if not cf_path.exists():
        print(
            f"ERROR: no brain found for BRAINCELL_PROJECT_ID={pid!r}.\n"
            f"  Expected {cf_path} — it does not exist.\n"
            f"  This ULID was never built (typo?), or BRAINCELL_DATA_NAMESPACE /\n"
            f"  XDG_DATA_HOME point at the wrong store. Refusing to fabricate an\n"
            f"  empty brain. Run `braincell build <path>` first, or fix the env.",
            file=sys.stderr,
        )
        sys.exit(1)
    return SqliteStore(cf_path)
