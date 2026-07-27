# SPDX-License-Identifier: AGPL-3.0-or-later
"""Native ★ Commands-modal coverage for explicit named-Pool operations."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip(
    "PySide6.QtWebEngineWidgets",
    reason="engine tests need QtWebEngine",
    exc_type=ImportError,
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

      nodes = [{id: '01PROJECTA', name: 'A', path: '/tmp/a'}];
      selected = '01PROJECTA';
      seedProjectId = '01PROJECTA';
      status.allow_writes = true;
      openCommandsModal();
      out.controls = {
        name: !!document.getElementById('cmd-pool-name'),
        query: !!document.getElementById('cmd-pool-query'),
        results: !!document.getElementById('cmd-pool-results'),
        retired_family: !!document.getElementById('cmd-pool-fam'),
        retired_prune: !!document.getElementById('cmd-pool-prune'),
      };

      const posted = [];
      const realPost = window.apiPost;
      window.apiPost = async (p, b) => {
        posted.push([p, b]);
        if (p.endsWith('/search')) return {hits: [{snippet: 'search hit'}], member_status: []};
        if (p.endsWith('/recall')) return {notes: [{content: 'recalled note'}], member_status: []};
        return {ok: true, pools: []};
      };

      await cmdPoolMembership('create');
      out.blank_posts = posted.length;
      document.getElementById('cmd-pool-name').value = 'Research';
      document.getElementById('cmd-pool-query').value = 'query';
      await cmdPoolMembership('create');
      await cmdPoolMembership('add');
      await cmdLivePool('search');
      await cmdLivePool('recall');
      await cmdPoolMembership('decouple');
      cmdConfirmGo();
      await new Promise(r => setTimeout(r, 100));
      out.posted = posted;
      out.results = document.getElementById('cmd-pool-results').textContent;
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
        check=False,
    )
    assert proc.returncode == 0, f"engine runner failed:\n{proc.stderr[-2000:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestNamedPoolCommands:
    def test_named_pool_controls_render_without_retired_materialization(self, modal_state):
        assert modal_state["controls"] == {
            "name": True,
            "query": True,
            "results": True,
            "retired_family": False,
            "retired_prune": False,
        }

    def test_blank_pool_name_never_posts(self, modal_state):
        assert modal_state["blank_posts"] == 0

    def test_membership_actions_use_stable_project_ulid(self, modal_state):
        posts = [entry for entry in modal_state["posted"] if entry[0] == "/api/pools"]
        assert posts == [
            ["/api/pools", {"action": "create", "name": "Research"}],
            [
                "/api/pools",
                {
                    "action": "add",
                    "name": "Research",
                    "project_ids": ["01PROJECTA"],
                },
            ],
            [
                "/api/pools",
                {
                    "action": "decouple",
                    "name": "Research",
                    "project_id": "01PROJECTA",
                },
            ],
        ]

    def test_live_queries_use_explicit_named_pool_routes(self, modal_state):
        posts = modal_state["posted"]
        assert [entry[0] for entry in posts] == [
            "/api/pools",
            "/api/pools",
            "/api/pools/search",
            "/api/pools/recall",
            "/api/pools",
        ]
        assert posts[2][1]["pool"] == "Research"
        assert posts[3][1]["pool"] == "Research"
        assert "recalled note" in modal_state["results"]
