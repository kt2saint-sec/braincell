# BrainCell fault ledger

Concise living record of verified faults. Resolved entries remain for regression
context; severity reflects pre-fix impact.

## Open

Identified 2026-07-31 by comparing the audit branch against the public remote
branches.

Identified 2026-08-02 by comparing the confirmed remote `v0.4.0` tag against
the current `braincell-public` working tree.

- **Later policy — storage hard limits:** configurable warning thresholds are
  now read-only, but a hard storage limit remains intentionally unimplemented.
  Any future limit must never silently delete memory or block a write without a
  separately reviewed product decision.

**Do not merge `project-only-architecture`:** it is superseded by `main`'s
preview-first, WAL-aware `legacy_recovery.py`; only its add-repo-runbook revision is worth cherry-picking. (Verdict recorded
2026-07-31.)

## Resolved in 1.0.0

- **High — `os.kill(pid, 0)` interrupted the whole console on Windows:** the
  parent-death watchdog's `_pid_alive` probe assumed POSIX signal-0 semantics,
  but CPython maps signal 0 on Windows to
  `GenerateConsoleCtrlEvent(CTRL_C_EVENT, ...)`, delivering Ctrl+C to every
  process sharing the console. Exercising the probe in tests aborted the whole
  Windows pytest session with `KeyboardInterrupt` (and earlier wedged Windows
  CI jobs until the 6-hour limit). The watchdog never runs on Windows in
  production (Windows uses a Job Object). Resolved: `_pid_alive`
  (`braincell/gui_ingest.py:204`) now refuses non-POSIX platforms outright;
  the real-subprocess watchdog tests are `skipif(win32)`-gated; the CI pytest
  step carries `timeout-minutes: 30` so any future hang fails fast with
  output. Regression:
  `tests/test_gui_ingest_parent_death_guard.py`
  (`test_refuses_to_probe_on_a_non_posix_platform`).

- **High — upgraders' Project skills were stranded as permanent conflicts:**
  skill bodies changed for 1.0.0 (client placeholders, removed `triggers:`),
  so a copy installed by an earlier release no longer byte-matched and was
  treated as user-edited — never updated, never removable. Resolved:
  `_HISTORICAL_SKILL_SHA256` (`braincell/install.py:243`) is an append-only
  registry of every skill body BrainCell itself shipped; a destination
  matching a historical digest is BrainCell-authored, so install replaces it
  (`updated`), remove deletes it, and status reports `outdated`
  (Memory Map label: **Update available**). Genuinely edited copies still
  conflict and are never touched. Regressions in
  `tests/test_skills_install.py` (update-in-place, removable, status,
  registry shape).

- **Medium — release gate failed healthy runs on soft memory events:**
  `scripts/release-check-safe.sh` treated any cgroup `memory.events` counter,
  including `MemoryHigh`'s normal `high` throttle, as a failure — a PySide6
  install plus wheel build could fail a healthy run. Resolved: only hard
  `max`/`oom`/`oom_kill` events (or the 90 % hard-ceiling stop) fail the
  check; `high` stays recorded in `resource-events.log` as evidence.

- **Low — release sandbox was hard-bound to one machine's mount:** the
  runner refused any sandbox outside `/mnt/nvme-fast`, so public contributors
  could not run the documented release gate at all. Resolved:
  `BRAINCELL_RELEASE_CHECK_ROOT` now accepts any dedicated absolute directory
  (the filesystem root, `$HOME` itself, and paths inside the checkout are
  refused); the default and every cgroup/lock/isolation guarantee are
  unchanged. `run_inside_scope` also pins its cwd to the project root.

- **Medium — suite-order dependence hid disconnected-server failures:**
  `braincell/cli.py` exports `BRAINCELL_PROJECT_ID` into `os.environ` on some
  code paths, so earlier CLI tests silently connected every later test in a
  full-suite run — `tests/test_registry.py`'s catalog-tool tests passed in
  the suite but failed alone. Resolved: the autouse `isolate_xdg` fixture
  deletes the variable before every test, and the catalog-tool test classes
  set their own connected Project explicitly. Verified by a full per-file
  isolated run (80/80 files green) plus a single-process full-suite run.

