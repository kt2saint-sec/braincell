# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_federate.py — opt-in federated family recall (braincell/federate.py).

Covers the corrected v2 design + the gap-analysis must-fixes:
  - build_federation_plan gating (off / global mode / unset PID) — invariant #1.
  - resolve_federation_targets: family members only, seed first, schema/brain gating.
  - Weighted RRF merge by rank + deterministic tie-break; active-project weighting.
  - Content-hash cross-store dedup.
  - Fault isolation: a missing/failing sibling is skipped, never fatal (GAP 4).
  - Read-only siblings under a LIVE concurrent writer: fresh reads, no write (GAP 1/2).
  - Fingerprint mismatch → lexical-only contribution (never mixes vector spaces).
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from braincell.config import (
    get_db_path,
    get_families_path,
    get_path_registry_path,
    get_project_id,
)
from braincell.federate import (
    _dedup_by_content,
    _dedup_hits,
    _rrf_merge_hits,
    _rrf_merge_notes,
    build_federation_plan,
    federated_recall,
    federated_search,
    plan_for_seed,
    resolve_federation_targets,
)
from braincell.project_registry import add_family_members
from braincell.store import Hit, Note, SqliteStore
from tests.conftest import _insert_doc_and_chunk, fake_vec


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_member(root: Path, notes: list[str]) -> str:
    """Register `root` as a project, build its brain, seed `notes`. Return its ULID."""
    pid = get_project_id(root)  # mints + registers in the path-registry
    db = get_db_path(pid)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.assert_schema_version()

    async def _seed() -> None:
        for i, text in enumerate(notes):
            await store.remember(text, "note", pid, embedding=fake_vec(i + 1))

    asyncio.run(_seed())
    store.close()
    return pid


def _make_search_member(root: Path, docs: list[tuple[str, str]]) -> str:
    """Register `root`, build its brain, seed (doc_key, text) chunks. Return ULID."""
    pid = get_project_id(root)
    db = get_db_path(pid)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.assert_schema_version()

    async def _seed() -> None:
        for i, (doc_key, text) in enumerate(docs):
            await _insert_doc_and_chunk(store, project=pid, doc_key=doc_key, text=text, seed=i + 1)

    asyncio.run(_seed())
    store.close()
    return pid


def _hit(chunk_id: int, doc_key: str, snippet: str) -> Hit:
    return Hit(chunk_id=chunk_id, doc_key=doc_key, title=doc_key, snippet=snippet, score=0.0)


def _note(pid: str, nid: int, content: str) -> Note:
    return Note(
        id=nid, project_id=pid, scope="project", kind="note", content=content,
        tags=[], confidence=None, source_hint=None, superseded_by=None,
        created_at="2026-07-04 00:00:00",
    )


def _enable_federation(monkeypatch, seed_pid: str) -> None:
    monkeypatch.setenv("BRAINCELL_FEDERATE", "on")
    monkeypatch.setenv("BRAINCELL_PROJECT_ID", seed_pid)
    monkeypatch.delenv("BRAINCELL_MODE", raising=False)  # → project (default)


# ── build_federation_plan gating (invariant #1) ────────────────────────────────

class TestBuildPlanGating:
    def test_off_returns_none(self, monkeypatch):
        monkeypatch.delenv("BRAINCELL_FEDERATE", raising=False)
        assert build_federation_plan(None, "family", None) is None

    def test_self_scope_returns_none(self, monkeypatch):
        monkeypatch.setenv("BRAINCELL_FEDERATE", "on")
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", "01SEED0000000000000000001A")
        assert build_federation_plan(None, "self", None) is None

    def test_explicit_project_returns_none(self, monkeypatch):
        monkeypatch.setenv("BRAINCELL_FEDERATE", "on")
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", "01SEED0000000000000000001A")
        assert build_federation_plan("01X", "family", None) is None

    def test_global_mode_returns_none(self, monkeypatch):
        monkeypatch.setenv("BRAINCELL_FEDERATE", "on")
        monkeypatch.setenv("BRAINCELL_MODE", "global")
        monkeypatch.setenv("BRAINCELL_PROJECT_ID", "01SEED0000000000000000001A")
        assert build_federation_plan(None, "family", None) is None

    def test_pid_unset_raises(self, monkeypatch):
        monkeypatch.setenv("BRAINCELL_FEDERATE", "on")
        monkeypatch.delenv("BRAINCELL_MODE", raising=False)
        monkeypatch.delenv("BRAINCELL_PROJECT_ID", raising=False)
        with pytest.raises(ValueError, match="BRAINCELL_PROJECT_ID"):
            build_federation_plan(None, "family", None)


