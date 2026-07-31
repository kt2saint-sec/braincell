# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
log.py — Logging setup for BrainCell.

Console: rich-formatted, coloured by level.
File:    plain text at data/braincell.log, rotated at 10MB, kept 5 backups.
"""

import logging
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

_configured = False
_root_logger = logging.getLogger("braincell")


def setup(log_file: Path | None = None, level: str | None = None) -> logging.Logger:
    """
    Call once at startup (cli.py). Subsequent calls to get() return the same logger.
    Level order: CLI arg → BRAINCELL_LOG_LEVEL env var → default INFO.
    """
    global _configured
    if _configured:
        return _root_logger

    resolved_level = level or os.environ.get("BRAINCELL_LOG_LEVEL", "INFO").upper()
    numeric = getattr(logging, resolved_level, logging.INFO)

    handlers: list[logging.Handler] = [
        RichHandler(
            console=Console(stderr=True),  # never write logs to stdout — it is the
                                           # MCP JSON-RPC protocol stream on a stdio server
            rich_tracebacks=True,
            tracebacks_show_locals=False,
            markup=True,
            show_path=False,
        )
    ]

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = _rotating_file_handler(log_file)
        if file_handler is not None:
            file_handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s")
            )
            handlers.append(file_handler)
        else:
            # Never fall back to an unbounded plain FileHandler — the console
            # RichHandler above already carries every record, so file logging
            # is simply skipped rather than risking a log that grows forever.
            print(
                f"WARNING: braincell could not open a rotating log file at "
                f"{log_file} — file logging is disabled for this run; console "
                "logging continues.",
                file=sys.stderr,
            )

    logging.basicConfig(level=numeric, handlers=handlers, force=True)
    _root_logger.setLevel(numeric)

    # Suppress noisy third-party loggers
    for noisy in ("httpx", "httpcore", "urllib3", "ollama"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    return _root_logger


def get(name: str = "braincell") -> logging.Logger:
    """Return a named child logger. Always safe to call before setup()."""
    return logging.getLogger(name)


def _rotating_file_handler(path: Path) -> logging.Handler | None:
    """Bounded rotating file handler, or ``None`` if one cannot be constructed.

    Previously fell back to a plain ``FileHandler`` on any failure — unbounded,
    so a permission/disk/locking problem that broke rotation would trade a
    capped 10MB x5 log for one that grows forever. Retry once with
    conservative defaults (smaller cap, no backups) in case the failure was
    itself capacity-related; if that also fails, give up on file logging
    entirely rather than risk an unbounded file. The console handler set up in
    ``setup()`` still carries every record either way — never-crash is
    preserved, just without an unbounded-growth escape hatch.
    """
    from logging.handlers import RotatingFileHandler

    try:
        return RotatingFileHandler(
            path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
    except Exception:  # noqa: BLE001, S110 — whatever broke rotation, retry conservatively below; logging setup must never crash startup
        pass
    try:
        return RotatingFileHandler(
            path, maxBytes=1 * 1024 * 1024, backupCount=1, encoding="utf-8"
        )
    except Exception:  # noqa: BLE001 — give up on file logging rather than fall back to an unbounded file; console logging still carries every record
        return None
