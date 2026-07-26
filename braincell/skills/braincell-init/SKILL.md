---
name: braincell-init
description: |
  Build memory for the CURRENT Project with the local qwen3-embedding:4b
  embedder, then connect BrainCell to Claude for that Project only. Run once per
  Project. Use when: "braincell-init", "init braincell memory", "set up
  braincell for this project", "bootstrap braincell".
triggers:
  - braincell-init
  - init braincell memory
  - set up braincell for this project
  - bootstrap braincell
allowed-tools:
  - Bash
  - Read
  - Glob
  - Grep
  - AskUserQuestion
---

# /braincell-init — build and connect BrainCell for this Project

Activates **BrainCell**, the local-first project-memory engine (SQLite, hybrid
vector + FTS5). After it runs and you restart Claude Code, BrainCell's MCP
tools become available for this Project only. Normal Recall and Search do not
read another Project unless you deliberately invoke a named Pool operation.

**Engine:** braincell-mcp — its own CLI (`braincell`), environment, Project
memory store, and MCP connection. braincell-mcp is **Project-scoped**: it
builds memory for the current working directory (`$PWD`), not a fixed engine
path.

**Embedder:** the shipped default is the **local Ollama `qwen3-embedding:4b` (1024-d)** — no API
key, nothing hosted. The embed fingerprint (`ollama:qwen3-embedding:4b:1024`) is gated: Project
memory must be built and served with the SAME embedder, or the store refuses to open. Do not
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

2. **Build Project memory (the heavy, one-time pass) — targets `$PWD`.**
   ```bash
   "$BC" build .
   ```
   Mints/confirms this Project's ULID, embeds curated notes with
   qwen3-embedding:4b, and reads supported prior transcripts. Writes only to
   BrainCell's per-Project data store — never into
   the Project tree. On existing Project memory the build is incremental (mtime→SHA / cluster
   skip); add `--reembed` only for a clean rebuild after an embedder change.

3. **Connect BrainCell to Claude.** The default is Claude's private
   local-Project connection. It does not create a user-wide registration or
   install a machine-wide hook:
   ```bash
   command -v claude >/dev/null || { echo "ERROR: claude CLI not found (needed to register the MCP)."; exit 1; }
   "$BC" connect . --client claude --scope local
   ```

4. **Verdict + restart note.** Report Project memory built (note + transcript counts from the
   build output) and BrainCell connected. Then tell the user plainly:
   > **Restart Claude Code** — `mcp__braincell__*` tools load at session start. After
  > restart, `/braincell-sync` keeps this Project's memory current.

## Guardrails
- **Local qwen3-embedding:4b by default — one embedder per Project memory store.** The store is fingerprint-gated
  (`ollama:qwen3-embedding:4b:1024`); mixing embed models/dims gives incomparable vectors. Change
  the embedder only via `braincell build --reembed`.
- **Project-scoped: builds `$PWD`**, not a fixed engine. Each Project has its
  own memory and stable ULID. Do not target another directory unintentionally.
- **Read-only over the Project tree** — braincell writes only its data store and
  Project registry; it never clones, deletes, or `rm -rf`s anything in the
  Project tree.
- **`braincell connect` is idempotent and non-clobbering** — safe to re-run;
  it preserves unrelated configuration and refuses a conflicting BrainCell
  entry.
- If `braincell connect` errors (e.g. `claude` CLI missing), surface it — do NOT
  silently proceed as if BrainCell were connected.
- Don't run while another process is mid-build on the same project (single-writer
  SQLite; a read-only MCP query is fine — WAL allows concurrent readers).
