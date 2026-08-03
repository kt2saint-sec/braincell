# BrainCell naming contract

This file is the public language contract for BrainCell's user interface, CLI
help, README, tutorials, error messages, and release documentation.

Use **BrainCell** for the product in prose and UI. Use lowercase `braincell`
only for commands, packages, executable names, paths, environment variables,
and code identifiers.

## Project boundaries

- **Project** — a directory with one BrainCell memory database and a stable
  project ULID. Say **project folder** only while asking for a filesystem path.
  Do not use “repo”; Git is not required for a BrainCell project.
- **Project memory** — searchable information stored in one Project database.
- **Viewed project** — the Project currently selected in the Memory Map for
  catalog identity, statistics, and membership controls; it does not change the
  source of ordinary memory panes.
- **Connected project** — the Project connected to the current MCP or Memory
  Map session. New memory always saves here, and ordinary Memory Map Search and
  Recent notes read here. When it differs from the Viewed project, say:
  “Viewing Project B's catalog. Connected Project A memory remains shown.”

## Everyday actions

- **Build** — read supported documents and transcripts into the selected
  Project's searchable memory. `sync` is a documented compatibility alias for
  Build during the transition period.
- **Connect BrainCell** — configure BrainCell for a selected client in one
  Project. Prefer **Connect to Codex**, **Connect to Claude**, **Connect to
  OpenCode**, or **Connect to VS Code**.
- **Disconnect BrainCell** — remove one Project's client connection without
  deleting its memory. Prefer client-specific labels.
- **Memory Map** — the BrainCell desktop application. “Native GUI” belongs only
  in developer/architecture documentation.
- **Search** — find ranked document and transcript content.
- **Recall** — retrieve saved memory notes and resolve corrections to current
  truth.
- **Remember** — save a curated memory note.
- **Forget** — soft-delete a memory note while preserving necessary history.
- **Correct memory** — replace saved memory with corrected information while
  preserving provenance.
- **Storage report** — a read-only account of BrainCell state and an optional
  backup-retention dry run. Never call a dry-run candidate list a cleanup or
  imply that BrainCell deleted memory.
- **Hard-prune review** — the Connected Project-only, preview-first review of
  eligible expired tombstones, old operation history, and unprotected backups.
  Say **review** or **plan** until the person has approved its exact digest.
- **Approval digest** — the exact stable value that binds final Apply to the
  reviewed hard-prune selection. Never describe it as a password or an LLM
  instruction.
- **Local recovery snapshot** — an optional same-host copy made before
  hard-prune. Always say it is not a guaranteed backup and describe its local
  disk growth.
- **Trust verified maintenance** — a per-Project setting that skips only the
  typed `DELETE` confirmation after its serious acknowledgement. Never imply
  that it permits unattended LLM cleanup or bypasses review, proof, digest,
  final Apply, snapshot choice, or execution safeguards.

## Pools

- **Pool** — a named group of Project ULIDs used only for intentional, live,
  read-only cross-project Recall or Search. A Pool has memberships, not copied
  memory and not a database.
- **Add to Pool** — add a Project's stable ULID to a Pool.
- **Search Pool** / **Recall from Pool** — explicit cross-project operations.
- **Decouple from Pool** — remove one Project's membership from one Pool. Show
  this explanation nearby: “Removes this project from the Pool. Its memory and
  client connections are unchanged.”
- **Automatic Pool recall** — optional proactive Pool-memory offer during a
  client session. It is project-local and Disabled by default.

Pool names are unique after Unicode NFKC normalization, trimming, collapsing
internal whitespace, and Unicode case-folding. The original spelling is kept
for display.

## Skills and legacy language

- **Project skill** — a BrainCell skill installed in a selected Project.
  Use **Add skills** and **Remove skills**; never imply machine-wide install.
- Legacy terms such as Global, Family, Federate, Unpool, Register MCP,
  Deregister MCP, Active Project, Launch Project, and Supersede may appear only
  in migration help, changelog history, compatibility notices, internal code,
  or tests that explicitly cover migration/deprecation.
