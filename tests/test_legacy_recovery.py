# SPDX-License-Identifier: AGPL-3.0-or-later
"""Disposable preview/apply matrix for retired shared-data recovery."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from tests.conftest import _insert_doc_and_chunk, fake_vec


def _legacy_fixture(tmp_path):
    from braincell.project_registry import register_path
    from braincell.store import SqliteStore

    for project_id in ("01ATTRIBUTABLE", "01POOLED"):
        project = tmp_path / project_id
        project.mkdir()
        register_path(project, project_id)
    source = tmp_path / "legacy.db"
    store = SqliteStore(source)
    store.assert_schema_version()

    async def seed():
        await _insert_doc_and_chunk(
            store, project="01ATTRIBUTABLE", doc_key="a-doc",
            text="attributable document", seed=1,
        )
        await _insert_doc_and_chunk(
            store, project="01POOLED", doc_key="p-doc",
            text="pooled document", seed=2,
        )
        await _insert_doc_and_chunk(
            store, project="01UNKNOWN", doc_key="u-doc",
            text="ambiguous document", seed=3,
        )
        first = int(await store.remember(
            "first attributable note", "note", "01ATTRIBUTABLE",
            embedding=fake_vec(1),
        ))
        second = int(await store.remember(
            "second attributable note", "note", "01ATTRIBUTABLE",
            embedding=fake_vec(2),
        ))
        await store.remember(
            "known pooled note", "note", "01POOLED", embedding=fake_vec(3)
        )
        await store.remember(
            "ambiguous note", "note", "01UNKNOWN", embedding=fake_vec(4)
        )
        connection = await store._conn_get()
        await connection.execute(
            "INSERT INTO bc_note_links(src_id,dst_id,kind,weight) VALUES (?,?,?,?)",
            (first, second, "related", 0.8),
        )
        await connection.execute(
            "UPDATE bc_documents SET pooled_from='01POOLED' "
            "WHERE project_id='01POOLED'"
        )
        await connection.execute(
            "UPDATE memory_notes SET pooled_from='01POOLED' "
            "WHERE project_id='01POOLED'"
        )
        await connection.commit()

    asyncio.run(seed())
    store.close()
    return source


def _wal_writer(path):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    return connection


def _database_rows(path):
    with sqlite3.connect(path) as connection:
        return {
            "documents": connection.execute("SELECT project_id,doc_key,content_hash FROM bc_documents ORDER BY id").fetchall(),
            "notes": connection.execute("SELECT project_id,content,note_uid FROM memory_notes ORDER BY id").fetchall(),
            "foreign_keys": connection.execute("PRAGMA foreign_key_check").fetchall(),
            "chunks_fts": connection.execute("INSERT INTO bc_chunks_fts(bc_chunks_fts) VALUES('integrity-check')").fetchall(),
            "notes_fts": connection.execute("INSERT INTO memory_fts(memory_fts) VALUES('integrity-check')").fetchall(),
        }


def test_preview_classifies_provenance_attribution_and_ambiguity(tmp_path):
    from braincell.legacy_recovery import preview

    source = _legacy_fixture(tmp_path)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    report = preview(source)

    assert report["classifications"]["known_pooled_from"]["01POOLED"] == 2
    assert report["classifications"]["attributable"]["01ATTRIBUTABLE"] == 3
    assert report["classifications"]["ambiguous_or_unattributed"]["01UNKNOWN"] == 2
    assert set(report["projects"]) == {"01ATTRIBUTABLE", "01POOLED"}
    assert len(report["approval_digest"]) == 64
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_apply_requires_exact_approval_selection_and_retains_backup(tmp_path):
    from braincell.legacy_recovery import LegacyRecoveryError, apply, preview

    source = _legacy_fixture(tmp_path)
    report = preview(source)
    with pytest.raises(LegacyRecoveryError, match="digest"):
        apply(
            source_path=source,
            project_ids=["01ATTRIBUTABLE"],
            approval_digest="wrong",
        )

    result = apply(
        source_path=source,
        project_ids=["01ATTRIBUTABLE", "01POOLED"],
        approval_digest=report["approval_digest"],
        backup_dir=tmp_path / "backups",
    )

    backup = tmp_path / "backups" / result["backup"].split("/")[-1]
    assert backup.is_file()
    verification = result["projects"]["01ATTRIBUTABLE"]["verification"]
    assert verification["ok"] is True
    assert verification["foreign_key_violations"] == 0
    assert verification["fts"] == {
        "bc_chunks_fts": "ok",
        "memory_fts": "ok",
    }
    assert verification["links_verified"] == 1
    assert result["projects"]["01POOLED"]["verification"]["ok"] is True


def test_destination_conflict_is_previewed_and_blocks_apply(tmp_path):
    from braincell.config import get_db_path
    from braincell.legacy_recovery import LegacyRecoveryError, apply, preview
    from braincell.store import SqliteStore

    source = _legacy_fixture(tmp_path)
    destination = SqliteStore(get_db_path("01ATTRIBUTABLE"))
    destination.assert_schema_version()

    async def conflicting_document():
        await _insert_doc_and_chunk(
            destination, project="01ATTRIBUTABLE", doc_key="a-doc",
            text="different destination content", seed=9,
        )

    asyncio.run(conflicting_document())
    destination.close()
    report = preview(source)
    assert report["projects"]["01ATTRIBUTABLE"]["conflicts"] == [
        {"kind": "document", "key": "a-doc"}
    ]
    with pytest.raises(LegacyRecoveryError, match="conflicts"):
        apply(
            source_path=source,
            project_ids=["01ATTRIBUTABLE"],
            approval_digest=report["approval_digest"],
        )


def test_legacy_recovery_is_not_imported_by_normal_runtime():
    import sys

    sys.modules.pop("braincell.legacy_recovery", None)
    import braincell.cli  # noqa: F401
    import braincell.gui  # noqa: F401
    import braincell.server  # noqa: F401

    assert "braincell.legacy_recovery" not in sys.modules


def test_cli_preview_only_uses_explicit_disposable_source(tmp_path, capsys):
    from braincell.cli import main

    source = _legacy_fixture(tmp_path)
    main(["legacy-recovery", "preview", "--source", str(source)])
    assert '"approval_digest"' in capsys.readouterr().out


def test_preview_opens_source_and_destination_read_only_without_filesystem_writes(tmp_path):
    from braincell.config import get_db_path
    from braincell.legacy_recovery import preview
    from braincell.store import SqliteStore

    source = _legacy_fixture(tmp_path)
    destination = get_db_path("01ATTRIBUTABLE")
    destination.parent.mkdir(parents=True)
    SqliteStore(destination).assert_schema_version()
    before = {path.relative_to(tmp_path): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in tmp_path.rglob("*") if path.is_file()}
    preview(source)
    after = {path.relative_to(tmp_path): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before


def test_pooled_from_project_id_disagreement_is_ambiguous_and_unselectable(tmp_path):
    from braincell.legacy_recovery import LegacyRecoveryError, apply, preview

    source = _legacy_fixture(tmp_path)
    with sqlite3.connect(source) as connection:
        connection.execute(
            "UPDATE bc_documents SET pooled_from='01POOLED' WHERE project_id='01ATTRIBUTABLE'"
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    report = preview(source)
    assert report["classifications"]["ambiguous_pooled_from_conflict"]["01POOLED"] == 1
    assert report["projects"]["01ATTRIBUTABLE"]["documents"] == 0
    with pytest.raises(LegacyRecoveryError, match="not attributable"):
        apply(source_path=source, project_ids=["01UNKNOWN"], approval_digest=report["approval_digest"])


def test_verification_failure_restores_existing_destination(tmp_path, monkeypatch):
    from braincell.config import get_db_path
    from braincell import legacy_recovery
    from braincell.store import SqliteStore

    source = _legacy_fixture(tmp_path)
    destination = get_db_path("01ATTRIBUTABLE")
    destination.parent.mkdir(parents=True)
    SqliteStore(destination).assert_schema_version()
    before = hashlib.sha256(destination.read_bytes()).hexdigest()
    report = legacy_recovery.preview(source)
    monkeypatch.setattr(legacy_recovery, "_verify", lambda *_args: {"ok": False, "failures": ["forced"]})
    with pytest.raises(legacy_recovery.LegacyRecoveryError, match="destination was restored"):
        legacy_recovery.apply(source_path=source, project_ids=["01ATTRIBUTABLE"], approval_digest=report["approval_digest"])
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == before


def test_second_project_failure_reports_completed_and_restores_failed_destination(tmp_path, monkeypatch):
    from braincell.config import get_db_path
    from braincell import legacy_recovery

    source = _legacy_fixture(tmp_path)
    report = legacy_recovery.preview(source)
    real_copy = legacy_recovery._copy_project

    def fail_second(connection, destination, project_id, selected):
        copied = real_copy(connection, destination, project_id, selected)
        if project_id == "01POOLED":
            raise RuntimeError("forced second-project failure")
        return copied

    monkeypatch.setattr(legacy_recovery, "_copy_project", fail_second)
    with pytest.raises(legacy_recovery.LegacyRecoveryError) as error:
        legacy_recovery.apply(source_path=source, project_ids=["01ATTRIBUTABLE", "01POOLED"], approval_digest=report["approval_digest"])
    assert error.value.completed_projects == ("01ATTRIBUTABLE",)
    assert get_db_path("01ATTRIBUTABLE").is_file()
    assert not get_db_path("01POOLED").exists()


def test_moved_source_retry_uses_stable_note_ids_and_is_idempotent(tmp_path):
    from braincell.config import get_db_path
    from braincell.legacy_recovery import apply, preview

    source = _legacy_fixture(tmp_path)
    first = preview(source)
    apply(source_path=source, project_ids=["01ATTRIBUTABLE"], approval_digest=first["approval_digest"])
    with sqlite3.connect(get_db_path("01ATTRIBUTABLE")) as destination:
        initial_uids = [row[0] for row in destination.execute("SELECT note_uid FROM memory_notes ORDER BY id")]
    moved = tmp_path / "moved-legacy.db"
    shutil.copy2(source, moved)
    retry = preview(moved)
    result = apply(source_path=moved, project_ids=["01ATTRIBUTABLE"], approval_digest=retry["approval_digest"])
    assert result["projects"]["01ATTRIBUTABLE"]["copied"]["notes"] == 0
    with sqlite3.connect(get_db_path("01ATTRIBUTABLE")) as destination:
        assert [row[0] for row in destination.execute("SELECT note_uid FROM memory_notes ORDER BY id")] == initial_uids


def test_verification_checks_foreign_keys_and_fts_for_selected_rows(tmp_path):
    from braincell import legacy_recovery
    from braincell.config import get_db_path

    source = _legacy_fixture(tmp_path)
    report = legacy_recovery.preview(source)
    legacy_recovery.apply(source_path=source, project_ids=["01ATTRIBUTABLE"], approval_digest=report["approval_digest"])
    with legacy_recovery._read_only(source, purpose="test") as connection:
        manifest, _ = legacy_recovery._manifest(connection, {"01ATTRIBUTABLE", "01POOLED"})
        verification = legacy_recovery._verify(connection, get_db_path("01ATTRIBUTABLE"), "01ATTRIBUTABLE", manifest["01ATTRIBUTABLE"])
    assert verification["ok"] is True
    assert verification["foreign_key_violations"] == 0
    assert verification["fts"] == {"bc_chunks_fts": "ok", "memory_fts": "ok"}


def test_preview_reads_committed_wal_rows_without_changing_database_artifacts(tmp_path):
    from braincell.legacy_recovery import preview

    source = _legacy_fixture(tmp_path)
    writer = _wal_writer(source)
    writer.execute("UPDATE bc_documents SET pooled_from='01POOLED' WHERE project_id='01ATTRIBUTABLE'")
    writer.commit()
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (source, source.with_name("legacy.db-wal"))}
    report = preview(source)
    after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (source, source.with_name("legacy.db-wal"))}
    writer.close()

    assert report["classifications"]["ambiguous_pooled_from_conflict"]["01POOLED"] == 1
    assert after == before


def test_preview_detects_destination_conflict_in_committed_wal(tmp_path):
    from braincell.config import get_db_path
    from braincell.legacy_recovery import preview
    from braincell.store import SqliteStore

    source = _legacy_fixture(tmp_path)
    destination = get_db_path("01ATTRIBUTABLE")
    destination.parent.mkdir(parents=True)
    SqliteStore(destination).assert_schema_version()
    writer = _wal_writer(destination)
    writer.execute(
        "INSERT INTO bc_documents(project_id,doc_key,title,content_hash) VALUES (?,?,?,?)",
        ("01ATTRIBUTABLE", "a-doc", "different", b"different"),
    )
    writer.commit()
    report = preview(source)
    writer.close()

    assert report["projects"]["01ATTRIBUTABLE"]["conflicts"] == [{"kind": "document", "key": "a-doc"}]


def test_wal_destination_backup_and_failed_recovery_restore_committed_data(tmp_path, monkeypatch):
    from braincell import legacy_recovery
    from braincell.config import get_db_path
    from braincell.store import SqliteStore

    source = _legacy_fixture(tmp_path)
    destination = get_db_path("01ATTRIBUTABLE")
    destination.parent.mkdir(parents=True)
    SqliteStore(destination).assert_schema_version()
    writer = _wal_writer(destination)
    writer.execute(
        "INSERT INTO memory_notes(project_id,scope,kind,content,note_uid) VALUES (?,?,?,?,?)",
        ("01ATTRIBUTABLE", "project", "note", "WAL-only retained note", "wal-retained"),
    )
    writer.commit()
    before = _database_rows(destination)
    report = legacy_recovery.preview(source)
    monkeypatch.setattr(legacy_recovery, "_verify", lambda *_args: {"ok": False, "failures": ["forced"]})
    with pytest.raises(legacy_recovery.LegacyRecoveryError, match="restored"):
        legacy_recovery.apply(source_path=source, project_ids=["01ATTRIBUTABLE"], approval_digest=report["approval_digest"], backup_dir=tmp_path / "backups")
    writer.close()

    assert _database_rows(destination) == before
    backups = list((tmp_path / "backups").glob("*.destination-backup-*.db"))
    assert len(backups) == 1
    assert _database_rows(backups[0])["notes"] == before["notes"]


def test_apply_refuses_destination_with_active_wal_writer(tmp_path):
    from braincell import legacy_recovery
    from braincell.config import get_db_path
    from braincell.store import SqliteStore

    source = _legacy_fixture(tmp_path)
    destination = get_db_path("01ATTRIBUTABLE")
    destination.parent.mkdir(parents=True)
    SqliteStore(destination).assert_schema_version()
    writer = _wal_writer(destination)
    writer.execute("BEGIN IMMEDIATE")
    report = legacy_recovery.preview(source)
    with pytest.raises(legacy_recovery.LegacyRecoveryError, match="active writer"):
        legacy_recovery.apply(source_path=source, project_ids=["01ATTRIBUTABLE"], approval_digest=report["approval_digest"])
    writer.rollback()
    writer.close()


def test_approval_digest_tracks_source_registry_and_destination_conflicts(tmp_path):
    from braincell.config import get_db_path
    from braincell.legacy_recovery import preview
    from braincell.project_registry import register_path
    from braincell.store import SqliteStore

    source = _legacy_fixture(tmp_path)
    initial = preview(source)
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE bc_documents SET title='changed source manifest' WHERE doc_key='a-doc'")
    source_changed = preview(source)
    assert source_changed["approval_digest"] != initial["approval_digest"]

    register_path(tmp_path / "reassigned", "01UNKNOWN")
    registry_changed = preview(source)
    assert registry_changed["approval_digest"] != source_changed["approval_digest"]

    destination = get_db_path("01ATTRIBUTABLE")
    destination.parent.mkdir(parents=True)
    SqliteStore(destination).assert_schema_version()
    with sqlite3.connect(destination) as connection:
        connection.execute(
            "INSERT INTO bc_documents(project_id,doc_key,title,content_hash) VALUES (?,?,?,?)",
            ("01ATTRIBUTABLE", "a-doc", "conflict", b"conflict"),
        )
    conflict_changed = preview(source)
    assert conflict_changed["approval_digest"] != registry_changed["approval_digest"]


def test_apply_retains_source_and_each_existing_destination_backup(tmp_path):
    from braincell import legacy_recovery
    from braincell.config import get_db_path
    from braincell.store import SqliteStore

    source = _legacy_fixture(tmp_path)
    destination = get_db_path("01ATTRIBUTABLE")
    destination.parent.mkdir(parents=True)
    SqliteStore(destination).assert_schema_version()
    before_destination = _database_rows(destination)
    report = legacy_recovery.preview(source)
    result = legacy_recovery.apply(
        source_path=source,
        project_ids=["01ATTRIBUTABLE", "01POOLED"],
        approval_digest=report["approval_digest"],
        backup_dir=tmp_path / "backups",
    )

    assert _database_rows(Path(result["backup"]))["documents"]
    destination_backup = Path(result["projects"]["01ATTRIBUTABLE"]["destination_backup"])
    assert _database_rows(destination_backup) == before_destination
    assert result["projects"]["01POOLED"]["destination_backup"] is None


def test_ambiguous_rows_are_never_copied(tmp_path):
    from braincell.config import get_db_path
    from braincell.legacy_recovery import apply, preview

    source = _legacy_fixture(tmp_path)
    report = preview(source)
    apply(source_path=source, project_ids=["01ATTRIBUTABLE"], approval_digest=report["approval_digest"])
    with sqlite3.connect(get_db_path("01ATTRIBUTABLE")) as destination:
        assert destination.execute("SELECT COUNT(*) FROM bc_documents WHERE project_id='01UNKNOWN'").fetchone()[0] == 0
        assert destination.execute("SELECT COUNT(*) FROM memory_notes WHERE project_id='01UNKNOWN'").fetchone()[0] == 0
