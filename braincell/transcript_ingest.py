# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
transcript_ingest.py — Tolerant JSONL transcript walker for BrainCell.

Implements the `braincell index` backfill:
  - Walk `~/.claude/projects/**` and `~/.codex/sessions/**` for JSONL files.
  - mtime→SHA gate (mirrors the cluster_hash discipline in pipeline.py).
  - Secret-scan every page before write (reject on hit).
  - Resume via the local mtime→sha ledger helpers (`_load_ledger`/`_save_ledger`).
  - Hard guards: no destructive filesystem ops; writes ONLY to
    this project's braincell.db (never the target repo).

Also implements foreign-document reconciliation (`preview_foreign_documents` /
`apply_foreign_document_migration`): a preview-only, explicit-opt-in workflow
for `bc_documents` rows already sitting in a project's database under a
DIFFERENT project's identity — historical rows predating the out-of-scope skip
above, or left behind by a path later reassociated to a different Project. See
that section near the bottom of this file.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .catalog_io import MutationBusyError, mutation_lock
from .compaction import compact_pages
from .config import get_db_path
from .embed import embed_texts
from .log import get as _get_log
from .project_registry import (
    load_path_registry,
    resolve_claude_dir_to_ulid,
    resolve_family_ulids,
    resolve_path_to_ulid,
)
from .schema import MEMORY_SCHEMA_VERSION
from .skill_tag import is_skill_body, skill_name_from_body
from .store import SqliteStore, _secret_scan


def _compaction_enabled() -> bool:
    """Return True when transcript compaction is active (default: ON).

    Read from env at call time so tests can monkeypatch os.environ.
    BRAINCELL_COMPACT unset / "1" / "true"  → ON  (cleanup-by-default).
    BRAINCELL_COMPACT "0" / "false"          → OFF (raw/inspection mode opt-in).
    """
    val = os.environ.get("BRAINCELL_COMPACT", "").strip().lower()
    return val not in ("0", "false")

log = _get_log("braincell.transcript_ingest")

# Paths to scan for JSONL conversation transcripts. Each file is attributed to its
# SOURCE project (claude: parent dirname encode-match; codex: session_meta.payload.cwd)
# and ingested only if that project is in the build's family. Codex
# payload-nesting extraction + cwd attribution keep ~/.codex in scope;
# un-attributable files are skipped (files_unattributed), never mis-tagged.
_TRANSCRIPT_ROOTS: list[Path] = [
    Path.home() / ".claude" / "projects",
    Path.home() / ".codex" / "sessions",
]

# Ledger file: tracks (path → content_hash) to gate re-ingestion.
# Lives in the BrainCell state dir alongside braincell.db.
_LEDGER_FILENAME = "transcript_ingest_ledger.json"


def _load_ledger(ledger_path: Path) -> dict[str, str]:
    """Load the mtime→sha ledger from disk. Returns {} on first run."""
    if not ledger_path.exists():
        return {}
    try:
        data = json.loads(ledger_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in data.items()
        ):
            return data
        log.warning("Transcript ledger has an invalid format: %s", ledger_path)
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Transcript ledger is unreadable (%s): %s", ledger_path, exc)
        return {}


def _save_ledger(ledger_path: Path, ledger: dict[str, str]) -> None:
    """Persist the ledger atomically."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = ledger_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    os.replace(tmp, ledger_path)


def _file_sha(path: Path) -> str:
    """SHA-256 of a file's contents (for the mtime→SHA gate).

    Streamed in 1 MiB chunks to avoid loading large session files into RAM.
    Produces the same digest as sha256(path.read_bytes()).hexdigest().
    """
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _coerce_content(content: object) -> str:
    """Coerce an Anthropic message content field to a plain string.

    Content is either a plain ``str`` or a list of typed blocks
    (e.g. ``[{"type": "text", "text": "..."}, {"type": "tool_use", ...}]``).
    Non-text blocks (tool_use, tool_result, thinking, image …) are skipped.
    Returns ``""`` for any other type so callers never see a non-str.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _text_from_record(obj: dict) -> str | None:
    """Pull a text string from one JSON record's common fields (Claude/raw).
    Returns the stripped text, or None if there's nothing usable.

    Change #1 (2026-05-29): Real Claude Code turns use the schema
      {"type": "user|assistant", "message": {"role": ..., "content": str | list}}
    The old code grabbed the whole `message` dict as `text`, which then failed
    both the list and str isinstance checks and returned None.  Now we unwrap
    message.content FIRST (before the top-level content/text fallbacks).
    Empty thinking/tool_use/tool_result blocks yield "" via the existing joiner
    (correct — Opus 4.8 defaults thinking.display=omitted, so thinking is empty).
    """
    # ── Change #1: unwrap real Claude Code turn schema ──────────────────────
    message = obj.get("message")
    if isinstance(message, dict):
        text = message.get("content")
    else:
        # Top-level content/text/message fallbacks for codex/raw schemas.
        text = (
            obj.get("content")
            or obj.get("text")
            or message  # non-dict message field (e.g. plain string)
            or (obj.get("messages") and " ".join(
                # Each message's content may be str OR an Anthropic block-list;
                # _coerce_content handles both so str.join never sees a list.
                _coerce_content(m.get("content", ""))
                for m in obj["messages"] if isinstance(m, dict)
            ))
        )
    if isinstance(text, list):
        # Anthropic-style content array: extract text blocks; thinking/tool_use
        # blocks contribute "" (correct — thinking is empty on Opus 4.8).
        text = _coerce_content(text)
    if isinstance(text, str) and text.strip():
        return text.strip()
    return None


