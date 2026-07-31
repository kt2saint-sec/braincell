# BrainCell fault ledger

Concise living record of verified faults. Resolved entries remain for regression
context; severity reflects pre-fix impact.

## Open

- **Medium — canonical skill authority:** Historical transcripts containing
  different bodies for one skill remain order-dependent.
  (`braincell/transcript_ingest.py:449`)
- **Medium — retention policy:** Storage is observable, but disappeared
  transcripts, tombstones, and operation history have no automatic expiry.
  Any future deletion executor must protect snapshots referenced by undo
  history. (`braincell/storage_accounting.py:57`)
- **Medium — legacy raw upserts:** Compatibility helpers commit caller-owned
  SQLite connections outside `SqliteStore` transaction ownership. Production
  ingest no longer calls them. (`braincell/store.py:3183`)

## Resolved in Unreleased

- **Critical — shared transaction ownership:** A second coroutine could commit
  or roll back another writer's unfinished transaction.
  (`braincell/store.py:1167`)
- **Critical — transcript split state:** Hash/checkpoint updates could survive
  failed embeddings or disagree with chunks and FTS rows.
  (`braincell/transcript_ingest.py:343`, `braincell/store.py:2937`)
- **Critical — Project identity/catalog safety:** Concurrent minting and unsafe
  registry values could create conflicting identities or redirect state outside
  the BrainCell namespace. (`braincell/project_registry.py:37`)
- **High — cross-interface mutation races:** CLI, Memory Map, schedules, and
  recovery did not share one destination mutation boundary.
  (`braincell/catalog_io.py:47`, `braincell/gui_mutation.py:11`)
- **High — invisible vectorless note:** An individual FTS insert failure could
  commit a note that neither semantic nor keyword Recall could find.
  (`braincell/store.py:1684`)
- **High — accumulating no-op backups:** Maintenance could retain full database
  snapshots when no mutation was planned, and second-resolution names could
  collide. (`braincell/cli.py:973`)
- **High — recovery state races:** Preview and apply could observe different
  source, registry, or destination states.
  (`braincell/legacy_recovery.py:417`)
- **High — embedding outage behavior:** Keyword operations unnecessarily
  depended on embeddings and hybrid Search lacked lexical degradation.
  (`braincell/server.py:255`)
- **Medium — blocking reranker and embedder lifetime:** Sequential synchronous
  model calls blocked the event loop, while warm-up immediately unloaded the
  embedding model. (`braincell/rerank.py:48`, `braincell/embed.py:252`)

## Remote comparison — 2026-07-31

The public repository was checked over SSH at
`git@github.com:kt2saint-sec/braincell.git`. The audit worktree's configured
`origin` still points at the stale `braincell-mcp` URL, which explains the
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
server code, documentation, and tests; merging the branches requires a manual
conflict-aware review.

The following remain open after comparing local commit `e2a1601` with those
remote branches:

- **High — foreign transcript cleanup:** future out-of-scope files are skipped,
  but historical foreign-owned rows still need a preview-only migration or
  reconciliation workflow. (`braincell/transcript_ingest.py:330`)
- **High — cross-platform parent-death cleanup:** subprocess protection uses
  Linux-only `prctl`; Windows and macOS lack an abrupt-parent-death equivalent.
  (`braincell/gui_ingest.py:77`)
- **High — native launcher platforms:** installation remains Linux/XDG-only.
  (`braincell/gui.py:903`)
- **High — safety-backup coverage:** consolidate/reflect require a successful
  backup, but reembed and clear still need an explicit backup/override policy.
  (`braincell/cli.py:214`, `braincell/gui_ingest.py:403`)
- **Medium — stats/storage diagnostics:** `braincell stats` does not report
  WAL/SHM, freelist, embedding, foreign-document, or orphan-database detail;
  the separate storage report is not a complete replacement.
  (`braincell/cli.py:590`, `braincell/storage_accounting.py:57`)
- **Medium — orphan reconciliation:** deleted or moved Projects can leave
  registry entries and databases without a preview/reassociate workflow.
  (`braincell/project_registry.py:48`)
- **Medium — token ACL parity:** token creation applies POSIX mode `0600`, but
  Windows ACL equivalence is not validated. (`braincell/gui.py:799`)
- **Medium — platform data roots:** default storage remains Linux-oriented
  `~/.local/share`; macOS/Windows migration is not implemented.
  (`braincell/config.py:33`)
- **Medium — SQLite compaction/WAL diagnostics:** no authorized hard-prune plus
  `VACUUM` workflow or WAL-starvation warning exists.
  (`braincell/storage_accounting.py:57`)
- **Low — logger fallback:** a failure constructing the rotating handler still
  falls back to an ordinary potentially unbounded file handler.
  (`braincell/log.py:68`)
