# Changelog

Release-facing notes for the public BrainCell project. This file records
verified product changes only; private engineering history and machine-specific
details are intentionally excluded.

## Unreleased — project-only architecture

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
- Native Memory Map Commands controls for creating, adding to, decoupling from,
  and deleting named Pools, plus explicit live Pool Search and Recall. These
  controls manage ULID membership metadata only; they do not materialize a
  shared memory database.

### Safety and behavior changes

- Retired global MCP registration and global Automatic Pool recall activation
  paths are no longer ordinary runtime options.
- The legacy user-level hook entry point now fails quiet and performs no memory
  work; existing user configuration is left untouched for explicit cleanup.
- Automatic Pool recall stores a Pool name and stable Project ULID, never an
  absolute machine path, and does nothing outside the connected Project.
- Pool membership changes never copy or delete Project memory.
- Ordinary Memory Map scope remains the connected Project. Cross-project reads
  require an explicitly named Pool action.

### Still in progress

- Final database-open isolation coverage for native Memory Map Pool queries.
- Preview-first recovery and migration of legacy global configuration and
  memory data.
- Retirement of legacy runtime readers after migration verification.
- Native GUI acceptance coverage, performance measurements, and lifecycle
  start/failure/restart notifications.