# ── resolve_federation_targets ─────────────────────────────────────────────────

class TestResolveTargets:
    def test_family_members_only_seed_first(self, tmp_path, monkeypatch):
        a = tmp_path / "projA"
        b = tmp_path / "projB"
        a.mkdir()
        b.mkdir()
        pid_a = _make_member(a, ["alpha note"])
        pid_b = _make_member(b, ["beta note"])
        add_family_members("fam", [str(a), str(b)])
        _enable_federation(monkeypatch, pid_a)

        targets = resolve_federation_targets(pid_a)
        pids = [t.project_id for t in targets]
        assert pids[0] == pid_a, "seed must be first"
        assert set(pids) == {pid_a, pid_b}
        assert all(t.fingerprint_ok for t in targets), "same-engine builds match fingerprint"

    def test_corrupt_member_at_resolve_time_not_fatal(self, tmp_path, monkeypatch):
        """Boris obj.#1: a sibling corrupted BEFORE planning must be skipped, not
        raise DatabaseError out of resolve_federation_targets."""
        a = tmp_path / "projA"
        b = tmp_path / "projB"
        a.mkdir()
        b.mkdir()
        pid_a = _make_member(a, ["alpha note"])
        pid_b = _make_member(b, ["beta note"])
        # Corrupt B's brain file so it is NOT a database (sqlite3.DatabaseError).
        get_db_path(pid_b).write_bytes(b"this is not a sqlite database at all")
        add_family_members("fam", [str(a), str(b)])
        _enable_federation(monkeypatch, pid_a)

        targets = resolve_federation_targets(pid_a)  # must not raise
        assert [t.project_id for t in targets] == [pid_a], "corrupt sibling skipped at resolve"

    def test_unbuilt_member_excluded(self, tmp_path, monkeypatch):
        a = tmp_path / "projA"
        c = tmp_path / "projC"
        a.mkdir()
        c.mkdir()
        pid_a = _make_member(a, ["alpha note"])
        get_project_id(c)  # registered but NEVER built (no brain file)
        add_family_members("fam", [str(a), str(c)])
        _enable_federation(monkeypatch, pid_a)

        targets = resolve_federation_targets(pid_a)
        assert [t.project_id for t in targets] == [pid_a], "unbuilt member dropped"

    def test_truncated_header_at_resolve_time_not_fatal(self, tmp_path, monkeypatch):
        """Boris obj.#1, variant: a sibling truncated to a partial header raises
        sqlite3.DatabaseError ("database disk image is malformed") — the PARENT
        exception class, not OperationalError — so it must be caught by the
        broadened ``except sqlite3.Error`` and skipped, never fatal."""
        a = tmp_path / "projA"
        b = tmp_path / "projB"
        a.mkdir()
        b.mkdir()
        pid_a = _make_member(a, ["alpha note"])
        pid_b = _make_member(b, ["beta note"])
        db_b = get_db_path(pid_b)
        db_b.write_bytes(db_b.read_bytes()[:40])  # partial header, real braincell.db bytes
        add_family_members("fam", [str(a), str(b)])
        _enable_federation(monkeypatch, pid_a)

        targets = resolve_federation_targets(pid_a)  # must not raise
        assert [t.project_id for t in targets] == [pid_a], "truncated sibling skipped at resolve"

    def test_empty_file_sibling_included_lexical_only(self, tmp_path, monkeypatch):
        """A zero-length sibling file is a VALID (empty) database to sqlite —
        the probe queries raise OperationalError (caught INSIDE the probe, not
        the skip-sentinel path), so the member is INCLUDED, just with
        fingerprint_ok=False (lexical-only). Must not be skipped or raise."""
        a = tmp_path / "projA"
        b = tmp_path / "projB"
        a.mkdir()
        b.mkdir()
        pid_a = _make_member(a, ["alpha note"])
        pid_b = _make_member(b, ["beta note"])
        get_db_path(pid_b).write_bytes(b"")  # zero-length file
        add_family_members("fam", [str(a), str(b)])
        _enable_federation(monkeypatch, pid_a)

        targets = resolve_federation_targets(pid_a)  # must not raise
        pids = [t.project_id for t in targets]
        assert pid_a in pids
        assert pid_b in pids, "empty file is a readable empty db, not corrupt — included"
        b_target = next(t for t in targets if t.project_id == pid_b)
        assert b_target.fingerprint_ok is False, "empty db has no fingerprint row → lexical-only"

    def test_corrupt_families_json_resolves_to_seed_only(self, tmp_path, monkeypatch):
        """A corrupt families.json fails safe to {} (project_registry.load_families),
        so resolve_federation_targets sees no family membership and returns just
        the seed — never raises."""
        a = tmp_path / "projA"
        b = tmp_path / "projB"
        a.mkdir()
        b.mkdir()
        pid_a = _make_member(a, ["alpha note"])
        _make_member(b, ["beta note"])
        add_family_members("fam", [str(a), str(b)])
        _enable_federation(monkeypatch, pid_a)
        get_families_path().write_text("{ not json", encoding="utf-8")

        targets = resolve_federation_targets(pid_a)  # must not raise
        assert [t.project_id for t in targets] == [pid_a], "corrupt families.json fails safe to seed-only"

    def test_corrupt_path_registry_json_resolves_to_seed_only(self, tmp_path, monkeypatch):
        """A corrupt path-registry.json fails safe to {} (load_path_registry), so
        resolve_family_ulids finds no owned paths for the seed and returns just
        the seed — never raises."""
        a = tmp_path / "projA"
        b = tmp_path / "projB"
        a.mkdir()
        b.mkdir()
        pid_a = _make_member(a, ["alpha note"])
        _make_member(b, ["beta note"])
        add_family_members("fam", [str(a), str(b)])
        _enable_federation(monkeypatch, pid_a)
        get_path_registry_path().write_text("{ not json", encoding="utf-8")

        targets = resolve_federation_targets(pid_a)  # must not raise
        assert [t.project_id for t in targets] == [pid_a], (
            "corrupt path-registry.json fails safe to seed-only"
        )


