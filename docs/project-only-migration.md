# Project-only migration

Use this guide only when recovering data or client configuration from an older
BrainCell installation that used shared state. BrainCell does not open that
state during normal Project operation.

## Before you begin

- Keep the legacy database and client configuration intact.
- Identify the Project ULIDs that should receive known, attributable rows.
- Do not assign an ambiguous row to a Project by guesswork.
- Use a new destination for every backup, report, and recovery receipt; the
  commands refuse to overwrite those records.

## Inspect without changing anything

```bash
braincell legacy-migration preview --source /path/to/legacy.db \
  --manifest /safe/reports/braincell-legacy-preview.json
```

The preview reports schema and embedding metadata, per-table counts, Project
ULIDs, known provenance, unattributed rows, note-link integrity, and legacy
operation-audit state. A preview is read-only.

## Make and verify a backup

```bash
braincell legacy-migration backup --source /path/to/legacy.db \
  --destination /safe/backups/braincell-legacy.db \
  --manifest /safe/reports/braincell-legacy-backup.json
```

The backup uses SQLite's backup mechanism, then verifies that the copy is
readable and passes `quick_check`. The original remains unchanged.

## Recover approved Project rows

```bash
braincell legacy-migration apply --source /path/to/legacy.db \
  --backup /safe/backups/braincell-legacy.db \
  --receipt /safe/reports/braincell-recovery-receipt.json \
  --project-id <project-a-ulid> \
  --project-id <project-b-ulid>
```

Apply copies only rows whose `project_id` and recorded provenance both match an
explicitly selected Project ULID. It uses a transaction per destination,
rebuilds full-text indexes, and verifies foreign keys before committing. It is
safe to repeat: existing matching notes and documents are skipped, and missing
chunks for an already copied document are repaired.

Rows without reliable provenance are preserved in the legacy source and
reported as skipped. The recovery receipt records the backup checksum, selected
Projects, exact copy/skip/conflict counts, and verification context.

## Audit history and retirement

Legacy operation audit rows remain in the original database and verified backup.
They are not copied automatically because their note IDs and backup paths need
a separate audited remapping workflow. Existing undo behavior already reports
missing notes safely.

Recovery is not retirement. Keep the original database, backup, preview, and
receipt until a later explicit retirement action is provided and independently
verified. Do not remove legacy client configuration automatically; use its
client-specific legacy cleanup preview and action instead.
