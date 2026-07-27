# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_search_cli.py — the `braincell search` CLI subcommand.

The CLI search path is a thin wrapper over ``server.search_hits`` (the same engine
the ``mcp__braincell__search`` tool uses), so these tests assert connected-Project
resolution, output, ranking selection, retired-selector rejection, and isolation.

NOTE the fixture difference from test_recall_cli.py: `recall` reads curated memory
NOTES (seeded via ``store.remember``), `search` reads ingested document CHUNKS.
Seeding notes here would leave the chunk index empty and every assertion would pass
vacuously — so these tests seed with ``_insert_doc_and_chunk``.

No live Ollama: ``server.embed_query_async`` is monkeypatched to a deterministic
fake vector, and chunks carry distinctive keywords so FTS retrieval is deterministic
regardless of the fake vector ranking.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from braincell.cli import main
from braincell.config import get_db_path, get_project_id
from braincell.store import SqliteStore
from tests.conftest import _insert_doc_and_chunk, fake_vec


# ── Helpers ────────────────────────────────────────────────────────────────────

def _seed_project(root, chunks: list[str]) -> str:
    """Register `root` as a project, build its brain, seed `chunks` as ingested
    document chunks (NOT memory notes — see the module docstring). Return its ULID."""
    root.mkdir(parents=True, exist_ok=True)
    pid = get_project_id(root)  # mints + registers
    db = get_db_path(pid)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.assert_schema_version()

    async def _go() -> None:
        for i, text in enumerate(chunks):
            await _insert_doc_and_chunk(
                store, project=pid, doc_key=f"doc{i}", text=text, seed=i + 1,
            )

    asyncio.run(_go())
    store.close()
    return pid


@pytest.fixture(autouse=True)
def _fast_embed_and_clean_env(monkeypatch):
    """Patch the query embedder to a fast fake (no Ollama), and let monkeypatch own
    BRAINCELL_PROJECT_ID so cmd_search's direct os.environ write is undone per test."""
    async def _fake_embed(text: str):
        return fake_vec(0)

    monkeypatch.setattr("braincell.server.embed_query_async", _fake_embed)
    monkeypatch.delenv("BRAINCELL_PROJECT_ID", raising=False)
    monkeypatch.delenv("BRAINCELL_FEDERATE", raising=False)


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_search_json_shape(tmp_path, capsys):
    root = tmp_path / "repoS"
    _seed_project(root, ["alpha zebra passage about caching layers"])

    main(["search", "zebra", "--path", str(root), "--json"])
    data = json.loads(capsys.readouterr().out)

    assert isinstance(data, list) and len(data) >= 1
    hit = data[0]
    for key in ("chunk_id", "doc_key", "title", "snippet", "score",
                "cosine", "fts_matched", "source_path", "metadata"):
        assert key in hit, f"missing key {key!r} in --json output"


def test_search_human_output(tmp_path, capsys):
    root = tmp_path / "repoSH"
    _seed_project(root, ["gamma passage about vector search internals"])

    main(["search", "gamma", "--path", str(root)])
    out = capsys.readouterr().out
    assert "[doc0]" in out
    assert "gamma passage" in out


def test_search_empty_result_human(tmp_path, capsys):
    root = tmp_path / "repoSE"
    _seed_project(root, [])

    main(["search", "anything", "--path", str(root)])
    assert "(no matching chunks)" in capsys.readouterr().out


def test_search_unregistered_path_errors(tmp_path, capsys):
    fresh = tmp_path / "never_built_search"
    fresh.mkdir()

    with pytest.raises(SystemExit) as exc:
        main(["search", "anything", "--path", str(fresh)])
    assert exc.value.code == 1
    assert "run `braincell build` first" in capsys.readouterr().err


def test_search_rank_keyword_retrieves_by_fts(tmp_path, capsys):
    """--rank keyword must retrieve on the FTS path and flag fts_matched, proving
    the new flag reaches the engine (and is NOT the project/global --mode flag)."""
    root = tmp_path / "repoSK"
    _seed_project(root, ["distinctivezebra passage only findable by keyword"])

    main(["search", "distinctivezebra", "--path", str(root),
          "--rank", "keyword", "--json"])
    data = json.loads(capsys.readouterr().out)

    assert len(data) >= 1, "keyword rank returned nothing for an exact FTS term"
    assert any(h["fts_matched"] for h in data), "no hit flagged fts_matched"


def test_search_rank_and_mode_are_independent_flags(tmp_path, capsys):
    """Regression guard for the flag collision this subcommand was blocked on:
    --rank (ranking strategy) and --mode (project/global brain) must coexist."""
    root = tmp_path / "repoSB"
    _seed_project(root, ["omega passage for the both-flags case"])

    main(["search", "omega", "--path", str(root),
          "--rank", "semantic", "--mode", "project", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list) and len(data) >= 1


def test_search_invalid_rank_rejected_by_argparse(tmp_path):
    """An unknown --rank is an argparse choices error (exit 2), never a traceback."""
    root = tmp_path / "repoSI"
    _seed_project(root, ["some passage"])

    with pytest.raises(SystemExit) as exc:
        main(["search", "x", "--path", str(root), "--rank", "bogus"])
    assert exc.value.code == 2


def test_search_k_out_of_range_errors_cleanly(tmp_path, capsys):
    """Engine-level validation (k must be 1-100) surfaces as SystemExit(2) with a
    clean stderr message, not a traceback — parity with cmd_recall."""
    root = tmp_path / "repoSKk"
    _seed_project(root, ["some passage"])

    with pytest.raises(SystemExit) as exc:
        main(["search", "passage", "--path", str(root), "-k", "0"])
    assert exc.value.code == 2
    assert "braincell search:" in capsys.readouterr().err


def test_search_retired_cross_project_scope_errors(tmp_path, capsys):
    """Ordinary Search refuses the retired cross-Project scope."""
    root = tmp_path / "repoSF"
    _seed_project(root, ["local passage"])

    with pytest.raises(SystemExit) as exc:
        main(["search", "passage", "--path", str(root), "--scope", "family"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "unrecognized arguments: --scope family" in err.lower()


def test_search_retired_scope_cannot_fan_out_when_legacy_flag_is_set(tmp_path, capsys, monkeypatch):
    """A legacy environment flag cannot make ordinary Search open a sibling DB."""
    a = tmp_path / "sfamA"
    b = tmp_path / "sfamB"
    _seed_project(a, ["alpha passage in project A"])
    _seed_project(b, ["distinctivezebra passage only in project B"])
    monkeypatch.setenv("BRAINCELL_FEDERATE", "on")

    with pytest.raises(SystemExit) as exc:
        main(["search", "distinctivezebra", "--path", str(a),
              "--scope", "family", "--json"])
    assert exc.value.code == 2
    assert "unrecognized arguments: --scope family" in capsys.readouterr().err.lower()


def test_search_default_is_pinned_to_connected_project(tmp_path, capsys, monkeypatch):
    """A retired federation environment flag cannot widen default Search."""
    a = tmp_path / "sselfA"
    b = tmp_path / "sselfB"
    _seed_project(a, ["alpha own passage"])
    _seed_project(b, ["distinctivezebra sibling passage"])
    monkeypatch.setenv("BRAINCELL_FEDERATE", "on")

    main(["search", "distinctivezebra", "--path", str(a), "--json"])
    data = json.loads(capsys.readouterr().out)
    blob = " ".join(h["snippet"] for h in data)
    assert "distinctivezebra" not in blob, "connected-Project Search leaked a sibling chunk"