# ── RRF merge, tie-break, weighting, dedup (unit) ──────────────────────────────

class TestRrfMerge:
    def test_rank_beats_position_in_larger_list(self):
        # Note X is rank #1 in a small list; Note Y is rank #5 in a bigger list.
        small = ("A", [_note("A", 1, "X")])
        big = ("B", [_note("B", i, f"n{i}") for i in range(1, 5)] + [_note("B", 5, "Y")])
        merged = _rrf_merge_notes([small, big], seed_pid="A", active_weight=1.0, k_rrf=60)
        contents = [n.content for n in merged]
        assert contents.index("X") < contents.index("Y")

    def test_active_weight_lifts_seed(self):
        # Same rank #1 in each store; active weight must break the tie for the seed.
        seed = ("A", [_note("A", 1, "seedhit")])
        sib = ("B", [_note("B", 1, "sibhit")])
        merged = _rrf_merge_notes([seed, sib], seed_pid="A", active_weight=2.0, k_rrf=60)
        assert merged[0].content == "seedhit"

    def test_deterministic_tiebreak(self):
        # Equal score + equal best_rank → ordered by (project_id, id).
        lists = [("B", [_note("B", 2, "b2")]), ("A", [_note("A", 1, "a1")])]
        merged = _rrf_merge_notes(lists, seed_pid="Z", active_weight=1.0, k_rrf=60)
        assert [(n.project_id, n.id) for n in merged] == [("A", 1), ("B", 2)]

    def test_dedup_by_content_collapses_cross_project(self):
        dupes = [_note("A", 1, "same lesson"), _note("B", 9, "same lesson"),
                 _note("A", 2, "unique")]
        out = _dedup_by_content(dupes)
        assert [n.content for n in out] == ["same lesson", "unique"]
        assert out[0].project_id == "A", "keeps first (highest-ranked) representative"

    def test_corroboration_outranks_equal_rank_unique(self):
        # The verified defect: the SAME lesson at rank #1 in three members must
        # SUM its RRF contributions (content-keyed fusion) and outrank a unique note
        # that is ALSO rank #1 in its own member — not be interleaved then deduped.
        lists = [
            ("A", [_note("A", 1, "shared lesson")]),   # seed, rank 1
            ("B", [_note("B", 1, "shared lesson")]),   # rank 1
            ("C", [_note("C", 1, "shared lesson")]),   # rank 1
            ("D", [_note("D", 1, "unique lesson")]),   # rank 1
        ]
        merged = _rrf_merge_notes(lists, seed_pid="A", active_weight=1.0, k_rrf=60)
        contents = [n.content for n in merged]
        assert contents[0] == "shared lesson", "3× corroboration wins over an equal-rank unique note"
        assert contents.count("shared lesson") == 1, "duplicates folded into one fused entry"
        assert set(contents) == {"shared lesson", "unique lesson"}

    def test_all_unique_matches_reference_rrf_order(self):
        # Regression: with all-unique content the content-keyed merge must produce
        # the SAME order as the old (project_id, id)-keyed algorithm. Expected order
        # is computed directly from the RRF formula, not from the old code.
        # a1/b1 tie at 1/(60+1) → tie-break (A,1) < (B,1); a2/b2 tie at 1/(60+2).
        lists = [
            ("A", [_note("A", 1, "a1"), _note("A", 2, "a2")]),  # seed
            ("B", [_note("B", 1, "b1"), _note("B", 2, "b2")]),
        ]
        merged = _rrf_merge_notes(lists, seed_pid="A", active_weight=1.0, k_rrf=60)
        assert [n.content for n in merged] == ["a1", "b1", "a2", "b2"]

    def test_representative_prefers_seed_instance(self):
        # Same content in seed A (first in target order) and sibling B → the kept
        # representative Note is the seed's instance (deterministic first-seen).
        lists = [
            ("A", [_note("A", 5, "shared lesson")]),  # seed first (resolve order)
            ("B", [_note("B", 9, "shared lesson")]),
        ]
        merged = _rrf_merge_notes(lists, seed_pid="A", active_weight=1.0, k_rrf=60)
        assert len(merged) == 1
        assert merged[0].project_id == "A" and merged[0].id == 5, "seed instance is the representative"

    def test_merge_folds_duplicates_no_dedup_pass_needed(self):
        # (c) the merge itself guarantees no duplicate content in the output.
        lists = [
            ("A", [_note("A", 1, "dup"), _note("A", 2, "solo-a")]),
            ("B", [_note("B", 1, "dup"), _note("B", 2, "solo-b")]),
        ]
        merged = _rrf_merge_notes(lists, seed_pid="A", active_weight=1.0, k_rrf=60)
        contents = [n.content for n in merged]
        assert len(contents) == len(set(contents)), "no duplicate content survives the merge"
        assert contents.count("dup") == 1


