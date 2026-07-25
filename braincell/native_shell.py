# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
native_shell.py — optional PySide6/QtWebEngine window around the EXISTING GUI.

An ADDITIVE native front door, not a replacement: the FastAPI + uvicorn server
and the embedded SPA stay exactly as they are (browser access keeps working
unchanged), and the Qt window is just a webview pointed at
``http://127.0.0.1:<port>/?t=…`` — the same origin the SPA is served from, so
its ``fetch()`` calls stay same-origin (no CORS, no custom scheme handlers, no
JS bridge, no SPA rewrite). QtWebEngine is Chromium, so rendering + fetch
behave as in a browser tab.

PySide6 is an OPTIONAL extra (``pip install braincell-mcp[native]``); every
caller must degrade to the browser path when it is absent —
:func:`native_available` is the one gate (import + display check).

Qt must own the MAIN thread, so :func:`serve_native` inverts run_gui's usual
arrangement: uvicorn runs on a daemon thread (uvicorn skips signal-handler
install off the main thread) and the Qt event loop blocks the main thread
until the window closes; then the server is asked to exit.
"""

from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger("braincell.native")

WINDOW_TITLE = "BrainCell"
# GNOME/Wayland group windows to taskbar icons by desktop-file id — this must
# match the basename of the installed applications/<id>.desktop entry
# (gui.install_launcher writes braincell-map.desktop).
DESKTOP_FILE_ID = "braincell-map"
_WINDOW_SIZE = (1280, 860)


def native_available() -> bool:
    """True when the native window can actually open here.

    Two conditions: PySide6's QtWebEngine imports, and a display is reachable
    (X11/Wayland — or an explicit ``QT_QPA_PLATFORM``, e.g. ``offscreen`` in
    tests). Callers use this to fall back to the browser path, so it must
    never raise.
    """
    if not (
        os.environ.get("DISPLAY")
        or os.environ.get("WAYLAND_DISPLAY")
        or os.environ.get("QT_QPA_PLATFORM")
    ):
        return False
    try:
        import PySide6.QtWebEngineWidgets  # noqa: F401
    except Exception:  # noqa: BLE001 — absent/broken install = unavailable
        return False
    return True


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


def open_window(url: str, *, title: str = WINDOW_TITLE) -> int:
    """Open a webview window at *url* and block until it closes.

    Returns the Qt exit code. The URL may carry ``?t=`` — the server's GET /
    strips it and hands the token back as the durable cookie, exactly as in a
    browser tab.
    """
    from PySide6.QtCore import QUrl
    from PySide6.QtWebEngineWidgets import QWebEngineView

    app = _qt_app()
    view = QWebEngineView()
    view.setWindowTitle(title)
    # Maximized by default: the SPA has always sized itself to a full-width
    # browser tab — at a fixed 1280 px the inspector dock overflows and grows
    # an unstyled default scrollbar (owner-reported, 2026-07-25). resize() only
    # sets the restore geometry for when the user un-maximizes; the window
    # stays freely resizable.
    view.resize(*_WINDOW_SIZE)
    view.load(QUrl(url))
    view.showMaximized()
    return app.exec()


def show_error(message: str, *, title: str = WINDOW_TITLE) -> None:
    """Modal error dialog — the desktop icon runs with ``Terminal=false``, so
    stderr is invisible and a silent exit reads as a dead click."""
    from PySide6.QtWidgets import QMessageBox

    _qt_app()
    QMessageBox.critical(None, title, message)


def _make_server(app, *, port: int):
    """Build the uvicorn Server (seam — tests substitute a fake here)."""
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port)
    return uvicorn.Server(config)


def serve_native(app, *, port: int, url: str, startup_timeout: float = 20.0) -> None:
    """Serve *app* on a background uvicorn thread and front it with a Qt window.

    Blocks until the window closes, then shuts the server down — closing the
    window IS quitting the app (the native shell's whole point: no orphaned
    "browser tab you might lose track of"). Raises RuntimeError if the server
    never binds (port taken by a race, etc.) so the caller can report it.
    """
    server = _make_server(app, port=port)
    thread = threading.Thread(
        target=server.run, name="braincell-gui-server", daemon=True
    )
    thread.start()
    deadline = time.monotonic() + startup_timeout
    while not getattr(server, "started", False):
        if not thread.is_alive():
            raise RuntimeError(
                f"GUI server exited before binding port {port} "
                "(port already in use?)"
            )
        if time.monotonic() > deadline:
            server.should_exit = True
            raise RuntimeError(
                f"GUI server did not start within {startup_timeout:.0f}s"
            )
        time.sleep(0.05)
    try:
        open_window(url)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