- **High — hard-prune recovery snapshot was itself prunable:** the same-host
  snapshot written before a hard-prune (`braincell-hard-prune-backup-*.db`)
  was categorized as an ordinary backup, so a later `braincell storage
  --apply --keep-backups N` or a second hard-prune could delete the only
  recovery copy of the previous prune, and the snapshot consumed a
  keep-newest-N backup slot. Resolved: `_category()`
  (`braincell/storage_accounting.py:43`) classifies these snapshots as
  `recovery_snapshots`, never a retention or hard-prune candidate; the
  delete-time category re-check (`braincell/storage_accounting.py:677`)
  fails closed for any pre-fix plan naming one. Regression:
  `tests/test_storage_accounting.py`
  (`test_hard_prune_recovery_snapshot_is_never_a_retention_candidate`).

- **High — GUI hard-prune accepted arbitrary filesystem roots:**
  `/api/ops/hard-prune/plan|apply` accepted a caller-supplied `backup_roots`
  path list that reached `storage_report()`'s recursive scan without
  `validate_project_target`, so a token-bearing localhost caller could aim
  backup deletion at any writable directory. The shipped interface never sent
  the field. Resolved: the request bodies no longer carry `backup_roots`
  (`braincell/gui_ops.py:99`); `extra="forbid"` turns a smuggled value into
  HTTP 422 and the GUI always scans only the BrainCell namespace. The CLI's
  explicit `--backup-root` flag is unchanged. Regression:
  `tests/test_gui_ops.py` (`test_hard_prune_rejects_caller_supplied_backup_roots`).

- **High — Memory Map skill actions could trust a renderer-supplied directory:**
  the embedded UI could previously describe or operate on skills without a
  backend-enforced Connected Project boundary. Resolved:
  `mount_skill_status_api()` (`braincell/gui_install.py:154`) and
  `api_skills()` (`:250`) resolve the launch session's Connected Project from
  the local registry; requests contain only the client and add/remove action.
  Project skill destinations are constrained below the selected root
  (`braincell/install.py:194`), and edited same-name skills are protected from
  overwrite and automatic removal (`:286`, `:317`). Regressions:
  `tests/test_gui_install.py`, `tests/test_gui_layout.py`,
  `tests/test_install.py`, and `tests/test_skills_install.py`.

- **Medium — SQLite compaction/hard-prune workflow:** permanent cleanup had no
  authorized execution path: the product could only warn about WAL starvation.
  Resolved: `hard_prune_plan()` creates a deterministic review selection and
  approval digest (`braincell/storage_accounting.py:549`); `execute_hard_prune()`
  recomputes that selection under the destination lock, requires the exact
  final confirmation, records durable started/completed/failed audit events,
  optionally makes a same-host recovery copy, then verifies SQLite integrity
  (`:877`). Only expired tombstones, old operation history, and unprotected
  backups are eligible. Active/superseded memory, indexed documents/chunks,
  semantic similarity, and LLM judgments are never deletion authority.
  `VACUUM` follows WAL TRUNCATE; a live reader is reported for retry rather
  than forced closed. The CLI preview/apply path is `braincell storage
  --hard-prune` (`braincell/cli.py:630`) and the Connected Project Memory Map
  uses the same plan/apply endpoints (`braincell/gui_ops.py:444`). Regressions:
  `tests/test_storage_accounting.py`, `tests/test_storage_cli.py`,
  `tests/test_gui_ops.py`, and `tests/test_gui_maintenance_panel.py`.

- **High — selected map Project could be mistaken for the source of ordinary
  memory panels:** exact evidence observed in the v1.0.0 working tree on
  2026-08-02, verbatim:

  ```javascript
  function scopeParams(){return "";}
  ```

  ```javascript
  /* ════════ VIEWED PROJECT vs CONNECTED PROJECT ════════
     activeProjectId = whose memory the GUI is showing: map focus, inspector,
     drawer notes/search. Switching it is a VIEW change only; writes stay pinned
     to the connected project's opened store. Init: ?active= URL param → connected
     seed → null (rides the URL like ?scope=,
     so a view is shareable/bookmarkable). */
  ```

  The first statement meant ordinary drawer `/api/notes` and `/api/search`
  requests never selected the map Project; the second claimed that they did.
  This could present Connected Project memory under a selected sibling's
  inspector. Resolved: map selection is now explicitly catalog-only (map,
  Project statistics, inspector, and membership state); both drawer sections
  visibly name **Connected Project memory**; selecting an already open Project
  no longer re-queries ordinary memory; and every cross-Project live read
  remains an explicit named Pool Search or Recall.

