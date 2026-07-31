# BrainCell fault ledger

Concise living record of verified faults. Resolved entries remain for regression
context; severity reflects pre-fix impact.

## Open

Identified 2026-07-31 by comparing the audit branch against the public remote
branches (full comparison narrative: `bugs-archive-2026-07-31.md`).

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
  `_required_auto_backup` is wired only into consolidate (`braincell/cli.py:548`)
  and reflect (`braincell/cli.py:686`).
  (`braincell/cli.py:237`, `braincell/cli.py:992`, `braincell/gui_ingest.py:403`)
- **Medium — stats/storage diagnostics:** `braincell storage` covers files,
  WAL/SHM, Project row counts, and backup retention, but not freelist,
  embedding, foreign-document, or orphan-database detail. `braincell stats`
  remains a separate chunk/doc count plus vector-search p95 benchmark and does
  not surface any of it. (`braincell/cli.py:556`, `braincell/cli.py:610`,
  `braincell/storage_accounting.py:170`)
- **Medium — orphan reconciliation:** no preview or detection of orphaned
  registry entries and databases left by deleted Projects. Reassociating a
  moved Project is already live (`braincell/cli.py:298`,
  `braincell/gui.py:699`); what is missing is the read-only orphan inventory.
  (`braincell/project_registry.py:76`)
- **Medium — token ACL parity:** token creation applies POSIX mode `0600`, but
  Windows ACL equivalence is not validated. (`braincell/gui.py:799`)
- **Medium — platform data roots:** default storage remains Linux-oriented
  `~/.local/share`; macOS/Windows migration is not implemented.
  (`braincell/config.py:33`)
- **Medium — SQLite compaction/WAL diagnostics:** no authorized hard-prune plus
  `VACUUM` workflow or WAL-starvation warning exists.
  (`braincell/storage_accounting.py:170`)
- **Low — logger fallback:** a failure constructing the rotating handler still
  falls back to an ordinary potentially unbounded file handler.
  (`braincell/log.py:68`)
- **Later policy — storage budgets:** warnings, configurable budgets, and
  explicit hard limits remain unimplemented and must not delete memory silently.

**Do not merge `project-only-architecture`:** it is superseded by `main`'s
preview-first, WAL-aware `legacy_recovery.py`; only its revision of
`docs/add-repo-runbook.md` is worth cherry-picking. (Verdict recorded
2026-07-31; details in `bugs-archive-2026-07-31.md`.)

## Resolved in Unreleased

- **Medium — canonical skill authority:** Historical transcripts containing
  different bodies for one skill were order-dependent (last writer wins).
  Resolved: canonical body = newest source-file mtime, tie broken on the
  lexicographically greatest content hash; the winning authority is persisted
  in the skill doc's metadata so re-ingestion in any order converges
  (`braincell/transcript_ingest.py:449`). Order-reversal and equal-mtime
  regressions in `tests/test_transcript_ingest.py:450`.
- **Medium — retention policy:** Disappeared transcripts, tombstones, and
  operation history had no expiry mechanism. Resolved: explicit, opt-in
  retention apply (`braincell/storage_accounting.py:322`, CLI
  `braincell storage --apply`) covering backup pruning, operation-history
  expiry, and tombstone purge — dry-run plan first, executed only under the
  destination mutation lock, every axis disabled by default and an
  unconfigured apply refused; curated (active/superseded) memory is never
  touched. No default retention age exists by design — expiry runs only when
  the owner passes an explicit window. Regressions in
  `tests/test_storage_accounting.py:187`.
- **Medium — future pruning safety:** The retention plan did not identify
  snapshots referenced by undo history as protected. Resolved: undo-referenced
  snapshots are planned as protected (`braincell/storage_accounting.py:77`),
  re-verified at delete time, and unreadable operation history fails the whole
  apply closed; a tombstoned note referenced by recorded undo history is
  likewise never purged. Regressions in
  `tests/test_storage_accounting.py:121`.
- **Medium — legacy raw upserts:** Free `upsert_document`/`upsert_chunk`
  helpers committed caller-owned SQLite connections outside `SqliteStore`
  transaction ownership; production ingest no longer called them. Resolved:
  removed outright — tests and `scripts/pool_bench.py` now seed through the
  owned atomic path (`braincell/store.py:2984` `replace_document`), and a
  retirement regression keeps the helpers gone
  (`tests/test_store.py:911`).

- **Critical — shared transaction ownership:** A second coroutine could commit
  or roll back another writer's unfinished transaction.
  (`braincell/store.py:1167`)
- **Critical — transcript split state:** Hash/checkpoint updates could survive
  failed embeddings or disagree with chunks and FTS rows.
  (`braincell/transcript_ingest.py:346`, `braincell/store.py:2984`)
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
- **Low — tests assert Linux-only semantics against correct code:** POSIX mode
  `0600` (`tests/test_gui_token.py:39`), absolute Unix path in an XDG `.desktop`
  Exec (`tests/test_gui_launcher.py:112`), display detection that
  `braincell/native_shell.py:47-52` deliberately restricts to Linux
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

## Archived

The 2026-07-31 remote-comparison narrative and the "Ledger corrections —
2026-07-31" record (applied anchor fixes, including one correction that was
itself wrong) were archived to `bugs-archive-2026-07-31.md`. Nothing was
discarded.
