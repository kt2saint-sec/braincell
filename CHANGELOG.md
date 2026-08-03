# Changelog

Release-facing notes for the public BrainCell project. This file records
verified product changes only; private engineering history and machine-specific
details are intentionally excluded.

## Unreleased

### Added

- Digest-gated `braincell storage --hard-prune` preview/apply workflow for
  eligible expired tombstones, old operation history, and unprotected backups.
  The apply step re-plans under the destination lock, requires the reviewed
  digest and an exact confirmation phrase, and records durable per-Project
  started/completed/failed audit events.
- Optional same-host local recovery snapshot before hard-prune. It is clearly
  optional and never represented as a guaranteed backup; a requested snapshot
  failure aborts before deletion.
- SQLite integrity/foreign-key verification and WAL TRUNCATE + `VACUUM` after
  hard-prune. Live readers are never forced closed: the GUI waits briefly then
  reports a safe compaction retry state.
- Connected Project-only Memory Map Storage & lifecycle review surface with
  disk-impact estimates, candidate proof, approval digest, explicit final
  confirmation, and the deliberately narrow Trust verified maintenance setting.

### Fixed

- Resolved the missing authorized SQLite compaction/hard-prune execution
  workflow. Active/superseded notes, indexed documents/chunks, semantic
  similarity, and LLM judgments remain outside permanent cleanup authority.

## 1.0.0 - 2026-08-01

### Added

- **Cross-platform SSoT module** (`braincell/platform.py`) — single source of
  truth for data/config paths, desktop launchers, and legacy-service removal
  across Linux, macOS, and Windows. All platform-specific ``sys.platform`` checks
  and hardcoded home-directory paths are consolidated into one module.
- **Cross-platform desktop launcher** (Linux XDG ``.desktop``, macOS ``.app``
  wrapper under ``~/Applications``, Windows Start Menu ``.lnk`` via PowerShell
  COM). The Windows launcher is dependency-free (no ``pywin32`` required).
- **Cross-platform legacy-service removal** covering Linux ``systemctl`` user
  units, macOS ``LaunchAgents``, and Windows Task Scheduler (defensive
  best-effort).
- **OpenCode client adapter** (`braincell install --client opencode`) for
  project-local ``opencode.json`` MCP configuration. Connect BrainCell, manage
  ``mcp.braincell`` entries (idempotent and conflict-safe), and disconnect
  alongside Codex, Claude, and VS Code.
- 22 cross-platform regression tests covering path resolution, launcher
  dispatch, and legacy-service removal on all three platforms.

- Windows and macOS test runners alongside Linux in continuous integration,
  with a cross-platform wheel smoke test.
- Read-only `braincell storage` accounting for Project databases, row counts,
  catalogs, locks, and owned backup files.
- Dry-run backup-retention planning with `--keep-backups`; optional
  `--backup-root` arguments include external recovery directories without
  deleting anything.
- Opt-in retention execution for `braincell storage`. `--expire-operations-days`
  and `--expire-tombstones-days` plan operation-history expiry and hard-purging
  of long-tombstoned notes; `--apply` executes the printed plan. Every axis is
  disabled by default, `--apply` is refused when no retention option is
  configured, and the work runs under the destination mutation lock.
- Retention plans mark backup snapshots referenced by undo history as
  **protected** rather than as removal candidates, and re-verify that protection
  at delete time. Active and superseded memory is never a candidate.
- Read-only foreign-document reconciliation: `braincell reconcile-foreign-documents
  preview|apply` finds `bc_documents` rows attributed to a Project other than the
  database they live in and migrates them into their true owner's database only
  after a source backup, full verification, and a whole-selection commit; it
  refuses the entire selection if any owner is unattributable or conflicted.
- Read-only orphan detection: `braincell storage --list-orphans` and a new
  `orphans` key in the storage report surface path-registry rows with no
  matching directory and Project databases with no registry row. Detection
  only — no deletion or auto-repair.
- `braincell storage` now also reports freelist/page detail, embedding
  coverage, a count of foreign-document rows, and a WAL-starvation warning.

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
- Gave canonical skill documents a deterministic authority. When historical
  transcripts carry several bodies for one skill, the newest source-file
  modification time wins, ties break on content hash, and the winning authority
  is persisted so re-ingestion in any order converges on the same body.
- Removed the free `upsert_document`/`upsert_chunk` helpers, which committed
  caller-owned SQLite connections outside the store's transaction ownership.
- Changed Ollama embedding warm-up to use a Build-scoped keep-alive followed by
  an explicit unload, and moved synchronous reranking calls off the async event
  loop with bounded concurrency.
- Gave the Memory Map a cross-platform build lifecycle: a killed or crashed
  Memory Map no longer orphans a running Build indefinitely on Windows (a Job
  Object with kill-on-close) or macOS (a detached watchdog process); Linux
  keeps its existing `prctl` guard unchanged.
- Extended native launcher installation to macOS (a minimal `.app` wrapper
  under `~/Applications`) and Windows (a Start Menu `.lnk`); all platforms
  launch the same single-command, per-project preflight path.
- Gave the Memory Map auth token real ACL restriction on Windows via `icacls`;
  previously `os.chmod` there only toggled the read-only bit.
- Resolved platform-appropriate default data roots on macOS
  (`~/Library/Application Support`) and Windows (`%LOCALAPPDATA%`), while an
  already-populated legacy `~/.local/share`-style root always wins and nothing
  is ever migrated automatically.
- Required the same safety backup `consolidate`/`reflect` already required
  before `build --reembed` and the Memory Map's clear operation, each with an
  explicit, loudly-logged override (`--no-backup`, `skip_backup`).
- Made a failed rotating-file-handler construction retry once with
  conservative defaults, then disable file logging rather than ever falling
  back to an unbounded file handler.

### Changed

- **PySide6/QtWebEngine is a required base dependency.** Every supported
  BrainCell installation includes the native Memory Map; there is no headless
  or server-only product mode. The historical `gui` and `native` extras remain
  as empty compatibility aliases, while `openai` and `dev` remain optional.
- Clarified and enforced the Memory Map context boundary: selecting a map
  Project is catalog-only; ordinary Search and Recent notes stay with the
  Connected Project; named Pool Search and Recall are the only cross-Project
  read paths.
- Added an explicit GUI confirmation-and-retry flow for intentional non-Git
  Projects, plus structured target error codes: malformed paths return `400`;
  acknowledgement and configuration conflicts return `409`.
- **GUI/QtWebEngine tests now run through a serialized, resource-bounded
  runner.** It isolates per-run state, disables GPU compositing, and contains
  renderer descendants so a test cannot leak work into the developer desktop.
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

- BrainCell never expires anything on its own. Retention runs only when you pass
  an explicit window and `--apply`; there is no default retention age, and
  indexed transcripts and curated memory are never expiry candidates.
- Storage budgets, configurable warnings, and hard limits remain unimplemented;
  BrainCell does not delete memory to enforce them.

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
  they are not performance guarantees.
