# Ruff lint-debt baseline

This is a technical-debt report, not a passing lint claim. The project-only
release uses incremental Ruff enforcement: fatal correctness rules are checked
over `braincell` and `tests`, while the configured rules are required only for
Python files changed by a pull request. The whole-tree configured check remains
a non-blocking CI report until a dedicated cleanup reduces this baseline to
zero.

Validated Ruff version: `0.16.0` (pinned in the `dev` extra).

Recorded on 2026-07-27 with:

```bash
uv run --extra dev ruff check braincell tests --statistics
```

| Rule | `origin/main` | project-only branch | Net |
| --- | ---: | ---: | ---: |
| BLE001 | 42 | 43 | +1 |
| C401 | 1 | 2 | +1 |
| C402 | 2 | 2 | 0 |
| C408 | 3 | 3 | 0 |
| DTZ005 | 6 | 6 | 0 |
| DTZ007 | 2 | 2 | 0 |
| EXE001 | 5 | 5 | 0 |
| FURB167 | 7 | 7 | 0 |
| FURB192 | 1 | 1 | 0 |
| I001 | 60 | 61 | +1 |
| ISC004 | 2 | 2 | 0 |
| PERF102 | 1 | 1 | 0 |
| PERF402 | 2 | 2 | 0 |
| PLR0402 | 3 | 6 | +3 |
| PLW1510 | 7 | 8 | +1 |
| RUF012 | 2 | 2 | 0 |
| RUF015 | 2 | 2 | 0 |
| RUF046 | 1 | 1 | 0 |
| RUF059 | 11 | 11 | 0 |
| RUF100 | 17 | 19 | +2 |
| S110 | 3 | 3 | 0 |
| SIM103 | 2 | 2 | 0 |
| SIM114 | 1 | 1 | 0 |
| SIM115 | 6 | 6 | 0 |
| SIM117 | 14 | 14 | 0 |
| SIM118 | 0 | 2 | +2 |
| TRY004 | 8 | 8 | 0 |
| UP012 | 4 | 4 | 0 |
| UP017 | 4 | 5 | +1 |
| UP031 | 1 | 1 | 0 |
| UP035 | 7 | 9 | +2 |
| UP037 | 1 | 1 | 0 |
| UP041 | 2 | 2 | 0 |
| UP045 | 234 | 226 | -8 |
| **Total** | **464** | **470** | **+6** |

The branch changes 21 Python files. At baseline, those files contain 131
configured-Ruff findings. Those findings must be resolved only within those
files before the incremental required check can pass; unrelated baseline files
are intentionally out of scope for this release.

To reproduce the clean-main comparison with the pinned version:

```bash
git worktree add --detach /tmp/braincell-main-lint origin/main
(cd /tmp/braincell-main-lint && uv run --extra dev ruff check braincell tests --statistics)
git worktree remove /tmp/braincell-main-lint
```

To run the required incremental check locally:

```bash
python scripts/lint_changed_python.py --base origin/main
```
