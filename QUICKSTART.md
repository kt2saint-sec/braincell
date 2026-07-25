# Quickstart

Get `braincell-mcp` running in about five minutes. This is the short path; the
[README](README.md) has the full reference.

The package is named `braincell-mcp`; the public source repository is
`kt2saint-sec/braincell`.

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

```bash
pip install braincell-mcp
```

(Or isolated per-user: `pipx install braincell-mcp`. From a source checkout,
`pip install .` is equivalent.) You get three commands on your PATH: `braincell` (the CLI,
including the `start` launcher), `braincell-mcp` (the MCP stdio server your client runs),
and `braincell-map` (the global Memory Map). The Memory Map GUI is part of the base
install — no extra needed.

## 3. Start

```bash
cd /path/to/your/project      # any folder you work in — no git required
braincell start
```

One command does the rest. It checks the embedder first — if Ollama is down or the model
isn't pulled, it prints the exact fix before anything else — then opens the native Memory
Map application. On a first run the map opens straight into a short **guided tour**; follow it
to **1 · ✚ Add project**, one wizard that **Builds** the folder's memory, **Registers the
MCP** server for your client (Claude Code, Codex, or VS Code), and optionally joins it to a
**Family** for cross-project recall.

Then reconnect your MCP client — run `/mcp` in Claude Code (or restart the client) — and
the `mcp__braincell__*` tools are live.

Two things to expect on a fresh start:

- **The embedder chip.** The map's header shows embedder status. If it's red, click it for
  the fix (`ollama pull qwen3-embedding:4b`) — Build refuses to run until the embedder is
  ready, rather than silently indexing without vectors.
- **A new folder starts small.** Build indexes the folder's agent transcripts and
  documents; a folder with no history yet builds a near-empty brain. That's normal —
  memory accrues as you work and `remember` notes land.

Day to day, just run `braincell start` again: it raises the already-running native window
instead of starting a second one. Closing the window exits the GUI and releases its localhost port.

The GUI is not a headless service. Its embedded renderer uses a token-protected
`127.0.0.1` FastAPI server internally, but the supported product surface is the native
window. Older installs that created `braincell-map.service` can inspect or remove that
retired unit with `braincell legacy-service status|remove`.

### Prefer the terminal?

The same flow, by hand:

```bash
braincell build .        # Build this folder's memory
braincell install        # Register the MCP server (+ the family-recall hook, disarmed)
# reconnect your client (/mcp in Claude Code) → the mcp__braincell__* tools light up
```

## 4. Use it

Your agent now has persistent memory tools (`search`, `recall`, `remember`, `supersede`,
`forget`, …). From the terminal you can also:

```bash
braincell recall "how did we handle rate limiting?"   # query curated notes
braincell search "throttle"                            # hybrid keyword + vector search
braincell start                                        # (re)open the visual Memory Map
```

## Where to next

- **Group related project folders** into a family for cross-project recall, or **pool**
  brains into a shared global brain — see the [README](README.md).
- **MCP on or off?** Click any cell on the map — its inspector shows the registration
  state with **Register MCP** / **Deregister MCP** buttons. To restart the MCP server
  itself, reconnect in your client (`/mcp` in Claude Code) — it runs inside the client,
  not the GUI.
- **Maintenance** (`consolidate`, `reflect`, `contradictions`, `backup`, `memory undo`) is
  available from the CLI and the Memory Map's ★ Commands panel.
- Replay the guided tour anytime from **? Help** in the map's toolbar.
