# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
test_log.py — regression tests for BUGS.md "logger fallback": a failure
constructing the rotating file handler must never fall back to an unbounded
plain `FileHandler`. It retries once with conservative defaults, and only
gives up on file logging (falling back to the console handler that already
exists) if that retry also fails.
"""

from __future__ import annotations

import logging
import logging.handlers

import pytest

import braincell.log as log_module


@pytest.fixture(autouse=True)
def _reset_logging_state():
    """setup() is a one-shot (module-level `_configured` guard); reset it and
    the root logger's handlers so each test observes its own call cleanly."""
    log_module._configured = False
    root = logging.getLogger()
    saved = list(root.handlers)
    yield
    log_module._configured = False
    root.handlers[:] = saved


class TestRotatingFileHandlerFallback:
    def test_first_attempt_succeeds_without_a_retry(self, tmp_path):
        handler = log_module._rotating_file_handler(tmp_path / "b.log")
        try:
            assert isinstance(handler, logging.handlers.RotatingFileHandler)
        finally:
            handler.close()

    def test_retries_with_conservative_defaults_on_first_failure(self, tmp_path, monkeypatch):
        """Fault injection: the first (10MB x5) construction fails; the retry
        with smaller, safer defaults must still produce a bounded handler."""
        real_cls = logging.handlers.RotatingFileHandler
        calls: list[dict] = []

        def _factory(*args, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise OSError("simulated failure constructing the first rotating handler")
            return real_cls(*args, **kwargs)

        monkeypatch.setattr(logging.handlers, "RotatingFileHandler", _factory)
        handler = log_module._rotating_file_handler(tmp_path / "b.log")
        try:
            assert handler is not None
            assert isinstance(handler, real_cls)
            assert len(calls) == 2
            assert calls[1]["maxBytes"] < calls[0]["maxBytes"]
        finally:
            handler.close()

    def test_never_falls_back_to_an_unbounded_file_handler(self, tmp_path, monkeypatch):
        """Adversarial: both rotating attempts fail (e.g. permission denied).
        The pre-fix behaviour was a plain, unbounded `FileHandler` here — the
        fix must return None instead so the caller never opens an unbounded
        file."""
        def _boom(*args, **kwargs):
            raise OSError("simulated permission denied")

        monkeypatch.setattr(logging.handlers, "RotatingFileHandler", _boom)
        handler = log_module._rotating_file_handler(tmp_path / "b.log")
        assert handler is None


class TestSetupNeverCrashesOnABrokenLogFile:
    def test_setup_skips_file_logging_and_warns_on_stderr(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(log_module, "_rotating_file_handler", lambda path: None)

        logger = log_module.setup(log_file=tmp_path / "b.log")

        assert logger is not None
        assert not any(
            isinstance(h, logging.FileHandler) for h in logging.getLogger().handlers
        )
        captured = capsys.readouterr()
        assert "could not open a rotating log file" in captured.err
        assert str(tmp_path / "b.log") in captured.err

    def test_setup_keeps_the_console_handler_when_file_logging_is_unavailable(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr(log_module, "_rotating_file_handler", lambda path: None)

        log_module.setup(log_file=tmp_path / "b.log")

        # The console (Rich) handler must still be installed and usable even
        # though file logging failed — never-crash, degraded not silent.
        assert len(logging.getLogger().handlers) == 1

    def test_setup_with_a_working_handler_installs_no_warning(self, tmp_path, capsys):
        log_module.setup(log_file=tmp_path / "b.log")
        assert any(
            isinstance(h, logging.handlers.RotatingFileHandler)
            for h in logging.getLogger().handlers
        )
        assert "could not open" not in capsys.readouterr().err
