# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_gui_commands_modal.py — ★ Commands modal behavior in the real engine
(2026-07-25 audit: every card must handle its empty state and report honestly).

The modal is CLIENT-rendered (openCommandsModal builds its HTML from the SPA's
`families`/`nodes` state), so served-HTML string assertions cannot see any of
this — these tests drive the real INDEX_HTML in offscreen QtWebEngine
(subprocess-isolated, same harness as test_gui_hittest) and assert DOM state
and toast text.

Pinned defects (both proven to fail on pre-fix code):
  1. POOL family <select> with 0 families rendered as a tiny EMPTY unlabeled
     box (owner screenshot). It must be disabled with an explanatory
     placeholder + title; Run stays guarded (never posts a blank family).
  2. cmdPool's prune toast read a top-level `res.pruned` that /api/pool never
     returns -> always "pruned 0". It must sum the real per-project
     notes_pruned/docs_pruned fields.

Requires the optional ``native`` extra (PySide6); skipped when absent.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip(
    "PySide6.QtWebEngineWidgets",
    reason="engine tests need QtWebEngine (pip install 'braincell-mcp[native]')",
)

_RUNNER = textwrap.dedent("""
    import json, sys
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWebEngineWidgets import QWebEngineView

    html_path = sys.argv[1]

    JS = '''
    (async function(){
      const out = {};
      if (typeof hideOverlay === 'function') hideOverlay();

      // ── 1. POOL empty state: 0 families (file:// harness has no API) ──
      families = [];
      openCommandsModal();
      let sel = document.getElementById('cmd-pool-fam');
      out.empty_state = {
        disabled: !!(sel && sel.disabled),
        option_text: sel && sel.options.length ? sel.options[0].textContent : '',
        title: sel ? (sel.title || '') : null,
        run_posts: null,
      };
      // Run with the empty select must NOT hit the network.
      let posted = [];
      const realPost = window.apiPost;
      window.apiPost = async (p, b) => { posted.push([p, b]); return {pooled: [], skipped: []}; };
      status.allow_writes = true;
      await cmdPool();
      out.empty_state.run_posts = posted.length;   // must be 0 (guard toast instead)
      closeModal();

      // ── 2. Prune toast honesty: canned response with real pruned counts ──
      families = [{name: 'famX', members: []}];
      openCommandsModal();
      sel = document.getElementById('cmd-pool-fam');
      out.with_families = {
        disabled: !!sel.disabled,
        options: [...sel.options].map(o => o.value),
      };
      window.apiPost = async (p, b) => {
        posted.push([p, b]);
        return {pooled: [
          {project_id: 'x', notes_copied: 2, notes_pruned: 2, docs_pruned: 1},
        ], skipped: []};
      };
      sel.value = 'famX';
      document.getElementById('cmd-pool-prune').checked = true;
      await cmdPool();          // prune -> inline confirm strip
      cmdConfirmGo();           // proceed
      await new Promise(r => setTimeout(r, 300));
      const toasts = [...document.querySelectorAll('.toast')].map(t => t.textContent);
      out.prune_toast = toasts.length ? toasts[toasts.length - 1] : '';
      const poolPosts = posted.filter(p => p[0] === '/api/pool');
      out.pool_body = poolPosts.length ? poolPosts[poolPosts.length - 1][1] : null;
      window.apiPost = realPost;
      return JSON.stringify(out);
    })()
    '''

    # runJavaScript cannot await a Promise -> stash the result in a global.
    KICK = "(function(){ %s.then(r => { window.__out = r; }); return 'started'; })()" % JS.strip()

    app = QApplication([])
    view = QWebEngineView()

    def poll(payload):
        if payload:
            print(payload)
            app.exit(0)
        else:
            QTimer.singleShot(300, lambda: view.page().runJavaScript("window.__out || null", 0, poll))

    def loaded(ok):
        view.page().runJavaScript(KICK, 0, lambda r: None)
        QTimer.singleShot(500, lambda: view.page().runJavaScript("window.__out || null", 0, poll))

    view.loadFinished.connect(lambda ok: QTimer.singleShot(1500, lambda: loaded(ok)))
    view.resize(1600, 900)
    view.load(QUrl.fromLocalFile(html_path))
    view.show()
    QTimer.singleShot(60000, lambda: app.exit(3))
    sys.exit(app.exec())
""")


@pytest.fixture(scope="module")
def modal_state(tmp_path_factory):
    import os
    from braincell.gui_template import INDEX_HTML

    tmp = tmp_path_factory.mktemp("cmdmodal")
    page = tmp / "index.html"
    page.write_text(INDEX_HTML, encoding="utf-8")
    runner = tmp / "runner.py"
    runner.write_text(_RUNNER, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(runner), str(page)],
        capture_output=True, text=True, timeout=180,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen",
             "QTWEBENGINE_CHROMIUM_FLAGS": "--no-sandbox"},
    )
    assert proc.returncode == 0, f"engine runner failed:\n{proc.stderr[-2000:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestPoolFamilySelect:
    def test_zero_families_renders_disabled_explained_select(self, modal_state):
        """No families => the select must be disabled with an explanatory
        placeholder + title, never a tiny empty unlabeled box."""
        st = modal_state["empty_state"]
        assert st["disabled"] is True, "empty family select must be disabled"
        assert "no families yet" in st["option_text"], (
            f"placeholder must explain the empty state, got {st['option_text']!r}"
        )
        assert "New family" in (st["title"] or ""), (
            "select title must point at the ＋ New family flow"
        )

    def test_run_with_empty_select_never_posts(self, modal_state):
        """The guard must keep Run from POSTing a blank family."""
        assert modal_state["empty_state"]["run_posts"] == 0

    def test_families_populate_and_enable_the_select(self, modal_state):
        st = modal_state["with_families"]
        assert st["disabled"] is False
        assert st["options"] == ["famX"]


class TestPoolPruneToast:
    def test_toast_reports_real_pruned_counts(self, modal_state):
        """/api/pool has no top-level `pruned`; the toast must sum the real
        per-project notes_pruned/docs_pruned (2+1=3 in the canned response) —
        pre-fix it always said 'pruned 0'."""
        assert "pruned 3" in modal_state["prune_toast"], (
            f"toast must report the real pruned total, got: "
            f"{modal_state['prune_toast']!r}"
        )

    def test_pool_body_shape(self, modal_state):
        body = modal_state["pool_body"]
        assert body == {"family": "famX", "all_projects": False, "prune": True}
