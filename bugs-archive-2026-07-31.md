# BUGS.md archive — 2026-07-31

Sections archived from `BUGS.md` after the 2026-07-31 audit cycle was committed.
Line numbers quoted here are historical (states at `61e0e7f` / `d02b04b`) and are
deliberately not re-anchored.

## Remote comparison — 2026-07-31 (narrative)

The public repository was checked over SSH at
`git@github.com:kt2saint-sec/braincell.git`. The audit worktree's configured
`origin` still pointed at the stale `braincell-mcp` URL, which explained the
initial `Repository not found` response. The correct repository is accessible.

The remote `ci/windows-macos-matrix` commit (`40d0a56`) changes only
`.github/workflows/ci.yml`: it adds Windows and macOS CI runners and a
cross-platform wheel smoke test. It does not implement native launcher support,
consumer Windows/macOS GUI validation, ACL enforcement, or platform-specific
storage paths.

The remote `project-only-architecture` migration series adds read-only legacy
inventory, verified backups, provenance-based migration, and WAL-safe recovery.
It does not delete existing foreign-owned transcript rows. Its changes overlap
the local audit in `cli.py`, GUI modules, registry/configuration, recovery,
server code, documentation, and tests.

It should **not** be merged: it is superseded, not merely conflicting. `main`
independently shipped a preview-first, WAL-aware, approval-digest-gated
`braincell/legacy_recovery.py` and a strictly larger isolation test. One hunk —
the remote's revision of `docs/add-repo-runbook.md` (the file itself already
exists at base `d817fce`) — is the only part worth cherry-picking.

The twelve open findings that this comparison produced were moved to the
standalone `## Open` section of `BUGS.md`, where they remain live.

## Ledger corrections — 2026-07-31

Anchors were first verified against `61e0e7f` and listed here rather than
silently applied. **They have since been applied in place (2026-07-31, owner
approved); this section is retained as the record of what the entries said
before.**

Every line number quoted *inside this section* is historical — it describes the
state at `61e0e7f` and is deliberately not re-anchored as code moves. The
entries in `BUGS.md` are the ones that must always cite current lines.

- `retention policy` and `SQLite compaction/WAL diagnostics`: cited
  `storage_accounting.py:57` is blank → **`braincell/storage_accounting.py:58`**.
- `safety-backup coverage`: cited `cli.py:214` is `unload_after_build = False`,
  not a backup site → **`braincell/cli.py:237`** or **`:973`**; the
  `gui_ingest.py:403` half is correct. Still open — `_required_auto_backup`
  covers only consolidate (`cli.py:548`) and reflect (`cli.py:667`).
- `orphan reconciliation`: cited `project_registry.py:48` is a path-safety
  validator, unrelated. Also **partly stale** — `reassociate_project_path` is
  live (`braincell/cli.py:298`, `braincell/gui.py:699`). Narrow the entry to
  "no preview/detection of orphaned registry entries".
- `stats/storage diagnostics`: **narrower than recorded** — residual gap is
  freelist, embedding, foreign-document and orphan-database detail.
  *This correction was itself partly wrong and is superseded:* it claimed
  `cmd_stats` "now delegates to `storage_report` (`braincell/cli.py:620-624`)".
  Re-verified against `d02b04b`, lines 620-624 are inside `cmd_storage`
  (`braincell/cli.py:610-627`), not `cmd_stats`. `cmd_stats`
  (`braincell/cli.py:590`) still only calls `_stats_async`
  (`braincell/cli.py:556`), which prints chunk/doc counts and a vector-search
  p95. `storage_report` does categorize WAL/SHM
  (`braincell/storage_accounting.py:42`), so only the WAL/SHM half of the
  original entry was stale — for `braincell storage`, not for `braincell stats`.
- The remote-comparison note said merging the branches "requires a manual
  conflict-aware review". That understates it: `project-only-architecture` is
  **superseded**, not merely conflicting — `main` independently shipped a
  preview-first, WAL-aware, approval-digest-gated `legacy_recovery.py` and a
  strictly larger isolation test. It should not be merged; one hunk
  (`docs/add-repo-runbook.md`) is worth cherry-picking.

Found during the same pass and also applied:

- `blocking reranker and embedder lifetime`: cited `rerank.py:48` is
  `rerank_window`, not a scoring site → **`braincell/rerank.py:54`**
  (`_ollama_score`; the bounded-concurrency fix is `_order_by_score` at
  `braincell/rerank.py:75-96`). `embed.py:252` is inside the docstring →
  **`braincell/embed.py:251`** (`prewarm_embed_model`).
- `orphan reconciliation` now anchors on the path↔ULID registry loader
  (`braincell/project_registry.py:76`) rather than the unrelated path-safety
  validator.
