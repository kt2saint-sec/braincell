# Quickstart

BrainCell connects to one selected **Project** at a time. Installing the
package does not enable it for unrelated Projects. The terminology contract is
[NAMINGS.md](NAMINGS.md).

## Install BrainCell

Three steps (Python 3.11+ required):

1. Install [Ollama](https://ollama.com) and make sure it is running.
2. `pipx install braincell-mcp`
3. In your project: `braincell setup . --client <claude|codex|vscode|opencode> --yes`

Setup previews every planned write first (add `--dry-run` to only preview) and
offers to download the default embedding model if it is not on your machine
yet — there is no separate model step. Everything else ships in the package,
including the required native Memory Map desktop GUI; there is no supported
`--server-only` installation. Platform-specific one-block installs live in the
[README](README.md); inside an activated virtual environment,
`python3 -m pip install braincell-mcp` works the same.

Developing BrainCell itself? A source checkout has its own installer:

```bash
git clone https://github.com/kt2saint-sec/braincell.git
cd braincell
./scripts/install.sh
```

## Build and connect a Project

```bash
cd /path/to/project
braincell build .
braincell connect . --client codex
```

Prefer one reviewed step? `braincell setup . --client codex --dry-run` shows
every planned write (database, registry, client configuration, optional
skills), and repeating it with `--yes` applies the plan.

The path is the Project selection; it is unrelated to where BrainCell is
installed. `connect` shows the selected Project and client before it changes
configuration. It resolves symlinks, refuses `/`, and asks for deliberate
acknowledgement for home directories, non-Git Projects, and privileged runs.

Choose a client explicitly:

```bash
braincell connect . --client claude --scope local   # private to this Project
braincell connect . --client claude --scope project # shareable .mcp.json
braincell connect . --client vscode                 # .vscode/mcp.json
braincell connect . --client opencode               # project opencode.json
```

For Codex, BrainCell writes only `.codex/config.toml`; Codex loads it only in a
trusted Project. For VS Code, it writes only `.vscode/mcp.json`. Existing
unrelated configuration is preserved. A conflicting BrainCell entry is left
alone and reported instead of overwritten.

Restart or reconnect the selected client after connecting. An unrelated folder
has no Project-local BrainCell registration to load.

Optionally add BrainCell skills to this Project:

```bash
braincell skills add . --client claude # .claude/skills
braincell skills add . --client codex  # .agents/skills
braincell skills add . --client opencode # .opencode/skills
```

Skills are not added by `connect`, never install machine-wide, and can be
removed with `braincell skills remove . --client <client>`. Edited same-name
skills are reported as conflicts and left untouched.

The Memory Map offers the same skills for the Connected Project only. Its
**Install skills** and **Remove unchanged skills** controls never accept a
directory, change Pool membership, or widen memory access. It reports each
skill as **Not installed**, **Up to date**, **Update available**, or
**Edited by you**. Update available means an earlier BrainCell release wrote
the installed copy; installing skills brings it current.

## Recall and Search

```bash
braincell recall "how did we handle rate limiting?"
braincell search "throttle"
braincell start .
```

Recall and Search operate on the connected Project's memory. `braincell start`
opens the native **Memory Map** for that Project. Its internal localhost
transport is not an external browser UI or background service.

Inside the Memory Map, a map selection is catalog context (Project identity,
statistics, and Pool membership). Ordinary Search and Recent notes remain
Connected-Project memory. Use a named Pool only for an intentional cross-Project
read.

To inspect disk use and preview backup retention without deleting anything:

```bash
braincell storage .
braincell storage . --keep-backups 3
# Warning-only thresholds, in bytes. They never block writes or remove memory.
braincell storage . --warn-project-bytes 1073741824 --warn-free-bytes 2147483648
```

Project storage grows with indexed content and retained history. The retention
list is a dry-run plan; BrainCell never silently expires curated memory.
Deleting anything requires an explicitly configured
`braincell storage . --keep-backups N --apply`, and snapshots referenced by
undo history are always kept.

Thresholds are an explicit per-command review aid, not a default reservation:
choose values appropriate for the machine. The Memory Map also shows the
Connected Project footprint and warns when its optional snapshot or future
compaction workspace cannot fit. None of these warnings changes memory.

For permanent stale-state cleanup, first review a digest-gated plan:

```bash
braincell storage . --hard-prune --expire-tombstones-days 180
braincell storage . --hard-prune --expire-tombstones-days 180 \
  --apply --approve <digest> \
  --confirm "DELETE WITHOUT LOCAL RECOVERY SNAPSHOT"
```

This workflow can only remove eligible expired tombstones, old operation
history, and unprotected backups. Use `--local-recovery-snapshot` when local
disk space permits, then type `DELETE` instead. The Memory Map provides the
same Connected Project-only review flow.

## Optional: create a Pool

Use a Pool only when you deliberately want a live, read-only query across named
Projects. A Pool holds stable Project ULIDs; it never copies memory.

```bash
braincell pool create "release work"
braincell pool add "release work" <project-a-ulid> <project-b-ulid>
braincell pool search "release work" "rollback"
braincell pool decouple "release work" <project-b-ulid>
```

Decouple from Pool changes only that membership. It does not delete memory or
disconnect an MCP client.

Optional Automatic Pool recall for Claude is Project-local and Disabled by
default:

```bash
braincell automatic-pool-recall enable . --pool "release work"
braincell automatic-pool-recall disable .
```

The default uses private `.claude/settings.local.json`. Use `--scope project`
only for an intentional shareable `.claude/settings.json`. It never changes
ordinary Project-only Recall or Search.

## Disconnect

```bash
braincell disconnect . --client codex
```

This removes only BrainCell's managed connection for the selected client and
Project. Project memory remains intact. `braincell uninstall` is a temporary
compatibility alias.
