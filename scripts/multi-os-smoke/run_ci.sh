#!/usr/bin/env bash
# Local CI runner — mirrors .github/workflows/ci.yml for Linux.
# Run from repo root: bash scripts/multi-os-smoke/run_ci.sh
set -euo pipefail

# Linux workstations must never recreate an uncaged Qt/package validation run.
# The dedicated runner isolates state on NVMe and puts tests and packaging in
# separate systemd cgroups. Other platforms retain this portable CI mirror.
if [ "$(uname -s)" = "Linux" ]; then
    SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
    exec "$SCRIPT_DIR/release-check-safe.sh"
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'
pass() { echo -e "${GREEN}PASS${NC} $*"; }
fail() { echo -e "${RED}FAIL${NC} $*"; exit 1; }

if [ -f .venv/bin/python ]; then
    PY=.venv/bin/python
else
    PY=python3
fi

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

echo "=== Install dev deps ==="
$PY -m pip install --upgrade pip --quiet
$PY -m pip install '.[dev,gui]' --quiet || fail "pip install [dev,gui]"

echo "=== Ruff fatal correctness rules ==="
$PY -m ruff check --select E9,F63,F7,F82 braincell tests || fail "ruff fatal rules"
pass "ruff fatal rules"

echo "=== Ruff changed Python files ==="
$PY -m ruff check braincell tests || fail "ruff changed files"
pass "ruff changed files"

echo "=== pytest ==="
$PY -m pytest -q || fail "pytest"
pass "pytest"

echo "=== Build wheel ==="
$PY -m pip install build twine --quiet
$PY -m build --wheel --outdir "$TMPDIR/dist" || fail "build"
pass "build"

echo "=== Twine check ==="
$PY -m twine check "$TMPDIR/dist"/*.whl || fail "twine"
pass "twine check"

echo "=== Inspect artifacts ==="
$PY scripts/inspect_release_artifacts.py "$TMPDIR/dist" || fail "inspect"
pass "inspect"

echo "=== Smoke test built wheel (no Qt) ==="
SMOKE_VENV="$TMPDIR/smoke-venv"
$PY -m venv "$SMOKE_VENV"
"$SMOKE_VENV/bin/pip" install --quiet "$TMPDIR/dist"/*.whl || fail "wheel install"

"$SMOKE_VENV/bin/braincell" --help > /dev/null || fail "braincell --help"
pass "braincell --help"

"$SMOKE_VENV/bin/braincell-mcp" --help > /dev/null || fail "braincell-mcp --help"
pass "braincell-mcp --help"

"$SMOKE_VENV/bin/braincell" setup --help > /dev/null || fail "braincell setup --help"
pass "braincell setup --help"

echo
echo -e "${GREEN}All CI checks passed.${NC}"
