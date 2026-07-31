# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""Retired global hook compatibility entry point.

Old user-level Claude configuration may still invoke this module. It must stay
fail-quiet and perform no reads while explicit legacy cleanup and Project-local
Automatic Pool recall are implemented.
"""

from __future__ import annotations

import sys


def main() -> None:
    if not sys.stdin.isatty():
        sys.stdin.read()
    print("{}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 — last-resort fail-quiet boundary; see comment below
        # Last-resort fail-quiet: no hook error may ever surface to the user.
        print("{}")
