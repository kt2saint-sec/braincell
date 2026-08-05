#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
#
# Run the local release gate without allowing package builds or smoke installs
# to consume host memory, swap, /tmp, or the user's normal cache directories.
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SANDBOX_ROOT="${BRAINCELL_RELEASE_CHECK_ROOT:-/mnt/nvme-fast/braincell-release-sandbox}"
BOOTSTRAP_PYTHON="${BRAINCELL_RELEASE_BOOTSTRAP_PYTHON:-python3}"
KEEP_RUN="${BRAINCELL_RELEASE_CHECK_KEEP:-0}"
RUNTIME_MAX="${BRAINCELL_RELEASE_RUNTIME_MAX:-15min}"
MEMORY_HIGH="${BRAINCELL_RELEASE_MEMORY_HIGH:-3G}"
MEMORY_MAX="${BRAINCELL_RELEASE_MEMORY_MAX:-4G}"
MIN_FREE_GIB="${BRAINCELL_RELEASE_MIN_FREE_GIB:-8}"
POLL_SECONDS="${BRAINCELL_RELEASE_POLL_SECONDS:-2}"

usage() {
    cat <<'EOF'
Usage: scripts/release-check-safe.sh

Runs the full GUI-safe test suite, captures a bounded local performance
baseline, then builds and checks the wheel and sdist inside a separate
NVMe-backed systemd cgroup. Packaging and clean-install smoke tests never use
the regular GUI-test virtualenv.

The default sandbox is /mnt/nvme-fast/braincell-release-sandbox; set
BRAINCELL_RELEASE_CHECK_ROOT to a dedicated absolute directory on a fast disk
to run elsewhere. The runner refuses the filesystem root, $HOME itself, a
sandbox inside the checkout, insufficient free space, or a missing user
systemd cgroup. Failed runs are retained with artifacts and resource logs; set
BRAINCELL_RELEASE_CHECK_KEEP=1 to retain successful runs too.
EOF
}

die() {
    printf '%s\n' "$*" >&2
    exit 2
}

cleanup_run() {
    local run_dir=$1 cleanup_status=$2
    if [ "$cleanup_status" -ne 0 ] || [ "$KEEP_RUN" = "1" ]; then
        printf 'Release-check artifacts retained: %s\n' "$run_dir" >&2
    else
        rm -rf -- "$run_dir"
    fi
    exit "$cleanup_status"
}

read_cgroup_value() {
    local path=$1 key=$2
    awk -v key="$key" '$1 == key { print $2; exit }' "$path" 2>/dev/null || true
}

find_scope_cgroup() {
    # Resolve a live user scope from its processes, not transient unit metadata.
    local unit_name=$1 proc_file cgroup_path
    for proc_file in /proc/[0-9]*/cgroup; do
        cgroup_path=$(sed -n 's/^0:://p' "$proc_file" 2>/dev/null || true)
        case "$cgroup_path" in
            *"/$unit_name.scope")
                printf '/sys/fs/cgroup%s\n' "$cgroup_path"
                return
                ;;
        esac
    done
}

monitor_scope() {
    local unit_name=$1 launcher_pid=$2 run_dir=$3
    local cgroup_dir="" last_events=""
    local current="" peak="" tasks="" high="" max="" oom="" oom_kill=""
    local max_bytes="" timestamp=""

    printf 'timestamp\tmemory_current\tmemory_peak\ttasks\thigh\tmax\toom\toom_kill\n' \
        > "$run_dir/resource-samples.tsv"

    while kill -0 "$launcher_pid" 2>/dev/null; do
        if [ -z "$cgroup_dir" ] || [ ! -d "$cgroup_dir" ]; then
            cgroup_dir=$(find_scope_cgroup "$unit_name" || true)
            if [ -n "$cgroup_dir" ] && [ -d "$cgroup_dir" ]; then
                max_bytes=$(cat "$cgroup_dir/memory.max" 2>/dev/null || true)
            fi
        fi

        if [ -n "$cgroup_dir" ] && [ -d "$cgroup_dir" ]; then
            current=$(cat "$cgroup_dir/memory.current" 2>/dev/null || true)
            peak=$(cat "$cgroup_dir/memory.peak" 2>/dev/null || true)
            tasks=$(wc -l < "$cgroup_dir/cgroup.procs" 2>/dev/null || true)
            high=$(read_cgroup_value "$cgroup_dir/memory.events" high)
            max=$(read_cgroup_value "$cgroup_dir/memory.events" max)
            oom=$(read_cgroup_value "$cgroup_dir/memory.events" oom)
            oom_kill=$(read_cgroup_value "$cgroup_dir/memory.events" oom_kill)
            timestamp=$(date --iso-8601=seconds)
            printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
                "$timestamp" "$current" "$peak" "$tasks" "$high" "$max" "$oom" "$oom_kill" \
                >> "$run_dir/resource-samples.tsv"

            local events="$high:$max:$oom:$oom_kill"
            if [ "$events" != "$last_events" ]; then
                printf '%s memory-events=%s\n' "$timestamp" "$events" >> "$run_dir/resource-events.log"
                last_events="$events"
            fi
            # `high` is MemoryHigh's normal soft-throttle counter, not a fault:
            # a PySide6 install plus wheel build can trip it on a healthy run.
            # It stays visible in resource-events.log; only hard events fail.
            if [ "${max:-0}" -gt 0 ] || [ "${oom:-0}" -gt 0 ] || [ "${oom_kill:-0}" -gt 0 ]; then
                touch "$run_dir/memory-pressure"
            fi
            if [ -n "$max_bytes" ] && [ "$max_bytes" != "max" ] \
                && [ "${current:-0}" -ge $((max_bytes * 9 / 10)) ]; then
                printf '%s stopping scope at 90%% of its hard memory ceiling\n' "$timestamp" \
                    >> "$run_dir/resource-events.log"
                touch "$run_dir/memory-pressure"
                systemctl --user stop "$unit_name" || true
                return
            fi
        fi
        sleep "$POLL_SECONDS"
    done
}

