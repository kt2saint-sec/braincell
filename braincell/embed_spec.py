# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
embed_spec.py — Single source of truth for the BrainCell embedding contract.

Imported by BOTH the ingest path (writer, `braincell build`) and the BrainCell
MCP server (reader) so the vector space is never silently mismatched (SSoT).

Provider is selected by the ``BRAINCELL_EMBED_PROVIDER`` env var (default
``"ollama"``, keeping the shipped default local-first — hosted embedding is
opt-in, never the default). BOTH the writer process (``braincell build``) and
the reader process (the MCP server) MUST set the same env, or the build-time
guard and the read-time guard (store._cosine_top_k + the persisted embed
fingerprint) fail loud — they never silently mix vector spaces.

The Ollama path keeps the model resident across one build's batches, then the
CLI explicitly unloads it. This avoids repeated cold loads while bounding the
VRAM lifecycle. (No-op for hosted providers.)
"""

import hashlib
import os

# Provider: "ollama" (local, default) | "openai" (hosted text-embedding-3-*).
PROVIDER: str = os.environ.get("BRAINCELL_EMBED_PROVIDER", "ollama").strip().lower()

if PROVIDER == "ollama":
    # Local embedder (default — local-first, no hosted dependency).
    # Default: qwen3-embedding:4b, MRL-truncated to 1024-d — 2026-07-08 fair prefix
    # re-run (the earlier bge-m3 pick was decided on an UNFAIR comparison: queries
    # and docs were embedded symmetrically with no prefixes, so only bge-m3
    # (naturally symmetric) ran at its official recipe). Once the per-model query
    # prefix is applied (see § below), qwen3-4b@1024 is #1 on BOTH braincell-
    # critical cross-project tests (H6 cross-project 0.768, H7 hard-case
    # 0.566/0.625 Cov@5) and its earlier H4 long-doc "collapse" (0.224) was the
    # missing prefix, not the architecture — prefixed H4 = 0.891. See
    # tests/benchmarks/embedder-recommendation-2026-07-04.md (superseded table) and
    # docs/embedder-selection-and-benchmarks.md § H7 (the fair prefix re-run).
    # bge-m3 and qwen3-embedding:0.6b remain available via BRAINCELL_EMBED_MODEL.
    # Model + dim are env-configurable (mirrors the openai branch) so alternative
    # embedders coexist: e.g. qwen3-embedding:0.6b is 1024-d native, qwen3-embedding:4b
    # is 2560-d native but MRL-truncatable to 1024-d via ``dimensions=DIM``.
    # embed.py passes ``dimensions=DIM`` to the Ollama call, so a native-DIM model
    # returns unchanged and an MRL model emits exactly DIM; a model whose native
    # output != DIM fails loud at the dim guard. Each (model, dim) yields a distinct
    # FINGERPRINT below, so the store/federation keep vector spaces separate.
    MODEL: str = os.environ.get("BRAINCELL_EMBED_MODEL", "qwen3-embedding:4b")
    DIM: int = int(os.environ.get("BRAINCELL_EMBED_DIM", "1024"))
elif PROVIDER == "openai":
    # Hosted OpenAI embeddings. Key via OPENAI_API_KEY in env (sops-delivered).
    # text-embedding-3-small = 1536d (default); -large = 3072d. Both overridable.
    MODEL = os.environ.get("BRAINCELL_EMBED_MODEL", "text-embedding-3-small")
    DIM = int(os.environ.get("BRAINCELL_EMBED_DIM", "1536"))
else:
    raise ValueError(
        f"Unknown BRAINCELL_EMBED_PROVIDER={PROVIDER!r}. Expected 'ollama' or 'openai'."
    )

# ── Asymmetric query/document prefix injection ───────────────────────────────
# Several embedders are trained with an ASYMMETRIC recipe: the query is wrapped
# in an instruction/prefix while the document (passage) is embedded bare (or with
# its own distinct prefix). Applying the officially-documented prefix at retrieval
# time closes the query↔document distribution gap and materially lifts recall on
# those models. embed.py applies DOC_PREFIX in embed_texts (the writer/document
# path) and QUERY_PREFIX in embed_query (the reader/query path).
#
# Contract:
#   * QUERY_PREFIX only rewrites the query input → it NEVER changes stored vectors
#     → it MUST NOT alter FINGERPRINT.
#   * DOC_PREFIX rewrites the passage input → it DOES change stored vectors → it
#     MUST alter FINGERPRINT (":dp=<8-char sha256>") so a store built without the
#     doc prefix is never silently mixed with one built with it.
#   * qwen3-embedding (the default) is asymmetric → QUERY_PREFIX is the non-empty
#     Instruct/Query template, DOC_PREFIX stays empty → FINGERPRINT stays
#     byte-identical ("ollama:qwen3-embedding:4b:1024") since only DOC_PREFIX can
#     alter it, but embed_query DOES rewrite the query text (not a no-op).
#     bge-m3 is symmetric → both prefixes empty → fully no-op if selected via env.
#
# Registry is keyed by MODEL FAMILY (the Ollama tag suffix like ":4b"/":latest" is
# stripped, then matched by longest-prefix), so both qwen3-embedding sizes share
# one entry. Env overrides BRAINCELL_QUERY_PREFIX / BRAINCELL_DOC_PREFIX win over
# the registry (an explicit empty string is a valid override → disables a prefix).
#
# qwen3-embedding uses the "Instruct: {task}\nQuery: {q}" template (query side
# only; documents bare). nomic-embed-text uses search_query:/search_document:.
# mxbai-embed-large uses a query-side representation instruction; documents bare.
_QWEN_TASK: str = (
    "Given a developer memory query, retrieve notes describing the same "
    "underlying engineering lesson"
)

# family-key -> (QUERY_PREFIX, DOC_PREFIX)
_PREFIX_REGISTRY: dict[str, tuple[str, str]] = {
    "bge-m3": ("", ""),
    "qwen3-embedding": (f"Instruct: {_QWEN_TASK}\nQuery: ", ""),
    "nomic-embed-text": ("search_query: ", "search_document: "),
    "mxbai-embed-large": (
        "Represent this sentence for searching relevant passages: ",
        "",
    ),
}


def _model_family(model: str) -> str:
    """Strip the Ollama tag suffix (':4b', ':latest', ...) from a model name."""
    return model.split(":", 1)[0]


def _resolve_prefixes(model: str) -> tuple[str, str]:
    """Resolve (QUERY_PREFIX, DOC_PREFIX) for ``model``.

    Env overrides win over the registry; an explicit empty-string env value is a
    valid override (disables that side). Unknown models default to no prefixes.
    """
    family = _model_family(model)
    q_reg, d_reg = "", ""
    # Longest matching registry key wins (robust to future overlapping families).
    for key in sorted(_PREFIX_REGISTRY, key=len, reverse=True):
        if family == key or family.startswith(key):
            q_reg, d_reg = _PREFIX_REGISTRY[key]
            break

    q_env = os.environ.get("BRAINCELL_QUERY_PREFIX")
    d_env = os.environ.get("BRAINCELL_DOC_PREFIX")
    query_prefix = q_env if q_env is not None else q_reg
    doc_prefix = d_env if d_env is not None else d_reg
    return query_prefix, doc_prefix


QUERY_PREFIX, DOC_PREFIX = _resolve_prefixes(MODEL)


# Distance metric. embed.py L2-normalises EVERY vector regardless of provider,
# so stored vectors are unit-length and inner product == cosine similarity for
# all providers (OpenAI vectors are not unit-length on the wire).
DISTANCE: str = "inner-product"

# Keep the embedding model resident across a build's sub-batches. The CLI
# explicitly unloads it when the build finishes; hosted providers ignore this.
KEEP_ALIVE: str = os.environ.get("BRAINCELL_EMBED_KEEP_ALIVE", "5m")

# Bounded timeout (seconds) for the Ollama embed HTTP call. The default ollama
# client has timeout=None (httpx → no timeout), so a daemon that is reachable but
# stalled (cold GPU model-load wedged) would hang forever. Fail loud instead.
OLLAMA_TIMEOUT: float = float(os.environ.get("BRAINCELL_OLLAMA_TIMEOUT", "120"))

# Vector-space fingerprint — lets the dimension guards refuse to silently mix
# vector spaces across a provider/model/dim change (fail-loud). A non-empty
# DOC_PREFIX changes the STORED vectors, so it extends the fingerprint (":dp=…");
# an empty DOC_PREFIX (the default, incl. bge-m3) leaves the fingerprint unchanged.
# The query-side prefix never touches stored vectors, so it is absent here by design.
FINGERPRINT: str = f"{PROVIDER}:{MODEL}:{DIM}"
if DOC_PREFIX:
    _DP_HASH: str = hashlib.sha256(DOC_PREFIX.encode("utf-8")).hexdigest()[:8]
    FINGERPRINT += f":dp={_DP_HASH}"
