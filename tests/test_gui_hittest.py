# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_gui_hittest.py — REAL hit-testing of the served SPA's chrome at narrow
viewport widths (the 2026-07-25 dead-toolbar bug).

The feed rail was a rigid ``flex:0 0 640px`` while the toolbar was absolutely
positioned with no right bound and the header could not wrap: at ≤1366px the
toolbar/header controls extended past the shrunken main column and UNDER the
opaque rail — they rendered and reported visible+enabled, but
``document.elementFromPoint`` at their center returned the rail, so clicks
never reached them (Playwright: "subtree intercepts pointer events"). A user
on a 1366px laptop could not press "⬇ Build memory (no MCP)" at all.

String assertions cannot catch this class of bug, so this test renders the
REAL ``INDEX_HTML`` in the real Chromium engine we ship (QtWebEngine,
offscreen — no window, no display needed) and asserts that every toolbar +
header control receives its own center-point hit at 1280 / 1366 / 1440 wide.
Qt runs in a SUBPROCESS so the pytest process never hosts a QApplication.

Requires the optional ``native`` extra (PySide6); skipped when absent.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip(
    "PySide6.QtWebEngineWidgets",
    reason="hit-testing needs QtWebEngine (pip install 'braincell-mcp[native]')",
)

# The controls a narrow viewport used to kill, plus their toolbar/header peers.
_CONTROL_IDS = [
    "scope-project", "add-repo-btn", "new-family-btn",
    "build-btn", "cmd-btn", "tut-btn",
]
_WIDTHS = [1280, 1366, 1440]

_RUNNER = textwrap.dedent("""
    import json, sys
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWebEngineWidgets import QWebEngineView

    html_path, ids_json, widths_json = sys.argv[1], sys.argv[2], sys.argv[3]
    ids, widths = json.loads(ids_json), json.loads(widths_json)

    JS = '''
    (function(){
      // file:// harness has no API, so the SPA paints its empty-state overlay
      // (pointer-events:all, its own CTA flow — not under test here). Hide it
      // via the SPA's own helper, exactly as the app does once data exists:
      // the bug under test is populated-state chrome overlapped by the rail.
      if (typeof hideOverlay === 'function') hideOverlay();
      const ids = %s;
      const out = {viewport: innerWidth, dead: []};
      for (const id of ids) {
        const el = document.getElementById(id);
        if (!el) { out.dead.push({id: id, why: 'missing'}); continue; }
        const b = el.getBoundingClientRect();
        const hit = document.elementFromPoint((b.left+b.right)/2, (b.top+b.bottom)/2);
        const ok = hit && (hit === el || el.contains(hit) || (hit.contains && hit.contains(el)));
        if (!ok) out.dead.push({id: id, hit: hit ? (hit.id || hit.className || hit.tagName) : null});
      }
      return JSON.stringify(out);
    })()
    ''' % ids_json

    app = QApplication([])
    view = QWebEngineView()
    results, idx = [], [0]

    def measure():
        view.page().runJavaScript(JS, 0, got)

    def got(payload):
        results.append(json.loads(payload))
        idx[0] += 1
        if idx[0] >= len(widths):
            print(json.dumps(results))
            app.exit(0)
        else:
            view.resize(widths[idx[0]], 860)
            QTimer.singleShot(900, measure)

    view.loadFinished.connect(lambda ok: QTimer.singleShot(1500, measure))
    view.resize(widths[0], 860)
    view.load(QUrl.fromLocalFile(html_path))
    view.show()
    QTimer.singleShot(90000, lambda: app.exit(3))
    sys.exit(app.exec())
""")


def test_toolbar_and_header_controls_hittable_at_narrow_widths(tmp_path: Path):
    from braincell.gui_template import INDEX_HTML

    page = tmp_path / "index.html"
    page.write_text(INDEX_HTML, encoding="utf-8")
    runner = tmp_path / "runner.py"
    runner.write_text(_RUNNER, encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(runner), str(page),
         json.dumps(_CONTROL_IDS), json.dumps(_WIDTHS)],
        capture_output=True, text=True, timeout=180,
        env={
            **__import__("os").environ,
            "QT_QPA_PLATFORM": "offscreen",   # no window, no display needed
            "QTWEBENGINE_CHROMIUM_FLAGS": "--no-sandbox",  # CI-safe
        },
    )
    assert proc.returncode == 0, f"hit-test runner failed:\n{proc.stderr[-2000:]}"
    results = json.loads(proc.stdout.strip().splitlines()[-1])
    assert len(results) == len(_WIDTHS)
    failures = {r["viewport"]: r["dead"] for r in results if r["dead"]}
    assert not failures, (
        "controls unreachable by pointer (overlapped by other chrome, e.g. the "
        f"feed rail) at these viewport widths: {failures}"
    )


# ── Tutorial dropdown, scrollbar theme, dock reflow (2026-07-25 audit #2) ─────
#
# One shared engine run (module-scope fixture) measures all three defect
# groups at 1620x900 and 1100x700 in real offscreen QtWebEngine. Each was
# proven to FAIL on pre-fix code: no Tutorial dropdown existed, zero scrollbar
# CSS existed (colorScheme 'normal', scrollbarColor 'auto'), and the dock's
# rigid 300px height / rigid column bases overflowed ~50px vertically and
# horizontally at narrow widths.

