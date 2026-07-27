# SPDX-License-Identifier: AGPL-3.0-or-later
"""Disposable preview/apply matrix for retired shared-data recovery."""

from __future__ import annotations

import asyncio
import hashlib

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
    assert verification["source_links"] == verification["destination_links"] == 1
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
