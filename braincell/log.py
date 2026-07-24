# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
log.py — Logging setup for BrainCell.

Console: rich-formatted, coloured by level.
File:    plain text at data/braincell.log, rotated at 10MB, kept 5 backups.
"""

import logging
import os
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
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s")
        )
        handlers.append(file_handler)

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


def _rotating_file_handler(path: Path) -> logging.Handler:
    try:
        from logging.handlers import RotatingFileHandler
        return RotatingFileHandler(
            path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
    except Exception:
        return logging.FileHandler(path, encoding="utf-8")