_RUNNER2 = textwrap.dedent("""
    import json, sys
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWebEngineWidgets import QWebEngineView

    html_path = sys.argv[1]
    SIZES = [(1620, 900), (1100, 700)]

    JS = '''
    (function(){
      if (typeof hideOverlay === 'function') hideOverlay();
      const out = {viewport: innerWidth + 'x' + innerHeight};

      // Tutorial dropdown: open it, hit-test trigger + both entries.
      const dd = document.getElementById('tut-dd');
      if (dd && dd.style.display === 'none' && typeof toggleTutorialMenu === 'function')
        toggleTutorialMenu();
      out.tut = [];
      for (const id of ['tut-btn', 'help-btn', 'tut-cmds']) {
        const el = document.getElementById(id);
        if (!el) { out.tut.push({id: id, why: 'missing'}); continue; }
        const b = el.getBoundingClientRect();
        if (!b.width || !b.height) { out.tut.push({id: id, why: 'zero-size'}); continue; }
        const hit = document.elementFromPoint((b.left+b.right)/2, (b.top+b.bottom)/2);
        const ok = hit && (hit === el || el.contains(hit) || (hit.contains && hit.contains(el)));
        if (!ok) out.tut.push({id: id, hit: hit ? (hit.id || hit.className || hit.tagName) : null});
      }

      // Scrollbar theme: computed values, not string-presence.
      out.color_scheme = getComputedStyle(document.documentElement).colorScheme;
      out.scrollbar_color = getComputedStyle(document.querySelector('.feed')).scrollbarColor;

      // Dock: open with representative content (real inspector content
      // measured ~348px tall) and measure overflow.
      const dock = document.getElementById('drawer');
      dock.classList.add('open');
      for (const col of dock.querySelectorAll('.col')) {
        if (!col.querySelector('.__spacer')) {
          const sp = document.createElement('div');
          sp.className = '__spacer'; sp.style.height = '320px';
          col.appendChild(sp);
        }
      }
      out.dock_h_overflow = Math.max(0, dock.scrollWidth - dock.clientWidth);
      out.col_v_overflow = Math.max(...[...dock.querySelectorAll('.col')]
        .map(c => Math.max(0, c.scrollHeight - c.clientHeight)));
      return JSON.stringify(out);
    })()
    '''

    app = QApplication([])
    view = QWebEngineView()
    results, idx = [], [0]

    def measure():
        view.page().runJavaScript(JS, 0, got)

    def got(payload):
        results.append(json.loads(payload))
        idx[0] += 1
        if idx[0] >= len(SIZES):
            print(json.dumps(results))
            app.exit(0)
        else:
            view.resize(*SIZES[idx[0]])
            QTimer.singleShot(900, measure)

    view.loadFinished.connect(lambda ok: QTimer.singleShot(1500, measure))
    view.resize(*SIZES[0])
    view.load(QUrl.fromLocalFile(html_path))
    view.show()
    QTimer.singleShot(90000, lambda: app.exit(3))
    sys.exit(app.exec())
""")


@pytest.fixture(scope="module")
def chrome_metrics(tmp_path_factory):
    import os
    from braincell.gui_template import INDEX_HTML

    tmp = tmp_path_factory.mktemp("hittest2")
    page = tmp / "index.html"
    page.write_text(INDEX_HTML, encoding="utf-8")
    runner = tmp / "runner2.py"
    runner.write_text(_RUNNER2, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(runner), str(page)],
        capture_output=True, text=True, timeout=180,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen",
             "QTWEBENGINE_CHROMIUM_FLAGS": "--no-sandbox"},
    )
    assert proc.returncode == 0, f"engine runner failed:\n{proc.stderr[-2000:]}"
    results = json.loads(proc.stdout.strip().splitlines()[-1])
    assert len(results) == 2
    return {r["viewport"]: r for r in results}


def test_tutorial_dropdown_open_items_hittable(chrome_metrics):
    """The Tutorial control and BOTH dropdown entries (guided tour = help-btn,
    Command List) must be pointer-reachable at wide AND narrow sizes."""
    failures = {vp: r["tut"] for vp, r in chrome_metrics.items() if r["tut"]}
    assert not failures, f"Tutorial dropdown controls unreachable: {failures}"


def test_dark_scrollbar_theme_computed(chrome_metrics):
    """color-scheme:dark and a themed scrollbar-color must be COMPUTED on the
    live page (raw Chromium default grey scrollbars on the dark theme were the
    owner-reported defect); 'normal'/'auto' = unstyled."""
    for vp, r in chrome_metrics.items():
        assert r["color_scheme"] == "dark", f"{vp}: color-scheme={r['color_scheme']!r}"
        assert r["scrollbar_color"] not in ("auto", "", None), (
            f"{vp}: scrollbar-color={r['scrollbar_color']!r} (unstyled default)"
        )


def test_dock_columns_fit_without_overflow(chrome_metrics):
    """With representative content (~real inspector height), dock columns must
    not overflow vertically at 1620x900 (was ~50px — truncated panels), and the
    dock must not scroll horizontally at 1100x700 (rigid column bases used to
    exceed the main column's width)."""
    wide = chrome_metrics["1620x900"]
    assert wide["col_v_overflow"] == 0, (
        f"dock column overflows vertically by {wide['col_v_overflow']}px at 1620x900"
    )
    narrow = chrome_metrics["1100x700"]
    assert narrow["dock_h_overflow"] == 0, (
        f"dock overflows horizontally by {narrow['dock_h_overflow']}px at 1100x700"
    )
