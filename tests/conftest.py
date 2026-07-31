# SPDX-License-Identifier: AGPL-3.0-or-later
"""
conftest.py — Shared fixtures for braincell regression tests.

Isolation guarantees:
- Every test gets a tmp_path-scoped store (single braincell.db).
- XDG_DATA_HOME is redirected to a temp dir so config.py never touches the
  real ~/.local/share/braincell registry/brains.
- BRAINCELL_DATA_NAMESPACE is set to an inert test namespace so even if a
  stray import reads the env, it hits a throwaway directory.
- No live Ollama: tests that need embeddings build fake unit vectors of
  embed_spec.DIM dimensions and call store.replace_document / store.recall
  directly.
"""

import os
from pathlib import Path

import numpy as np
import pytest

# ── Isolate XDG before any braincell import resolves paths ───────────────────

# Freeze-at-import guard (root cause of the TestApiPool order flake):
# braincell/config.py snapshots BRAINCELL_DATA_NAMESPACE into the module
# constant DATA_NAMESPACE at IMPORT time, and several test modules import
# braincell at module scope — so pytest COLLECTION (which runs before any
# fixture) freezes the constant from the raw shell env ("braincell").
# The per-test fixture below then sets the env to "braincell_test", and helpers
# that read the env at call time (e.g. test_gui._init_global_db) diverge from
# config.get_global_db_path(), which uses the frozen constant. Whether a run
# passed depended on whether test_global.py's importlib.reload(config) happened
# to run first and repair the constant as a side effect.
# conftest.py is imported before any test module is collected, so setting the
# env HERE guarantees every collection-time import freezes the test namespace.
# (embed_spec.py freezes BRAINCELL_EMBED_PROVIDER the same way — mirrored too.)
os.environ["BRAINCELL_DATA_NAMESPACE"] = "braincell_test"
os.environ["BRAINCELL_EMBED_PROVIDER"] = "ollama"


@pytest.fixture(autouse=True)
def isolate_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect all XDG / env lookups to a per-test temp dir.

    autouse=True means EVERY test gets isolation automatically.
    """
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    monkeypatch.setenv("BRAINCELL_DATA_NAMESPACE", "braincell_test")
    # Ensure the env-var-driven embed_spec does NOT default to openai
    monkeypatch.setenv("BRAINCELL_EMBED_PROVIDER", "ollama")


# ── Store lifecycle ───────────────────────────────────────────────────────────

# Stores handed out by make_store() during the current test. Closed at teardown.
#
# Why this exists: only 13 of ~116 make_store() call sites closed their store, so
# each leaked an aiosqlite connection whose worker THREAD outlived the
# asyncio.run() loop that created it. When such a thread later delivered a result
# via `future.get_loop().call_soon_threadsafe(...)` it hit the closed loop and
# raised `RuntimeError: Event loop is closed`, surfacing as
# PytestUnhandledThreadExceptionWarning attributed to whichever test happened to
# be running at the time — not the one that leaked it.
#
# Closing here (no running loop at teardown) drives SqliteStore.close() ->
# asyncio.run(aclose()), which shuts the worker thread down deterministically.
_OPEN_TEST_STORES: list = []


@pytest.fixture(autouse=True)
def _close_stores_after_test():
    """Close every store make_store() handed out, even if the test forgot to."""
    _OPEN_TEST_STORES.clear()
    yield
    while _OPEN_TEST_STORES:
        store = _OPEN_TEST_STORES.pop()
        try:
            store.close()
        except Exception:  # noqa: BLE001, S110 — teardown must never mask a test failure
            pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_store(tmp_path: Path):
    """Return a fresh SqliteStore with schema bootstrapped.

    The store is registered for automatic close at test teardown, so callers do
    not have to remember; an explicit ``store.close()`` remains safe (close is
    idempotent) and is still the right thing when a test asserts on a closed store.
    """
    # Import here (after env is patched by the fixture).
    from braincell.store import SqliteStore

    db_path = tmp_path / "braincell.db"
    store = SqliteStore(db_path)
    store.assert_schema_version()
    _OPEN_TEST_STORES.append(store)
    return store


def fake_vec(seed: int = 0) -> np.ndarray:
    """Return a deterministic unit float32 vector of embed_spec.DIM dimensions."""
    from braincell import embed_spec
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(embed_spec.DIM).astype(np.float32)
    return v / np.linalg.norm(v)


async def _insert_doc_and_chunk(store, *, project: str, doc_key: str, text: str,
                                 seed: int = 0):
    """Insert one document + one chunk with a fake embedding (helper for tests)."""
    import hashlib

    doc_id, _changed = await store.replace_document(
        project_id=project, doc_key=doc_key, title=doc_key,
        content_hash=hashlib.sha256(text.encode()).digest(),
        content_type="cell",
        chunks=[(text, fake_vec(seed))],
    )
    return doc_id
