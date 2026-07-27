# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_recall_cli.py — the `braincell recall` CLI subcommand (A1).

The CLI recall path is a thin wrapper over ``server.recall_notes`` (the same engine
the ``mcp__braincell__recall`` tool uses), so these tests assert connected-Project
resolution, output, retired-selector rejection, and Project isolation.

No live Ollama: ``server.embed_query_async`` is monkeypatched to a deterministic
fake vector, and notes are seeded with distinctive keywords so FTS retrieval is
deterministic regardless of the fake vector ranking.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from braincell.cli import main
from braincell.config import get_db_path, get_project_id
from braincell.store import SqliteStore
from tests.conftest import fake_vec


# ── Helpers ────────────────────────────────────────────────────────────────────

def _seed_project(root, notes: list[str]) -> str:
    """Register `root` as a project, build its brain, seed `notes`. Return its ULID."""
    root.mkdir(parents=True, exist_ok=True)
    pid = get_project_id(root)  # mints + registers
    db = get_db_path(pid)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.assert_schema_version()

    async def _go() -> None:
        for i, text in enumerate(notes):
            await store.remember(text, "note", pid, embedding=fake_vec(i + 1))

    asyncio.run(_go())
    store.close()
    return pid


@pytest.fixture(autouse=True)
def _fast_embed_and_clean_env(monkeypatch):
    """Patch the query embedder to a fast fake (no Ollama), and let monkeypatch own
    BRAINCELL_PROJECT_ID so cmd_recall's direct os.environ write is undone per test."""
    async def _fake_embed(text: str):
        return fake_vec(0)

    monkeypatch.setattr("braincell.server.embed_query_async", _fake_embed)
    monkeypatch.delenv("BRAINCELL_PROJECT_ID", raising=False)
    monkeypatch.delenv("BRAINCELL_FEDERATE", raising=False)


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_recall_json_shape(tmp_path, capsys):
    root = tmp_path / "repoR"
    _seed_project(root, ["alpha zebra decision about caching"])

    main(["recall", "zebra", "--path", str(root), "--json"])
    out = capsys.readouterr().out
    data = json.loads(out)

    assert isinstance(data, list) and len(data) >= 1
    note = data[0]
    for key in ("id", "project_id", "scope", "kind", "content",
                "tags", "confidence", "source_hint", "superseded_by",
                "created_at", "expansion"):
        assert key in note, f"missing key {key!r} in --json output"
    assert "zebra" in note["content"]


def test_recall_human_output(tmp_path, capsys):
    root = tmp_path / "repoH"
    _seed_project(root, ["gamma insight about vector search"])

    main(["recall", "gamma", "--path", str(root)])
    out = capsys.readouterr().out
    assert "[note]" in out
    assert "gamma insight" in out


def test_recall_empty_result_human(tmp_path, capsys):
    # A registered project with a built brain but ZERO notes → recall returns []
    # deterministically → the empty notice prints (human path).
    root = tmp_path / "repoE"
    _seed_project(root, [])

    main(["recall", "anything", "--path", str(root)])
    out = capsys.readouterr().out
    assert "(no matching notes)" in out


def test_recall_unregistered_path_errors(tmp_path, capsys):
    fresh = tmp_path / "never_built"
    fresh.mkdir()

    with pytest.raises(SystemExit) as exc:
        main(["recall", "anything", "--path", str(fresh)])
    assert exc.value.code == 1
    assert "run `braincell build` first" in capsys.readouterr().err


def test_recall_retired_cross_project_scope_errors(tmp_path, capsys):
    """Ordinary Recall refuses the retired cross-Project scope."""
    root = tmp_path / "repoF"
    _seed_project(root, ["local note"])

    with pytest.raises(SystemExit) as exc:
        main(["recall", "note", "--path", str(root), "--scope", "family"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "unrecognized arguments: --scope family" in err.lower()


def test_recall_retired_scope_cannot_fan_out_when_legacy_flag_is_set(tmp_path, capsys, monkeypatch):
    """A legacy environment flag cannot make ordinary Recall open a sibling DB."""
    a = tmp_path / "famA"
    b = tmp_path / "famB"
    _seed_project(a, ["alpha note in project A"])
    _seed_project(b, ["distinctivezebra note only in project B"])
    monkeypatch.setenv("BRAINCELL_FEDERATE", "on")
    with pytest.raises(SystemExit) as exc:
        main(["recall", "distinctivezebra", "--path", str(a),
              "--scope", "family", "--json"])
    assert exc.value.code == 2
    assert "unrecognized arguments: --scope family" in capsys.readouterr().err.lower()


def test_recall_default_is_pinned_to_connected_project(tmp_path, capsys, monkeypatch):
    """A retired federation environment flag cannot widen default Recall."""
    a = tmp_path / "selfA"
    b = tmp_path / "selfB"
    _seed_project(a, ["alpha own note"])
    _seed_project(b, ["distinctivezebra sibling note"])
    monkeypatch.setenv("BRAINCELL_FEDERATE", "on")

    main(["recall", "distinctivezebra", "--path", str(a), "--json"])
    data = json.loads(capsys.readouterr().out)
    contents = " ".join(n["content"] for n in data)
    assert "distinctivezebra" not in contents, "connected-Project Recall leaked a sibling note"