- **High — intentional non-Git Projects had no GUI acknowledgement path:**
  exact evidence observed in the v1.0.0 working tree on 2026-08-02, verbatim:

  ```javascript
  const res=await apiPost("/api/install",{path:arPath,client,scope});
  ```

  ```python
  acknowledge_non_git: bool = False
  ```

  The API correctly required `acknowledge_non_git`, but the GUI neither sent it
  nor explained how to give it. Resolved: target failures now return a
  structured `non_git_acknowledgement_required` code; the GUI opens a clear
  confirmation explaining that GitLab clones normally have `.git`, and retries
  the same install or deregistration request with
  `acknowledge_non_git: true` only after consent. Codex remains Git-required by
  design; new folders, archive extracts, and other non-Git Projects remain
  supported for clients that do not require Git.

- **Medium — malformed target paths were classified as a generic conflict:**
  exact evidence observed in the v1.0.0 working tree on 2026-08-02, verbatim:

  ```python
  if not resolved.is_dir():
      raise ProjectTargetError(f"Project target is not a directory: {resolved}")
  ```

  ```python
  except ProjectTargetError as exc:
      raise HTTPException(409, str(exc)) from exc
  ```

  A nonexistent path is invalid input, not a state conflict or consent request.
  Resolved: `ProjectTargetError` now carries a stable code; filesystem-root and
  non-directory targets return HTTP `400` (`filesystem_root_forbidden` or
  `target_not_directory`), while acknowledgement and client-configuration
  conflicts return HTTP `409` with `{code, message}` detail. The same target
  mapping is shared by the GUI install, uninstall, skills, and Automatic Pool
  recall endpoints.

- **High — release documentation advertised retired headless behavior:** the
  README, Quickstart, architecture guide, contribution guide, and changelog
  described PySide6 as optional, offered `--server-only`, or prescribed `[gui]`
  installation even though the native Memory Map is now a required base runtime.
  Resolved: all tracked release documentation now states the mandatory GUI
  contract, uses base-package install commands, and identifies `gui`/`native`
  only as empty compatibility aliases. Verified against `pyproject.toml`,
  `scripts/install.sh`, and the GUI entry-point preflight paths.

- **High — required-GUI remediation described an optional install:** a partial
  or broken PySide6/QtWebEngine installation reported the native Memory Map as
  optional and advised `braincell-mcp[gui]`, contradicting the mandatory GUI
  contract. Resolved: `native_unavailable_reason()`
  (`braincell/native_shell.py:40`) now identifies the required runtime and
  directs the user to repair/reinstall BrainCell; the broken-import regression
  asserts the required-runtime wording and rejects both `optional` and `[gui]`.

- **High — GUI tests could escape mocks and initialize real QtWebEngine on the
  developer workstation:** `cmd_start` and `run_gui` changed from the boolean
  `native_available()` seam to `native_unavailable_reason()`, while several
  tests still mocked the old seam. Their supposed failure paths then imported
  QtWebEngine, spawned Chromium helpers, and could hang inside the ordinary
  pytest process. Resolved: affected tests now mock the active seam;
  `test_native_shell.py` runs its real import probe in a bounded process group
  and kills descendants; `scripts/test-gui-safe.sh` serializes all GUI tests in
  an isolated HOME/XDG/tmp/cache/bytecode environment with offscreen,
  software-rendered QtWebEngine and a user-cgroup memory/task/CPU/time limit.
  The focused GUI safety selection passes 109 tests in that environment.