def _page_from_line(raw_line: bytes) -> str | None:
    """Extract one bounded text page from a raw JSONL line."""
    line = raw_line.decode("utf-8", errors="replace").strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return line[:2000] if len(line) > 20 else None
    if not isinstance(obj, dict):
        return None
    text = _text_from_record(obj)
    if text is None and isinstance(obj.get("payload"), dict):
        text = _text_from_record(obj["payload"])
    return text[:2000] if text else None


def _extract_text_and_sha(path: Path) -> tuple[list[str], str]:
    """Extract pages and hash the exact same single-pass source snapshot."""
    pages: list[str] = []
    digest = hashlib.sha256()
    try:
        fh = path.open("rb")
    except OSError:
        return [], ""
    with fh:
        for raw_line in fh:
            digest.update(raw_line)
            if page := _page_from_line(raw_line):
                pages.append(page)
    return pages, digest.hexdigest()


def _extract_text_from_jsonl(path: Path) -> list[str]:
    """Tolerantly extract text pages from a JSONL transcript file.

    Each line may be a JSON object with various schemas (Claude Code,
    Codex, raw). We extract the 'content' or 'text' or 'message' field
    if present, skipping malformed lines silently.
    Returns a list of non-empty text strings (one per usable line).
    """
    return _extract_text_and_sha(path)[0]


# ── Source-project attribution ─────────────────────────────────────────────────

def _is_codex_path(fpath: Path) -> bool:
    """True if the transcript lives under ~/.codex (date-filed → attributed by cwd)."""
    return ".codex" in fpath.parts


