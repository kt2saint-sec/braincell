# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pool controls exercised in the shipped PySide6/QtWebEngine renderer."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip(
    "PySide6.QtWebEngineWidgets",
    reason="requires the native Memory Map renderer",
    exc_type=ImportError,
)


def test_native_memory_map_exercises_all_pool_actions(tmp_path):
    from braincell.gui_template import INDEX_HTML

    page = tmp_path / "index.html"
    page.write_text(INDEX_HTML, encoding="utf-8")
    runner = tmp_path / "runner.py"
    runner.write_text(textwrap.dedent("""
        import sys
        from PySide6.QtCore import QTimer, QUrl
        from PySide6.QtWidgets import QApplication
        from PySide6.QtWebEngineWidgets import QWebEngineView

        app = QApplication([])
        view = QWebEngineView()
        script = '''
        (async function(){
          hideOverlay(); status.allow_writes=true; seedProjectId='01A'; selected='01A';
          nodes=[{id:'01A',name:'A',path:'/tmp/a'}];
          const posted=[];
          window.apiPost=async (path,body)=>{
            posted.push([path,body]);
            if(path.includes('/search')) return {hits:[{title:'hit'}],member_status:[{project_id:'01BAD',status:'corrupt',detail:'bad database'}]};
            if(path.includes('/recall')) return {notes:[{content:'remembered'}],member_status:[{project_id:'01MISS',status:'missing',detail:'not built'}]};
            return {ok:true,pools:[]};
          };
          openCommandsModal();
          document.getElementById('cmd-pool-name').value='Research';
          document.getElementById('cmd-pool-query').value='query';
          await cmdPoolMembership('create'); await cmdPoolMembership('add');
          await cmdLivePool('search'); const searchResults=document.getElementById('cmd-pool-results').textContent;
          await cmdLivePool('recall'); const recallResults=document.getElementById('cmd-pool-results').textContent;
          await cmdPoolMembership('decouple'); cmdConfirmGo();
          await new Promise(r=>setTimeout(r,50));
          return JSON.stringify({posted,searchResults,recallResults});
        })().then(v=>window.__out=v);
        ''';
        def poll(value):
          if value: print(value); app.exit(0)
          else: QTimer.singleShot(100, lambda:view.page().runJavaScript('window.__out||null', 0, poll))
        view.loadFinished.connect(lambda _: QTimer.singleShot(900, lambda:view.page().runJavaScript(script, 0, lambda _:None)))
        QTimer.singleShot(1100, lambda:view.page().runJavaScript('window.__out||null', 0, poll))
        QTimer.singleShot(30000, lambda:app.exit(3))
        view.load(QUrl.fromLocalFile(sys.argv[1])); view.show(); sys.exit(app.exec())
    """), encoding="utf-8")
    process = subprocess.run(
        [sys.executable, str(runner), str(page)], capture_output=True, text=True,
        timeout=60, check=False,
        env={
            **os.environ,
            "QT_QPA_PLATFORM": "offscreen",
            "QTWEBENGINE_CHROMIUM_FLAGS": "--no-sandbox --disable-gpu --disable-gpu-compositing",
            "LIBGL_ALWAYS_SOFTWARE": "1",
        },
    )
    assert process.returncode == 0, process.stderr[-2000:]
    result = json.loads(process.stdout.strip().splitlines()[-1])
    paths = [path for path, _body in result["posted"]]
    assert paths == ["/api/pools", "/api/pools", "/api/pools/search", "/api/pools/recall", "/api/pools"]
    assert "Skipped 01BAD — corrupt" in result["searchResults"]
    assert "Skipped 01MISS — missing" in result["recallResults"]