- **Later policy — storage budgets:** warnings, configurable budgets, and
  explicit hard limits remain unimplemented and must not delete memory silently.

## Cross-platform CI — 2026-07-31

Findings from PR #4 (`ci/windows-macos-matrix`, head `40d0a56`, run
`30604081359`). That commit changes only `.github/workflows/ci.yml`; it was
authored server-side through GitHub, not pushed from any local clone
(committer `GitHub <noreply@github.com>` / `web-flow`, GitHub PGP signature,
and no `PushEvent` for the ref). Results: Ubuntu green; Windows 15 failures on
3.11/3.12 and 8 on 3.13; macOS 1 failure on all three.

**One production defect, eight test-scoping defects, one latent workflow bug.**
The 15-vs-8 Windows split is the diagnostic: `os.fchmod` gained Windows support
in Python 3.13.

- **High — non-portable atomic write (three defects in one block):**
  `os.fchmod` is absent on Windows before 3.13; when it raised, the `mkstemp`
  descriptor leaked because `os.fdopen` never ran; the cleanup `unlink` then
  failed on the still-open file and **replaced** the original exception. The
  `WinError 32` in CI logs was that mask, not the fault.
  (`braincell/install.py:415-427`) — **fix authored, uncommitted**, on branch
  `ci/windows-macos-matrix`: write via `fdopen`, `os.chmod` by path after
  close, cleanup under `suppress(OSError)`. Brings this path into agreement
  with the already-correct `braincell/catalog_io.py:42-58`.
- **Medium — Windows token ACL gap (product, not test):** scoping the mode
  assertion off Windows is correct, but the GUI auth token then carries no ACL
  restriction there at all — `os.chmod` only toggles the read-only bit on
  Windows. Mitigating: the token sits under the user's config dir, already
  user-scoped by default Windows ACLs; real exposure is custom or relocated
  data roots. Gate with `pytest.mark.xfail(..., strict=True)` so support
  landing forces the marker's removal. (`braincell/gui.py:799`)
- **Low — tests assert Linux-only semantics against correct code:** POSIX mode
  `0600` (`tests/test_gui_token.py:39`), absolute Unix path in an XDG `.desktop`
  Exec (`tests/test_gui_launcher.py:112`), display detection that
  `braincell/native_shell.py:44-52` deliberately restricts to Linux
  (`tests/test_native_shell.py:26` — the *only* macOS failure), and
  `monkeypatch.setattr(os, "geteuid")` without `raising=False` against
  production that already uses `getattr` (`tests/test_project_target_safety.py:68`,
  cf. `braincell/project_target.py:31`).
- **Low — locale-codec assumptions in tests:** `read_text`/`write_text` without
  `encoding="utf-8"` fail under the Windows cp1252 default.
  (`tests/test_automatic_pool_recall.py:115`,
  `tests/test_gui_active_memory_ui.py:259`). Setting `PYTHONUTF8=1` in CI was
  rejected: it would hide the exact behavior Windows users hit.
- **Low — CRLF and TOML-escaping assumptions:** text-mode stdin to
  `git check-ignore --stdin` returns C-quoted names with a literal `\r`
  (`tests/test_repo_hygiene.py:57-59`); a raw Windows path is asserted as a
  substring of rendered TOML, which escapes backslashes
  (`tests/test_install.py:185`).
- **Low — non-portable wheel smoke path (latent):** `/tmp/braincell-wheel`
  under `shell: bash` on Windows; never executed because pytest failed first.
  Use `RUNNER_TEMP` or `mktemp -d`. (`.github/workflows/ci.yml`, PR #4 head)

Not defects, recorded to stop them being re-reported: `prctl` use is already
guarded (`braincell/gui_ingest.py:92`); `os.replace` itself is Windows-safe;
`lint-debt-report` carries `continue-on-error: true` and has failed on `main`
since `d817fce` — its 301 findings are pre-existing debt, not a PR #4
regression, and the job should get `--exit-zero` so it stops reporting red.

## Ledger corrections — 2026-07-31

Anchors verified against `61e0e7f`. Correcting in place would obscure what was
recorded before, so the corrections are listed rather than silently applied.

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
- `stats/storage diagnostics`: **narrower than recorded** — `cmd_stats` now
  delegates to `storage_report` (`braincell/cli.py:620-624`). Residual gap is
  freelist, embedding, foreign-document and orphan-database detail.
- The remote-comparison note above says merging the branches "requires a manual
  conflict-aware review". That understates it: `project-only-architecture` is
  **superseded**, not merely conflicting — `main` independently shipped a
  preview-first, WAL-aware, approval-digest-gated `legacy_recovery.py` and a
  strictly larger isolation test. It should not be merged; one hunk
  (`docs/add-repo-runbook.md`) is worth cherry-picking.
