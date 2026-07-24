---
name: braincell-sync
description: |
  Incrementally refresh the CURRENT repo's braincell-mcp memory: re-embed only
  content-changed notes and pick up new Claude/Codex transcripts with the local
  qwen3-embedding:4b embedder (mtime→SHA + cluster skip, so unchanged content is
  not re-embedded). Assumes /braincell-init already ran (MCP registered for this
  repo). Use when: "braincell-sync", "sync braincell", "refresh braincell
  memory", "update braincell for this repo".
triggers:
  - braincell-sync
  - sync braincell
  - refresh braincell memory
  - update braincell for this repo
allowed-tools:
  - Bash
  - Read
  - Glob
  - Grep
---

# /braincell-sync — incrementally refresh braincell-mcp memory for THIS repo

Keeps this repo's braincell brain current after more work. Unlike `/braincell-init`,
this does NOT re-register the MCP and does NOT rebuild from scratch — it processes only
what changed.

**Engine:** braincell-mcp, **project-scoped** — syncs the brain for the CURRENT working
directory (`$PWD`), the same per-project brain the MCP serves. **Embedder:** the local
Ollama `qwen3-embedding:4b` (1024-d) default; the sync must use the SAME embedder the brain was
built with (fingerprint-gated) or it fails loud rather than corrupting the vector space.

## Steps

Resolve the CLI once — it ships with the `braincell-mcp` package as a console script:
```bash
BC="$(command -v braincell || true)"
[ -n "$BC" ] || { echo "ERROR: braincell CLI not found on PATH. Activate the environment where braincell-mcp is installed, then re-run."; exit 1; }
```

1. **Pre-flight.** Confirm this repo already has a braincell brain (built +
   registered); if not, this is the wrong skill → run `/braincell-init`. Confirm the
   local embedder is ready:
   ```bash
   ollama list 2>/dev/null | grep -q 'qwen3-embedding:4b' || { echo "ERROR: qwen3-embedding:4b not pulled. Run: ollama pull qwen3-embedding:4b"; exit 1; }
   ```

2. **Incremental sync — local qwen3-embedding:4b, targets `$PWD` (the served brain):**
   ```bash
   "$BC" sync .
   ```
   Re-embeds only content-changed notes + ingests new transcript pages for this repo;
   unchanged content is skipped (mtime→SHA / cluster gate). Writes only to the XDG
   store. Cheap on repeat runs.

3. **Verdict.** Report what changed (notes re-embedded / new transcript pages / skipped
   counts from the sync output). No restart needed — the already-registered MCP serves
   the updated store on its next query.

## Guardrails
- **Same embedder as the build — one embedder per brain.** braincell's store is
  fingerprint-gated (`ollama:qwen3-embedding:4b:1024` by default); syncing with a different
  model/dim fails the dimension guard rather than silently corrupting search. If you
  deliberately changed the embedder, use `braincell build --reembed` (a full rebuild),
  not sync.
- **Project-scoped: syncs `$PWD`**, the brain the MCP serves for this repo. Do not
  target another directory.
- **Read-only over the repo** (writes only the XDG DBs).
- If this repo has no braincell brain yet, `sync` has nothing to refresh → run
  `/braincell-init` first; do not mint identity here.
- Don't run concurrently with an active build on the same project (single-writer
  SQLite). A read-only MCP server querying the brain is fine — WAL allows concurrent
  readers.
