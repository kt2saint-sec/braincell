"""Run configured Ruff only for Python files changed since a merge base."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys


def _git(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True
    ).stdout


def changed_python_files(base: str, head: str) -> list[str]:
    """Return destination paths for added, copied, modified, and renamed Python files."""
    merge_base = _git("merge-base", base, head).strip().decode()
    paths = _git(
        "diff",
        "--name-only",
        "-z",
        "--find-renames",
        "--find-copies-harder",
        "--diff-filter=ACMR",
        merge_base,
        head,
    ).split(b"\0")
    return [os.fsdecode(path) for path in paths if path.endswith(b".py")]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Git ref used to find the PR merge base")
    parser.add_argument("--head", default="HEAD", help="Git ref being checked (default: HEAD)")
    args = parser.parse_args()

    try:
        paths = changed_python_files(args.base, args.head)
    except subprocess.CalledProcessError as error:
        sys.stderr.buffer.write(error.stderr)
        return error.returncode

    if not paths:
        print("No changed Python files; configured Ruff check not needed.")
        return 0

    print("Configured Ruff checks these changed Python files:")
    for path in paths:
        print(path)
    return subprocess.run(["ruff", "check", "--", *paths], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
