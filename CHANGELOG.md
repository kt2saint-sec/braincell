# Changelog

Release-facing notes for the public BrainCell project. This file records
verified product changes only; private engineering history and machine-specific
details are intentionally excluded.

## Unreleased

- No unreleased changes.

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
