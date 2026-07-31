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
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .compaction import compact_pages
from .embed import embed_texts
from .log import get as _get_log
from .project_registry import (
    load_path_registry,
    resolve_claude_dir_to_ulid,
    resolve_family_ulids,
    resolve_path_to_ulid,
)
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
        # Dedup check: if a doc with that key already exists (changed=False on an
        # unmodified hash), we skip re-embedding (cross-session + cross-run dedup).
        skill_failed = False
        for skill_page, skill_name in skill_body_pages:
            skill_doc_key = f"skill:{skill_name}"
            skill_hash = hashlib.sha256(skill_page.encode()).digest()
            if await store.document_is_current(
                project_id,
                skill_doc_key,
                skill_hash,
                expected_chunks=1,
            ):
                stats["skill_bodies_deduped"] += 1
                continue
            # New or changed skill body — embed and store.
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
                metadata={"skill_name": skill_name},
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
