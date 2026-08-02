# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
native_shell.py — PySide6/QtWebEngine host for the interactive BrainCell GUI.

The embedded SPA and FastAPI API communicate over a localhost-only same-origin
connection. Qt owns the visible application; uvicorn is an implementation
detail running on a background thread. There is no external-viewer fallback.

Qt must own the MAIN thread, so :func:`serve_native` inverts run_gui's usual
arrangement: uvicorn runs on a daemon thread (uvicorn skips signal-handler
install off the main thread) and the Qt event loop blocks the main thread
until the window closes; then the server is asked to exit.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("braincell.native")

WINDOW_TITLE = "BrainCell"
# GNOME/Wayland group windows to taskbar icons by desktop-file id — this must
# match the basename of the installed applications/<id>.desktop entry
# (gui.install_launcher writes braincell-map.desktop).
DESKTOP_FILE_ID = "braincell-map"
_WINDOW_SIZE = (1280, 860)
_PICKER_TIMEOUT_S = 180.0
_SIGNAL_POLL_MS = 200


def native_unavailable_reason() -> str | None:
    """None when the native window can open here, else an actionable message.

    Linux requires an X11/Wayland display (or an explicit Qt platform for
    deterministic offscreen tests). Windows and macOS do not advertise their
    desktop sessions through those environment variables.
    """
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY")
        or os.environ.get("WAYLAND_DISPLAY")
        or os.environ.get("QT_QPA_PLATFORM")
    ):
        return (
            "No graphical display detected. "
            "Run BrainCell from a graphical desktop session."
        )
    try:
        import PySide6.QtWebEngineWidgets  # noqa: F401
    except Exception:  # noqa: BLE001 — absent/broken install = unavailable
        return (
            "BrainCell's required native Memory Map runtime "
            "(PySide6/QtWebEngine) could not be loaded. Repair or reinstall "
            "BrainCell, then retry:\n"
            "  python -m pip install --upgrade --force-reinstall braincell-mcp\n"
            "(pipx: pipx reinstall braincell-mcp)"
        )
    return None


def native_available() -> bool:
    """True when the native window can actually open here."""
    return native_unavailable_reason() is None


@dataclass
class _PickerRequest:
    done: threading.Event
    result: dict | None = None

    def finish(self, result: dict) -> None:
        self.result = result
        self.done.set()


class NativeBridge:
    """Thread-safe bridge from FastAPI's server thread to Qt's main thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._activate: Callable[[], None] | None = None
        self._pick: Callable[[object], None] | None = None

    def attach(
        self,
        *,
        activate: Callable[[], None],
        pick_folder: Callable[[object], None],
    ) -> None:
        with self._lock:
            self._activate = activate
            self._pick = pick_folder

    def detach(self) -> None:
        with self._lock:
            self._activate = None
            self._pick = None

    def activate(self) -> bool:
        with self._lock:
            callback = self._activate
        if callback is None:
            return False
        callback()
        return True

    def pick_folder(self, timeout: float = _PICKER_TIMEOUT_S) -> dict:
        with self._lock:
            callback = self._pick
        if callback is None:
            return {"unavailable": True, "reason": "native window is not ready"}
        request = _PickerRequest(done=threading.Event())
        callback(request)
        if not request.done.wait(timeout):
            return {"unavailable": True, "reason": "folder picker timed out"}
        return request.result or {"cancelled": True}


def _load_icon():
    """Window icon from the packaged PNG assets (None if assets are missing)."""
    from importlib.resources import files

    from PySide6.QtGui import QIcon, QPixmap

    icon = QIcon()
    loaded = False
    for size in (48, 128, 256, 512):
        try:
            data = files("braincell").joinpath(
                "assets", f"braincell-{size}.png"
            ).read_bytes()
        except FileNotFoundError:  # pragma: no cover — partial install
            continue
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            icon.addPixmap(pixmap)
            loaded = True
    return icon if loaded else None


def _qt_app():
    """Create (or reuse) the QApplication, stamped with braincell's identity."""
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    # Must be set BEFORE the QApplication exists for Wayland taskbar grouping.
    QGuiApplication.setDesktopFileName(DESKTOP_FILE_ID)
    app = QApplication.instance() or QApplication([])
    app.setApplicationName(WINDOW_TITLE)
    icon = _load_icon()
    if icon is not None:
        app.setWindowIcon(icon)
    return app


def _install_quit_signal_handlers(quit_callback: Callable[[], None]) -> dict:
    """Route terminal/service shutdown signals through Qt's normal exit."""
    previous = {}

    def request_quit(_signum, _frame) -> None:
        quit_callback()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            old_handler = signal.getsignal(signum)
            signal.signal(signum, request_quit)
        except (OSError, ValueError):
            # Signal handlers can only be installed from Python's main thread.
            continue
        previous[signum] = old_handler
    return previous


def _restore_signal_handlers(previous: dict) -> None:
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except (OSError, ValueError):
            log.warning("Could not restore signal handler for %s", signum)


