# Quickstart

Get `braincell-mcp` running in about five minutes. This is the short path; the
[README](README.md) has the full reference.

## 1. Prerequisites — a local embedder

braincell embeds locally with [Ollama](https://ollama.com) by default, so **no API key is
required**. Install Ollama and pull the default embedding model before installing braincell:

```bash
# Install Ollama — see https://ollama.com/download (Linux one-liner shown):
curl -fsSL https://ollama.com/install.sh | sh

# Pull the default embedder (~2.5 GB). Ollama serves it on localhost:11434:
ollama pull qwen3-embedding:4b
```

> Prefer hosted embeddings? Set `BRAINCELL_EMBED_PROVIDER=openai` and `OPENAI_API_KEY`
> instead — no Ollama needed.

## 2. Install braincell

From a checkout:

```bash
pip install .          # core (CLI + MCP server)
pip install .[gui]     # also the Memory Map web UI
```

Or, per-user, without cloning:

```bash
pipx install 'braincell-mcp[gui]'
```

Either way you get three commands on your PATH: `braincell` (CLI), `braincell-mcp` (MCP
server), and `braincell-map` (opens the Memory Map).

## 3. Build your first brain

Index the current project into its own brain (creates it on first run):

```bash
cd /path/to/your/project
braincell build .
```

## 4. Connect it to your MCP client

One command registers the MCP server for the current project (Claude Code shown; use
`--client codex` or `--client vscode` for others):

```bash
braincell install
# then restart your client so it loads the server —
# the mcp__braincell__* tools light up
```

## 5. Use it

Your agent now has persistent memory tools (`search`, `recall`, `remember`, `supersede`,
`forget`, …). From the terminal you can also:

```bash
braincell recall "how did we handle rate limiting?"   # query curated notes
braincell search "throttle"                            # hybrid keyword + vector search
braincell gui                                          # open the visual Memory Map
```

## Where to next

- **Group related repos** into a family for cross-project recall, or **pool** brains into a
  shared global brain — see the [README](README.md).
- **Maintenance** (`consolidate`, `reflect`, `contradictions`, `backup`, `memory undo`) is
  available from the CLI and the Memory Map's ⌘ Commands panel.
