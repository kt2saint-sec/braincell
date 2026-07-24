---
name: braincell-init
description: |
  One-time bootstrap of braincell-mcp persistent memory for the CURRENT repo:
  build the per-project brain with the local qwen3-embedding:4b embedder (embed
  notes + ingest prior Claude/Codex transcripts), then `braincell install` to
  register the braincell MCP server (and the optional family-recall hook,
  disarmed) so mcp__braincell__* tools light up after a Claude Code restart. Run
  once per repo. Use when: "braincell-init", "init braincell memory", "set up
  braincell for this repo", "bootstrap braincell".
triggers:
  - braincell-init
  - init braincell memory
  - set up braincell for this repo
  - bootstrap braincell
allowed-tools:
  - Bash
  - Read
  - Glob
  - Grep
  - AskUserQuestion
---

# /braincell-init — stand up braincell-mcp memory + MCP for THIS repo

Activates **braincell-mcp**, the standalone local-first persistent-memory engine
(SQLite, hybrid vector + FTS5). After it runs and you restart Claude Code, the
`mcp__braincell__*` tools (search / recall / remember / forget / supersede /
get_document / ingest_status / list_documents / list_projects / list_families)
become available for THIS repo.

**Engine:** braincell-mcp — its OWN CLI (`braincell`), venv, store namespace, and MCP
registration. braincell-mcp is **project-scoped**: it builds a brain for the CURRENT
working directory (`$PWD`), not a fixed engine path.

**Embedder:** the shipped default is the **local Ollama `qwen3-embedding:4b` (1024-d)** — no API
key, nothing hosted. The embed fingerprint (`ollama:qwen3-embedding:4b:1024`) is gated: a brain
must be built and served with the SAME embedder, or the store refuses to open. Do not
mix embedders (switch only with `braincell build --reembed`).

## Steps

Resolve the CLI once — it ships with the `braincell-mcp` package as a console script:
```bash
BC="$(command -v braincell || true)"
[ -n "$BC" ] || { echo "ERROR: braincell CLI not found on PATH. Activate the environment where braincell-mcp is installed, then re-run."; exit 1; }
```

1. **Pre-flight.** Confirm the local embedder is ready — braincell's default build
   embeds via Ollama `qwen3-embedding:4b`:
   ```bash
   ollama list 2>/dev/null | grep -q 'qwen3-embedding:4b' || { echo "ERROR: qwen3-embedding:4b not pulled. Run: ollama pull qwen3-embedding:4b"; exit 1; }
   ```
   (Ollama must be running. To use a different embedder set `BRAINCELL_EMBED_MODEL`/
   `BRAINCELL_EMBED_DIM` — but then build AND serve with the same one.)

2. **Build the brain (the heavy, one-time pass) — local qwen3-embedding:4b, targets `$PWD`.**
   ```bash
   "$BC" build .
   ```
   Mints/confirms this repo's ULID (path-registry), embeds curated notes with qwen3-embedding:4b,
   and ingests prior `~/.claude/projects/**` + `~/.codex/sessions/**` transcripts for
   this repo. Writes ONLY to the XDG store (`~/.local/share/braincell/…`) — never into
   the repo tree. On an EXISTING brain the build is incremental (mtime→SHA / cluster
   skip); add `--reembed` only for a clean rebuild after an embedder change.

3. **Register the MCP (+ install the hook, disarmed).** `braincell install` shells out
   to `claude mcp add` (no hand-edited config) and appends the family-recall hook to
   Claude Code settings, append-only (co-resident hooks are preserved):
   ```bash
   command -v claude >/dev/null || { echo "ERROR: claude CLI not found (needed to register the MCP)."; exit 1; }
   "$BC" install .            # add --no-hook to register the MCP only
   "$BC" hook status          # confirm the hook state (installed DISARMED by default)
   ```

4. **(Optional) arm proactive family memory.** The family-recall hook is OFF by
   default. Arm it only if this repo belongs to a family (`braincell family add`) and
   you want sibling-project notes auto-surfaced each turn:
   ```bash
   "$BC" hook on             # turn off any time with: braincell hook off
   ```

5. **Verdict + restart note.** Report brain built (note + transcript counts from the
   build output) and MCP registered. Then tell the user plainly:
   > **Restart Claude Code** — `mcp__braincell__*` tools load at session start. After
   > restart, `/braincell-sync` keeps this repo's brain current.

## Guardrails
- **Local qwen3-embedding:4b by default — one embedder per brain.** The store is fingerprint-gated
  (`ollama:qwen3-embedding:4b:1024`); mixing embed models/dims gives incomparable vectors. Change
  the embedder only via `braincell build --reembed`.
- **Project-scoped: builds `$PWD`**, not a fixed engine. Each repo is its own brain
  (path → ULID in the registry). This is the whole point — do not target another dir.
- **Read-only over the repo** — braincell writes only the XDG DBs + the path-registry;
  it never clones, deletes, or `rm -rf`s anything in the repo tree.
- **`braincell install` is idempotent + append-only** — safe to re-run; it never
  clobbers other Claude Code hooks or MCP servers.
- If `braincell install` errors (e.g. `claude` CLI missing), surface it — do NOT
  silently proceed as if the MCP were registered.
- Don't run while another process is mid-build on the same project (single-writer
  SQLite; a read-only MCP query is fine — WAL allows concurrent readers).