run_inside_scope() {
    local run_dir=$1 sandbox_root=$2
    [ "${BRAINCELL_RELEASE_SCOPE:-}" = "1" ] \
        || die "release-check-safe refuses an uncaged packaging run"

    local release_venv="$sandbox_root/venv"
    local smoke_venv artifact
    cd "$PROJECT_ROOT"
    export HOME="$run_dir/home"
    export TMPDIR="$run_dir/tmp"
    export TMP="$run_dir/tmp"
    export TEMP="$run_dir/tmp"
    export XDG_CACHE_HOME="$run_dir/xdg/cache"
    export XDG_CONFIG_HOME="$run_dir/xdg/config"
    export XDG_DATA_HOME="$run_dir/xdg/data"
    export XDG_STATE_HOME="$run_dir/xdg/state"
    export PYTHONPYCACHEPREFIX="$run_dir/pycache"
    export PIP_CACHE_DIR="$sandbox_root/pip-cache"
    export PYTHONNOUSERSITE=1
    mkdir -p "$HOME" "$TMPDIR" "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" \
        "$XDG_DATA_HOME" "$XDG_STATE_HOME" "$PYTHONPYCACHEPREFIX" "$PIP_CACHE_DIR"

    if [ ! -x "$release_venv/bin/python" ]; then
        "$BOOTSTRAP_PYTHON" -m venv "$release_venv"
    fi
    export PATH="$release_venv/bin:$PATH"
    python -m pip install --upgrade pip
    python -m pip install '.[dev]' build twine

    python -m ruff check --select E9,F63,F7,F82 braincell tests
    python scripts/lint_changed_python.py --base origin/main
    # QtWebEngine's offscreen renderer can retain platform state inside a broad
    # pytest selection. Run the real hittest subprocess module in its own fresh
    # cgroup after the rest of the suite, rather than weakening renderer coverage
    # or allowing one renderer run to affect another.
    "$PROJECT_ROOT/scripts/test-gui-safe.sh" tests --ignore=tests/test_gui_hittest.py
    "$PROJECT_ROOT/scripts/test-gui-safe.sh" tests/test_gui_hittest.py

    # A small, deterministic observation rather than a hardware-specific
    # latency gate.  It exercises connected-Project and Pool reads plus the
    # actual native Map startup without using the host's normal venv or /tmp.
    BRAINCELL_BENCH_BIN_DIR="$release_venv/bin" \
        python scripts/pool_bench.py --iterations 8 --notes 20 --chunks 20 --members 4 \
        > "$run_dir/performance-baseline.json"
    python -c 'import json, pathlib, sys; payload = json.loads(pathlib.Path(sys.argv[1]).read_text()); assert payload["metadata"]["workspace_retained"] is False; assert payload["query_timings"]["pools"]; assert all(not item.get("timed_out", False) for item in payload["console_and_processes"].values())' \
        "$run_dir/performance-baseline.json"

    mkdir -p "$run_dir/dist"
    python -m build --outdir "$run_dir/dist"
    python -m twine check "$run_dir/dist"/*
    python scripts/inspect_release_artifacts.py "$run_dir/dist"

    for artifact in "$run_dir/dist"/*.whl "$run_dir/dist"/*.tar.gz; do
        smoke_venv="$run_dir/smoke-$(basename "$artifact" | sed 's/[^A-Za-z0-9]/-/g')"
        python -m venv "$smoke_venv"
        "$smoke_venv/bin/pip" install --force-reinstall "$artifact"
        "$smoke_venv/bin/braincell" --help >/dev/null
        "$smoke_venv/bin/braincell-mcp" --help >/dev/null
        "$smoke_venv/bin/braincell-map" --help >/dev/null
        "$smoke_venv/bin/braincell" setup --help >/dev/null
    done
}

run_release_check() {
    # The default sandbox is this project's dedicated NVMe mount. Contributors
    # without that mount point BRAINCELL_RELEASE_CHECK_ROOT at any dedicated
    # absolute directory on a fast disk; the cgroup, lock, free-space, and
    # isolated HOME/XDG/tmp guarantees below apply identically there.
    case "$SANDBOX_ROOT" in
        /) die "release-check-safe refuses the filesystem root as a sandbox" ;;
        /*) ;;
        *) die "BRAINCELL_RELEASE_CHECK_ROOT must be an absolute path" ;;
    esac
    [ "$SANDBOX_ROOT" != "$HOME" ] || die "release-check-safe refuses \$HOME itself as a sandbox"
    case "$SANDBOX_ROOT" in
        "$PROJECT_ROOT"|"$PROJECT_ROOT"/*) die "release-check-safe refuses a sandbox inside the checkout" ;;
    esac
    command -v systemd-run >/dev/null || die "systemd-run is required; refusing uncaged release validation"
    command -v systemctl >/dev/null || die "systemctl is required; refusing uncaged release validation"
    command -v flock >/dev/null || die "flock is required; refusing concurrent release validation"
    command -v "$BOOTSTRAP_PYTHON" >/dev/null || die "bootstrap Python not found: $BOOTSTRAP_PYTHON"
    [[ "$MIN_FREE_GIB" =~ ^[0-9]+$ ]] || die "BRAINCELL_RELEASE_MIN_FREE_GIB must be a whole number"
    [[ "$POLL_SECONDS" =~ ^[1-9][0-9]*$ ]] || die "BRAINCELL_RELEASE_POLL_SECONDS must be positive"

    mkdir -p "$SANDBOX_ROOT"
    local available_kib minimum_kib
    available_kib=$(df -Pk "$SANDBOX_ROOT" | awk 'END { print $4 }')
    minimum_kib=$((MIN_FREE_GIB * 1024 * 1024))
    [ "$available_kib" -ge "$minimum_kib" ] || die "need at least ${MIN_FREE_GIB} GiB free under $SANDBOX_ROOT"

    local run_dir unit_name launcher_pid monitor_pid status
    run_dir=$(mktemp -d "$SANDBOX_ROOT/run-$(date +%Y%m%d-%H%M%S)-XXXXXX")
    unit_name="braincell-release-check-$(date +%Y%m%d%H%M%S)-$$"
    trap "cleanup_run $(printf '%q' "$run_dir") \$?" EXIT

    flock -n "$SANDBOX_ROOT/release-check.lock" \
        systemd-run --user --scope --quiet --collect --unit="$unit_name" \
            -p "MemoryHigh=$MEMORY_HIGH" \
            -p "MemoryMax=$MEMORY_MAX" \
            -p "MemorySwapMax=0" \
            -p "OOMPolicy=kill" \
            -p "CPUQuota=400%" \
            -p "TasksMax=128" \
            -p "RuntimeMaxSec=$RUNTIME_MAX" \
            env "BRAINCELL_RELEASE_SCOPE=1" \
                "BRAINCELL_RELEASE_BOOTSTRAP_PYTHON=$BOOTSTRAP_PYTHON" \
                bash "$0" --inside "$run_dir" "$SANDBOX_ROOT" &
    launcher_pid=$!
    monitor_scope "$unit_name" "$launcher_pid" "$run_dir" &
    monitor_pid=$!

    set +e
    wait "$launcher_pid"
    status=$?
    set -e
    kill "$monitor_pid" 2>/dev/null || true
    wait "$monitor_pid" 2>/dev/null || true
    if [ -e "$run_dir/memory-pressure" ]; then
        printf 'release check encountered cgroup memory pressure; see %s\n' "$run_dir/resource-events.log" >&2
        return 1
    fi
    return "$status"
}

if [ "${1:-}" = "--help" ]; then
    usage
elif [ "${1:-}" = "--inside" ]; then
    [ "$#" -eq 3 ] || die "internal release-check invocation is invalid"
    run_inside_scope "$2" "$3"
elif [ "$#" -eq 0 ]; then
    run_release_check
else
    usage >&2
    exit 2
fi
