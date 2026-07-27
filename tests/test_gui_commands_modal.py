# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_gui_commands_modal.py — ★ Commands modal behavior in the real engine.

The modal is client-rendered, so these tests drive the real INDEX_HTML in
offscreen QtWebEngine and assert that explicit Pool membership and live-query
controls render without a materialized/global action.

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

      nodes = [{id: 'project-a', name: 'Project A'}];
      selected = 'project-a';
      openCommandsModal();
      out.controls = {
        pool_name: !!document.getElementById('cmd-pool-name'),
        pool_query: !!document.getElementById('cmd-pool-query'),
        create: document.body.innerText.includes('Create Pool'),
        add: document.body.innerText.includes('Add to Pool'),
        decouple: document.body.innerText.includes('Decouple from Pool'),
        search: document.body.innerText.includes('Search Pool'),
        recall: document.body.innerText.includes('Recall from Pool'),
      };
      let posted = [];
      const realPost = window.apiPost;
      window.apiPost = async (p, b) => { posted.push([p, b]); return {pooled: [], skipped: []}; };
      status.allow_writes = true;
      document.getElementById('cmd-pool-name').value = 'team';
      await cmdPoolMembership('add');
      out.add_body = posted.length ? posted[0] : null;
      closeModal();
      openCommandsModal();
      document.getElementById('cmd-pool-name').value = 'team';
      document.getElementById('cmd-pool-query').value = 'needle';
      window.apiPost = async (p, b) => { posted.push([p, b]); return {hits: [], notes: []}; };
      await cmdLivePool('search');
      out.search_body = posted.length ? posted[posted.length - 1] : null;
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


class TestLivePoolModal:
    def test_controls_render(self, modal_state):
        assert modal_state["controls"] == {
            "pool_name": True, "pool_query": True, "create": True,
            "add": True, "decouple": True, "search": True, "recall": True,
        }

    def test_add_to_pool_posts_project_id(self, modal_state):
        path, body = modal_state["add_body"]
        assert path == "/api/pools"
        assert body == {"action": "add", "name": "team", "project_ids": ["project-a"]}

    def test_search_pool_posts_explicit_name_and_query(self, modal_state):
        path, body = modal_state["search_body"]
        assert path == "/api/pools/search"
        assert body["pool"] == "team"
        assert body["query"] == "needle"
