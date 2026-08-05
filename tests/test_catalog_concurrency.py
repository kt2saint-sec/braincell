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


def _identity_worker(xdg: str, project_path: str, start, results) -> None:
    os.environ["XDG_DATA_HOME"] = xdg
    os.environ["BRAINCELL_DATA_NAMESPACE"] = "braincell_test"
    from braincell.config import get_project_id

    start.wait()
    results.put(get_project_id(Path(project_path)))


def _hold_mutation_lock(destination: str, ready, release) -> None:
    from braincell.catalog_io import mutation_lock

    with mutation_lock(Path(destination), operation="first-build"):
        ready.set()
        release.wait(timeout=20)


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


def test_concurrent_same_path_identity_creation_returns_one_ulid(tmp_path):
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    xdg = tmp_path / "xdg"
    project = tmp_path / "one-project"
    project.mkdir()
    processes = [
        context.Process(
            target=_identity_worker,
            args=(str(xdg), str(project), start, results),
        )
        for _ in range(12)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    identities = {results.get(timeout=2) for _ in processes}
    assert len(identities) == 1


def test_corrupt_registry_mutation_refuses_and_preserves_bytes(tmp_path):
    from braincell.config import get_path_registry_path
    from braincell.project_registry import register_path

    catalog = get_path_registry_path()
    catalog.parent.mkdir(parents=True)
    original = b"{ definitely not valid json"
    catalog.write_bytes(original)

    import pytest

    with pytest.raises(RuntimeError, match="refusing to mutate"):
        register_path(tmp_path / "project", "01PROJECT")
    assert catalog.read_bytes() == original


def test_structurally_valid_registry_cannot_escape_state_root(tmp_path):
    import json

    from braincell.config import get_path_registry_path
    from braincell.project_registry import load_path_registry, register_path

    catalog = get_path_registry_path()
    catalog.parent.mkdir(parents=True)
    original = json.dumps({str(tmp_path.resolve()): "/tmp/escaped"}).encode()
    catalog.write_bytes(original)

    assert load_path_registry() == {}
    import pytest
    with pytest.raises(RuntimeError, match="invalid format"):
        register_path(tmp_path / "project", "01PROJECT")
    assert catalog.read_bytes() == original


def test_destination_mutation_lock_has_one_owner_and_deterministic_busy_result(
    tmp_path,
):
    from braincell.catalog_io import MutationBusyError, mutation_lock

    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    destination = tmp_path / "project" / "braincell.db"
    process = context.Process(
        target=_hold_mutation_lock,
        args=(str(destination), ready, release),
    )
    process.start()
    assert ready.wait(timeout=10)
    try:
        import pytest

        with (
            pytest.raises(MutationBusyError, match="another mutation already owns"),
            mutation_lock(destination, operation="second-build"),
        ):
            pass
    finally:
        release.set()
        process.join(timeout=20)
    assert process.exitcode == 0
    assert destination.with_name("braincell.db.mutation.lock").exists()


def test_windows_mutation_lock_never_writes_the_locked_region(tmp_path, monkeypatch):
    """msvcrt's non-blocking lock covers byte 0, and rewriting or truncating
    that byte while locked breaks the CRT's region bookkeeping — LK_UNLCK then
    fails with EACCES, which failed every locked mutation exit in Windows CI.
    Contract: the lockfile is written only to seed its single byte BEFORE the
    lock is taken, never while it is held, and a CRT-level unlock failure is
    tolerated because closing the handle releases the OS lock regardless."""
    import sys

    from braincell import catalog_io

    lock_calls: list[int] = []
    writes_while_locked: list[str] = []

    class _FakeMsvcrt:
        LK_NBLCK = 2
        LK_UNLCK = 0
        fail_unlock = False

        @classmethod
        def locking(cls, fd, mode, nbytes):
            lock_calls.append(mode)
            if mode == cls.LK_UNLCK and cls.fail_unlock:
                raise OSError(13, "Permission denied")

    def _locked() -> bool:
        return (
            lock_calls.count(_FakeMsvcrt.LK_NBLCK)
            > lock_calls.count(_FakeMsvcrt.LK_UNLCK)
        )

    class _SpyFile:
        def __init__(self, handle):
            self._handle = handle

        def write(self, data):
            if _locked():
                writes_while_locked.append(f"write:{data!r}")
            return self._handle.write(data)

        def truncate(self, size=None):
            if _locked():
                writes_while_locked.append("truncate")
            return self._handle.truncate(size)

        def __getattr__(self, name):
            return getattr(self._handle, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._handle.__exit__(*exc)

    real_open = Path.open

    def _spy_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        if self.name.endswith(".mutation.lock"):
            return _SpyFile(handle)
        return handle

    monkeypatch.setattr(catalog_io.os, "name", "nt")
    monkeypatch.setitem(sys.modules, "msvcrt", _FakeMsvcrt())
    monkeypatch.setattr(Path, "open", _spy_open)

    destination = tmp_path / "project" / "braincell.db"
    with catalog_io.mutation_lock(destination, operation="win-lock-check"):
        pass

    assert lock_calls == [_FakeMsvcrt.LK_NBLCK, _FakeMsvcrt.LK_UNLCK]
    assert not writes_while_locked, (
        f"the locked lockfile region was written while held: {writes_while_locked}"
    )
    lock_path = destination.with_name("braincell.db.mutation.lock")
    assert lock_path.read_bytes() == b"\0"

    # Reacquisition neither rewrites nor grows the already-seeded lockfile.
    with catalog_io.mutation_lock(destination, operation="win-lock-check"):
        pass
    assert lock_path.read_bytes() == b"\0"
    assert not writes_while_locked

    # A CRT-level unlock failure never converts a completed mutation into an
    # error — the handle close releases the OS region lock regardless.
    _FakeMsvcrt.fail_unlock = True
    with catalog_io.mutation_lock(destination, operation="win-lock-check"):
        pass
