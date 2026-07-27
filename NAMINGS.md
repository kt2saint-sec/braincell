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
- **Viewed project** — the Project currently displayed in the Memory Map. In
  the current project-only Memory Map, ordinary memory views remain on the
  Connected project; selecting another Project catalog entry shows metadata and
  explains that its memory is not open. Pool results identify their source
  Project instead.
- **Connected project** — the Project connected to the current MCP or Memory
  Map session. New memory always saves here. When it differs from the Viewed
  project, say: “Viewing Project B. New memory still saves to Project A.”

## Everyday actions

- **Build** — read supported documents and transcripts into the selected
  Project's searchable memory. `sync` is a documented compatibility alias for
  Build during the transition period.
- **Connect BrainCell** — configure BrainCell for a selected client in one
  Project. Prefer **Connect to Codex**, **Connect to Claude**, or **Connect to
  VS Code**.
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

## Legacy recovery

- **Legacy recovery** — the preview-first workflow for data from an earlier
  shared-data installation. It is not a normal runtime mode.
- **Recovery receipt** — a non-overwriting JSON record of a completed,
  provenance-only legacy recovery apply. It records the verified backup,
  selected Project ULIDs, results, and retained audit trail. It does not retire
  or delete the original source.

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
