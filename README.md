# BrainCell

BrainCell is local-first project memory for MCP clients. Each **Project** has
one database and a stable project ULID. Connecting BrainCell to Project A never
starts it for, or exposes it to, Project B.

The public vocabulary is defined in [NAMINGS.md](NAMINGS.md). In particular,
BrainCell uses **Project**, **Build**, **Connect**, **Pool**, and **Decouple
from Pool**. Earlier terms are compatibility or migration language only.
See [CHANGELOG.md](CHANGELOG.md) for verified public release notes.
For recovery from an older shared-data installation, see
[the project-only migration guide](docs/project-only-migration.md).

## Current status

This release establishes project-local connections and skills, CLI Pools,
Project-local Automatic Pool recall for Claude, and native Memory Map Pool
membership/live-query controls. Ordinary Memory Map operations are now guarded
to the Connected Project; native desktop acceptance and the migration retirement
workflow are still in progress. Read-only inventory, verified backups, an
explicit provenance-only apply command, and a required recovery receipt are
available; ambiguous rows remain untouched and no retirement occurs. Do not
rely on legacy automation or shared-data behavior as part of the project-only
workflow.

## What is isolated

- Project memory is stored and queried per Project.
- Codex uses only `<project>/.codex/config.toml`; Codex must trust the project
  before it loads that configuration.
- VS Code uses only `<project>/.vscode/mcp.json`.
- Claude connection is project-bounded: private local-project scope is the
  default, and shareable `.mcp.json` scope is an explicit choice.
- A package installation can live anywhere on the machine. It does not select a
  Project or enable BrainCell in another Project.

Connection management preserves unrelated client configuration. It writes only
BrainCell's entry, refuses a conflicting user-managed entry, creates a backup,
and replaces the configuration atomically. Existing legacy client-wide entries
are detected for explicit cleanup; BrainCell never silently removes them.

## Install

```bash
git clone https://github.com/kt2saint-sec/braincell.git
cd braincell
./scripts/install.sh
```

Or install with pip:

```bash
python3 -m pip install "braincell-mcp @ git+https://github.com/kt2saint-sec/braincell.git"
ollama pull qwen3-embedding:4b
```

The default embedder is local Ollama `qwen3-embedding:4b`. For hosted
embeddings, install the `[openai]` extra and configure its documented provider
environment. The commands are `braincell`, `braincell-mcp`, and
`braincell-map`.

## Connect one Project

Choose the Project deliberately. BrainCell resolves symlinks, refuses `/`, and
requires acknowledgements for a home directory, a non-Git Project, or a
privileged/root invocation.

```bash
cd /path/to/project
braincell build .
braincell connect . --client codex
# or: braincell connect . --client claude --scope local
# or: braincell connect . --client vscode
```

`braincell install` remains a compatibility alias for `braincell connect`;
`uninstall` remains an alias for `disconnect`. Disconnecting removes only
BrainCell's managed entry for that client and Project. It does not delete
Project memory.

For Codex, open the selected trusted Project after connecting. A Codex session
outside that Project has no BrainCell project configuration to load.

Skills are a separate, explicit choice:

```bash
braincell skills add . --client claude # writes .claude/skills
braincell skills add . --client codex  # writes .agents/skills
braincell skills remove . --client claude
```

BrainCell never installs these skills machine-wide. Add and Remove preserve an
edited same-name skill and report it as a conflict.

## Use Project memory

```bash
braincell recall "how did we handle rate limiting?"
braincell search "throttle"
braincell start .
```

`braincell start`, `braincell gui`, and `braincell-map` open the native
**Memory Map** for the selected Project. The embedded localhost server is an
implementation detail of that desktop app, not a browser product or an
always-on service.

Build reads supported documents and transcripts into that Project's database.
`braincell sync` is the incremental compatibility alias for Build. Remember,
Forget, and Correct memory are MCP actions; normal Recall and Search are always
limited to the connected Project.

## Pools: intentional, live cross-Project reads

A **Pool** is a named set of stable Project ULIDs. It stores memberships only:
it never contains copied notes, documents, chunks, or vectors. Pool Search and
Recall resolve members through the registry at query time and open their
databases read-only. Missing, inaccessible, corrupt, or incompatible members
are reported and skipped without failing the whole query.

```bash
# Obtain a Project ULID when building/registering the Project.
braincell pool create "release work"
braincell pool add "release work" <project-a-ulid> <project-b-ulid>
braincell pool search "release work" "deployment rollback"
braincell pool recall "release work" "which rollout guardrail applies?"
braincell pool decouple "release work" <project-b-ulid>
```

Decouple from Pool removes only that membership. It never changes either
Project's memory, client connection, Project registration, or membership in
another Pool. Re-adding a member restores its live results without a Build.
Pool names compare after Unicode NFKC normalization, whitespace normalization,
and case-folding; the original spelling remains visible.

### Optional Automatic Pool recall

Automatic Pool recall is Disabled by default. Enable it only for a selected
Project and Pool:

```bash
braincell automatic-pool-recall enable . --pool "release work"
braincell automatic-pool-recall status .
braincell automatic-pool-recall disable .
```

Claude private-local scope writes only `.claude/settings.local.json`. Add
`--scope project` only when you intentionally want shareable
`.claude/settings.json`. The hook command stores the stable Project ULID and
Pool name, not an absolute Project path. It no-ops outside that connected
Project, and ordinary Recall remains Project-only.

## Safety model

- There is no normal all-Projects query.
- There is no shared operational memory database.
- Writes remain pinned to the Connected Project.
- Pool reads are explicit; an ordinary Recall or Search never silently widens
  scope.
- A legacy shared installation or database is a recovery/migration concern,
  not a normal runtime mode. Do not delete it until the dedicated migration
  workflow has previewed, backed up, and verified the recovery.
- A completed legacy recovery apply writes a non-overwriting receipt. It is not
  a retirement action; retain the original source and verified backup until a
  later, explicit retirement workflow is available.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,openai]"
python -m pytest
ruff check braincell tests
```

The Memory Map is a PySide6/QtWebEngine application. Test its native window and
bridge for desktop changes; a standalone-browser test is supplemental only.
See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution requirements.
