#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
#
# Run BrainCell GUI/Qt tests in a host-safe, per-run environment.  This script
# intentionally fails when user systemd or flock is unavailable: it must never
# fall back to an uncaged QtWebEngine run.
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SANDBOX_ROOT="${BRAINCELL_GUI_TEST_ROOT:-/mnt/nvme-fast/braincell-test-sandbox}"
PYTHON_BIN="${BRAINCELL_TEST_PYTHON:-$SANDBOX_ROOT/venv/bin/python}"
KEEP_RUN="${BRAINCELL_GUI_TEST_KEEP:-0}"
RUNTIME_MAX="${BRAINCELL_GUI_TEST_RUNTIME_MAX:-5min}"

usage() {
    cat <<'EOF'
Usage: scripts/test-gui-safe.sh <pytest path or option> [...]

Runs the supplied GUI/Qt pytest selection inside a user cgroup with isolated
temporary files, HOME/XDG state, bytecode, and pytest cache. Successful run
directories are removed; set BRAINCELL_GUI_TEST_KEEP=1 to retain one for
inspection. By default it uses $SANDBOX_ROOT/venv; set
BRAINCELL_TEST_PYTHON only to select another dedicated test virtualenv.
The entire scope is killed after five minutes by default; override that limit
with BRAINCELL_GUI_TEST_RUNTIME_MAX only for a known-long-running selection.
EOF
}

if [ "$#" -eq 0 ]; then
    usage >&2
    exit 2
fi
if [ ! -x "$PYTHON_BIN" ]; then
    printf 'Python executable not found: %s\n' "$PYTHON_BIN" >&2
    exit 2
fi
if ! command -v systemd-run >/dev/null || ! command -v flock >/dev/null; then
    printf 'systemd-run and flock are required; refusing uncaged GUI test run.\n' >&2
    exit 2
fi

mkdir -p "$SANDBOX_ROOT"
RUN_DIR="$(mktemp -d "$SANDBOX_ROOT/run-$(date +%Y%m%d-%H%M%S)-XXXXXX")"
LOCK_FILE="$SANDBOX_ROOT/gui-tests.lock"
UNIT_NAME="braincell-gui-test-$(date +%Y%m%d%H%M%S)-$$"

cleanup() {
    local status=$?
    if [ "$status" -ne 0 ] || [ "$KEEP_RUN" = "1" ]; then
        printf 'GUI test artifacts retained: %s\n' "$RUN_DIR" >&2
    else
        rm -rf -- "$RUN_DIR"
    fi
    exit "$status"
}
trap cleanup EXIT

mkdir -p \
    "$RUN_DIR/home" \
    "$RUN_DIR/tmp" \
    "$RUN_DIR/xdg/cache" \
    "$RUN_DIR/xdg/config" \
    "$RUN_DIR/xdg/data" \
    "$RUN_DIR/xdg/state" \
    "$RUN_DIR/pycache"

cd "$PROJECT_ROOT"
flock -n "$LOCK_FILE" \
    systemd-run --user --scope --quiet --collect --unit="$UNIT_NAME" \
        -p "MemoryHigh=${BRAINCELL_GUI_TEST_MEMORY_HIGH:-6G}" \
        -p "MemoryMax=${BRAINCELL_GUI_TEST_MEMORY_MAX:-8G}" \
        -p "MemorySwapMax=${BRAINCELL_GUI_TEST_MEMORY_SWAP_MAX:-0}" \
        -p "CPUQuota=${BRAINCELL_GUI_TEST_CPU_QUOTA:-400%}" \
        -p "TasksMax=${BRAINCELL_GUI_TEST_TASKS_MAX:-256}" \
        -p "RuntimeMaxSec=$RUNTIME_MAX" \
        env \
            "HOME=$RUN_DIR/home" \
            "TMPDIR=$RUN_DIR/tmp" \
            "TMP=$RUN_DIR/tmp" \
            "TEMP=$RUN_DIR/tmp" \
            "XDG_CACHE_HOME=$RUN_DIR/xdg/cache" \
            "XDG_CONFIG_HOME=$RUN_DIR/xdg/config" \
            "XDG_DATA_HOME=$RUN_DIR/xdg/data" \
            "XDG_STATE_HOME=$RUN_DIR/xdg/state" \
            "PYTHONPYCACHEPREFIX=$RUN_DIR/pycache" \
            "QT_QPA_PLATFORM=offscreen" \
            "QTWEBENGINE_CHROMIUM_FLAGS=--no-sandbox --disable-gpu --disable-gpu-compositing" \
            "LIBGL_ALWAYS_SOFTWARE=1" \
            "$PYTHON_BIN" -m pytest -q -ra -o "cache_dir=$RUN_DIR/pytest-cache" "$@"
