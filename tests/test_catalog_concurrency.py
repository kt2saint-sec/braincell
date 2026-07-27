# SPDX-License-Identifier: AGPL-3.0-or-later
"""Concurrent catalog writers must not lose Project or Pool updates."""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path


def _register_worker(xdg: str, index: int, start) -> None:
    os.environ["XDG_DATA_HOME"] = xdg
    os.environ["BRAINCELL_DATA_NAMESPACE"] = "braincell_test"
    from braincell.project_registry import register_path

    start.wait()
    register_path(Path(xdg) / f"project-{index}", f"01PROJECT{index:03d}")


def _pool_worker(xdg: str, index: int, start) -> None:
    os.environ["XDG_DATA_HOME"] = xdg
    os.environ["BRAINCELL_DATA_NAMESPACE"] = "braincell_test"
    from braincell.project_registry import add_to_pool

    start.wait()
    add_to_pool("Concurrent", [f"01MEMBER{index:03d}"])


def _run_writers(worker, xdg: Path, count: int = 16) -> None:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    processes = [
        context.Process(target=worker, args=(str(xdg), index, start))
        for index in range(count)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0


def test_concurrent_path_registry_updates_are_not_lost(tmp_path):
    from braincell.project_registry import load_path_registry

    xdg = tmp_path / "xdg"
    _run_writers(_register_worker, xdg)
    registry = load_path_registry()
    assert len(registry) == 16
    assert set(registry.values()) == {f"01PROJECT{index:03d}" for index in range(16)}


def test_concurrent_pool_membership_updates_are_not_lost(tmp_path):
    from braincell.project_registry import create_pool, resolve_pool

    xdg = tmp_path / "xdg"
    create_pool("Concurrent")
    _run_writers(_pool_worker, xdg)
    _name, members = resolve_pool("concurrent")
    assert members == tuple(f"01MEMBER{index:03d}" for index in range(16))
