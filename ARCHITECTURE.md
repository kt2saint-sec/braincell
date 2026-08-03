# BrainCell architecture

`last_verified: 2026-08-02` against the current
`~/braincell-public` integration working tree and the confirmed
remote `v0.4.0` tag (`e92aaf0`). This tree carries the v1 integration work,
including cross-platform lifecycle/launcher hardening and the required native
Memory Map runtime; see `BUGS.md` for verified open and resolved faults.

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

Server and CLI runtime dependencies (FastAPI, uvicorn, and friends) are base
dependencies. PySide6/QtWebEngine — the native Memory Map renderer — is also a
required base dependency: every supported BrainCell installation includes the
desktop application, and there is no headless or server-only product mode.
`gui` and `native` remain empty compatibility aliases; `openai` (hosted
embeddings) and `dev` (pytest, ruff) are the functional optional extras.

## Module map

### Storage and schema

| Module | Holds |
|---|---|
| `store.py` (the largest module) | The `Store` protocol and `SqliteStore`. One coroutine owns a write transaction from `BEGIN` through commit via `_write_transaction` (`store.py:1167`). Document replacement is atomic across hash, chunks, and FTS (`replace_document`, `store.py:2984`). |
| `schema.py` | Raw DDL only. |
| `compaction.py` | Pure transcript-page compaction; no I/O. |
| `storage_accounting.py` | `storage_report` (`:386`) — read-only file/row accounting, warning-only Project/disk pressure, and retention planning. `hard_prune_plan` (`:544`) creates a deterministic eligible selection and approval digest; `execute_hard_prune` (`:872`) re-plans under `mutation_lock`, records durable audit events, optionally snapshots, performs eligible retention, then verifies integrity and attempts WAL TRUNCATE + `VACUUM`. |

Tables created by `schema.py`: `bc_documents`, `bc_chunks`, `bc_chunks_fts`
(FTS5 virtual), `bc_note_links`, `bc_operations`, `bc_operation_notes`,
`schema_version`, `embed_fingerprint`, `memory_notes`, `memory_fts` (FTS5
virtual). Everything for one Project lives in that Project's single
`braincell.db`.

### Identity, configuration, and catalogs

| Module | Holds |
|---|---|
| `config.py` | XDG path resolution — the only place that decides where state goes, including per-Project maintenance preferences and audit catalogs. |
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
| `gui.py` | The FastAPI app, durable auth token, and native-window orchestration. |
| `platform.py` | Cross-platform source of truth for data paths, desktop launchers, and retired-service handling. |
| `native_shell.py` | Required PySide6/QtWebEngine host. `native_unavailable_reason` preflights a Linux display and reports a damaged required runtime. |
| `gui_template.py` | The single-page app, inlined as HTML+CSS+JS. |
| `gui_mutation.py` | `GuiMutationCoordinator` (`:12`) — one ownership gate across ingest, maintenance, clear, and undo. |
| `gui_ingest.py` | Build subprocess management, schedules, and `clear_project`; ties a child build to the GUI with Linux `PR_SET_PDEATHSIG`, a Windows Job Object, or a macOS watchdog. |
| `gui_ops.py` | Maintenance endpoints, including Connected-Project hard-prune preview/apply workers. |
| `gui_install.py` | Client connect/disconnect from the GUI. |

The Memory Map is window-owned: closing the window stops the server. There is
deliberately no headless fallback and no always-on service — see
[CONTRIBUTING.md](CONTRIBUTING.md).

The map has two deliberately separate contexts: the selected Project controls
catalog identity, statistics, inspector state, and Pool membership controls;
the Connected Project owns ordinary Search, Recent notes, and writes. A named
Pool is the only cross-Project query surface, and it opens member databases
read-only at query time. This prevents a map selection from silently widening
or misattributing memory context.

### Installation and connection

| Module | Holds |
|---|---|
| `install.py` | Project-local client connections and Project skills. `_atomic_write_text` (`:408-428`) backs up, then writes via `mkstemp` + `os.replace`. Skill destinations: Claude `.claude/skills`, Codex `.agents/skills`, OpenCode `.opencode/skills` (`:165`). The Memory Map locally resolves skill status and mutations from its Connected Project identity, never a renderer-supplied path. |
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

These two ARE reachable — via explicit, one-purpose subcommands only (see CLI
command map below) — not ordinary product surfaces:

- `legacy_service.py` — one-release cleanup bridge for the retired
  `braincell-map.service` unit; reachable only via `legacy-service`, an
  explicit-removal command.
- `legacy_recovery.py` — the only runtime consumer of legacy pooled rows.
  Preview-first, approval-digest-gated, WAL-aware (`apply`, `:417`); reachable
  only via `legacy-recovery preview|apply`.

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

`storage` is read-only unless `--apply` is passed. Ordinary retention apply is
refused unless at least one of `--keep-backups`, `--expire-operations-days`,
or `--expire-tombstones-days` is configured. `storage --hard-prune` is a
separate preview-first path: its `--apply` requires the exact digest from the
preview plus `DELETE`, or `DELETE WITHOUT LOCAL RECOVERY SNAPSHOT` when no
local snapshot was requested. It can touch only eligible expired tombstones,
old operation history, and unprotected backups.

**Memory Map** — `start`, `gui`.

Every path-taking command that mints or writes routes through
`validate_project_target`, and every database mutation holds the
destination-scoped `mutation_lock`.

`stats` and `storage` are different instruments and are not interchangeable:
`cmd_stats` (`cli.py:610`) prints chunk/doc counts and a vector-search p95
benchmark via `_stats_async` (`cli.py:576`); `cmd_storage` (`cli.py:630`)
delegates to `storage_accounting.storage_report` or the digest-gated
hard-prune planner/executor when explicitly requested.

`storage --warn-project-bytes N --warn-free-bytes N` adds explicit,
warning-only review thresholds to that report. It never blocks a write,
modifies state, or grants cleanup authority; the report also flags when the
exact optional snapshot or compaction estimate cannot fit on the current disk.

## On-disk state

All under `$XDG_DATA_HOME/braincell/` (default `~/.local/share/braincell/`);
the namespace is overridable with `BRAINCELL_DATA_NAMESPACE`.

| Path | Contents |
|---|---|
| `projects/<ulid>/braincell.db` | The Project's entire memory |
| `projects/<ulid>/transcript_ingest_ledger.json` | Ingest checkpoint |
| `projects/<ulid>/braincell.db.mutation.lock` | Fixed per-destination interprocess lock |
| `projects/<ulid>/maintenance-preferences.json` | Per-Project typed-delete bypass preference; default false |
| `projects/<ulid>/maintenance-audit.json` | Durable hard-prune started/completed/failed evidence; not a recovery copy |
| `path-registry.json` | Absolute Project path → ULID |
| `pools.json` | ULID-only Pool membership; never copied memory or paths |
| `families.json` | Retired family memberships |
| `gui-token` | Memory Map auth token, mode `0600` |
| `gui-tour-seen` | Onboarding flag |
| `global/braincell.db` | Retired shared brain; a recovery concern only |

Project-local files BrainCell writes inside a connected Project:
`.codex/config.toml`, `.vscode/mcp.json`, `.mcp.json` (Claude project scope),
`.claude/settings.local.json` or `.claude/settings.json` (Automatic Pool
recall), `.claude/skills`, `.agents/skills`, and `.opencode/skills`
directories.

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
