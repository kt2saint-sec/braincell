# BrainCell architecture

`last_verified: 2026-07-31` against worktree `fix/audit-2026-07-31` (base
`d817fce`, HEAD `d02b04b`).

The cross-repo map: where each concern lives, what the CLI surface is, what the
database schema holds, and which on-disk files are state. Product language is
governed by [NAMINGS.md](NAMINGS.md); user-facing behavior by
[README.md](README.md); verified faults by [BUGS.md](BUGS.md).

## Shape of the system

BrainCell is a single Python package, `braincell`, with three console entry
points declared in `pyproject.toml`:

| Command | Target | Role |
|---|---|---|
| `braincell` | `braincell.cli:main` | The full CLI |
| `braincell-mcp` | `braincell.server:main` | FastMCP stdio server (also `python -m braincell`) |
| `braincell-map` | `braincell.cli:main_map` | Memory Map launcher |

Runtime dependencies are all declared as base deps — FastAPI, uvicorn, and
PySide6 included, because `gui.py` imports them unconditionally. The `native`
and `gui` extras are empty compatibility aliases. Only `openai` (hosted
embeddings) and `dev` (pytest, ruff) add anything.

## Module map

### Storage and schema

| Module | Holds |
|---|---|
| `store.py` (the largest module) | The `Store` protocol and `SqliteStore`. One coroutine owns a write transaction from `BEGIN` through commit via `_write_transaction` (`store.py:1167`). Document replacement is atomic across hash, chunks, and FTS (`replace_document`, `store.py:2984`). |
| `schema.py` | Raw DDL only. |
| `compaction.py` | Pure transcript-page compaction; no I/O. |
| `storage_accounting.py` | `storage_report` (`:170`) — read-only file/row accounting plus a dry-run retention plan that lists backups referenced by undo history as *protected* rather than as candidates (`referenced_backup_paths`, `:77`). `apply_retention` (`:322`) is the only executor. |

Tables created by `schema.py`: `bc_documents`, `bc_chunks`, `bc_chunks_fts`
(FTS5 virtual), `bc_note_links`, `bc_operations`, `bc_operation_notes`,
`schema_version`, `embed_fingerprint`, `memory_notes`, `memory_fts` (FTS5
virtual). Everything for one Project lives in that Project's single
`braincell.db`.

### Identity, configuration, and catalogs

| Module | Holds |
|---|---|
| `config.py` | XDG path resolution — the only place that decides where state goes. |
| `project_registry.py` | The path↔ULID registry (`load_path_registry`, `:76`), safe-identity validation (`is_safe_project_id`, `:37`), Pool membership (`load_pools`, `:228`), and retired family helpers. |
| `project_target.py` | Shared safety checks for a selected Project — refuses `/`, requires acknowledgement for home, non-Git, and privileged targets. |
| `catalog_io.py` | The two concurrency primitives: `catalog_lock` (`:19`), `mutation_lock` (`:47`), and the durable `atomic_write_json` (`:89`) that fsyncs the parent directory before returning. |
| `mode.py` | Project-only runtime mode resolution. |

### Ingest and embedding

| Module | Holds |
|---|---|
| `transcript_ingest.py` | Walks `~/.claude/projects/**` and `~/.codex/sessions/**` for JSONL. Attributes each file to its *true source* Project — Codex by `session_meta.payload.cwd`, Claude by encoded ancestor directory name (`_resolve_source_ulid`, `:237`) — never to the build Project. Checkpoints into `transcript_ingest_ledger.json` beside the database. |
| `skill_tag.py` | Pure skill-body detection and name extraction. |
| `embed_spec.py` | Single source of truth for the embedding contract. Default `qwen3-embedding:4b` MRL-truncated to 1024-d; fingerprint is `provider:model:dim`. |
| `embed.py` | Provider calls, output validation, `prewarm_embed_model` (`:251`). |
| `rerank.py` | Optional local reranker. `_ollama_score` (`:54`) is synchronous and is driven through a bounded `asyncio.to_thread` pool in `_order_by_score` (`:75-96`). |

### Serving

| Module | Holds |
|---|---|
| `server.py` | FastMCP stdio server. Tools: `search`, `recall`, `remember`, `forget`, `supersede`, `get_document`, `ingest_status`, `list_documents`, `list_projects`, `list_pools`. `search_hits` (`:255`) is shared with the CLI. |
| `contradictions.py`, `reflect.py` | Offline audit and LLM consolidation passes. |

### Memory Map (native desktop)

| Module | Holds |
|---|---|
| `gui.py` | The FastAPI app, auth token (`:790-801`), and Linux XDG desktop-launcher installer (`:903`). |
| `native_shell.py` | PySide6/QtWebEngine host. `native_available` (`:47-52`) deliberately requires a display only on Linux. |
| `gui_template.py` | The single-page app, inlined as HTML+CSS+JS. |
| `gui_mutation.py` | `GuiMutationCoordinator` (`:12`) — one ownership gate across ingest, maintenance, clear, and undo. |
| `gui_ingest.py` | Build subprocess management, schedules, and `clear_project` (`:403`). Ties the child's life to the GUI with `PR_SET_PDEATHSIG` on Linux (`_pdeathsig_preexec`, `:77`, guarded at `:92`). |
| `gui_ops.py` | Maintenance endpoints. |
| `gui_install.py` | Client connect/disconnect from the GUI. |

