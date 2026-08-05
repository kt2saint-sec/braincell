# BrainCell

[![PyPI](https://img.shields.io/pypi/v/braincell-mcp)](https://pypi.org/project/braincell-mcp/)

Created by [Karl Toussaint (kt2saint)](https://github.com/kt2saint-sec).

BrainCell is a local-first memory platform for your projects. Each **Project** gets its own private memory — one database with hybrid semantic + keyword recall and a native **Memory Map** desktop app — and your AI coding tools (Claude Code, Codex, VS Code, OpenCode) connect to it over MCP. Everything runs on your machine.

Isolation is the core contract: each Project has one database and a stable project ULID, and connecting BrainCell to Project A never starts it for, or exposes it to, Project B.

See [CHANGELOG.md](CHANGELOG.md) for verified public release notes.

## Current status

This release establishes project-local connections and skills, live named Pools, native Memory Map Pool controls, and preview-first recovery for retired shared data. Legacy automation and shared-data behavior are not part of the project-only workflow.

## What is isolated

- Project memory is stored and queried per Project.
- Codex uses only `<project>/.codex/config.toml`; Codex must trust the project before it loads that configuration.
- VS Code uses only `<project>/.vscode/mcp.json`.
- Claude connection is project-bounded: private local-project scope is the default, and shareable `.mcp.json` scope is an explicit choice.
- A package installation can live anywhere on the machine. It does not select a Project or enable BrainCell in another Project.

Connection management preserves unrelated client configuration. It writes only BrainCell's entry, refuses a conflicting user-managed entry, creates a backup, and replaces the configuration atomically. Existing legacy client-wide entries are detected for explicit cleanup; BrainCell never silently removes them.

## Install

BrainCell supports Linux, macOS, and Windows — every release is tested on all
three (Python 3.11–3.13). You need:

- **Python 3.11 or newer**
- **[Ollama](https://ollama.com)** running locally for the default embedding
  model (or the optional OpenAI extra for hosted embeddings)

The native Memory Map desktop GUI (PySide6/QtWebEngine) is a required BrainCell
runtime dependency and is installed with every supported installation. There is
no supported headless or server-only BrainCell installation.

### Install with pipx

One copy-paste block per platform installs BrainCell and Ollama. The verified
default embedding model (`qwen3-embedding:4b`, several GB) is **downloaded by
BrainCell itself, with your consent, the first time you run `braincell setup`
or `braincell build`** — there is no separate model step.

#### Debian/Ubuntu

```bash
sudo apt update && sudo apt install -y pipx python3-venv
pipx ensurepath && source ~/.bashrc
pipx install braincell-mcp
curl -fsSL https://ollama.com/install.sh | sh   # Ollama's official installer
```

Desktop installs already have the system libraries Qt needs. On a minimal or
container Ubuntu/Debian (no desktop session), also install them:

```bash
sudo apt install -y --no-install-recommends \
  libegl1 libgl1 libopengl0 libxkbcommon0 libxcb-cursor0 libnss3 \
  libasound2t64 libxcomposite1 libxdamage1 libxrandr2 libxtst6 \
  libgbm1 libxkbfile1 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
  libxcb-render-util0 libxcb-shape0 libxcb-xkb1
```

#### macOS

```bash
brew install pipx ollama
pipx ensurepath && source ~/.zshrc
pipx install braincell-mcp
brew services start ollama   # or run `ollama serve` in a spare terminal
```

#### Windows PowerShell

```powershell
py -m pip install --user pipx
pipx ensurepath
pipx install braincell-mcp
```

Then install Ollama from [ollama.com](https://ollama.com/download/windows) —
the Ollama application starts its service automatically.

If you prefer to download the model yourself (or need it before a
non-interactive run), the manual command is `ollama pull qwen3-embedding:4b`.

Verify:

```bash
braincell --help
braincell-mcp --help
pipx list
```

`braincell --help` lists every subcommand; `pipx list` reports the installed
`braincell-mcp` version. There is no `braincell --version` flag.

Upgrade later:

```bash
pipx upgrade braincell-mcp
```

Ubuntu/Debian may reject ordinary system `pip install` commands because of the externally managed Python environment. `pipx` is the recommended production installation method.

For hosted embeddings, install the optional OpenAI extra and configure its documented provider environment:

```bash
pipx install "braincell-mcp[openai]"
```

For source/developer installation from a checkout:

```bash
git clone https://github.com/kt2saint-sec/braincell.git
cd braincell
./scripts/install.sh
```

For a temporary source install directly from Git:

```bash
python3 -m pip install "braincell-mcp @ git+https://github.com/kt2saint-sec/braincell.git"
```

Installing the package never selects a Project, creates a database, or changes a client configuration. After installation, connect one selected Project with the dry-run/apply flow below. The commands are `braincell`, `braincell-mcp`, and `braincell-map`.

## Connect one Project

Choose the Project deliberately. BrainCell resolves symlinks, refuses `/`, and requires acknowledgements for a home directory, a non-Git Project, or a privileged/root invocation.

```bash
cd /path/to/project
braincell setup . --dry-run --client codex
braincell setup . --client codex --yes
```

The first command resolves the path and displays every planned database, registry, client-configuration, skills, optional Pool-recall write — and, when the embedding model is not downloaded yet, the model download — without applying any of it. `--yes` applies the plan. Use `--with-skills` for project-local skills and `--automatic-pool-recall "Pool name"` only for an existing named Pool with Claude.

```bash
braincell build .
braincell connect . --client claude --scope local
```

`braincell install` remains a compatibility alias for `braincell connect`; `uninstall` remains an alias for `disconnect`. Disconnecting removes only BrainCell's managed entry for that client and Project. It does not delete Project memory.

For Codex, open the selected trusted Project after connecting. A Codex session outside that Project has no BrainCell project configuration to load.

Skills are a separate, explicit choice:

```bash
braincell skills add . --client claude
braincell skills add . --client codex
braincell skills add . --client opencode
braincell skills remove . --client claude
```

BrainCell never installs these skills machine-wide. Installing and removing
skills preserves an edited same-name skill and reports it as protected;
a skill an earlier BrainCell release installed is recognized and updated in
place.
In the Memory Map, **Install skills** and **Remove unchanged skills** apply
only to the Connected Project. They never change Pool membership or widen
memory access.

## Use Project memory

```bash
braincell recall "how did we handle rate limiting?"
braincell search "throttle"
braincell start .
```

`braincell start`, `braincell gui`, and `braincell-map` open the native **Memory Map** for the selected Project. The embedded localhost server is an implementation detail of that desktop app, not a browser product or an always-on service. `braincell gui . --install-launcher` adds a desktop launcher for the Project (Linux application menu, macOS `~/Applications`, Windows Start Menu).

Build reads supported documents and transcripts into that Project's database. `braincell sync` is the incremental compatibility alias for Build. Remember, Forget, and Correct memory are MCP actions; normal Recall and Search are always limited to the connected Project.

In the Memory Map, selecting a Project changes its catalog card, statistics, and
Pool membership controls. The ordinary Search and Recent notes panes always
name and read the Connected Project. Use named Pool Search or Recall for an
intentional cross-Project read.

Inspect persistent state without changing it:

```bash
braincell storage .
braincell storage . --keep-backups 3
braincell storage . --keep-backups 3 --backup-root /path/to/recovery-backups
# Warning-only review thresholds; values are bytes and never change memory.
braincell storage . --warn-project-bytes 1073741824 --warn-free-bytes 2147483648
```

The report includes file sizes and Project row counts. Retention output is a
dry-run plan by default: it never deletes backups, indexed transcripts,
operation history, tombstones, or curated memory. Project databases grow with
indexed content and retained history, so use this report to review storage
deliberately.

The optional `--warn-project-bytes` and `--warn-free-bytes` thresholds make a
read-only review warning visible in CLI output. They are intentionally per
command rather than a hidden machine assumption: choose margins that suit the
actual disk. A warning never blocks normal use, deletes memory, or enables
cleanup. The Memory Map also highlights when the exact optional snapshot or
compaction workspace cannot fit on the local disk.

Executing retention is a separate, explicit step:

```bash
braincell storage . --keep-backups 3 --apply
braincell storage . --expire-operations-days 180 --expire-tombstones-days 180 --apply
```

Nothing is ever expired by default — `--apply` is refused unless at least one
retention option is configured, snapshots referenced by undo history (and
tombstoned notes referenced by recorded operations) are never deleted, and
active or superseded memory is never touched.

### Permanent stale-state cleanup and compaction

For the smaller, evidence-backed permanent workflow, preview first and retain
the printed digest:

```bash
braincell storage . --hard-prune --keep-backups 3 --expire-tombstones-days 180
braincell storage . --hard-prune --keep-backups 3 --expire-tombstones-days 180 \
  --apply --approve <digest> \
  --confirm "DELETE WITHOUT LOCAL RECOVERY SNAPSHOT"
```

Hard-prune can only remove expired tombstones, old operation history, and
unprotected backup files. It never selects active/superseded memory, indexed
documents/chunks, semantic similarity matches, or LLM suggestions. Add
`--local-recovery-snapshot` to request a same-host copy first, then confirm
with `DELETE`; a snapshot is optional and is not a guaranteed backup. If a
live reader blocks WAL truncation, cleanup remains recorded and consistent,
while compaction reports a safe retry state instead of closing clients.

The Memory Map offers the same Connected Project-only Analyze → Review →
Confirm → Run flow. Its optional trust setting only skips retyping `DELETE`;
it never skips evidence, digest verification, final Apply, or execution
safeguards.

## Pools: intentional, live cross-Project reads

A **Pool** is a named set of stable Project ULIDs. It stores memberships only: it never contains copied notes, documents, chunks, or vectors. Pool Search and Recall resolve members through the registry at query time and open their databases read-only. Missing, inaccessible, corrupt, or incompatible members are reported and skipped without failing the whole query.

```bash
braincell pool create "release work"
braincell pool add "release work" <project-a-ulid> <project-b-ulid>
braincell pool search "release work" "deployment rollback"
braincell pool recall "release work" "which rollout guardrail applies?"
braincell pool decouple "release work" <project-b-ulid>
```

Decouple from Pool removes only that membership. It never changes either Project's memory, client connection, Project registration, or membership in another Pool. Re-adding a member restores its live results without a Build.

## Optional Automatic Pool recall

Automatic Pool recall is disabled by default. Enable it only for a selected Project and Pool:

```bash
braincell automatic-pool-recall enable . --pool "release work"
braincell automatic-pool-recall status .
braincell automatic-pool-recall disable .
```

Claude private-local scope writes only `.claude/settings.local.json`. Add `--scope project` only when you intentionally want shareable `.claude/settings.json`. The hook stores the stable Project ULID and Pool name, not an absolute Project path. It no-ops outside that connected Project, and ordinary Recall remains Project-only.

## Safety model

- There is no ordinary query that reads every Project.
- There is no shared operational memory database.
- Writes remain pinned to the Connected Project.
- Concurrent Build and maintenance mutations for one Project are refused rather
  than allowed to interleave.
- Pool reads are explicit; ordinary Recall or Search never silently widens scope.
- Keyword operations remain available when embeddings are unavailable; an
  explicitly semantic Search still reports the provider failure.
- A legacy shared installation or database is a recovery/migration concern, not a normal runtime mode. Do not delete it until the dedicated migration workflow has previewed, backed up, and verified the recovery.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e ".[dev,openai]"
python3 -m pytest
ruff check braincell tests
```
The Memory Map is a PySide6/QtWebEngine application. Test its native window and bridge for desktop changes; a standalone-browser test is supplemental only. See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution requirements and [ARCHITECTURE.md](ARCHITECTURE.md) for the module, CLI, schema, and on-disk state map.
