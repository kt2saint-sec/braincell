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

## Remote comparison — 2026-07-31

The public repository was checked over SSH at
`git@github.com:kt2saint-sec/braincell.git`. The audit worktree's configured
`origin` still points at the stale `braincell-mcp` URL, which explains the
initial `Repository not found` response. The correct repository is accessible.

The remote `ci/windows-macos-matrix` commit (`40d0a56`) changes only
`.github/workflows/ci.yml`: it adds Windows and macOS CI runners and a
cross-platform wheel smoke test. It does not implement native launcher support,
consumer Windows/macOS GUI validation, ACL enforcement, or platform-specific
storage paths.

The remote `project-only-architecture` migration series adds read-only legacy
inventory, verified backups, provenance-based migration, and WAL-safe recovery.
It does not delete existing foreign-owned transcript rows. Its changes overlap
the local audit in `cli.py`, GUI modules, registry/configuration, recovery,
server code, documentation, and tests; merging the branches requires a manual
conflict-aware review.

The following remain open after comparing local commit `e2a1601` with those
remote branches:

- **High — foreign transcript cleanup:** future out-of-scope files are skipped,
  but historical foreign-owned rows still need a preview-only migration or
  reconciliation workflow. (`braincell/transcript_ingest.py:330`)
- **High — cross-platform parent-death cleanup:** subprocess protection uses
  Linux-only `prctl`; Windows and macOS lack an abrupt-parent-death equivalent.
  (`braincell/gui_ingest.py:77`)
- **High — native launcher platforms:** installation remains Linux/XDG-only.
  (`braincell/gui.py:903`)
- **High — safety-backup coverage:** consolidate/reflect require a successful
  backup, but reembed and clear still need an explicit backup/override policy.
  (`braincell/cli.py:214`, `braincell/gui_ingest.py:403`)
- **Medium — stats/storage diagnostics:** `braincell stats` does not report
  WAL/SHM, freelist, embedding, foreign-document, or orphan-database detail;
  the separate storage report is not a complete replacement.
  (`braincell/cli.py:590`, `braincell/storage_accounting.py:57`)
- **Medium — orphan reconciliation:** deleted or moved Projects can leave
  registry entries and databases without a preview/reassociate workflow.
  (`braincell/project_registry.py:48`)
- **Medium — token ACL parity:** token creation applies POSIX mode `0600`, but
  Windows ACL equivalence is not validated. (`braincell/gui.py:799`)
- **Medium — platform data roots:** default storage remains Linux-oriented
  `~/.local/share`; macOS/Windows migration is not implemented.
  (`braincell/config.py:33`)
- **Medium — SQLite compaction/WAL diagnostics:** no authorized hard-prune plus
  `VACUUM` workflow or WAL-starvation warning exists.
  (`braincell/storage_accounting.py:57`)
- **Low — logger fallback:** a failure constructing the rotating handler still
  falls back to an ordinary potentially unbounded file handler.
  (`braincell/log.py:68`)
- **Later policy — storage budgets:** warnings, configurable budgets, and
  explicit hard limits remain unimplemented and must not delete memory silently.
