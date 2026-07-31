# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_stats.py — B2 vector-search instrumentation + `braincell stats`.

The sqlite-vec (vec0) ANN backend itself is DEFERRED behind the supply-chain
gate; what ships now is the measurement that makes the adopt-decision data-driven:
a per-session p95 of the brute-force decode+matmul, surfaced by `braincell stats`.
"""

from __future__ import annotations

import asyncio

import pytest

from braincell import store as store_mod
from tests.conftest import _insert_doc_and_chunk, make_store


def _run(coro):
    return asyncio.run(coro)


def test_p95_none_before_any_search(tmp_path):
    s = make_store(tmp_path)
    assert s.vec_search_p95_ms() is None


def test_p95_computation_on_known_samples(tmp_path):
    s = make_store(tmp_path)
    for v in [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]:
        s._record_vec_search_ms(v)
    # p95 index of 10 samples = round(0.95*9)=9 → the max (10.0)
    assert s.vec_search_p95_ms() == pytest.approx(10.0)


def test_search_records_timing(tmp_path):
    s = make_store(tmp_path)

    async def go():
        await _insert_doc_and_chunk(s, project="P", doc_key="d1", text="alpha beta", seed=1)
        import numpy as np

        from braincell import embed_spec
        v = np.zeros(embed_spec.DIM, dtype=np.float32)
        v[0] = 1.0
        await s.search(v, "", None, 5, "semantic")
        p95 = s.vec_search_p95_ms()
        await s.aclose()
        return p95

    p95 = _run(go())
    assert p95 is not None and p95 >= 0.0


def test_default_backend_is_bruteforce():
    assert store_mod._BACKEND == "bruteforce"


def test_cli_stats_prints_counts(tmp_path, capsys):
    from braincell.cli import main
    from braincell.config import get_db_path
    from braincell.project_registry import register_path
    from braincell.store import SqliteStore

    root = tmp_path / "repoS"
    root.mkdir()
    register_path(str(root), "PROJSTATS")

    async def build():
        src = SqliteStore(get_db_path("PROJSTATS"))
        src.assert_schema_version()
        await _insert_doc_and_chunk(src, project="PROJSTATS", doc_key="d1",
                                    text="alpha beta gamma", seed=1)
        await src.aclose()

    _run(build())
    main(["stats", str(root), "--iters", "3"])
    out = capsys.readouterr().out
    assert "chunks: 1" in out
    assert "vector-search p95" in out