def open_window(
    url: str,
    *,
    title: str = WINDOW_TITLE,
    bridge: NativeBridge | None = None,
) -> int:
    """Open the BrainCell window at *url* and block until it closes."""
    from PySide6.QtCore import QObject, QTimer, QUrl, Signal, Slot
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QFileDialog

    class _WindowController(QObject):
        activate_requested = Signal()
        pick_folder_requested = Signal(object)

        def __init__(self, window: QWebEngineView) -> None:
            super().__init__()
            self.window = window
            self.activate_requested.connect(self._activate)
            self.pick_folder_requested.connect(self._pick_folder)

        @Slot()
        def _activate(self) -> None:
            if self.window.isMinimized():
                self.window.showNormal()
            else:
                self.window.show()
            self.window.raise_()
            self.window.activateWindow()

        @Slot(object)
        def _pick_folder(self, request: _PickerRequest) -> None:
            selected = QFileDialog.getExistingDirectory(
                self.window, "Select a project folder"
            )
            if not selected:
                request.finish({"cancelled": True})
                return
            path = Path(selected).expanduser()
            if not path.is_dir():
                request.finish(
                    {"unavailable": True, "reason": "selected path is not a directory"}
                )
                return
            request.finish({"path": str(path.resolve())})

    app = _qt_app()
    view = QWebEngineView()
    controller = _WindowController(view)
    view.setWindowTitle(title)
    # resize() sets the restore geometry; the window starts maximized and
    # remains freely resizable.
    view.resize(*_WINDOW_SIZE)
    view.load(QUrl(url))
    view.showMaximized()
    if bridge is not None:
        bridge.attach(
            activate=controller.activate_requested.emit,
            pick_folder=controller.pick_folder_requested.emit,
        )
    # A periodic Python callback lets CPython dispatch SIGINT/SIGTERM while
    # Qt's C++ event loop is blocking in app.exec().
    signal_timer = QTimer(app)
    signal_timer.timeout.connect(lambda: None)
    signal_timer.start(_SIGNAL_POLL_MS)
    previous_handlers = _install_quit_signal_handlers(app.quit)
    try:
        return app.exec()
    finally:
        signal_timer.stop()
        _restore_signal_handlers(previous_handlers)
        if bridge is not None:
            bridge.detach()


def show_error(message: str, *, title: str = WINDOW_TITLE) -> None:
    """Modal error dialog — the desktop icon runs with ``Terminal=false``, so
    stderr is invisible and a silent exit reads as a dead click."""
    from PySide6.QtWidgets import QMessageBox

    _qt_app()
    QMessageBox.critical(None, title, message)


def alert(message: str, *, title: str = WINDOW_TITLE) -> bool:
    """Best-effort VISIBLE error — never raises, never silent by design.

    The desktop icon runs with ``Terminal=false``: anything printed to stderr
    is invisible, so every launch failure must surface through something the
    user can see. Order: Qt modal dialog (when PySide6 + a display work) →
    ``notify-send`` desktop notification (when Qt itself is what's broken).
    Returns True when something was (probably) shown.
    """
    try:
        if native_available():
            show_error(message, title=title)
            return True
    except Exception:  # Qt broken ≠ stay silent; fall through to notify-send
        log.exception("Qt error dialog failed — falling back to notify-send")
    try:
        import subprocess

        subprocess.run(
            ["notify-send", "--urgency=critical", title, message],
            check=False, timeout=10,
        )
        return True
    except Exception:  # last resort exhausted; caller printed to stderr
        log.exception("notify-send fallback failed")
        return False


def _make_server(app, *, port: int):
    """Build the uvicorn Server (seam — tests substitute a fake here)."""
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port)
    return uvicorn.Server(config)


def serve_native(
    app,
    *,
    port: int,
    url: str,
    bridge: NativeBridge | None = None,
    startup_timeout: float = 20.0,
) -> None:
    """Serve *app* on a background uvicorn thread and front it with a Qt window.

    Blocks until the window closes, then shuts the server down — closing the
    window IS quitting the app. Raises RuntimeError if the server never binds
    (port taken by a race, etc.) so the caller can report it.
    """
    server = _make_server(app, port=port)
    thread = threading.Thread(
        target=server.run, name="braincell-gui-server", daemon=True
    )
    thread.start()
    try:
        deadline = time.monotonic() + startup_timeout
        while not getattr(server, "started", False):
            if not thread.is_alive():
                raise RuntimeError(
                    f"GUI server exited before binding port {port} "
                    "(port already in use?)"
                )
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"GUI server did not start within {startup_timeout:.0f}s — "
                    "the brain database may be locked by a running build/ingest, "
                    "or a schema migration is waiting on it. Let the build finish "
                    "(or stop it) and click again."
                )
            time.sleep(0.05)
        open_window(url, bridge=bridge)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError("GUI server did not stop within 10 seconds")
