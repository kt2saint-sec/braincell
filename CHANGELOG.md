# Changelog

Release-facing notes for the public BrainCell project. This file records
verified product changes only; private engineering history and machine-specific
details are intentionally excluded.

## Unreleased

### Added

- Windows and macOS test runners alongside Linux in continuous integration,
  with a cross-platform wheel smoke test.

### Fixed

- Configuration files are now written atomically on Windows. The previous
  implementation used a POSIX-only call unavailable before Python 3.13, left the
  temporary file's handle open when that call failed, and then reported the
  resulting cleanup error in place of the original cause. Permissions are now
  applied after the file is closed, which works on every supported platform.

### Changed

- The test suite no longer assumes a Linux host. File reads and writes declare
  UTF-8 explicitly instead of relying on the platform default encoding, and
  checks that assert Linux-specific behaviour — XDG desktop launchers, display
  detection, and POSIX file modes — now run only on Linux.

### Known limitations

- The Memory Map authentication token is created with POSIX mode `0600`. No
  equivalent restriction is applied on Windows; the file inherits the
  permissions of the user configuration directory, which is user-scoped by
  default.

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