- **High — foreign transcript cleanup:** historical `bc_documents` rows
  attributed to a Project other than the database they live in (predating the
  out-of-scope skip in `ingest_transcripts`, or left by a path later
  reassociated to a different Project) had no reconciliation path. Resolved:
  `preview_foreign_documents()` (`braincell/transcript_ingest.py:757`) is a
  READ-ONLY inventory grouped by true owner, classifying each as
  `migratable` (currently registered) or unattributable, plus destination
  doc_key/content-hash conflicts — mirrors `legacy_recovery.preview`'s
  approval-digest shape. `apply_foreign_document_migration()`
  (`braincell/transcript_ingest.py:945`) re-plans under the SOURCE
  database's mutation lock, refuses the WHOLE apply if any selected owner is
  unattributable or conflicted (no partial best-effort), takes a
  pre-mutation backup of the source database, copies + verifies every
  document and chunk into the owner's OWN database (creating it if it
  doesn't exist yet), and deletes the migrated rows from source only after
  that verification and the destination's commit — never before. CLI:
  `braincell reconcile-foreign-documents <path> preview|apply`
  (`braincell/cli.py:cmd_reconcile_foreign` at `:1780`, parser at `:2169`).
  Regressions in `tests/test_foreign_document_reconciliation.py`
  (owner classification, destination-conflict detection, stale-digest /
  unattributable / conflicted-owner refusal with adversarial byte-identical
  non-mutation checks, successful migration with FTS rebuild verified via a
  live `MATCH` query, destination auto-creation, partial-selection
  isolation).
