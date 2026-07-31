# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
embed.py — Embedding helpers for BrainCell.

Sync path (embed_texts / prewarm) is used by the `braincell build` ingest
pipeline (which calls the `ollama` SDK synchronously).

Async wrapper (embed_texts_async / embed_query_async) is used by the
FastMCP server via asyncio.get_event_loop().run_in_executor().

The embedding model stays resident across one build's batches and the CLI
explicitly unloads it at the end, avoiding repeated cold loads without leaking
VRAM beyond the build lifecycle.
"""

from __future__ import annotations

import asyncio
import os
import time
from functools import lru_cache

import numpy as np

from . import embed_spec
from .log import get as _get_log

log = _get_log("braincell.embed")


# ── Sync helpers (pipeline path) ─────────────────────────────────────────────

def embed_texts(texts: list[str]) -> list[np.ndarray]:
    """Embed a batch of DOCUMENTS. Returns a list of float32 unit vectors.

    This is the writer/document path (``braincell build`` ingest). Each text is
    prefixed with embed_spec.DOC_PREFIX (empty for symmetric models like the
    bge-m3 default → no-op) before embedding, which is why a non-empty DOC_PREFIX
    extends embed_spec.FINGERPRINT (stored vectors change). Queries go through
    embed_query, which applies embed_spec.QUERY_PREFIX instead.

    Routes to the configured provider (embed_spec.PROVIDER): the local Ollama
    client (default) or the OpenAI embeddings API. EVERY returned vector is
    L2-normalised, so inner product == cosine for the store regardless of
    provider, and each is validated against embed_spec.DIM (fail loud on a
    model/dimension mismatch). Raises on connectivity/auth failure;
    the caller decides how to handle.
    """
    if not texts:
        return []
    if embed_spec.DOC_PREFIX:
        texts = [embed_spec.DOC_PREFIX + t for t in texts]
    return _embed_normalized(texts)


def _embed_normalized(texts: list[str]) -> list[np.ndarray]:
    """Embed already-prefixed texts → L2-normalised, dim-guarded unit vectors.

    Prefix-agnostic core shared by the document path (embed_texts, DOC_PREFIX) and
    the query path (_embed_query_cached, QUERY_PREFIX) so neither ever applies the
    other's prefix. Callers pass text that already carries whatever prefix applies.
    """
    if not texts:
        return []

    if embed_spec.PROVIDER == "openai":
        raw = _embed_openai(texts)
    else:
        raw = _embed_ollama(texts)

    if len(raw) != len(texts):
        raise ValueError(
            "BrainCell embed provider contract violation: returned "
            f"{len(raw)} embeddings for {len(texts)} inputs."
        )

    vecs: list[np.ndarray] = []
    for index, emb in enumerate(raw):
        vec = np.asarray(emb, dtype=np.float32)
        if vec.ndim != 1 or vec.shape[0] != embed_spec.DIM:
            raise ValueError(
                f"BrainCell embed dimension mismatch: model {embed_spec.MODEL!r} "
                f"returned shape {vec.shape!r} for input {index}, but "
                f"embed_spec.DIM={embed_spec.DIM}. "
                f"Check BRAINCELL_EMBED_MODEL / BRAINCELL_EMBED_DIM."
            )
        if not np.isfinite(vec).all():
            raise ValueError(
                f"BrainCell embed provider returned non-finite values for input {index}."
            )
        norm = float(np.linalg.norm(vec))
        if not np.isfinite(norm) or norm <= 0.0:
            raise ValueError(
                f"BrainCell embed provider returned a zero-norm vector for input {index}."
            )
        vecs.append(vec / norm)  # L2-normalise → inner product == cosine
    return vecs


# Ollama sub-batch limits — prevents single oversized requests against a
# cold-loaded model on GPU. Sized conservatively;
# Ollama has no strict API cap but GPU OOM is real for large transcript dumps.
_OLLAMA_MAX_INPUTS = 64
_OLLAMA_MAX_CHARS = 100_000
_OLLAMA_MAX_RETRIES = 3


def _embed_ollama(texts: list[str]) -> list:
    """Local Ollama embed call with build-scoped keepalive.

    Uses an explicit client with a bounded timeout (embed_spec.OLLAMA_TIMEOUT) so a
    daemon that is reachable but stalled (cold GPU model-load wedged) fails loud
    instead of hanging forever — the default client has timeout=None.

    Sub-batches ``texts`` to avoid single oversized requests (reuses
    _batched_by_size with Ollama-appropriate limits). Retries transient errors
    with exponential backoff. After retries exhausted, re-raises as a
    branded BrainCell RuntimeError naming the host, model, and remediation hint.
    """
    # Lazy import mirrors the sync ollama path — do not hoist to module scope.
    import httpx
    import ollama

    client = ollama.Client(timeout=embed_spec.OLLAMA_TIMEOUT)
    _host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    out: list = []

    for batch in _batched_by_size(texts, _OLLAMA_MAX_INPUTS, _OLLAMA_MAX_CHARS):
        _last_exc: BaseException | None = None
        for attempt in range(_OLLAMA_MAX_RETRIES):
            try:
                response = client.embed(
                    model=embed_spec.MODEL,
                    input=batch,
                    keep_alive=embed_spec.KEEP_ALIVE,
                    # MRL: ask the model for exactly DIM dims. An MRL model (qwen3)
                    # emits a valid DIM-d vector (e.g. 4b's native 2560 → 1024); a
                    # model whose native output already equals DIM returns it
                    # unchanged; a genuine mismatch fails loud at the dim guard in
                    # embed_texts. Requires an Ollama that honors `dimensions`.
                    dimensions=embed_spec.DIM,
                )
                out.extend(response.embeddings)
                _last_exc = None
                break
            except (ollama.ResponseError, httpx.TransportError, ConnectionError) as exc:
                _last_exc = exc
                if attempt < _OLLAMA_MAX_RETRIES - 1:
                    wait = 2 ** attempt  # 1 s, 2 s …
                    log.warning(
                        "BrainCell embed: Ollama transient error"
                        " (attempt %d/%d, retry in %ds): %s",
                        attempt + 1, _OLLAMA_MAX_RETRIES, wait, exc,
                    )
                    time.sleep(wait)
        if _last_exc is not None:
            raise RuntimeError(
                f"BrainCell embed: Ollama failed after {_OLLAMA_MAX_RETRIES} attempts "
                f"(host={_host!r}, model={embed_spec.MODEL!r}). "
                f"Is the daemon running? (`ollama serve`). "
                f"Is the model pulled? (`ollama pull {embed_spec.MODEL}`). "
                f"Last error: {_last_exc}"
            ) from _last_exc

    return out


# OpenAI embeddings request limits (text-embedding-3-*): a single request caps
# inputs (~2048) and total tokens. The build batches a whole transcript file's
# pages into one embed_texts() call, so we MUST sub-batch here or large files
# 400 and fall back to null embeddings. Bound by both input count and chars
# (pages are ≤2000 chars, so the char budget keeps us well under the token cap).
_OPENAI_MAX_INPUTS = 2048
_OPENAI_MAX_CHARS = 600_000  # ~150k tokens — safely under the per-request ceiling


def _batched_by_size(texts: list[str], max_inputs: int, max_chars: int):
    """Yield sub-lists of ``texts`` bounded by input count and total chars."""
    batch: list[str] = []
    nchars = 0
    for t in texts:
        if batch and (len(batch) >= max_inputs or nchars + len(t) > max_chars):
            yield batch
            batch, nchars = [], 0
        batch.append(t)
        nchars += len(t)
    if batch:
        yield batch


def _embed_openai(texts: list[str]) -> list:
    """Hosted OpenAI embeddings. OPENAI_API_KEY is read from env (sops-delivered).

    Mirrors the sync openai path. Sub-batches to stay under the per-request
    input/token cap, and sorts each response by .index so order matches input
    order regardless of the API's response ordering.
    """
    # Lazy import, mirrors the sync openai path — do not hoist to module scope.
    from openai import OpenAI

    client = OpenAI()  # base_url + OPENAI_API_KEY from env
    # Pass dimensions= for text-embedding-3-* which supports per-request
    # truncation (openai SDK ≥ 1.10.0). Older models (ada-002) do not accept
    # this kwarg, so guard by model prefix rather than SDK version sniffing.
    _dim_kwarg: dict = (
        {"dimensions": embed_spec.DIM}
        if embed_spec.MODEL.startswith("text-embedding-3-")
        else {}
    )
    out: list = []
    for batch in _batched_by_size(texts, _OPENAI_MAX_INPUTS, _OPENAI_MAX_CHARS):
        resp = client.embeddings.create(
            model=embed_spec.MODEL, input=batch, **_dim_kwarg
        )
        out.extend(d.embedding for d in sorted(resp.data, key=lambda d: d.index))
    return out


@lru_cache(maxsize=512)
def _embed_query_cached(prefixed_text: str) -> bytes:
    """Embed one ALREADY-PREFIXED query string, returning raw float32 bytes.

    Cached because queries repeat heavily within a session and the Ollama
    round-trip (tens of ms) dominates query latency — far more than the matmul.
    Bytes (not a mutable ndarray) are cached so a caller can never mutate the
    shared cache entry. ``lru_cache`` does not cache exceptions, so an embedder
    outage is retried (never poisons the cache). The embed model/dim/prefix is
    fixed for a process lifetime, and the cache key is the FULLY-PREFIXED query
    text, so the text→vector mapping is stable and safe to memoise (a QUERY_PREFIX
    change yields a different key). Uses the prefix-free core so the query prefix
    is never doubled with the document prefix.
    """
    vecs = _embed_normalized([prefixed_text])
    if not vecs:
        return np.zeros(embed_spec.DIM, dtype=np.float32).tobytes()
    return vecs[0].astype(np.float32).tobytes()


def embed_query(text: str) -> np.ndarray:
    """Embed a single query string. Returns a float32 unit vector (session-cached).

    Applies embed_spec.QUERY_PREFIX (the asymmetric query-side instruction; empty
    for symmetric models like the bge-m3 default → no-op) before embedding, so the
    query matches the recipe the model was trained on. QUERY_PREFIX rewrites
    only the query input and never the stored vectors, so it does NOT change
    embed_spec.FINGERPRINT.
    """
    prefixed = embed_spec.QUERY_PREFIX + text if embed_spec.QUERY_PREFIX else text
    return np.frombuffer(_embed_query_cached(prefixed), dtype=np.float32)


def prewarm_embed_model() -> bool:
    """Send a minimal embed call to warm the model into VRAM.

    Called at pipeline start (before the main ingest loop) so the first
    real embed call does not pay the model-load latency.
    Returns True on success, False on failure (non-fatal — pipeline continues).

    No-op for hosted providers (no local model to load into VRAM).
    """
    if embed_spec.PROVIDER != "ollama":
        return True
    try:
        import ollama  # lazy, as in _embed_ollama
        client = ollama.Client(timeout=embed_spec.OLLAMA_TIMEOUT)
        client.embed(
            model=embed_spec.MODEL,
            input=["warm"],
            keep_alive=embed_spec.KEEP_ALIVE,
        )
        log.debug("BrainCell embed model pre-warmed: %s", embed_spec.MODEL)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "BrainCell embed prewarm failed (non-fatal, embeddings skipped "
            "for this run): %s", exc,
        )
        return False


def unload_embed_model() -> bool:
    """Explicitly release the Ollama embedding model after a build."""
    if embed_spec.PROVIDER != "ollama":
        return True
    try:
        import ollama

        client = ollama.Client(timeout=embed_spec.OLLAMA_TIMEOUT)
        client.generate(model=embed_spec.MODEL, prompt="", keep_alive=0)
        log.debug("BrainCell embed model unloaded: %s", embed_spec.MODEL)
        return True
    except Exception as exc:  # noqa: BLE001 - cleanup is best-effort after build.
        log.warning("BrainCell embed model unload failed (non-fatal): %s", exc)
        return False


def embedder_status(timeout: float = 2.0) -> dict:
    """Read-only embedder health probe — never loads a model into VRAM.

    Answers "can a Build / remember embed right now?" BEFORE the user falls off
    the silent-NULL-embedding cliff. Ollama branch: one ``Client.list()`` call
    (reachability + default-model presence in a single cheap round trip —
    deliberately NOT prewarm_embed_model(), which performs a real embed and can
    stall on a cold GPU model load). openai branch: OPENAI_API_KEY presence
    only, no network call.

    Returns (never raises):
        {provider, model, dim, reachable, model_present, ok, detail}
    where ``ok = reachable AND model_present`` and ``detail`` carries the
    actionable fix ("install Ollama…", "ollama pull <model>") when not ok —
    mirroring the remediation strings in _embed_ollama's failure message.
    """
    base = {
        "provider": embed_spec.PROVIDER,
        "model": embed_spec.MODEL,
        "dim": embed_spec.DIM,
    }
    if embed_spec.PROVIDER == "openai":
        has_key = bool(os.environ.get("OPENAI_API_KEY"))
        return {
            **base,
            "reachable": has_key,
            "model_present": has_key,
            "ok": has_key,
            "detail": "" if has_key else (
                "OPENAI_API_KEY is not set — export it, or unset "
                "BRAINCELL_EMBED_PROVIDER to use the local Ollama embedder."
            ),
        }
    try:
        import ollama  # lazy import mirrors _embed_ollama
        client = ollama.Client(timeout=timeout)
        listed = client.list()
    except Exception as exc:  # noqa: BLE001 — a probe reports, never raises
        return {
            **base,
            "reachable": False,
            "model_present": False,
            "ok": False,
            "detail": (
                f"Ollama unreachable — install it from https://ollama.com, "
                f"start it (`ollama serve`), then run: "
                f"ollama pull {embed_spec.MODEL} ({exc})"
            ),
        }
    names: set[str] = set()
    for m in getattr(listed, "models", None) or []:
        name = getattr(m, "model", None)
        if name is None and isinstance(m, dict):
            name = m.get("model") or m.get("name")
        if name:
            names.add(str(name))
    # A tagless MODEL is listed by Ollama as "<model>:latest" — accept both.
    present = embed_spec.MODEL in names or f"{embed_spec.MODEL}:latest" in names
    return {
        **base,
        "reachable": True,
        "model_present": present,
        "ok": present,
        "detail": "" if present else (
            f"Embedding model not pulled — run: ollama pull {embed_spec.MODEL}"
        ),
    }


# ── Async wrappers (MCP server path) ─────────────────────────────────────────

async def embed_texts_async(texts: list[str]) -> list[np.ndarray]:
    """Async wrapper: runs embed_texts in a thread pool executor.

    Used by the FastMCP server so the event loop is not blocked by the
    synchronous Ollama HTTP call.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, embed_texts, texts)


async def embed_query_async(text: str) -> np.ndarray:
    """Async wrapper for single-query embed."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, embed_query, text)
