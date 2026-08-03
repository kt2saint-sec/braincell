# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keep the local release runner's host-safety contract from quietly eroding."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RELEASE_RUNNER = REPO / "scripts" / "release-check-safe.sh"
LOCAL_CI_RUNNER = REPO / "scripts" / "multi-os-smoke" / "run_ci.sh"


def test_release_runner_retains_nvme_cgroup_and_monitoring_guards():
    """Release validation must stay contained even if its implementation evolves."""
    text = RELEASE_RUNNER.read_text(encoding="utf-8")
    required = (
        "/mnt/nvme-fast/braincell-release-sandbox",
        "MemoryHigh=$MEMORY_HIGH",
        "MemoryMax=$MEMORY_MAX",
        "MemorySwapMax=0",
        "OOMPolicy=kill",
        "TasksMax=128",
        "resource-samples.tsv",
        "memory.events",
        "memory-pressure",
        "refuses an uncaged packaging run",
        "test-gui-safe.sh\" tests",
        "python -m build --outdir",
        "python -m twine check",
    )
    missing = [token for token in required if token not in text]
    assert not missing, f"release safety guarantees missing from runner: {missing}"


def test_linux_local_ci_delegates_to_the_safe_release_runner():
    """Do not let the legacy portable script recreate an uncaged Linux run."""
    text = LOCAL_CI_RUNNER.read_text(encoding="utf-8")
    assert '"$(uname -s)" = "Linux"' in text
    assert 'exec "$SCRIPT_DIR/release-check-safe.sh"' in text
