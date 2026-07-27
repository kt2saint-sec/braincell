#!/usr/bin/env python3
"""Fail closed when release archives contain local-only development material."""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path


FORBIDDEN_PARTS = (".env", ".git/", "uv.lock", "__pycache__", ".pytest_cache", "evals/")


def _names(path: Path) -> list[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()
    with tarfile.open(path) as archive:
        return archive.getnames()


def main() -> None:
    artifacts = [path for path in Path(sys.argv[1]).iterdir() if path.suffix in {".whl", ".gz"}]
    if not artifacts:
        raise SystemExit("no wheel or sdist found")
    for artifact in artifacts:
        bad = [name for name in _names(artifact) if any(part in name for part in FORBIDDEN_PARTS)]
        if bad:
            raise SystemExit(f"{artifact}: forbidden release content: {bad}")
        print(f"ok: {artifact.name}")


if __name__ == "__main__":
    main()
