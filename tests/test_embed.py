# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_embed.py — Regression tests for braincell/embed.py.

No live Ollama required.

Patching note: `ollama`, `httpx`, and `openai.OpenAI` are lazy-imported INSIDE
the function bodies of _embed_ollama / _embed_openai (PLC0415 pattern).  They
are NOT module-level attributes of braincell.embed, so patch("braincell.embed.ollama")
doesn't work.  The correct approach is patch.dict(sys.modules, ...) which
intercepts the `import <name>` call inside the function before CPython falls
through to the real package.
"""

from __future__ import annotations

import importlib
import sys
import types
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from braincell import embed_spec
from braincell.embed import _batched_by_size, embed_query, embed_texts


def _reload_embed_spec():
    """Reload braincell.embed_spec so its module-level MODEL/DIM/FINGERPRINT
    constants are recomputed from the CURRENT environment.

    embed_spec reads BRAINCELL_EMBED_MODEL / BRAINCELL_EMBED_DIM at import
    time, so changing the env alone (e.g. via monkeypatch.setenv) does not
    change the already-imported module's attributes — a reload is required.
    Because importlib.reload() mutates the SAME module object in place,
    braincell.embed's `from . import embed_spec` reference stays valid and
    picks up the new values immediately (no reload of embed.py needed).

    Callers MUST restore the baseline afterwards (monkeypatch.undo() then
    reload again) so later tests don't inherit a mutated embed_spec.
    """
    return importlib.reload(embed_spec)


# ── shared helper: build a mock ollama module ─────────────────────────────────

def _mock_ollama_module(client_instance: MagicMock) -> MagicMock:
    """Return a fake `ollama` module (a MagicMock with ResponseError = RuntimeError)."""
    mod = MagicMock()
    mod.Client.return_value = client_instance
    mod.ResponseError = RuntimeError  # any exception class
    return mod


def _mock_httpx_module() -> MagicMock:
    """Return a fake `httpx` module with TransportError = RuntimeError."""
    mod = MagicMock()
    mod.TransportError = RuntimeError
    return mod


def _good_ollama_response(n: int = 1, seed: int = 0) -> MagicMock:
    """A mock ollama embed() response with n unit vectors of embed_spec.DIM."""
    vecs = [
        np.random.default_rng(seed + i).standard_normal(embed_spec.DIM).tolist()
        for i in range(n)
    ]
    resp = MagicMock()
    resp.embeddings = vecs
    return resp


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1: _batched_by_size
# ═══════════════════════════════════════════════════════════════════════════════

class TestBatchedBySize:
    """_batched_by_size splits correctly by count and by chars."""

    def test_splits_by_count(self):
        texts = [f"item{i}" for i in range(10)]
        batches = list(_batched_by_size(texts, max_inputs=3, max_chars=999_999))
        assert len(batches) == 4
        flat = [t for b in batches for t in b]
        assert flat == texts
        for b in batches[:-1]:
            assert len(b) == 3

    def test_splits_by_chars(self):
        # 5 texts each 10 chars; max_chars=25 means items 0+1 fit (20), item 2 overflows
        texts = ["0123456789"] * 5  # each 10 chars
        batches = list(_batched_by_size(texts, max_inputs=100, max_chars=25))
        # Batches: [0,1] → [2,3] → [4]
        assert len(batches) == 3
        flat = [t for b in batches for t in b]
        assert flat == texts

    def test_empty_input(self):
        batches = list(_batched_by_size([], max_inputs=5, max_chars=1000))
        assert batches == []

    def test_single_item(self):
        batches = list(_batched_by_size(["hello"], max_inputs=10, max_chars=100))
        assert batches == [["hello"]]

    def test_count_and_chars_both_respected(self):
        """Flush should trigger on EITHER limit, whichever is hit first."""
        texts = ["ab"] * 6  # 6 items, 2 chars each
        batches = list(_batched_by_size(texts, max_inputs=3, max_chars=4))
        for b in batches:
            total_chars = sum(len(t) for t in b)
            assert total_chars <= 4
            assert len(b) <= 3

    def test_all_items_preserved(self):
        """No item should be lost or duplicated across batches."""
        texts = [f"text-{i}" for i in range(37)]
        batches = list(_batched_by_size(texts, max_inputs=7, max_chars=50_000))
        flat = [t for b in batches for t in b]
        assert flat == texts


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2: provider output contract
# ═══════════════════════════════════════════════════════════════════════════════

class TestZeroNormVector:
    """Degenerate provider output fails before it can pollute stored vectors."""

    def test_zero_norm_is_rejected(self):
        good_vec = np.random.default_rng(1).standard_normal(embed_spec.DIM).astype(np.float32)
        zero_vec = np.zeros(embed_spec.DIM, dtype=np.float32)
        raw = [good_vec.tolist(), zero_vec.tolist(), good_vec.tolist()]

        mock_resp = MagicMock()
        mock_resp.embeddings = raw
        client = MagicMock()
        client.embed.return_value = mock_resp

        mock_ol = _mock_ollama_module(client)
        mock_hx = _mock_httpx_module()

        with patch.dict(sys.modules, {"ollama": mock_ol, "httpx": mock_hx}), \
             pytest.raises(ValueError, match="zero-norm"):
            embed_texts(["text0", "text1", "text2"])

    @pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
    def test_non_finite_vector_is_rejected(self, bad):
        raw = [np.full(embed_spec.DIM, bad, dtype=np.float32).tolist()]

        mock_resp = MagicMock()
        mock_resp.embeddings = raw
        client = MagicMock()
        client.embed.return_value = mock_resp

        mock_ol = _mock_ollama_module(client)
        mock_hx = _mock_httpx_module()

        with patch.dict(sys.modules, {"ollama": mock_ol, "httpx": mock_hx}), \
             pytest.raises(ValueError, match="non-finite"):
            embed_texts(["bad"])

    def test_provider_cardinality_mismatch_is_rejected(self):
        good = np.ones(embed_spec.DIM, dtype=np.float32).tolist()
        mock_resp = MagicMock()
        mock_resp.embeddings = [good]
        client = MagicMock()
        client.embed.return_value = mock_resp

        with patch.dict(
            sys.modules,
            {
                "ollama": _mock_ollama_module(client),
                "httpx": _mock_httpx_module(),
            },
        ), pytest.raises(ValueError, match="1 embeddings for 2 inputs"):
            embed_texts(["first", "second"])


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3: Ollama retry behaviour
# ═══════════════════════════════════════════════════════════════════════════════

class TestOllamaRetry:
    """Transient error retries up to 3x then raises a branded RuntimeError naming
    host+model; a dimension mismatch fails fast (no retry)."""

    def test_transient_error_retries_3x_then_raises_runtime_error(self):
        """After MAX_RETRIES (3) consecutive failures, must raise RuntimeError."""
        client = MagicMock()
        client.embed.side_effect = ConnectionError("always fails")

        mock_ol = _mock_ollama_module(client)
        mock_hx = _mock_httpx_module()
        # ConnectionError must be in the caught exception tuple.
        mock_ol.ResponseError = ConnectionError
        mock_hx.TransportError = ConnectionError

        with patch.dict(sys.modules, {"ollama": mock_ol, "httpx": mock_hx}), \
             patch("braincell.embed.time") as mock_time:
            mock_time.sleep = MagicMock()
            with pytest.raises(RuntimeError) as exc_info:
                embed_texts(["hello"])

        err = str(exc_info.value)
        # Branded error: must name host AND model.
        assert embed_spec.MODEL in err, f"model not in error: {err}"
        assert ("host=" in err or "localhost" in err), f"host not in error: {err}"
        # Must have been called exactly 3 times (MAX_RETRIES).
        assert client.embed.call_count == 3

    def test_transient_error_succeeds_after_one_retry(self):
        """If the 2nd attempt succeeds, embed_texts must return without raising."""
        good_vec = np.random.default_rng(3).standard_normal(embed_spec.DIM).astype(np.float32).tolist()
        mock_resp = MagicMock()
        mock_resp.embeddings = [good_vec]

        call_count = {"n": 0}

        def side_effect(**kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ConnectionError("transient")
            return mock_resp

        client = MagicMock()
        client.embed.side_effect = side_effect

        mock_ol = _mock_ollama_module(client)
        mock_hx = _mock_httpx_module()
        mock_ol.ResponseError = ConnectionError
        mock_hx.TransportError = ConnectionError

        with patch.dict(sys.modules, {"ollama": mock_ol, "httpx": mock_hx}), \
             patch("braincell.embed.time") as mock_time:
            mock_time.sleep = MagicMock()
            result = embed_texts(["hello"])

        assert len(result) == 1
        assert call_count["n"] == 2

    def test_dimension_mismatch_fails_fast_no_retry(self):
        """A dimension mismatch in the returned vector must raise ValueError immediately,
        without retrying (the mismatch is not a transient connectivity error)."""
        wrong_dim_vec = np.ones(embed_spec.DIM + 5, dtype=np.float32).tolist()
        mock_resp = MagicMock()
        mock_resp.embeddings = [wrong_dim_vec]
        client = MagicMock()
        client.embed.return_value = mock_resp

        mock_ol = _mock_ollama_module(client)
        mock_hx = _mock_httpx_module()

        with (
            patch.dict(sys.modules, {"ollama": mock_ol, "httpx": mock_hx}),
            pytest.raises(ValueError, match="embed dimension mismatch"),
        ):
            embed_texts(["hello"])

        # Only one attempt — dimension mismatch is NOT retried.
        assert client.embed.call_count == 1


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4: _embed_openai dimensions kwarg
# ═══════════════════════════════════════════════════════════════════════════════

class TestEmbedOpenai:
    """_embed_openai passes dimensions= only for text-embedding-3-* models."""

    def _mock_openai_module(self, client_instance: MagicMock) -> MagicMock:
        """Fake `openai` module whose OpenAI(...) returns client_instance."""
        mod = MagicMock()
        mod.OpenAI.return_value = client_instance
        return mod

    def _mock_response(self, dim: int) -> MagicMock:
        vec = np.random.default_rng(4).standard_normal(dim).astype(np.float32).tolist()
        emb = MagicMock()
        emb.embedding = vec
        emb.index = 0
        resp = MagicMock()
        resp.data = [emb]
        return resp

    def test_text_embedding_3_model_passes_dimensions_kwarg(self, monkeypatch):
        """For 'text-embedding-3-small', dimensions= must be passed to the API."""
        from braincell.embed import _embed_openai

        # Use a SimpleNamespace to represent embed_spec for this test — avoids
        # the reload-and-global-state mess from importlib.reload(embed_spec).
        mock_spec = types.SimpleNamespace(
            PROVIDER="openai",
            MODEL="text-embedding-3-small",
            DIM=1536,
            FINGERPRINT="openai:text-embedding-3-small:1536",
            KEEP_ALIVE="0",
            OLLAMA_TIMEOUT=120.0,
        )
        client_instance = MagicMock()
        client_instance.embeddings.create.return_value = self._mock_response(1536)
        mock_openai = self._mock_openai_module(client_instance)

        # Patch embed_spec at braincell.embed module level AND intercept
        # 'from openai import OpenAI' via sys.modules.
        with patch("braincell.embed.embed_spec", mock_spec), \
             patch.dict(sys.modules, {"openai": mock_openai}):
            _embed_openai(["test text"])

        _, kwargs = client_instance.embeddings.create.call_args
        assert "dimensions" in kwargs, \
            f"dimensions= NOT passed for text-embedding-3-* model; kwargs={kwargs}"
        assert kwargs["dimensions"] == 1536

    def test_legacy_ada_model_does_not_pass_dimensions_kwarg(self, monkeypatch):
        """For 'text-embedding-ada-002', dimensions= must NOT be passed."""
        from braincell.embed import _embed_openai

        mock_spec = types.SimpleNamespace(
            PROVIDER="openai",
            MODEL="text-embedding-ada-002",
            DIM=1536,
            FINGERPRINT="openai:text-embedding-ada-002:1536",
            KEEP_ALIVE="0",
            OLLAMA_TIMEOUT=120.0,
        )
        client_instance = MagicMock()
        client_instance.embeddings.create.return_value = self._mock_response(1536)
        mock_openai = self._mock_openai_module(client_instance)

        with patch("braincell.embed.embed_spec", mock_spec), \
             patch.dict(sys.modules, {"openai": mock_openai}):
            _embed_openai(["test text"])

        _, kwargs = client_instance.embeddings.create.call_args
        assert "dimensions" not in kwargs, \
            f"dimensions= incorrectly passed for ada-002 model; kwargs={kwargs}"


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5: embed_spec env-configurability (regression: commit 5a72d69 bumped
# the default Ollama model 0.6b -> 4b but left embed_spec.DIM hardcoded at 1024,
# so 4b's native 2560-d output tripped the dim guard on EVERY build. Both MODEL
# and DIM must be independently env-configurable so either embedding version is
# selectable, and the FINGERPRINT must reflect whichever is active.)
# ═══════════════════════════════════════════════════════════════════════════════

class TestEmbedSpecEnvConfigurable:
    """embed_spec.MODEL / DIM / FINGERPRINT are env-configurable for both the
    0.6b (1024-d native) and 4b (2560-d native, MRL-truncatable) versions."""

    def test_configurable_as_0_6b_1024(self, monkeypatch):
        monkeypatch.setenv("BRAINCELL_EMBED_MODEL", "qwen3-embedding:0.6b")
        monkeypatch.setenv("BRAINCELL_EMBED_DIM", "1024")
        try:
            mod = _reload_embed_spec()
            assert mod.MODEL == "qwen3-embedding:0.6b"
            assert mod.DIM == 1024
            assert mod.FINGERPRINT == "ollama:qwen3-embedding:0.6b:1024"
        finally:
            monkeypatch.undo()
            _reload_embed_spec()

    def test_configurable_as_4b_2560(self, monkeypatch):
        monkeypatch.setenv("BRAINCELL_EMBED_MODEL", "qwen3-embedding:4b")
        monkeypatch.setenv("BRAINCELL_EMBED_DIM", "2560")
        try:
            mod = _reload_embed_spec()
            assert mod.MODEL == "qwen3-embedding:4b"
            assert mod.DIM == 2560
            assert mod.FINGERPRINT == "ollama:qwen3-embedding:4b:2560"
        finally:
            monkeypatch.undo()
            _reload_embed_spec()

    def test_fingerprint_differs_between_the_two_versions(self, monkeypatch):
        """Confirms both versions are independently selectable: same PROVIDER,
        different (MODEL, DIM) must yield different FINGERPRINTs so the store/
        federation never silently mix the two vector spaces (Rule #6)."""
        try:
            monkeypatch.setenv("BRAINCELL_EMBED_MODEL", "qwen3-embedding:0.6b")
            monkeypatch.setenv("BRAINCELL_EMBED_DIM", "1024")
            fp_small = _reload_embed_spec().FINGERPRINT

            monkeypatch.setenv("BRAINCELL_EMBED_MODEL", "qwen3-embedding:4b")
            monkeypatch.setenv("BRAINCELL_EMBED_DIM", "2560")
            fp_large = _reload_embed_spec().FINGERPRINT

            assert fp_small != fp_large
        finally:
            monkeypatch.undo()
            _reload_embed_spec()


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6: THE regression — dimensions= must reach ollama client.embed()
# ═══════════════════════════════════════════════════════════════════════════════

class TestOllamaDimensionsKwargPassed:
    """Regression target for commit 5a72d69: _embed_ollama MUST pass
    dimensions=embed_spec.DIM to client.embed() so an MRL model
    (qwen3-embedding:4b, 2560-d native) is asked to truncate to embed_spec.DIM
    instead of returning its native width and tripping the dim guard.

    Pre-fix code never passed `dimensions=` at all — this test fails against
    that code (KeyError / assertion on a missing kwarg) and passes against the
    fix in braincell/embed.py::_embed_ollama.
    """

    def test_dimensions_kwarg_passed_to_ollama_embed(self):
        good_vec = (
            np.random.default_rng(7)
            .standard_normal(embed_spec.DIM)
            .astype(np.float32)
            .tolist()
        )
        mock_resp = MagicMock()
        mock_resp.embeddings = [good_vec]
        client = MagicMock()
        client.embed.return_value = mock_resp

        mock_ol = _mock_ollama_module(client)
        mock_hx = _mock_httpx_module()

        with patch.dict(sys.modules, {"ollama": mock_ol, "httpx": mock_hx}):
            embed_texts(["hello"])

        assert client.embed.call_count == 1, "no live Ollama call expected/made"
        _, kwargs = client.embed.call_args
        assert "dimensions" in kwargs, (
            "dimensions= NOT passed to ollama client.embed() — this is the "
            "commit 5a72d69 regression (MRL truncation never requested, so a "
            "native-width model output would trip the dim guard on every build)."
        )
        assert kwargs["dimensions"] == embed_spec.DIM


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7: dim guard behaviour under a reloaded (model, dim) configuration
# ═══════════════════════════════════════════════════════════════════════════════

class TestOllamaDimGuardWithConfiguredDim:
    """With embed_spec reloaded to the 4b / 2560-d config: a native 2560-d
    response is accepted and unit-normalised (no false-positive guard trip);
    a genuine width mismatch still fails loud (Rule #6 guard stays intact)."""

    def test_native_2560_accepted_when_dim_configured_to_2560(self, monkeypatch):
        monkeypatch.setenv("BRAINCELL_EMBED_MODEL", "qwen3-embedding:4b")
        monkeypatch.setenv("BRAINCELL_EMBED_DIM", "2560")
        try:
            _reload_embed_spec()

            vec = (
                np.random.default_rng(9)
                .standard_normal(2560)
                .astype(np.float32)
                .tolist()
            )
            mock_resp = MagicMock()
            mock_resp.embeddings = [vec]
            client = MagicMock()
            client.embed.return_value = mock_resp

            mock_ol = _mock_ollama_module(client)
            mock_hx = _mock_httpx_module()

            with patch.dict(sys.modules, {"ollama": mock_ol, "httpx": mock_hx}):
                result = embed_texts(["hello"])

            assert len(result) == 1
            assert result[0].shape[0] == 2560
            assert np.isclose(np.linalg.norm(result[0]), 1.0, atol=1e-5)
            _, kwargs = client.embed.call_args
            assert kwargs["dimensions"] == 2560
        finally:
            monkeypatch.undo()
            _reload_embed_spec()

    def test_dim_guard_fails_loud_on_genuine_mismatch_at_2560(self, monkeypatch):
        monkeypatch.setenv("BRAINCELL_EMBED_MODEL", "qwen3-embedding:4b")
        monkeypatch.setenv("BRAINCELL_EMBED_DIM", "2560")
        try:
            _reload_embed_spec()

            wrong_dim_vec = np.ones(2560 + 1, dtype=np.float32).tolist()
            mock_resp = MagicMock()
            mock_resp.embeddings = [wrong_dim_vec]
            client = MagicMock()
            client.embed.return_value = mock_resp

            mock_ol = _mock_ollama_module(client)
            mock_hx = _mock_httpx_module()

            with (
                patch.dict(sys.modules, {"ollama": mock_ol, "httpx": mock_hx}),
                pytest.raises(ValueError, match="embed dimension mismatch"),
            ):
                embed_texts(["hello"])

            assert client.embed.call_count == 1, "mismatch must not be retried"
        finally:
            monkeypatch.undo()
            _reload_embed_spec()


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8: P0-2 asymmetric query/document prefix injection
# ═══════════════════════════════════════════════════════════════════════════════

# Deliberately mid-file: grouped with the P0-2 section these imports support.
import hashlib

from braincell import embed as _embmod


def _configure(monkeypatch, model: str, dim: int):
    """Reload embed_spec for (model, dim) and clear the query-embed cache."""
    monkeypatch.setenv("BRAINCELL_EMBED_MODEL", model)
    monkeypatch.setenv("BRAINCELL_EMBED_DIM", str(dim))
    mod = _reload_embed_spec()
    _embmod._embed_query_cached.cache_clear()
    return mod


def _captured_input(client) -> list[str]:
    """The `input=` list passed to the last client.embed() call."""
    _, kwargs = client.embed.call_args
    return kwargs["input"]


class TestPrefixRegistryResolution:
    """embed_spec resolves per-model QUERY_PREFIX / DOC_PREFIX and reflects the
    doc-prefix (only) in FINGERPRINT."""

    def test_default_qwen3_4b_query_prefix_fingerprint_unchanged(self, monkeypatch):
        """Code default (no env override): qwen3-embedding:4b @ 1024 — asymmetric,
        non-empty QUERY_PREFIX, empty DOC_PREFIX, FINGERPRINT byte-identical since
        only DOC_PREFIX can extend it."""
        monkeypatch.delenv("BRAINCELL_EMBED_MODEL", raising=False)
        monkeypatch.delenv("BRAINCELL_EMBED_DIM", raising=False)
        try:
            mod = _reload_embed_spec()
            assert mod.MODEL == "qwen3-embedding:4b"
            assert mod.DIM == 1024
            assert mod.QUERY_PREFIX.startswith("Instruct: ")
            assert mod.DOC_PREFIX == ""
            assert mod.FINGERPRINT == "ollama:qwen3-embedding:4b:1024"
        finally:
            monkeypatch.undo()
            _reload_embed_spec()

    def test_bge_m3_no_prefixes_fingerprint_unchanged(self, monkeypatch):
        """bge-m3 via explicit env override: both prefixes empty, FINGERPRINT
        byte-identical (symmetric model, no-op embed path)."""
        try:
            mod = _configure(monkeypatch, "bge-m3", 1024)
            assert mod.QUERY_PREFIX == ""
            assert mod.DOC_PREFIX == ""
            assert mod.FINGERPRINT == "ollama:bge-m3:1024"
        finally:
            monkeypatch.undo()
            _reload_embed_spec()

    def test_qwen_query_template_docs_bare_fingerprint_unchanged(self, monkeypatch):
        """qwen3-embedding (both sizes): Instruct/Query template on the query,
        bare documents, so FINGERPRINT has NO :dp suffix."""
        try:
            for size, dim in (("qwen3-embedding:4b", 2560), ("qwen3-embedding:0.6b", 1024)):
                mod = _configure(monkeypatch, size, dim)
                assert mod.QUERY_PREFIX.startswith("Instruct: ")
                assert mod.QUERY_PREFIX.endswith("\nQuery: ")
                assert "same underlying engineering lesson" in mod.QUERY_PREFIX
                assert mod.DOC_PREFIX == ""
                assert mod.FINGERPRINT == f"ollama:{size}:{dim}"
                assert ":dp=" not in mod.FINGERPRINT
        finally:
            monkeypatch.undo()
            _reload_embed_spec()

    def test_nomic_both_prefixes_fingerprint_gains_dp(self, monkeypatch):
        """nomic-embed-text: search_query:/search_document: prefixes; the non-empty
        DOC_PREFIX extends FINGERPRINT with the sha256[:8] of the doc prefix."""
        try:
            mod = _configure(monkeypatch, "nomic-embed-text", 768)
            assert mod.QUERY_PREFIX == "search_query: "
            assert mod.DOC_PREFIX == "search_document: "
            expect = hashlib.sha256(b"search_document: ").hexdigest()[:8]
            assert mod.FINGERPRINT == f"ollama:nomic-embed-text:768:dp={expect}"
        finally:
            monkeypatch.undo()
            _reload_embed_spec()

    def test_mxbai_query_only_fingerprint_unchanged(self, monkeypatch):
        try:
            mod = _configure(monkeypatch, "mxbai-embed-large", 1024)
            assert mod.QUERY_PREFIX.startswith("Represent this sentence")
            assert mod.DOC_PREFIX == ""
            assert mod.FINGERPRINT == "ollama:mxbai-embed-large:1024"
        finally:
            monkeypatch.undo()
            _reload_embed_spec()

    def test_env_override_wins_over_registry(self, monkeypatch):
        """Explicit env prefixes override the registry; a doc-prefix override
        re-hashes into FINGERPRINT; an explicit empty doc-prefix removes :dp."""
        try:
            monkeypatch.setenv("BRAINCELL_QUERY_PREFIX", "Q: ")
            monkeypatch.setenv("BRAINCELL_DOC_PREFIX", "D: ")
            mod = _configure(monkeypatch, "nomic-embed-text", 768)
            assert mod.QUERY_PREFIX == "Q: "
            assert mod.DOC_PREFIX == "D: "
            expect = hashlib.sha256(b"D: ").hexdigest()[:8]
            assert mod.FINGERPRINT == f"ollama:nomic-embed-text:768:dp={expect}"

            # Explicit empty doc-prefix override disables the doc prefix entirely.
            monkeypatch.setenv("BRAINCELL_DOC_PREFIX", "")
            mod = _reload_embed_spec()
            assert mod.DOC_PREFIX == ""
            assert mod.FINGERPRINT == "ollama:nomic-embed-text:768"
        finally:
            monkeypatch.undo()
            _reload_embed_spec()


class TestPrefixApplicationInEmbedPath:
    """The prefixes actually reach the embedder: DOC_PREFIX on the document path
    (embed_texts), QUERY_PREFIX on the query path (embed_query), never crossed."""

    def _wire(self, dim: int):
        client = MagicMock()
        client.embed.return_value = _good_ollama_response(1, seed=11)
        # _good_ollama_response sizes to embed_spec.DIM which we've reloaded to `dim`.
        assert len(client.embed.return_value.embeddings[0]) == dim
        return client

    def test_bge_m3_is_noop_no_prefix_applied(self, monkeypatch):
        try:
            _configure(monkeypatch, "bge-m3", 1024)
            client = self._wire(1024)
            mock_ol = _mock_ollama_module(client)
            mock_hx = _mock_httpx_module()
            with patch.dict(sys.modules, {"ollama": mock_ol, "httpx": mock_hx}):
                embed_texts(["hello"])
                assert _captured_input(client) == ["hello"]
                embed_query("hello")
                assert _captured_input(client) == ["hello"]
        finally:
            monkeypatch.undo()
            _reload_embed_spec()

    def test_default_qwen3_4b_query_prefixed_documents_bare(self, monkeypatch):
        """Code default (no env override): query gets the Instruct/Query template,
        documents stay bare — the default is no longer a fully no-op path."""
        monkeypatch.delenv("BRAINCELL_EMBED_MODEL", raising=False)
        monkeypatch.delenv("BRAINCELL_EMBED_DIM", raising=False)
        try:
            mod = _reload_embed_spec()
            _embmod._embed_query_cached.cache_clear()
            client = self._wire(1024)
            mock_ol = _mock_ollama_module(client)
            mock_hx = _mock_httpx_module()
            with patch.dict(sys.modules, {"ollama": mock_ol, "httpx": mock_hx}):
                embed_texts(["a document"])
                assert _captured_input(client) == ["a document"]  # bare
                embed_query("my query")
                assert _captured_input(client) == [mod.QUERY_PREFIX + "my query"]
        finally:
            monkeypatch.undo()
            _reload_embed_spec()

    def test_nomic_applies_distinct_doc_and_query_prefixes(self, monkeypatch):
        try:
            _configure(monkeypatch, "nomic-embed-text", 768)
            client = self._wire(768)
            mock_ol = _mock_ollama_module(client)
            mock_hx = _mock_httpx_module()
            with patch.dict(sys.modules, {"ollama": mock_ol, "httpx": mock_hx}):
                embed_texts(["hello"])
                assert _captured_input(client) == ["search_document: hello"]
                embed_query("hello")
                assert _captured_input(client) == ["search_query: hello"]
        finally:
            monkeypatch.undo()
            _reload_embed_spec()

    def test_qwen_query_prefixed_documents_bare(self, monkeypatch):
        try:
            mod = _configure(monkeypatch, "qwen3-embedding:0.6b", 1024)
            client = self._wire(1024)
            mock_ol = _mock_ollama_module(client)
            mock_hx = _mock_httpx_module()
            with patch.dict(sys.modules, {"ollama": mock_ol, "httpx": mock_hx}):
                embed_texts(["a document"])
                assert _captured_input(client) == ["a document"]  # bare
                embed_query("my query")
                assert _captured_input(client) == [mod.QUERY_PREFIX + "my query"]
        finally:
            monkeypatch.undo()
            _reload_embed_spec()