- **High — cross-platform parent-death cleanup:** subprocess protection used
  Linux-only `prctl` (`_pdeathsig_preexec`); Windows and macOS had no
  abrupt-parent-death equivalent, so a killed/crashed GUI could orphan a
  running build indefinitely on those platforms (the same failure mode the
  Linux `prctl` guard exists to close). Resolved:
  `_start_parent_death_guard()`/`_release_parent_death_guard()`
  (`braincell/gui_ingest.py:258`, `:288`) install a platform-appropriate
  guard AFTER spawn (Linux's `preexec_fn` guard is unaffected and still
  arms first). Windows: `_win32_job_kill_on_close()`
  (`braincell/gui_ingest.py:162`) creates a Job Object with
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` via ctypes and assigns the build
  process to it — the OS itself closes every handle a crashed process
  owned, including the job handle, which fires the kill with no code
  required to run inside the dying GUI. macOS: `_run_parent_death_watchdog()`
  (`braincell/gui_ingest.py:215`), spawned as its own DETACHED subprocess by
  `_spawn_macos_watchdog()` (`:249`) — `preexec_fn` cannot host this
  (`exec()` destroys any thread started there before a later parent death
  could ever be observed), and an in-GUI-process watchdog would die
  alongside the GUI in exactly the crash case it exists to catch. Polling
  (`os.kill(pid, 0)`, not kqueue `EVFILT_PROC`/`NOTE_EXIT`) was chosen
  because the identical loop is then plain POSIX and testable for real on
  Linux, not just macOS; the bug's original impact (24+ minutes) tolerates a
  multi-second interval. Fail-open on installation failure (never blocks the
  build spawn), fail-closed on effect (a warning is always logged, never
  silently degraded). Regressions in
  `tests/test_gui_ingest_parent_death_guard.py` (real-subprocess kill/no-op
  cases for the watchdog loop, `_watchdog_main` argv validation and
  module-invocation smoke test, platform-dispatch mocking for both guards
  and their failure paths, mocked `icacls`-style ctypes call-sequence
  coverage for every Job Object failure point, and an end-to-end check that
  the guard is installed/released around a real ingest job).
- **High — native launcher platforms:** installation was Linux/XDG-only.
  Resolved: `install_launcher()` (`braincell/platform.py:355`) now dispatches
  by `sys.platform` — Linux keeps its existing XDG `.desktop` + hicolor-icon
  behaviour unchanged (`_install_launcher_linux`, `braincell/platform.py:154`);
  macOS gets a minimal `.app` WRAPPER under `~/Applications`
  (`_install_launcher_macos`, `braincell/platform.py:236` — a plain `+x` shell script at
  `Contents/MacOS/braincell-launch`, not a compiled binary; no `.icns` is
  generated since `iconutil`/`sips` are macOS-only tools this repo's Linux
  hosts can neither run nor verify, so the bundle shows Finder's generic
  icon); Windows gets a Start Menu `.lnk` authored via PowerShell's
  `WScript.Shell` COM object (`_install_launcher_windows`, `braincell/platform.py:322` — the
  `.lnk` format has no stdlib writer, and `powershell.exe` ships with every
  supported Windows release, so this stays dependency-free). All three
  launch the SAME one-command `braincell start "<project_path>"`, so every
  platform gets full preflight/single-instance-reuse/per-project behaviour.
  Regressions in `tests/test_gui_launcher.py`
  (`TestInstallLauncherMacOS`/`TestInstallLauncherWindows`: bundle/plist/
  script contents and exec bit, idempotency, cwd default, mocked-PowerShell
  invocation-argument coverage, PowerShell-failure non-raising path,
  `APPDATA` fallback, platform dispatch).
- **Medium — token ACL parity:** token creation applied POSIX mode `0600`,
  but on Windows `os.chmod` only toggles the read-only bit — no real ACL
  restriction. Resolved: `_windows_restrict_token_acl()`
  (`braincell/gui.py:857`), wired into `_resolve_gui_token()` (`:902`),
  invokes `icacls <path> /inheritance:r /grant:r <user>:F` via subprocess
  (stdlib-only) to remove inherited permissions and grant only the current
  user. Fail-closed as a warning: a missing `USERNAME`, a non-NTFS volume, or
  `icacls` itself erroring is logged loudly rather than silently accepted —
  the token still gets written (already scoped by the user's own config-dir
  ACLs by default; see the Cross-platform CI section below) rather than
  blocking the GUI from starting. Regressions in
  `tests/test_gui_token.py` (`TestWindowsGuiTokenAcl`: mocked-icacls
  invocation shape, domain-qualified account, missing-`USERNAME` and
  `icacls`-failure warn-without-raise paths) plus the existing mint/persist/
  reuse coverage split so the POSIX-mode-bits assertion is
  `skipif(win32)`-gated rather than asserted unconditionally.
  **Landing note for `ci/windows-macos-matrix`:** that branch's
  `tests/test_gui_token.py::test_mints_persists_0600_and_reuses` carries
  `@pytest.mark.xfail(sys.platform == "win32", strict=True, ...)` because
  Windows had no ACL restriction at the time it was written
  (`braincell/gui.py:799` in that branch's numbering). With this fix landed
  the test now PASSES on Windows too, and `strict=True` turns an unexpected
  pass into a hard failure — **that xfail marker must be removed when the
  branches merge.**
- **Medium — platform data roots:** default storage was Linux-oriented
  `~/.local/share` unconditionally, on every platform. Resolved:
  `_xdg_data_home()` (`braincell/config.py:51`) now resolves a
  platform-appropriate default via `_platform_data_home_default()`
  (`braincell/platform.py:32`)
  — macOS `~/Library/Application Support`, Windows `%LOCALAPPDATA%`
  (`~/AppData/Local` if unset) — while `XDG_DATA_HOME` still overrides
  unconditionally on every platform as before. Backward compatible by
  construction, no silent migration: a pre-fix install's data can already
  live at the legacy `~/.local/share/<namespace>` path on macOS/Windows, so
  an EXISTING populated legacy root always wins over the new platform
  default (`_legacy_linux_style_data_home()`, `braincell/platform.py:59`); if both a legacy and a
  platform-default root are populated, the legacy (already-in-use) root
  still wins and a once-per-process warning is logged — nothing is ever
  copied, moved, or deleted by this function. Linux itself is unchanged
  (its default already was, and remains, `~/.local/share`). Regressions in
  `tests/test_config_data_home.py` (env-override precedence on every
  platform, Linux behaviour/no-warning unchanged, macOS/Windows fresh-install
  defaults, legacy-root preference with warn-once verified across two calls,
  both-populated mismatch warning with a non-mutation check on both roots,
  `LOCALAPPDATA` fallback, and a downstream `get_local_state_dir()` check
  that the platform default actually reaches real per-project paths).
- **High — safety-backup coverage:** consolidate/reflect required a successful
  backup but reembed and clear did not. Resolved: `build --reembed`
  (`braincell/cli.py:_execute_build`, backup call at `braincell/cli.py:230`)
  and the GUI's `clear_project` (`braincell/gui_ingest.py:625`) now require the
  same `_required_auto_backup` snapshot before their destructive wipe,
  fail-closed (`RuntimeError`, surfaced as HTTP 409 from `/api/clear`) if the
  backup cannot be made. Both wipes still run through the CLI (`braincell
  build --reembed` is what the GUI's ingest subprocess shells out to), so one
  fix covers CLI and GUI reembed; clear only ever existed as a GUI path.
  Each got an explicit, off-by-default override with a loud warning:
  `--no-backup` on `build` (`braincell/cli.py:1826`) and `skip_backup` on
  `POST /api/clear` (`braincell/gui_ingest.py:68`). Regressions in
  `tests/test_reembed_backup_coverage.py` (fault-injects `_vacuum_into`
  failure; proves the wipe never ran) and `tests/test_gui_ingest.py:298`
  (`TestClear` additions, same fault-injection + skip_backup bypass).
- **Medium — orphan reconciliation:** no preview or detection of orphaned
  registry entries and databases left by deleted Projects. Resolved:
  `find_orphans()` (`braincell/project_registry.py:193`) is a READ-ONLY
  inventory of (a) path-registry rows whose path no longer exists on disk and
  (b) `projects/<ulid>/braincell.db` files with no registry row naming that
  ULID — detection only, no deletion or auto-repair (reassociating a moved
  Project stays the existing `reassociate_project_path` workflow,
  `braincell/cli.py:318`, parser at `:1785`). Surfaced through the new
  `"orphans"` key in `storage_report()`
  (`braincell/storage_accounting.py:490`) and a standalone
  `braincell storage --list-orphans` listing that needs no registered project
  (`braincell/cli.py:639`, parser at `braincell/cli.py:2314`). Regressions in
  `tests/test_project_orphans.py` (stale path, orphaned database, reassociate
  clearing the orphan, non-mutation adversarial check) and
  `tests/test_storage_cli.py`.
- **Medium — stats/storage diagnostics:** `braincell storage` covered files,
  WAL/SHM, Project row counts, and backup retention but not freelist,
  embedding, foreign-document, or orphan-database detail; `braincell stats`
  surfaced none of it. Resolved: `storage_report()` now includes
  `"database_diagnostics"` (`braincell/storage_accounting.py:482`) —
  `PRAGMA freelist_count`/`page_count`/`page_size`, embedded/null-embedding
  chunk counts plus total vector bytes, and a count of `bc_documents` rows
  owned by a different project — and a WAL-starvation warning (WAL file past
  both a byte floor and a ratio against the database's own size; constants
  `_WAL_STARVATION_MIN_BYTES` / `_WAL_STARVATION_RATIO`), printed by
  `braincell storage` (`braincell/cli.py:630`). Orphan-database detail is the
  same `find_orphans()` from the orphan-reconciliation entry above, built once
  and reused rather than duplicated. Hard-prune and `VACUUM` execution are
  covered by the resolved workflow entry above. Regressions in
  `tests/test_storage_accounting.py:289` (`TestDatabaseDiagnostics`,
  `TestOrphansSurfacedInStorageReport`) and `tests/test_storage_cli.py`
  (WAL-warning printed / silent cases).
- **Low — logger fallback:** a failure constructing the rotating file handler
  fell back to an ordinary, potentially unbounded `FileHandler`. Resolved:
  `_rotating_file_handler()` (`braincell/log.py:80`) retries once with
  conservative defaults (smaller cap, one backup) and, if that also fails,
  returns `None` rather than ever opening an unbounded file — `setup()` then
  skips file logging and warns on stderr, leaving the console handler as the
  sole (never-crash) sink. Regressions in `tests/test_log.py`
  (`TestRotatingFileHandlerFallback`: retry-then-succeed and both-fail cases;
  `TestSetupNeverCrashesOnABrokenLogFile`).
- **Medium — canonical skill authority:** Historical transcripts containing
  different bodies for one skill were order-dependent (last writer wins).
  Resolved: canonical body = newest source-file mtime, tie broken on the
  lexicographically greatest content hash; the winning authority is persisted
  in the skill doc's metadata so re-ingestion in any order converges
  (`braincell/transcript_ingest.py:449`). Order-reversal and equal-mtime
  regressions in `tests/test_transcript_ingest.py:450`.
- **Medium — retention policy:** Disappeared transcripts, tombstones, and
  operation history had no expiry mechanism. Resolved: explicit, opt-in
  retention apply (`braincell/storage_accounting.py:695`, CLI
  `braincell storage --apply`) covering backup pruning, operation-history
  expiry, and tombstone purge — dry-run plan first, executed only under the
  destination mutation lock, every axis disabled by default and an
  unconfigured apply refused; curated (active/superseded) memory is never
  touched. No default retention age exists by design — expiry runs only when
  the owner passes an explicit window. Regressions in
  `tests/test_storage_accounting.py:187`.
- **Medium — future pruning safety:** The retention plan did not identify
  snapshots referenced by undo history as protected. Resolved: undo-referenced
  snapshots are planned as protected (`braincell/storage_accounting.py:298`),
  re-verified at delete time, and unreadable operation history fails the whole
  apply closed; a tombstoned note referenced by recorded undo history is
  likewise never purged. Regressions in
  `tests/test_storage_accounting.py:121`.
- **Medium — legacy raw upserts:** Free `upsert_document`/`upsert_chunk`
  helpers committed caller-owned SQLite connections outside `SqliteStore`
  transaction ownership; production ingest no longer called them. Resolved:
  removed outright — tests and `scripts/pool_bench.py` now seed through the
  owned atomic path (`braincell/store.py:2986` `replace_document`), and a
  retirement regression keeps the helpers gone
  (`tests/test_store.py:911`).

- **Critical — shared transaction ownership:** A second coroutine could commit
  or roll back another writer's unfinished transaction.
  (`braincell/store.py:1167`)
- **Critical — transcript split state:** Hash/checkpoint updates could survive
  failed embeddings or disagree with chunks and FTS rows.
  (`braincell/transcript_ingest.py:346`, `braincell/store.py:2986`)
- **Critical — Project identity/catalog safety:** Concurrent minting and unsafe
  registry values could create conflicting identities or redirect state outside
  the BrainCell namespace. (`braincell/project_registry.py:37`)
- **High — cross-interface mutation races:** CLI, Memory Map, schedules, and
  recovery did not share one destination mutation boundary.
  (`braincell/catalog_io.py:47`, `braincell/gui_mutation.py:12`)
- **High — invisible vectorless note:** An individual FTS insert failure could
  commit a note that neither semantic nor keyword Recall could find.
  (`braincell/store.py:1684`)
- **High — accumulating no-op backups:** Maintenance could retain full database
  snapshots when no mutation was planned, and second-resolution names could
  collide. (`braincell/cli.py:992`)
- **High — recovery state races:** Preview and apply could observe different
  source, registry, or destination states.
  (`braincell/legacy_recovery.py:417`)
- **High — embedding outage behavior:** Keyword operations unnecessarily
  depended on embeddings and hybrid Search lacked lexical degradation.
  (`braincell/server.py:255`)
- **Medium — blocking reranker and embedder lifetime:** Sequential synchronous
  model calls blocked the event loop, while warm-up immediately unloaded the
  embedding model. (`braincell/rerank.py:54`, `braincell/embed.py:251`)

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
  with the already-correct `braincell/catalog_io.py:89-114`
  (`atomic_write_json`).
- **Medium — Windows token ACL gap (product, not test):** scoping the mode
  assertion off Windows is correct, but the GUI auth token then carries no ACL
  restriction there at all — `os.chmod` only toggles the read-only bit on
  Windows. Mitigating: the token sits under the user's config dir, already
  user-scoped by default Windows ACLs; real exposure is custom or relocated
  data roots. Gate with `pytest.mark.xfail(..., strict=True)` so support
  landing forces the marker's removal. (`braincell/gui.py:799`)
  **Update (2026-07-31, `fix/audit-2026-07-31`):** implemented via
  `icacls` — see "token ACL parity" under Resolved in Unreleased above.
  This branch's xfail(strict) marker on
  `test_gui_token.py::test_mints_persists_0600_and_reuses` must be removed
  when the branches merge; the test now passes on Windows.
- **Low — tests assert Linux-only semantics against correct code:** POSIX mode
  `0600` (`tests/test_gui_token.py:39`), absolute Unix path in an XDG `.desktop`
  Exec (`tests/test_gui_launcher.py:112`), display detection that
  `braincell/native_shell.py:47-52` deliberately restricts to Linux
  (`tests/test_native_shell.py:26` — the *only* macOS failure), and
  `monkeypatch.setattr(os, "geteuid")` without `raising=False` against
  production that already uses `getattr` (`tests/test_project_target_safety.py:68`,
  cf. `braincell/project_target.py:31`). Not enumerated in the original sweep:
  `stat.S_IMODE(...) == 0o640` in
  `test_codex_config_preserves_unrelated_content_permissions_and_final_newline`
  (`tests/test_install.py:188`) is the same POSIX-mode-bits pattern — Windows
  `chmod` cannot express arbitrary mode bits (returns something like `0o666`
  regardless of what was requested), and `_atomic_write_text`
  (`braincell/install.py:408-428`, the mode-preservation call site this test
  exercises) has no ACL-preservation step of its own to assert in its place,
  unlike the GUI token's `_windows_restrict_token_acl`. Fixed in this checkout
  (`fix/audit-2026-07-31`): the assertion is now `if sys.platform != "win32"`-
  gated; the content/backup/newline assertions in the same test are genuinely
  platform-agnostic and stay unconditional.
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
- **Low — Windows `git check-ignore` matches `evals/task-ab/tasks/` that
  Linux/macOS do not (newly surfaced, not a regression):** once the CRLF fix
  above let `test_no_tracked_file_references_a_gitignored_path`
  (`tests/test_repo_hygiene.py:64`) actually run on Windows (PR #4 latest run,
  head `5920eea`), it fails there because `git check-ignore --stdin` reports
  `evals/task-ab/tasks/` — the token `_PATH_TOKEN` extracts from
  `pyproject.toml`'s `extend-exclude = ["evals/task-ab/tasks/*/repo"]`
  (`pyproject.toml:80`) — as ignored on Windows but NOT on Linux/macOS.
  Confirmed on this checkout's Linux host: `echo "evals/task-ab/tasks/" | git
  check-ignore --stdin` prints nothing (not ignored), matching Linux/macOS CI.
  `.gitignore` names only two `evals/task-ab/*` patterns
  (`/evals/task-ab/*.log`, `/evals/task-ab/**/__pycache__/` — neither should
  match `evals/task-ab/tasks/` on any platform), there is no nested
  `evals/.gitignore`, no `core.excludesfile`, and no `.git/info/exclude`
  entry — so this is `git check-ignore` itself, not this repo's ignore rules,
  disagreeing across platforms; `extend-exclude` in `pyproject.toml` is Ruff
  lint config, not a git mechanism, so it cannot be the direct cause either.
  Left **document-only**, not fixed: reproducing or safely fixing a Windows
  git-ignore-matching discrepancy needs a real Windows host to verify against,
  which this Linux dev/CI checkout cannot do — a speculative change to
  `.gitignore` or the test's `git check-ignore` invocation risks masking a
  real dangling-reference bug instead of fixing a platform quirk.

Not defects, recorded to stop them being re-reported: `prctl` use is already
guarded (`braincell/gui_ingest.py:92`); `os.replace` itself is Windows-safe;
`lint-debt-report` carried `continue-on-error: true` and had failed on `main`
since `d817fce` with 301 pre-existing findings, not a PR #4 regression.
**Resolved in `78510a7`:** Ruff stock-rule findings reduced 286→0 and the job
gained `--exit-zero`, so it no longer reports red.

**Confirmed 2026-07-31 via `gh pr view 4`:** PR #4 went fully green — all 10
checks (`test` × ubuntu/windows/macos × 3.11/3.12/3.13, plus
`lint-debt-report`) reported `SUCCESS` at head `129ce65` — and **merged to
`main` as `471bc71`** (`mergedAt: 2026-07-31T16:22:26Z`). Windows and macOS
CI, the atomic-write fix, and the cleared lint debt are now on `main`, not
just on a topic branch. This supersedes the earlier "expected to go green"
framing. The Windows token-ACL fix (`icacls`) is NOT part of that merge — it
arrives with this branch, rebased onto `471bc71`, along with the rest of the
cross-platform audit work.

## Archived

The 2026-07-31 remote-comparison narrative and the "Ledger corrections —
2026-07-31" record (applied anchor fixes, including one correction that was
itself wrong) were archived. Nothing was
discarded.
