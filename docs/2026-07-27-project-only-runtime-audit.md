# Project-only runtime audit

BrainCell's current product boundary is one database per Project and explicit,
named live Pools. This audit records the remaining legacy names so they are not
mistaken for supported product behavior.

| Reference | Classification | Disposition |
| --- | --- | --- |
| `braincell.legacy_recovery`, `pooled_from`, legacy `global/braincell.db` discovery | migration-only | Retained: preview-first recovery reads old provenance and restores only approved rows. |
| `families.json` discovery | migration-only | Retained only as a discovery/inventory artifact for recovery; it must not select runtime reads or writes. |
| `braincell.pool` / `pool_into_global` | dead code | Materialized copy behavior; no recovery caller. Remove with its retired positive tests. |
| Family commands, `resolve_family_ulids`, `BRAINCELL_FEDERATE`, `--federate` | dead code | Retire after transcript ingestion is restricted to its selected Project and legacy tests are replaced. |
| `get_global_db_path`, global `open_store`/build/launch/server branches | dead code | Shared-memory selection is retired; reject its environment variable and remove branches. |
| `braincell.family_hook` | supported compatibility entry point | Cleanup-only no-op for users who still have an old Claude hook; normal setup never installs it. |
| `braincell.legacy_service` | supported compatibility entry point | Explicit `braincell legacy-service remove` cleanup only. Normal CLI and GUI preflight must not invoke it or spawn `systemctl`. |
| `main_map` / `braincell-map` | supported compatibility entry point | Launches only the native Memory Map for a validated Project path; it cannot choose shared/global memory. |
| `federate.py` live Pool query functions | current runtime (rename pending) | The file name is historical; `plan_for_pool`, `federated_search`, and `federated_recall` implement explicit read-only named-Pool fan-out. |
| `_dedup_by_content` | dead code | It served retired Family fusion; live Pool fusion retains independent source provenance. |
| `get_structure_dir` | dead code | No call sites; removed in this checkpoint. |

The code removal is intentionally staged: recovery schema/provenance and the
normal path registry remain until the recovery command and fixtures are retired
together. No retained compatibility path is described as ordinary product UI.
