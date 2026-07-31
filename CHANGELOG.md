# Changelog

Release-facing notes for the public BrainCell project. This file records
verified product changes only; private engineering history and machine-specific
details are intentionally excluded.

## Unreleased

### Added

- Windows and macOS test runners alongside Linux in continuous integration,
  with a cross-platform wheel smoke test.
- Read-only `braincell storage` accounting for Project databases, row counts,
  catalogs, locks, and owned backup files.
- Dry-run backup-retention planning with `--keep-backups`; optional
  `--backup-root` arguments include external recovery directories without
  deleting anything.

### Fixed

- Configuration files are now written atomically on Windows. The previous
  implementation used a POSIX-only call unavailable before Python 3.13, left the
  temporary file's handle open when that call failed, and then reported the
  resulting cleanup error in place of the original cause. Permissions are now
  applied after the file is closed, which works on every supported platform.

### Reliability and safety

- Serialized SQLite mutations across async tasks, CLI processes, GUI ingest,
  maintenance, clear, undo, and legacy recovery.
- Made transcript replacement atomic across its content hash, chunks, FTS rows,
  and ingestion checkpoint. Failed or incomplete embedding work remains
  retryable and shorter replacements remove stale chunks.
- Enforced embedding output cardinality, dimensions, finiteness, and nonzero
  norms before persistence.
- Hardened Project registry updates with locked compare-and-set identity
  creation, atomic durable writes, corruption preservation, and path-component
  validation that prevents state from escaping the BrainCell namespace.
- Kept keyword Search and Recall available during embedding-provider outages;
  semantic-only requests continue to fail explicitly.
- Made scheduled Build attempts distinguish success, failure, and GUI mutation
  contention, with locked atomic schedule persistence.
- Made legacy recovery apply the exact approved immutable snapshot while
  holding the destination mutation lock.
- Made destructive maintenance backups collision-resistant and mandatory, and
  create them only after a non-empty mutation plan exists.
- Made note persistence and its FTS row one all-or-nothing transaction for
  individual indexing failures.
- Changed Ollama embedding warm-up to use a Build-scoped keep-alive followed by
  an explicit unload, and moved synchronous reranking calls off the async event
  loop with bounded concurrency.

### Changed

- The test suite no longer assumes a Linux host. File reads and writes declare
  UTF-8 explicitly instead of relying on the platform default encoding, and
  checks that assert Linux-specific behaviour — XDG desktop launchers, display
  detection, and POSIX file modes — now run only on Linux. The repository
  hygiene check no longer mistakes a directory prefix for an ignored path when
  the working tree carries CRLF line endings.
- Lint debt cleared: 286 findings under Ruff's stock rule set reduced to zero
  (mechanical annotation modernisation, import ordering, and stale-suppression
  cleanup; no runtime behaviour changed). The lint configuration now documents
  which rules are deliberately ignored, and every remaining broad-exception and
  configuration-error boundary carries a justification naming why it is
  deliberate. The advisory lint-debt CI job reports without failing the run.

### Known limitations

- Storage reporting is informational. BrainCell does not automatically expire
  indexed transcripts, tombstoned notes, operation history, or curated memory.
- Canonical skill documents still need a defined monotonic authority when
  historical transcripts contain multiple versions of the same skill.

## v0.4.0 - 2026-07-27

### Added

- Project-local MCP connections for Codex, Claude, and VS Code.
- Conflict-safe, atomic configuration updates that preserve unrelated client
  settings and detect legacy client-wide registrations.
- Project-local BrainCell skills for Claude (`.claude/skills`) and Codex
  (`.agents/skills`), with explicit Add/Remove operations and no-clobber
  conflict handling.
- Named Pool membership by stable Project ULID.
- Explicit live, read-only Pool Search and Recall commands.
- Project-local Automatic Pool recall for Claude, Disabled by default. Private
  local scope uses `.claude/settings.local.json`; intentional shareable scope
  uses `.claude/settings.json`.
- Native Memory Map controls for Project-local skills and Automatic Pool recall.
- Native Memory Map Pool controls for named membership, Decouple from Pool, and
  explicit live Search Pool / Recall from Pool operations.
- Preview-first legacy shared-data recovery with approval digest, retained
  backups, exact verification, and transaction rollback on failure.

### Safety and behavior changes

- Retired global MCP registration and global Automatic Pool recall activation
  paths are no longer ordinary runtime options.
- The legacy user-level hook entry point now fails quiet and performs no memory
  work; existing user configuration is left untouched for explicit cleanup.
- Automatic Pool recall stores a Pool name and stable Project ULID, never an
  absolute machine path, and does nothing outside the connected Project.
- Pool membership changes never copy or delete Project memory.
- Normal Recall and Search remain connected-Project-only; cross-Project Recall
  and Search require an explicit named Pool.
- The Memory Map uses Connect BrainCell language for client setup and no longer
  presents retired MCP-registration copy in the active UI.

### Baseline evidence

- Release performance measurements are recorded as baseline observations only;
  they are not performance guarantees. See
  `docs/2026-07-27-v0.4.0-performance-baseline.md`.
