# Connect a Project to BrainCell

This runbook connects BrainCell to one selected **Project**. A Project does not
need Git, but BrainCell makes that boundary explicit and avoids machine-wide MCP
activation. See [NAMINGS.md](../NAMINGS.md) for the public vocabulary.

> **Current scope:** this runbook covers the shipped project-local connection
> and skills plus the CLI Pool foundation. Legacy migration, project-local
> automatic Pool recall, and complete Memory Map Pool controls remain separate
> work.

## Prerequisites

- BrainCell and the selected client's CLI are installed.
- The default local embedder is available: `ollama pull qwen3-embedding:4b`.
- You know the Project folder to build and connect.

## Build and connect

```bash
braincell build /path/to/project
braincell connect /path/to/project --client codex
```

The second command manages only the selected Project's client configuration:

| Client | Project-local configuration |
| --- | --- |
| Codex | `.codex/config.toml` |
| Claude | private local-project scope by default; explicit shareable `.mcp.json` project scope is available with `--scope project` |
| VS Code | `.vscode/mcp.json` |

The configuration writer preserves unrelated content, refuses a conflicting
user-managed BrainCell entry, creates a backup, and replaces the target file
atomically. It does not use a client-wide registration command.

Codex loads project configuration only after the Project is trusted. Therefore
the correct activation check is a new Codex session in this trusted Project; an
unrelated Project or non-Project directory must not gain BrainCell from this
connection.

## Optional Project skills

Adding skills is separate from connecting BrainCell:

```bash
braincell skills add /path/to/project --client claude
braincell skills add /path/to/project --client codex
```

Claude skills go only to `<project>/.claude/skills`; Codex skills go only to
`<project>/.agents/skills`. Remove them with `braincell skills remove` and the
same Project/client selection. BrainCell leaves edited same-name skills
untouched and reports the conflict.

## Target safety

BrainCell resolves symlinks and displays the resolved target. It refuses `/`.
It requires an explicit acknowledgement for a home directory, a non-Git
Project, and root/sudo execution. In noninteractive use supply the matching
acknowledgement flag; do not depend on a prompt. Package installation location
never selects a Project.

## Intentional cross-Project queries

Use a **Pool** when cross-Project reading is needed. Pools store stable Project
ULIDs, not paths and not copied memory:

```bash
braincell pool create "release work"
braincell pool add "release work" <project-a-ulid> <project-b-ulid>
braincell pool recall "release work" "rollback guardrails"
```

Normal Recall and Search stay in the connected Project. Pool operations are
explicit and open members read-only. If a member has moved, is unavailable,
corrupt, or schema-incompatible, BrainCell reports and skips that member rather
than widening the query or failing all results.

To remove one membership without touching any memory or client connection:

```bash
braincell pool decouple "release work" <project-b-ulid>
```

## Disconnect safely

```bash
braincell disconnect /path/to/project --client codex
```

This removes only BrainCell's managed entry for the selected client and
Project. It preserves the Project database and all unrelated configuration.
Existing legacy client-wide entries are detected for explicit cleanup; they are
not silently removed.
