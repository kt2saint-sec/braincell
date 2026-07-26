# Quickstart

BrainCell connects to one selected **Project** at a time. Installing the
package does not enable it for unrelated Projects. The terminology contract is
[NAMINGS.md](NAMINGS.md).

## Install BrainCell

```bash
git clone https://github.com/kt2saint-sec/braincell.git
cd braincell
./scripts/install.sh
```

The installer creates a local environment and, when available, fetches the
default Ollama embedding model. With an existing environment, use:

```bash
python3 -m pip install "braincell-mcp @ git+https://github.com/kt2saint-sec/braincell.git"
ollama pull qwen3-embedding:4b
```

## Build and connect a Project

```bash
cd /path/to/project
braincell build .
braincell connect . --client codex
```

The path is the Project selection; it is unrelated to where BrainCell is
installed. `connect` shows the selected Project and client before it changes
configuration. It resolves symlinks, refuses `/`, and asks for deliberate
acknowledgement for home directories, non-Git Projects, and privileged runs.

Choose a client explicitly:

```bash
braincell connect . --client claude --scope local   # private to this Project
braincell connect . --client claude --scope project # shareable .mcp.json
braincell connect . --client vscode                 # .vscode/mcp.json
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
```

Skills are not added by `connect`, never install machine-wide, and can be
removed with `braincell skills remove . --client <client>`. Edited same-name
skills are reported as conflicts and left untouched.

## Recall and Search

```bash
braincell recall "how did we handle rate limiting?"
braincell search "throttle"
braincell start .
```

Recall and Search operate on the selected Project's memory. `braincell start`
opens the native **Memory Map** for that Project. Its internal localhost
transport is not an external browser UI or background service.

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

## Disconnect

```bash
braincell disconnect . --client codex
```

This removes only BrainCell's managed connection for the selected client and
Project. Project memory remains intact. `braincell uninstall` is a temporary
compatibility alias.