The Memory Map is window-owned: closing the window stops the server. There is
deliberately no headless fallback and no always-on service — see
[CONTRIBUTING.md](CONTRIBUTING.md).

### Installation and connection

| Module | Holds |
|---|---|
| `install.py` | Project-local client connections and Project skills. `_atomic_write_text` (`:408-428`) backs up, then writes via `mkstemp` + `os.replace`. Skill destinations: Claude `.claude/skills`, Codex `.agents/skills` (`:165`). |
| `launch.py` | `braincell start` preflight — single-instance probe and pre-launch report. |
| `automatic_pool_recall.py` | The opt-in Claude hook, project-local and disabled by default. |
| `log.py` | Rotating file handler with a plain-handler fallback (`:68`). |

### Retired surfaces retained as code

These are not reachable from the CLI parser and must not be re-exposed:

- `pool.py` — materialized `pool_into_global` merging. Its CLI driver
  `cmd_pool` (`cli.py:1112`) and `_resolve_pool_sources` (`cli.py:1091`) are
  defined but wired to no subparser.
- `cmd_family_add` / `cmd_family_rm` / `cmd_family_ls` (`cli.py:1060`, `:1072`,
  `:1414`) — likewise unwired.
- `federate.py` — opt-in family recall behind `BRAINCELL_FEDERATE`.
- `family_hook.py` — the retired global hook entry point; fails quiet.
- `legacy_service.py` — one-release cleanup bridge for the retired
  `braincell-map.service` unit.
- `legacy_recovery.py` — the only runtime consumer of legacy pooled rows.
  Preview-first, approval-digest-gated, WAL-aware (`apply`, `:417`).

## CLI command map

Defined in `main` (`braincell/cli.py:1705`) to end of file. Aliases are marked.

**Project lifecycle** — `build` (alias `sync`), `register`, `project reassociate`,
`setup`, `connect` (alias `install`), `disconnect` (alias `uninstall`), `skills
add|remove`.

**Query** — `recall`, `search`, `serve`.

**Pools** — `pool create|add|decouple|delete|list|search|recall`,
`automatic-pool-recall enable|disable|status|run`.

**Maintenance** — `consolidate`, `reflect`, `contradictions`, `reembed-notes`,
`memory log|undo`, `backup`, `stats`, `storage`, `legacy-service`,
`legacy-recovery preview|apply`.

`storage` is read-only unless `--apply` is passed, and `--apply` is refused
unless at least one of `--keep-backups`, `--expire-operations-days`, or
`--expire-tombstones-days` is configured.

**Memory Map** — `start`, `gui`.

Every path-taking command that mints or writes routes through
`validate_project_target`, and every database mutation holds the
destination-scoped `mutation_lock`.

`stats` and `storage` are different instruments and are not interchangeable:
`cmd_stats` (`cli.py:590`) prints chunk/doc counts and a vector-search p95
benchmark via `_stats_async` (`cli.py:556`); `cmd_storage` (`cli.py:610`)
delegates to `storage_accounting.storage_report`.

## On-disk state

All under `$XDG_DATA_HOME/braincell/` (default `~/.local/share/braincell/`);
the namespace is overridable with `BRAINCELL_DATA_NAMESPACE`.

| Path | Contents |
|---|---|
| `projects/<ulid>/braincell.db` | The Project's entire memory |
| `projects/<ulid>/transcript_ingest_ledger.json` | Ingest checkpoint |
| `projects/<ulid>/braincell.db.mutation.lock` | Fixed per-destination interprocess lock |
| `path-registry.json` | Absolute Project path → ULID |
| `pools.json` | ULID-only Pool membership; never copied memory or paths |
| `families.json` | Retired family memberships |
| `gui-token` | Memory Map auth token, mode `0600` |
| `gui-tour-seen` | Onboarding flag |
| `global/braincell.db` | Retired shared brain; a recovery concern only |

Project-local files BrainCell writes inside a connected Project:
`.codex/config.toml`, `.vscode/mcp.json`, `.mcp.json` (Claude project scope),
`.claude/settings.local.json` or `.claude/settings.json` (Automatic Pool
recall), `.claude/skills` and `.agents/skills` directories.

## Document set

One home per fact. When these disagree, the listed owner wins.

| Doc | Owns |
|---|---|
| `ARCHITECTURE.md` | This map: modules, CLI surface, schema, state layout |
| `README.md` | User-facing install, connect, and behavior |
| `QUICKSTART.md` | The shortest correct path to a working Project |
| `NAMINGS.md` | Product vocabulary and forbidden legacy terms |
| `CHANGELOG.md` | Release-facing verified changes |
| `BUGS.md` | Verified faults, with `file:line` anchors |
| `CONTRIBUTING.md` | Contribution rules, CLA, dev checks, invariants for contributors |
| `AGENTS.md` (internal, gitignored) | Repair-worktree agent instructions and the pre-fix evidence ledger |
| `COMMERCIAL-LICENSE.md` | Dual-licensing terms |

A second internal, gitignored instructions file (the Claude one) owns worktree
scope and authority; like `AGENTS.md` it exists only in working copies and is
never published.

`AGENTS.md` line anchors are deliberately *historical* — they record where each
fault lived before its repair so regressions stay traceable. Do not "correct"
them to current lines. `BUGS.md` anchors are the opposite: they must always
point at current code.
