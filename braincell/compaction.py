# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
compaction.py — Pure transcript-page compaction for BrainCell.

Drops machine-noise pages and folds near-exact duplicates BEFORE embedding.
Pure: no I/O, no DB, no network, no LLM. No cross-call state (per-file scope).
Single pass, order-preserving. KEEP wins over DROP (when in doubt, KEEP).
All patterns are module-level compiled constants — tunable + testable.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

# ── KEEP patterns (override any DROP match) ────────────────────────────────────

_KEEP_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"Traceback \(most recent call last\)", re.IGNORECASE),
    re.compile(r"\bError:", re.IGNORECASE),
    re.compile(r"\bException\b", re.IGNORECASE),
    re.compile(r"\b\w+\.py:\d+"),        # file:lineno reference
    re.compile(r"diff --git "),
    re.compile(r"^@@ ", re.MULTILINE),
    re.compile(r"^\+\+\+ ", re.MULTILINE),
    re.compile(r"^--- ", re.MULTILINE),
]

# ── DROP patterns (fired only when no KEEP pattern matched) ────────────────────

_ACK_PATTERN = re.compile(
    r"^(ok|okay|yes|yep|no|nope|thanks|thank you|done|sure|got it|k|ack|👍)[.!]?$",
    re.IGNORECASE,
)
_TASK_NOTIFICATION_START = "<task-notification>"
_TASK_NOTIFICATION_BOTH = (re.compile(r"<task-id>"), re.compile(r"<tool-use-id>"))
_HARNESS_ECHO_PREFIXES: tuple[str, ...] = (
    "<system-reminder>",
    # NOTE: <command-message>/<command-name> are deliberately NOT dropped — those
    # pages carry the user's slash-command ARGS (their actual request, e.g.
    # "/investigate <task>"), which are real intent, not noise. Verified 2026-05-29:
    # 20/59 command pages held substantial <command-args> (median 378 chars). Only
    # truly content-free harness echoes drop here.
    "<local-command-stdout>",
)
_PROGRESS_SPAM = re.compile(r"^Resuming \d+ of \d+", re.IGNORECASE)
_PERCENT_LINE = re.compile(r"^\s*\d{1,3}%[\s\[|]")

# ── Volatile-token patterns for normalisation ─────────────────────────────────

_RE_TS = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:\d{2})?",
)
_RE_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_RE_TOOLU = re.compile(r"\btoolu_[A-Za-z0-9]+\b")
_RE_HEX_ID = re.compile(r"\b[0-9a-f]{16,}\b", re.IGNORECASE)
_RE_TMP_PATH = re.compile(r"/tmp/claude-[^\s]+")
_RE_TASK_OUTPUT = re.compile(r"/tasks/[^\s]+output[^\s]*", re.IGNORECASE)
_VOLATILE_PATTERNS: list[re.Pattern[str]] = [
    _RE_TS, _RE_UUID, _RE_TOOLU, _RE_HEX_ID, _RE_TMP_PATH, _RE_TASK_OUTPUT,
]
_WS_RUN = re.compile(r"\s+")


def _normalize(page: str) -> str:
    """Strip volatile tokens (timestamps, UUIDs, toolu_*, hex ids, /tmp paths)
    and collapse whitespace. Load-bearing tokens are preserved verbatim."""
    result = page
    for pat in _VOLATILE_PATTERNS:
        result = pat.sub("__volatile__", result)
    result = _WS_RUN.sub(" ", result)
    return unicodedata.normalize("NFC", result).strip()


def _content_hash(text: str) -> str:
    # Non-cryptographic use: a content fingerprint for in-call dedup only. sha256
    # (not sha1) keeps the security gate clean; the cost is negligible for ≤2KB pages.
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def is_noise(page: str) -> bool:
    """Return True if *page* is machine noise. KEEP wins over DROP."""
    for kp in _KEEP_PATTERNS:
        if kp.search(page):
            return False

    stripped = page.strip()
    if not stripped:
        return True
    if len(stripped) <= 12 and _ACK_PATTERN.match(stripped):
        return True
    if stripped.startswith(_TASK_NOTIFICATION_START):
        return True
    if _TASK_NOTIFICATION_BOTH[0].search(page) and _TASK_NOTIFICATION_BOTH[1].search(page):
        return True
    for prefix in _HARNESS_ECHO_PREFIXES:
        if stripped.startswith(prefix):
            return True
    if _PROGRESS_SPAM.match(stripped):
        return True
    if _PERCENT_LINE.match(stripped):
        return True
    return False


def compact_pages(pages: list[str]) -> tuple[list[str], dict]:
    """Drop noise and fold near-exact duplicates. Order-preserving, single pass.

    Args:
        pages: Pages from one transcript file (one session).

    Returns:
        (kept_pages, {"dropped_noise": int, "deduped": int}).
        Pure: no cross-call state — session-scoping is free (one call = one file).
    """
    kept: list[str] = []
    seen_exact: set[str] = set()
    seen_norm: set[str] = set()
    dropped_noise = 0
    deduped = 0

    for p in pages:
        if is_noise(p):
            dropped_noise += 1
            continue
        sha_e = _content_hash(p)
        if sha_e in seen_exact:
            deduped += 1
            continue
        norm = _normalize(p)
        sha_n = _content_hash(norm)
        if sha_n in seen_norm:
            deduped += 1
            continue
        seen_exact.add(sha_e)
        seen_norm.add(sha_n)
        kept.append(p)

    return kept, {"dropped_noise": dropped_noise, "deduped": deduped}
