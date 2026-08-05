# SPDX-License-Identifier: AGPL-3.0-or-later
"""Crash-safe, interprocess-serialized writes for BrainCell JSON catalogs."""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class MutationBusyError(RuntimeError):
    """Another process already owns a destination-scoped mutation lock."""


@contextmanager
def catalog_lock(catalog_path: Path) -> Iterator[None]:
    """Hold an exclusive process lock associated with *catalog_path*."""
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = catalog_path.with_name(f"{catalog_path.name}.lock")
    # r+b after touch, mirroring mutation_lock: append mode sent the one-byte
    # seed to EOF on every acquisition, growing the lockfile forever.
    lock_path.touch(exist_ok=True)
    with lock_path.open("r+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            # Seed exactly one byte BEFORE locking, never write while locked
            # (same CRT region-bookkeeping constraint as mutation_lock).
            # Windows region locks are MANDATORY: while a contender holds
            # byte 0, even reading it raises EACCES — which itself proves the
            # byte exists, so treat any OSError here as already-seeded. The
            # same race can hit the seed write; a failed seed means another
            # process seeded and locked first, which is equally fine.
            try:
                lock_file.seek(0)
                if lock_file.read(1) == b"":
                    lock_file.seek(0)
                    lock_file.write(b"\0")
                    lock_file.flush()
            except OSError:
                pass
            # A real blocking acquire. msvcrt's LK_LOCK is NOT one: it retries
            # ten times a second apart, then raises EDEADLK ("Resource
            # deadlock avoided") — observed in CI with 16 contending catalog
            # writers on a slow runner. POSIX flock blocks indefinitely, and
            # this loop matches that contract.
            while True:
                lock_file.seek(0)
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    # The handle close (next) releases the OS region lock; a
                    # CRT-level unlock failure must not fail the completed
                    # catalog write.
                    pass
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def mutation_lock(destination: Path, *, operation: str) -> Iterator[None]:
    """Acquire one non-blocking interprocess lock for a mutable destination.

    The fixed sibling lockfile is reused forever; it is not a per-run artifact.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock_path = destination.with_name(f"{destination.name}.mutation.lock")
    # r+b, not a+b: append mode silently redirects every positioned write to
    # EOF, so the metadata rewrite below would accumulate one stale line per
    # acquisition instead of replacing the previous owner's line.
    lock_path.touch(exist_ok=True)
    with lock_path.open("r+b") as lock_file:
        try:
            if os.name == "nt":
                import msvcrt

                # Seed exactly one byte BEFORE locking so a region exists to
                # lock. The file is never written again while byte 0 is
                # locked: rewriting or truncating the locked region breaks
                # the CRT's region bookkeeping and LK_UNLCK then fails with
                # EACCES (observed failing every locked mutation exit in
                # Windows CI). Owner metadata is therefore POSIX-only.
                lock_file.seek(0)
                if lock_file.read(1) == b"":
                    lock_file.seek(0)
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            raise MutationBusyError(
                f"{operation} refused: another mutation already owns {destination}"
            ) from exc

        if os.name != "nt":
            # flock is whole-file and position-independent, so the owner line
            # can be rewritten safely while held. Write-then-truncate keeps
            # the file one line long across acquisitions.
            lock_file.seek(0)
            lock_file.write(f"pid={os.getpid()} operation={operation}\n".encode())
            lock_file.truncate()
            lock_file.flush()
        try:
            yield
        finally:
            if os.name == "nt":
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    # Closing the handle (the with-block, next) releases the
                    # OS region lock regardless; a CRT-level unlock failure
                    # must not convert a completed mutation into an error.
                    pass
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def atomic_write_json(path: Path, value: object, *, sort_keys: bool = False) -> None:
    """Write JSON beside *path*, fsync it, then atomically replace the catalog."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=sort_keys)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            # Persist the directory entry as well as the file contents. Without
            # this fsync, a power loss can lose the rename after a successful
            # return even though the temporary file itself was durable.
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()
