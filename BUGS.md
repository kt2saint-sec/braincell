# BrainCell fault ledger

Concise living record of verified faults. Resolved entries remain for regression
context; severity reflects pre-fix impact.

## Open

- **Medium — canonical skill authority:** Historical transcripts containing
  different bodies for one skill remain order-dependent.
  (`braincell/transcript_ingest.py:449`)
- **Medium — retention policy:** Storage is observable, but disappeared
  transcripts, tombstones, and operation history have no automatic expiry.
  Any future deletion executor must protect snapshots referenced by undo
  history. (`braincell/storage_accounting.py:57`)
- **Medium — legacy raw upserts:** Compatibility helpers commit caller-owned
  SQLite connections outside `SqliteStore` transaction ownership. Production
  ingest no longer calls them. (`braincell/store.py:3183`)

## Resolved in Unreleased

- **Critical — shared transaction ownership:** A second coroutine could commit
  or roll back another writer's unfinished transaction.
  (`braincell/store.py:1167`)
- **Critical — transcript split state:** Hash/checkpoint updates could survive
  failed embeddings or disagree with chunks and FTS rows.
  (`braincell/transcript_ingest.py:343`, `braincell/store.py:2937`)
- **Critical — Project identity/catalog safety:** Concurrent minting and unsafe
  registry values could create conflicting identities or redirect state outside
  the BrainCell namespace. (`braincell/project_registry.py:37`)
- **High — cross-interface mutation races:** CLI, Memory Map, schedules, and
  recovery did not share one destination mutation boundary.
  (`braincell/catalog_io.py:47`, `braincell/gui_mutation.py:11`)
- **High — invisible vectorless note:** An individual FTS insert failure could
  commit a note that neither semantic nor keyword Recall could find.
  (`braincell/store.py:1684`)
- **High — accumulating no-op backups:** Maintenance could retain full database
  snapshots when no mutation was planned, and second-resolution names could
  collide. (`braincell/cli.py:973`)
- **High — recovery state races:** Preview and apply could observe different
  source, registry, or destination states.
  (`braincell/legacy_recovery.py:417`)
- **High — embedding outage behavior:** Keyword operations unnecessarily
  depended on embeddings and hybrid Search lacked lexical degradation.
  (`braincell/server.py:255`)
- **Medium — blocking reranker and embedder lifetime:** Sequential synchronous
  model calls blocked the event loop, while warm-up immediately unloaded the
  embedding model. (`braincell/rerank.py:48`, `braincell/embed.py:252`)