# ── federated_recall integration ──────────────────────────────────────────────

def _open_seed(pid: str) -> SqliteStore:
    return SqliteStore(get_db_path(pid))


class TestFederatedRecall:
    def test_merges_across_members(self, tmp_path, monkeypatch):
        a = tmp_path / "projA"
        b = tmp_path / "projB"
        a.mkdir()
        b.mkdir()
        pid_a = _make_member(a, ["alpha lesson about caching"])
        _make_member(b, ["beta lesson about caching"])
        add_family_members("fam", [str(a), str(b)])
        _enable_federation(monkeypatch, pid_a)
        plan = build_federation_plan(None, "family", None)

        async def _run():
            self_store = _open_seed(pid_a)
            try:
                return await federated_recall(self_store, plan, None, 10, qtext="caching")
            finally:
                await self_store.aclose()

        notes = asyncio.run(_run())
        contents = {n.content for n in notes}
        assert "alpha lesson about caching" in contents
        assert "beta lesson about caching" in contents

    def test_non_member_project_not_recalled(self, tmp_path, monkeypatch):
        a = tmp_path / "projA"
        b = tmp_path / "projB"
        x = tmp_path / "projX"
        a.mkdir()
        b.mkdir()
        x.mkdir()
        pid_a = _make_member(a, ["alpha lesson"])
        _make_member(b, ["beta lesson"])
        _make_member(x, ["stranger lesson"])          # built but NOT in the family
        add_family_members("fam", [str(a), str(b)])   # x excluded
        _enable_federation(monkeypatch, pid_a)
        plan = build_federation_plan(None, "family", None)

        async def _run():
            self_store = _open_seed(pid_a)
            try:
                return await federated_recall(self_store, plan, None, 10, qtext="lesson")
            finally:
                await self_store.aclose()

        contents = {n.content for n in asyncio.run(_run())}
        assert "stranger lesson" not in contents

    def test_missing_sibling_skipped_not_fatal(self, tmp_path, monkeypatch):
        a = tmp_path / "projA"
        b = tmp_path / "projB"
        a.mkdir()
        b.mkdir()
        pid_a = _make_member(a, ["alpha lesson"])
        pid_b = _make_member(b, ["beta lesson"])
        add_family_members("fam", [str(a), str(b)])
        _enable_federation(monkeypatch, pid_a)
        plan = build_federation_plan(None, "family", None)
        # Corrupt B's brain AFTER planning → its recall must fail in isolation.
        get_db_path(pid_b).write_bytes(b"not a database")

        async def _run():
            self_store = _open_seed(pid_a)
            try:
                return await federated_recall(self_store, plan, None, 10, qtext="lesson")
            finally:
                await self_store.aclose()

        contents = {n.content for n in asyncio.run(_run())}
        assert "alpha lesson" in contents, "seed survives a broken sibling"

    def test_fingerprint_mismatch_lexical_only(self, tmp_path, monkeypatch):
        a = tmp_path / "projA"
        b = tmp_path / "projB"
        a.mkdir()
        b.mkdir()
        pid_a = _make_member(a, ["alpha lesson"])
        pid_b = _make_member(b, ["beta zebra lesson"])
        # Rewrite B's fingerprint so its vectors are NOT trusted.
        con = sqlite3.connect(str(get_db_path(pid_b)))
        con.execute("UPDATE embed_fingerprint SET fingerprint = ?", ("bogus:model:9",))
        con.commit()
        con.close()
        add_family_members("fam", [str(a), str(b)])
        _enable_federation(monkeypatch, pid_a)

        targets = resolve_federation_targets(pid_a)
        b_target = next(t for t in targets if t.project_id == pid_b)
        assert b_target.fingerprint_ok is False, "mismatched brain flagged"

        plan = build_federation_plan(None, "family", None)

        async def _run():
            self_store = _open_seed(pid_a)
            try:
                # keyword still finds B's note even though its vectors are excluded
                return await federated_recall(self_store, plan, fake_vec(3), 10, qtext="zebra")
            finally:
                await self_store.aclose()

        contents = {n.content for n in asyncio.run(_run())}
        assert "beta zebra lesson" in contents

    def test_readonly_sibling_fresh_under_live_writer(self, tmp_path, monkeypatch):
        """GAP 1/2: with a sibling held open by a live WAL writer, the read-only
        federated read sees the newly-committed note AND never writes the main DB."""
        a = tmp_path / "projA"
        b = tmp_path / "projB"
        a.mkdir()
        b.mkdir()
        pid_a = _make_member(a, ["alpha lesson"])
        pid_b = _make_member(b, ["beta lesson"])
        add_family_members("fam", [str(a), str(b)])
        _enable_federation(monkeypatch, pid_a)
        plan = build_federation_plan(None, "family", None)
        b_db = get_db_path(pid_b)

        async def _run():
            self_store = _open_seed(pid_a)
            writer = SqliteStore(b_db)  # live read-write WAL owner of the sibling
            try:
                await writer.remember("fresh zebra insight", "note", pid_b,
                                      embedding=fake_vec(7))  # commits to B's WAL
                mtime_before = b_db.stat().st_mtime_ns
                notes = await federated_recall(self_store, plan, None, 10, qtext="zebra")
                mtime_after = b_db.stat().st_mtime_ns
                return notes, mtime_before, mtime_after
            finally:
                await writer.aclose()
                await self_store.aclose()

        notes, before, after = asyncio.run(_run())
        contents = {n.content for n in notes}
        assert "fresh zebra insight" in contents, "RO read sees the live WAL commit (freshness)"
        assert before == after, "read-only federation must not write the sibling main DB"

    def test_corrupt_sibling_before_plan_build_end_to_end(self, tmp_path, monkeypatch):
        """Boris obj.#1, end-to-end: a sibling corrupted BEFORE
        build_federation_plan is excluded at plan-build time (resolve happens
        inside build_federation_plan), and federated_recall through a real seed
        store still returns the seed's notes — no raise anywhere in the chain."""
        a = tmp_path / "projA"
        b = tmp_path / "projB"
        a.mkdir()
        b.mkdir()
        pid_a = _make_member(a, ["alpha lesson"])
        pid_b = _make_member(b, ["beta lesson"])
        add_family_members("fam", [str(a), str(b)])
        _enable_federation(monkeypatch, pid_a)
        get_db_path(pid_b).write_bytes(b"not a database")  # corrupt BEFORE planning

        plan = build_federation_plan(None, "family", None)  # must not raise
        assert [t.project_id for t in plan.targets] == [pid_a], "corrupt sibling excluded at plan build"

        async def _run():
            self_store = _open_seed(pid_a)
            try:
                return await federated_recall(self_store, plan, None, 10, qtext="lesson")
            finally:
                await self_store.aclose()

        contents = {n.content for n in asyncio.run(_run())}
        assert "alpha lesson" in contents, "seed survives a sibling corrupted before plan build"


