# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
test_reembed_backup_coverage.py — regression tests for BUGS.md "safety-backup
coverage": `build --reembed` must require the same mandatory pre-wipe backup
`consolidate --apply` / `reflect --apply` already require, fail closed on
backup failure, and only skip the snapshot when the caller explicitly passes
`--no-backup`.
"""

from __future__ import annotations

import sqlite3

import pytest

from braincell import cli
from braincell.cli import _run_build


def _doc_count(db_path) -> int:
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute("SELECT COUNT(*) FROM bc_documents").fetchone()[0]
    finally:
        con.close()


def _seed_one_doc(root, monkeypatch, ns: str) -> object:
    """Build once (no reembed) so the project has an existing db + one doc."""
    from tests.conftest import fake_vec

    monkeypatch.setenv("BRAINCELL_DATA_NAMESPACE", ns)
    root.mkdir(exist_ok=True)
    _run_build(root, skip_transcripts=True, reembed=False, verbose=False, mode=None)

    import asyncio
    import hashlib

    from braincell.config import get_db_path, get_project_id
    from braincell.store import SqliteStore

    project_id = get_project_id(root)
    db_path = get_db_path(project_id)
    store = SqliteStore(db_path)
    asyncio.run(store.replace_document(
        project_id=project_id,
        doc_key="doc-1",
        title="t",
        content_hash=hashlib.sha256(b"x").digest(),
        content_type="cell",
        chunks=[("chunk text", fake_vec(0))],
    ))
    store.close()
    return db_path


class TestReembedRequiresBackup:
    def test_reembed_creates_a_pre_wipe_snapshot(self, tmp_path, monkeypatch):
        root = tmp_path / "project"
        db_path = _seed_one_doc(root, monkeypatch, "test_reembed_backup_ok")
        assert _doc_count(db_path) == 1

        before = set(db_path.parent.glob("braincell-prereembed-*.db"))
        _run_build(root, skip_transcripts=True, reembed=True, verbose=False, mode=None)
        after = set(db_path.parent.glob("braincell-prereembed-*.db"))

        new_backups = after - before
        assert len(new_backups) == 1
        assert next(iter(new_backups)).is_file()
        # The wipe still happened (the point of --reembed).
        assert _doc_count(db_path) == 0

    def test_reembed_fails_closed_when_backup_fails(self, tmp_path, monkeypatch):
        """Fault injection: VACUUM INTO fails -> reembed refuses, docs untouched."""
        root = tmp_path / "project"
        db_path = _seed_one_doc(root, monkeypatch, "test_reembed_backup_fails_closed")
        assert _doc_count(db_path) == 1

        def _boom(src, dest):
            raise RuntimeError("simulated disk-full during VACUUM INTO")

        monkeypatch.setattr(cli, "_vacuum_into", _boom)

        with pytest.raises(RuntimeError, match="Refusing reembed"):
            _run_build(root, skip_transcripts=True, reembed=True, verbose=False, mode=None)

        # Fail-closed: the destructive wipe must never have run.
        assert _doc_count(db_path) == 1

    def test_no_backup_flag_skips_snapshot_and_still_wipes(self, tmp_path, monkeypatch, capsys):
        """--no-backup is the explicit, off-by-default escape hatch: it must
        proceed even when the backup path is broken, and must warn loudly."""
        root = tmp_path / "project"
        db_path = _seed_one_doc(root, monkeypatch, "test_reembed_no_backup")
        assert _doc_count(db_path) == 1

        def _boom(src, dest):
            raise RuntimeError("simulated disk-full during VACUUM INTO")

        monkeypatch.setattr(cli, "_vacuum_into", _boom)

        _run_build(
            root, skip_transcripts=True, reembed=True, verbose=False, mode=None,
            no_backup=True,
        )

        assert _doc_count(db_path) == 0  # wipe proceeded despite the broken backup path
        captured = capsys.readouterr()
        assert "no-backup" in captured.err.lower()
        assert "no safety snapshot" in captured.err.lower()

    def test_first_build_never_requires_a_backup(self, tmp_path, monkeypatch):
        """--reembed on a project with no existing db has nothing to lose."""
        root = tmp_path / "project"
        monkeypatch.setenv("BRAINCELL_DATA_NAMESPACE", "test_reembed_first_build")
        root.mkdir()

        def _boom(src, dest):  # would fail loudly if ever called
            raise AssertionError("backup must not be attempted on a first build")

        monkeypatch.setattr(cli, "_vacuum_into", _boom)

        # Must not raise even though the (unused) backup path is broken.
        _run_build(root, skip_transcripts=True, reembed=True, verbose=False, mode=None)