def _codex_session_cwd(path: Path) -> str | None:
    """The working dir a codex session ran in, from session_meta.payload.cwd (the
    first record; also early turn_context records). Reads only the first few records
    — no full-file scan. None if absent/malformed."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for _ in range(5):
                line = fh.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = obj.get("payload") if isinstance(obj, dict) else None
                if isinstance(payload, dict):
                    cwd = payload.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        return cwd
    except OSError:
        return None
    return None


def _resolve_source_ulid(fpath: Path, registry: dict[str, str]) -> str | None:
    """Resolve the SOURCE project ULID for a transcript file.

    - codex (~/.codex): session_meta.payload.cwd → path-registry.
    - claude (~/.claude/projects): walk UP to the nearest ancestor dir whose name
      encode-matches a registered path. Claude Code nests session files in per-
      session subdirs (and a `memory/` dir) under the encoded-cwd dir, so the
      encoded-cwd is NOT always the immediate parent — matching only `parent.name`
      silently drops the nested sessions. Encode-direction match (never decode).

    None when no ancestor matches a registered project (lazy-link → caller skips
    the file; we NEVER fall back to the build project — no mis-attribution).
    """
    if _is_codex_path(fpath):
        cwd = _codex_session_cwd(fpath)
        return resolve_path_to_ulid(cwd, registry) if cwd else None
    for ancestor in fpath.parents:
        ulid = resolve_claude_dir_to_ulid(ancestor.name, registry)
        if ulid is not None:
            return ulid
    return None


async def ingest_transcripts(
    store: SqliteStore,
    project_id: str,
    *,
    incremental: bool = True,
    ledger_path: Path | None = None,
    progress_cb: callable | None = None,
) -> dict:
    """Walk transcript roots, embed pages, upsert into braincell tables.

    Args:
        store:        Open SqliteStore for the project.
        project_id:   BUILD project ULID — its db owns the rows and its family
                      scopes the ingest. Each doc is tagged with its TRUE source
                      project (NOT this), so search can scope self vs family.
        incremental:  When True, skip files whose SHA matches the ledger.
        ledger_path:  Explicit ledger path (defaults to braincell.db sibling).
        progress_cb:  Optional callable(str) for progress messages.

    Returns:
        Stats dict: files_scanned / _ingested / _skipped / _failed / _unattributed /
        _out_of_scope / chunks_written / secrets_rejected.
    """
    if ledger_path is None:
        ledger_path = store._db_path.parent / _LEDGER_FILENAME

    ledger = _load_ledger(ledger_path) if incremental else {}

    # Source-attribution context: `project_id` is the BUILD project (its db
    # owns the rows + its family scopes search). Each file is tagged with its TRUE
    # source project; only files whose source ∈ this build's family are ingested
    # (relevance + no cross-project pollution). Un-attributable files are skipped.
    registry = load_path_registry()
    family = resolve_family_ulids(project_id, registry=registry)

    stats = {
        "files_scanned": 0,
        "files_ingested": 0,
        "files_skipped": 0,
        "files_failed": 0,
        "files_unattributed": 0,
        "files_out_of_scope": 0,
        "chunks_written": 0,
        "secrets_rejected": 0,
        "pages_dropped_noise": 0,
        "pages_deduped": 0,
        # Change #2 (2026-05-29): skill dedup-and-tag
        "skill_bodies_deduped": 0,   # skill bodies seen but NOT re-embedded (already in store)
        "skill_docs_created": 0,     # canonical skill docs newly embedded this run
        "skill_bodies_stale": 0,     # BC-21: bodies outranked by the stored canonical authority
    }

    # Collect candidate JSONL files.
    candidates: list[Path] = []
    for root in _TRANSCRIPT_ROOTS:
        if not root.exists():
            continue
        candidates.extend(root.rglob("*.jsonl"))
        # Also try plain .json files in session dirs.
        candidates.extend(root.rglob("*.json"))

    if not candidates:
        log.info("No transcript files found in: %s", _TRANSCRIPT_ROOTS)
        return stats

    for fpath in candidates:
        stats["files_scanned"] += 1

        # ── Source-project attribution ──
        # Tag with the file's TRUE source project — NEVER the build project.
        source_ulid = _resolve_source_ulid(fpath, registry)
        if source_ulid is None:
            stats["files_unattributed"] += 1  # source repo not registered → skip
            continue
        if source_ulid not in family:
            stats["files_out_of_scope"] += 1  # not in this build's family → skip
            continue

        # Identity = "<source-ULID>:<session-id>" (session id = filename stem, stable
        # across moves). The ledger keys on this identity, so a moved/renamed file is
        # recognised rather than re-ingested as a duplicate.
        doc_key = f"{source_ulid}:{fpath.stem}"

        try:
            # Extraction and digest came from one file descriptor above, so an
            # append between two separate reads cannot checkpoint mismatched data.
            pages, source_digest = _extract_text_and_sha(fpath)
        except Exception as exc:  # noqa: BLE001 — one malformed transcript is skipped, not ledgered, retried next run
            log.warning(
                "Extraction failed for %s: %s — skipped (not ledgered; retried next run)",
                fpath, exc,
            )
            stats["files_failed"] += 1
            continue
        if not source_digest:
            continue

        if incremental and ledger.get(doc_key) == source_digest:
            stats["files_skipped"] += 1
            continue
        if not pages:
            empty_hash = hashlib.sha256(b"").digest()
            await store.replace_document(
                project_id=source_ulid,
                doc_key=doc_key,
                title=fpath.name,
                content_hash=empty_hash,
                content_type="transcript",
                chunks=[],
                metadata={"source_path": str(fpath), "invoked_skills": []},
            )
            ledger[doc_key] = source_digest
            continue

        # Compact BEFORE secret-scan (less to scan).
        if _compaction_enabled():
            pages, _c = compact_pages(pages)
            stats["pages_dropped_noise"] += _c["dropped_noise"]
            stats["pages_deduped"] += _c["deduped"]
            if not pages:
                # The new source is empty after filtering: atomically clear any
                # older chunks so the ledger never hides stale searchable text.
                await store.replace_document(
                    project_id=source_ulid,
                    doc_key=doc_key,
                    title=fpath.name,
                    content_hash=hashlib.sha256(b"").digest(),
                    content_type="transcript",
                    chunks=[],
                    metadata={"source_path": str(fpath), "invoked_skills": []},
                )
                ledger[doc_key] = source_digest
                continue

        # Secret-scan every page BEFORE write.
        clean_pages: list[str] = []
        for page in pages:
            hit = _secret_scan(page)
            if hit:
                stats["secrets_rejected"] += 1
                # Log the reason WITHOUT the matched value.
                log.warning(
                    "Sensitive-content guard triggered in %s — page skipped (%s)",
                    fpath.name, hit,
                )
                continue
            clean_pages.append(page)

        if not clean_pages:
            await store.replace_document(
                project_id=source_ulid,
                doc_key=doc_key,
                title=fpath.name,
                content_hash=hashlib.sha256(b"").digest(),
                content_type="transcript",
                chunks=[],
                metadata={"source_path": str(fpath), "invoked_skills": []},
            )
            ledger[doc_key] = source_digest
            continue

        # ── Change #2: skill dedup-and-tag ─────────────────────────────────────
        # Partition clean_pages into skill-body pages (rendered SKILL.md injections
        # that are cross-session boilerplate) and content pages (everything else,
        # including <command-name> invocation pages whose args are real user intent).
        # Skill bodies are embedded ONCE as canonical skill docs (content_type="skill",
        # doc_key="skill:<name>"); content pages become the session transcript doc.
        # Skill docs are orthogonal to transcript docs — separate concern.
        skill_body_pages: list[tuple[str, str]] = []  # (page, skill_name)
        content_pages: list[str] = []
        invoked_skills: set[str] = set()

        for page in clean_pages:
            if is_skill_body(page):
                name = skill_name_from_body(page)
                if name:
                    skill_body_pages.append((page, name))
                    invoked_skills.add(name)
                else:
                    # Name extraction failed — treat as content rather than silently
                    # dropping (no silent discards).
                    content_pages.append(page)
            else:
                content_pages.append(page)

        # Embed each skill body ONCE: canonical doc_key="skill:<name>" per project.
        #
        # CANONICAL SKILL AUTHORITY (BC-21): when historical transcripts carry
        # DIFFERENT bodies for one skill, the canonical body is the candidate
        # whose source transcript file has the newest mtime; equal mtimes break
        # the tie on the lexicographically greatest content-hash hex. The winning
        # authority pair is persisted in the skill doc's metadata
        # ("source_mtime_ns" + the stored content hash), and a candidate replaces
        # the stored body only when its (mtime, hash) ranks STRICTLY higher —
        # so re-ingesting the same transcript set in any order converges on the
        # same canonical body. An equal-hash candidate from a newer source still
        # raises the recorded authority, closing the ordering hole where an
        # authority-less duplicate could later lose to an older body.
        skill_failed = False
        try:
            source_mtime_ns = fpath.stat().st_mtime_ns
        except OSError:
            source_mtime_ns = 0
        for skill_page, skill_name in skill_body_pages:
            skill_doc_key = f"skill:{skill_name}"
            skill_hash = hashlib.sha256(skill_page.encode()).digest()
            candidate_authority = (source_mtime_ns, skill_hash.hex())
            existing = await store.document_metadata(project_id, skill_doc_key)
            if existing is not None:
                stored_hash = existing["content_hash"]
                stored_meta = existing["metadata"]
                try:
                    stored_mtime = int(stored_meta.get("source_mtime_ns", -1))
                except (TypeError, ValueError):
                    stored_mtime = -1
                stored_authority = (
                    stored_mtime,
                    stored_hash.hex() if stored_hash else "",
                )
                stored_complete = await store.document_is_current(
                    project_id,
                    skill_doc_key,
                    stored_hash if stored_hash else b"",
                    expected_chunks=1,
                )
                if stored_hash == skill_hash and stored_complete:
                    stats["skill_bodies_deduped"] += 1
                    if candidate_authority > stored_authority:
                        await store.update_document_metadata(
                            project_id,
                            skill_doc_key,
                            {**stored_meta, "source_mtime_ns": source_mtime_ns},
                        )
                    continue
                if stored_complete and candidate_authority <= stored_authority:
                    # A lower-ranked different body never replaces the canonical
                    # one, in any ingestion order. An INCOMPLETE stored doc falls
                    # through instead: any candidate may repair it, and the true
                    # winner still lands once its source is (re)visited.
                    stats["skill_bodies_stale"] += 1
                    continue
            # New skill, repair, or a strictly higher-authority body — embed.
            try:
                skill_embeddings = embed_texts([skill_page])
            except Exception as exc:  # noqa: BLE001 — embedder outage skips this skill body, never writes null vectors
                log.warning(
                    "Embed failed for skill '%s' from %s: %s — skipped",
                    skill_name, fpath.name, exc,
                )
                skill_failed = True
                continue
            await store.replace_document(
                project_id=project_id,
                doc_key=skill_doc_key,
                title=f"skill:{skill_name}",
                content_hash=skill_hash,
                content_type="skill",
                chunks=[(skill_page, skill_embeddings[0])],
                metadata={
                    "skill_name": skill_name,
                    "source_mtime_ns": source_mtime_ns,
                },
            )
            stats["skill_docs_created"] += 1
        # ── End Change #2 skill embedding ──────────────────────────────────────

        if not content_pages:
            # File now contains only skill bodies. Clear any old transcript chunks;
            # canonical skill documents above remain independently searchable.
            await store.replace_document(
                project_id=source_ulid,
                doc_key=doc_key,
                title=fpath.name,
                content_hash=hashlib.sha256(b"").digest(),
                content_type="transcript",
                chunks=[],
                metadata={
                    "source_path": str(fpath),
                    "invoked_skills": sorted(invoked_skills),
                },
            )
            if not skill_failed:
                ledger[doc_key] = source_digest
            else:
                stats["files_failed"] = stats.get("files_failed", 0) + 1
            continue

        content_hash = hashlib.sha256(
            "\n".join(content_pages).encode()
        ).digest()

        if incremental and await store.document_is_current(
            source_ulid,
            doc_key,
            content_hash,
            expected_chunks=len(content_pages),
        ):
            if not skill_failed:
                ledger[doc_key] = source_digest
                stats["files_skipped"] += 1
            else:
                stats["files_failed"] = stats.get("files_failed", 0) + 1
            continue

        # CHECKPOINT-ON-SUCCESS: on embed failure, do NOT write null chunks and do
        # NOT ledger this file — it's retried next run (no permanent null rows).
        try:
            embeddings = embed_texts(content_pages)
        except Exception as exc:  # noqa: BLE001 — upholds checkpoint-on-success: no null chunks, no ledger entry
            log.warning(
                "Embed failed for %s: %s — skipped (not ledgered; retried next run)",
                fpath, exc,
            )
            stats["files_failed"] = stats.get("files_failed", 0) + 1
            continue

        await store.replace_document(
            project_id=source_ulid,
            doc_key=doc_key,
            title=fpath.name,
            content_hash=content_hash,
            content_type="transcript",
            chunks=list(zip(content_pages, embeddings, strict=True)),
            metadata={
                "source_path": str(fpath),
                "invoked_skills": sorted(invoked_skills),
            },
        )
        stats["chunks_written"] += len(content_pages)

        stats["files_ingested"] += 1
        if not skill_failed:
            ledger[doc_key] = source_digest
        else:
            stats["files_failed"] = stats.get("files_failed", 0) + 1

        if progress_cb:
            progress_cb(
                f"Ingested transcript: {fpath.name} "
                f"({len(content_pages)} content pages, "
                f"{len(skill_body_pages)} skill bodies)"
            )

    # Persist the updated ledger.
    _save_ledger(ledger_path, ledger)

    log.info(
        "Transcript ingest complete: %s files scanned, %s ingested, "
        "%s skipped, %s failed (retry next run), %s chunks, %s secret-rejections",
        stats["files_scanned"], stats["files_ingested"],
        stats["files_skipped"], stats.get("files_failed", 0),
        stats["chunks_written"], stats["secrets_rejected"],
    )
    return stats


# ── Foreign-document reconciliation (preview-only, explicit opt-in apply) ──────
#
# ingest_transcripts() above only ever ATTRIBUTES new rows to their true
# source project and skips files outside the build's family
# (files_out_of_scope, just above). Rows written before that scope check
# existed — or left behind after a path was reassociated to a different
# Project (project_registry.reassociate_project_path) — can still carry a
# `bc_documents.project_id` that does not match the database they live in.
# storage_accounting._db_diagnostics() already surfaces a COUNT of these
# ("foreign_documents"); this section resolves them, following the same
# preview-digest + explicit-opt-in-apply + destination-mutation-lock
# discipline as legacy_recovery.py and project_registry.find_orphans().


class ForeignDocumentReconciliationError(RuntimeError):
    """Foreign-document migration cannot proceed safely."""

    def __init__(self, message: str, *, completed_owners: Iterable[str] = ()) -> None:
        self.completed_owners = tuple(completed_owners)
        suffix = (
            f" Completed migrations remain applied: {', '.join(self.completed_owners)}."
            if self.completed_owners else ""
        )
        super().__init__(message + suffix)


def _wal_sibling(path: Path, suffix: str) -> Path:
    return path.with_name(f"{path.name}-{suffix}")


@contextmanager
def _read_only_project_db(path: Path, *, purpose: str) -> Iterator[sqlite3.Connection]:
    """Open a stable read-only snapshot without hiding committed WAL frames.

    Mirrors `legacy_recovery._read_only`'s discipline: refuse rather than
    create a WAL shared-memory index during preview, or silently omit
    committed WAL frames via ``immutable=1``.
    """
    wal, shm = _wal_sibling(path, "wal"), _wal_sibling(path, "shm")
    if wal.exists() and not shm.exists():
        raise ForeignDocumentReconciliationError(
            f"{purpose} cannot safely read {path}: committed WAL data exists "
            "but the WAL shared-memory index is unavailable. Close the "
            "writer, then retry."
        )
    query = "mode=ro&cache=private" if wal.exists() else "mode=ro&immutable=1"
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?{query}", uri=True, timeout=0
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def _exclusive_project_db(path: Path, *, purpose: str) -> Iterator[sqlite3.Connection]:
    """Hold SQLite's writer lock for one migration step (read AND delete/insert
    happen inside the same transaction so nothing else can write in between)."""
    connection = sqlite3.connect(path, timeout=0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout=0")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise ForeignDocumentReconciliationError(
                f"{purpose}: {path} has an active writer. Stop BrainCell (or "
                "its ingest) before applying reconciliation."
            ) from exc
        yield connection
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _backup_project_db(path: Path, backup_dir: Path | None = None) -> Path:
    """Retain a transactionally consistent copy of *path* before it is mutated."""
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    directory = (backup_dir or path.parent).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{path.stem}.reconcile-backup-{timestamp}.db"
    serial = 1
    while destination.exists():
        destination = directory / f"{path.stem}.reconcile-backup-{timestamp}-{serial}.db"
        serial += 1
    with (
        _read_only_project_db(path, purpose="Backup") as original,
        sqlite3.connect(destination) as backup,
    ):
        original.backup(backup)
    return destination


def _foreign_row_conflicts(
    source: sqlite3.Connection,
    destination_path: Path,
    owner: str,
    doc_ids: list[int],
) -> list[dict[str, Any]]:
    """doc_key collisions where the owner's OWN database already holds
    different content under that key — apply must refuse these, never
    silently overwrite or duplicate."""
    if not doc_ids or not destination_path.is_file():
        return []
    placeholders = ",".join("?" for _ in doc_ids)
    conflicts: list[dict[str, Any]] = []
    with _read_only_project_db(destination_path, purpose="Preview") as dest:
        for row in source.execute(
            f"SELECT doc_key, content_hash FROM bc_documents WHERE id IN ({placeholders})",
            doc_ids,
        ):
            existing = dest.execute(
                "SELECT content_hash FROM bc_documents WHERE project_id=? AND doc_key=?",
                (owner, row["doc_key"]),
            ).fetchone()
            if existing is not None and existing[0] != row["content_hash"]:
                conflicts.append({"doc_key": row["doc_key"]})
    return conflicts


def preview_foreign_documents(project_id: str) -> dict[str, Any]:
    """Read-only inventory of `bc_documents` rows in `project_id`'s own
    database whose `project_id` column names a DIFFERENT Project.

    Groups them by true owner and classifies each owner as `migratable`
    (currently registered in the path-registry — apply can copy its rows into
    ITS OWN database) or unattributable (no such registered Project; apply
    always refuses these — there is nowhere safe to send them). Also lists
    destination doc_key collisions per owner as `conflicts` (same key,
    different content) — apply refuses a selected owner with any unresolved
    conflict, mirroring `legacy_recovery.preview`/`apply`.

    Detection only: every connection here is opened read-only; nothing is
    written, moved, or deleted. Missing database => empty, valid report.
    """
    database = get_db_path(project_id)
    owners: dict[str, dict[str, Any]] = {}
    if database.is_file():
        registered = set(load_path_registry().values())
        with _read_only_project_db(database, purpose="Preview") as source:
            rows = source.execute(
                "SELECT id, project_id, doc_key, title, content_type, created_at "
                "FROM bc_documents WHERE project_id != ? ORDER BY project_id, id",
                (project_id,),
            ).fetchall()
            by_owner: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                by_owner.setdefault(str(row["project_id"]), []).append(row)
            for owner, owner_rows in by_owner.items():
                doc_ids = [int(r["id"]) for r in owner_rows]
                migratable = owner in registered
                owners[owner] = {
                    "migratable": migratable,
                    "documents": [
                        {
                            "id": int(r["id"]),
                            "doc_key": r["doc_key"],
                            "title": r["title"],
                            "content_type": r["content_type"],
                            "created_at": r["created_at"],
                        }
                        for r in owner_rows
                    ],
                    "conflicts": (
                        _foreign_row_conflicts(source, get_db_path(owner), owner, doc_ids)
                        if migratable else []
                    ),
                }
    report: dict[str, Any] = {
        "project_id": project_id,
        "database": str(database),
        "owners": owners,
        "unattributable_owners": sorted(
            owner for owner, detail in owners.items() if not detail["migratable"]
        ),
    }
    report["approval_digest"] = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return report


def _migrate_owner_rows(
    source_path: Path,
    destination_path: Path,
    owner: str,
    doc_meta: list[dict[str, Any]],
) -> dict[str, int]:
    """Copy one owner's foreign rows out of `source_path` and into
    `destination_path` (creating/validating its schema first), verify every
    row landed under its content hash, THEN delete the migrated rows from
    `source_path` — the source delete never runs unless the destination
    commit immediately above it already succeeded and was verified.
    """
    doc_ids = [item["id"] for item in doc_meta]
    if not doc_ids:
        return {"documents_migrated": 0, "chunks_migrated": 0, "documents_removed_from_source": 0}

    if destination_path.is_file():
        with _read_only_project_db(destination_path, purpose="Apply") as dest:
            row = dest.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            version = int(row[0]) if row else 0
            if version != MEMORY_SCHEMA_VERSION:
                raise ForeignDocumentReconciliationError(
                    f"Destination {destination_path} is not at supported schema "
                    f"v{MEMORY_SCHEMA_VERSION}; upgrade it before reconciliation."
                )
    else:
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        store = SqliteStore(destination_path)
        store.assert_schema_version()
        store.close()

    placeholders = ",".join("?" for _ in doc_ids)
    copied_documents = 0
    copied_chunks = 0
    with _exclusive_project_db(source_path, purpose="Reconciliation source") as source:
        conflicts = _foreign_row_conflicts(source, destination_path, owner, doc_ids)
        if conflicts:
            raise ForeignDocumentReconciliationError(
                f"Destination conflicts must be resolved before migrating to "
                f"{owner}: {conflicts}"
            )
        with _exclusive_project_db(
            destination_path, purpose="Reconciliation destination"
        ) as dest:
            document_map: dict[int, int] = {}
            for document in source.execute(
                f"SELECT * FROM bc_documents WHERE id IN ({placeholders}) ORDER BY id",
                doc_ids,
            ):
                existing = dest.execute(
                    "SELECT id, content_hash FROM bc_documents WHERE project_id=? AND doc_key=?",
                    (owner, document["doc_key"]),
                ).fetchone()
                if existing is not None:
                    # Conflicts were ruled out above, so a hit here means the
                    # correct row is already in the destination (e.g. a prior
                    # partial migration) — nothing new to insert, but the
                    # stale duplicate is still safe to remove from source.
                    document_map[int(document["id"])] = int(existing["id"])
                    continue
                cursor = dest.execute(
                    "INSERT INTO bc_documents (project_id,doc_key,title,content_hash,"
                    "content_type,commit_sha,run_id,created_at,updated_at,metadata,"
                    "pooled_from) VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                    (
                        owner, document["doc_key"], document["title"],
                        document["content_hash"], document["content_type"],
                        document["commit_sha"], document["run_id"],
                        document["created_at"], document["updated_at"],
                        document["metadata"],
                    ),
                )
                document_map[int(document["id"])] = int(cursor.lastrowid)
                copied_documents += 1
            for source_document_id, destination_document_id in document_map.items():
                for chunk in source.execute(
                    "SELECT * FROM bc_chunks WHERE document_id=? ORDER BY chunk_index",
                    (source_document_id,),
                ):
                    existing_chunk = dest.execute(
                        "SELECT id FROM bc_chunks WHERE document_id=? AND chunk_index=?",
                        (destination_document_id, chunk["chunk_index"]),
                    ).fetchone()
                    if existing_chunk is None:
                        dest.execute(
                            "INSERT INTO bc_chunks (document_id,chunk_index,chunk_text,"
                            "chunk_hash,embedding,run_id) VALUES (?,?,?,?,?,?)",
                            (
                                destination_document_id, chunk["chunk_index"],
                                chunk["chunk_text"], chunk["chunk_hash"],
                                chunk["embedding"], chunk["run_id"],
                            ),
                        )
                        copied_chunks += 1
            dest.execute("INSERT INTO bc_chunks_fts(bc_chunks_fts) VALUES('rebuild')")

            for document in source.execute(
                f"SELECT doc_key, content_hash FROM bc_documents WHERE id IN ({placeholders})",
                doc_ids,
            ):
                actual = dest.execute(
                    "SELECT content_hash FROM bc_documents WHERE project_id=? AND doc_key=?",
                    (owner, document["doc_key"]),
                ).fetchone()
                if actual is None or actual["content_hash"] != document["content_hash"]:
                    raise ForeignDocumentReconciliationError(
                        f"Post-copy verification failed for {document['doc_key']!r} "
                        f"in {destination_path}; source rows were left untouched."
                    )
            dest.commit()

        # Only now — after the destination transaction committed and every
        # row verified present — remove the migrated rows from source.
        # bc_chunks cascade off bc_documents (ON DELETE CASCADE); the FTS
        # index is rebuilt the same way a fresh copy rebuilds the destination's.
        source.execute(f"DELETE FROM bc_documents WHERE id IN ({placeholders})", doc_ids)
        source.execute("INSERT INTO bc_chunks_fts(bc_chunks_fts) VALUES('rebuild')")
        source.commit()

    return {
        "documents_migrated": copied_documents,
        "chunks_migrated": copied_chunks,
        "documents_removed_from_source": len(doc_ids),
    }


def apply_foreign_document_migration(
    project_id: str,
    *,
    owner_project_ids: list[str],
    approval_digest: str,
    backup_dir: Path | None = None,
) -> dict[str, Any]:
    """Explicit opt-in migration of foreign-owned `bc_documents` rows out of
    `project_id`'s database and into each owner's OWN database.

    Re-plans under `project_id`'s destination mutation lock so it can never
    execute a stale preview (mirrors `legacy_recovery.apply`). Every selected
    owner must still be `migratable` (a currently registered Project) and
    conflict-free in the FRESH preview; unattributable or conflicted owners
    are always refused — no partial best-effort migration. A pre-mutation
    backup of the source database is taken before any row is deleted, the
    same discipline `_required_auto_backup` applies to other destructive
    Project wipes elsewhere in this codebase (`cli.py:_execute_build`'s
    `--reembed`, `gui_ingest.py`'s `clear_project`). One owner's failure never
    rolls back an already-migrated prior owner in the same call — their
    transactions already committed — so `completed_owners` on the raised
    error reports what NOT to redo, only what still needs a fresh preview.
    """
    if not owner_project_ids:
        raise ForeignDocumentReconciliationError(
            "Apply requires at least one selected owner Project."
        )
    database = get_db_path(project_id)
    with mutation_lock(database, operation="foreign-document-reconciliation"):
        report = preview_foreign_documents(project_id)
        if approval_digest != report["approval_digest"]:
            raise ForeignDocumentReconciliationError(
                "Approval digest does not match the current preview; preview again."
            )
        selected = sorted(set(owner_project_ids))
        if unknown := [owner for owner in selected if owner not in report["owners"]]:
            raise ForeignDocumentReconciliationError(
                "Selected owners have no foreign rows in the current preview: "
                f"{', '.join(unknown)}"
            )
        if unattributable := [
            owner for owner in selected if not report["owners"][owner]["migratable"]
        ]:
            raise ForeignDocumentReconciliationError(
                "Selected owners are not registered Projects; apply refuses to "
                f"migrate into an unattributable destination: {', '.join(unattributable)}"
            )
        if conflicted := {
            owner: report["owners"][owner]["conflicts"]
            for owner in selected if report["owners"][owner]["conflicts"]
        }:
            raise ForeignDocumentReconciliationError(
                f"Destination conflicts must be resolved before apply: {conflicted}"
            )

        source_backup = _backup_project_db(database, backup_dir)
        results: dict[str, Any] = {}
        for owner in selected:
            destination = get_db_path(owner)
            try:
                with mutation_lock(destination, operation="foreign-document-reconciliation"):
                    results[owner] = _migrate_owner_rows(
                        database, destination, owner, report["owners"][owner]["documents"],
                    )
            except MutationBusyError as exc:
                raise ForeignDocumentReconciliationError(
                    str(exc), completed_owners=results,
                ) from exc
            except Exception as exc:
                raise ForeignDocumentReconciliationError(
                    f"Migration failed for {owner}; its rows remain in {database} "
                    f"untouched. {exc}",
                    completed_owners=results,
                ) from exc

    return {
        "project_id": project_id,
        "database": str(database),
        "source_backup": str(source_backup),
        "migrated": results,
    }