# ── federated_search (Phase 2) ─────────────────────────────────────────────────

class TestFederatedSearch:
    def test_rrf_merge_hits_by_rank(self):
        small = ("A", [_hit(1, "dA", "X")])
        big = ("B", [_hit(i, "dB", f"n{i}") for i in range(1, 5)] + [_hit(5, "dB", "Y")])
        merged = _rrf_merge_hits([small, big], seed_pid="A", active_weight=1.0, k_rrf=60)
        snippets = [h.snippet for h in merged]
        assert snippets.index("X") < snippets.index("Y")

    def test_dedup_hits_collapses_identical(self):
        dupes = [_hit(1, "d", "same chunk"), _hit(9, "d", "same chunk"), _hit(2, "d", "other")]
        out = _dedup_hits(dupes)
        assert [h.snippet for h in out] == ["same chunk", "other"]

    def test_corroboration_outranks_equal_rank_unique_hits(self):
        # (d) hits-side of the fusion fix: identical (doc_key+snippet) chunk in three
        # members SUMS its RRF contributions and outranks an equal-rank unique chunk.
        lists = [
            ("A", [_hit(1, "d", "shared chunk")]),  # seed, rank 1
            ("B", [_hit(2, "d", "shared chunk")]),  # rank 1
            ("C", [_hit(3, "d", "shared chunk")]),  # rank 1
            ("D", [_hit(4, "d", "unique chunk")]),  # rank 1
        ]
        merged = _rrf_merge_hits(lists, seed_pid="A", active_weight=1.0, k_rrf=60)
        snippets = [h.snippet for h in merged]
        assert snippets[0] == "shared chunk", "3× corroboration wins over an equal-rank unique chunk"
        assert snippets.count("shared chunk") == 1, "identical chunks folded into one fused entry"

    def test_hits_representative_prefers_seed_instance(self):
        # Same chunk text in seed A (first) and sibling B → the kept representative
        # Hit is the seed's instance (its chunk_id survives).
        lists = [
            ("A", [_hit(7, "d", "shared chunk")]),  # seed first
            ("B", [_hit(8, "d", "shared chunk")]),
        ]
        merged = _rrf_merge_hits(lists, seed_pid="A", active_weight=1.0, k_rrf=60)
        assert len(merged) == 1
        assert merged[0].chunk_id == 7, "seed instance is the representative"

    def test_hits_all_unique_matches_reference_rrf_order(self):
        # Regression: all-unique chunks keep the reference RRF order (parity with the
        # notes-side all-unique regression). a-chunk/b-chunk tie at 1/61 → tie-break
        # by (member_pid, chunk_id): (A,1) < (B,2).
        lists = [
            ("A", [_hit(1, "dA", "a chunk")]),  # seed
            ("B", [_hit(2, "dB", "b chunk")]),
        ]
        merged = _rrf_merge_hits(lists, seed_pid="A", active_weight=1.0, k_rrf=60)
        assert [h.snippet for h in merged] == ["a chunk", "b chunk"]

    def test_federated_search_merges_across_members(self, tmp_path, monkeypatch):
        a = tmp_path / "sA"
        b = tmp_path / "sB"
        a.mkdir()
        b.mkdir()
        pid_a = _make_search_member(a, [("docA", "alpha caching wisdom")])
        _make_search_member(b, [("docB", "beta caching wisdom")])
        add_family_members("fam", [str(a), str(b)])
        _enable_federation(monkeypatch, pid_a)
        plan = build_federation_plan(None, "family", None)

        async def _run():
            self_store = _open_seed(pid_a)
            try:
                return await federated_search(self_store, plan, fake_vec(1), "caching", 10, "keyword")
            finally:
                await self_store.aclose()

        keys = {h.doc_key for h in asyncio.run(_run())}
        assert {"docA", "docB"} <= keys


# ── GUI federation toggle ──────────────────────────────────────────────────────

class TestGuiFederation:
    def test_notes_federate_toggle(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from braincell.gui import create_app

        a = tmp_path / "gA"
        b = tmp_path / "gB"
        a.mkdir()
        b.mkdir()
        pid_a = _make_member(a, ["alpha lesson caching"])
        _make_member(b, ["beta lesson caching"])
        add_family_members("fam", [str(a), str(b)])
        monkeypatch.setenv("BRAINCELL_FEDERATE", "on")
        monkeypatch.delenv("BRAINCELL_MODE", raising=False)

        app = create_app(db_path=get_db_path(pid_a), seed_project_id=pid_a)
        with TestClient(app) as client:
            fed = client.get("/api/notes?q=caching&federate=true").json()
            solo = client.get("/api/notes?q=caching").json()

        fed_contents = {n["content"] for n in fed["notes"]}
        solo_contents = {n["content"] for n in solo["notes"]}
        assert {"alpha lesson caching", "beta lesson caching"} <= fed_contents
        assert "beta lesson caching" not in solo_contents, "no federation → seed only"

    def test_plan_for_seed_bypasses_env(self, tmp_path):
        a = tmp_path / "gA"
        b = tmp_path / "gB"
        a.mkdir()
        b.mkdir()
        pid_a = _make_member(a, ["alpha"])
        pid_b = _make_member(b, ["beta"])
        add_family_members("fam", [str(a), str(b)])
        plan = plan_for_seed(pid_a)  # no BRAINCELL_PROJECT_ID / flag needed
        assert plan.seed_pid == pid_a
        assert {t.project_id for t in plan.targets} == {pid_a, pid_b}
