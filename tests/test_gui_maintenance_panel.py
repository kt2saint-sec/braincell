# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""Real renderer coverage for the Connected-Project maintenance panel."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip(
    "PySide6.QtWebEngineWidgets",
    reason="maintenance-panel coverage needs the native Memory Map renderer",
    exc_type=ImportError,
)


def test_maintenance_panel_stays_connected_project_scoped(tmp_path):
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
          hideOverlay();
          status.allow_writes=true;
          seedProjectId='01CONNECTED'; activeProjectId='01CONNECTED';
          nodes=[
            {id:'01CONNECTED',name:'Connected',path:'/tmp/connected',shortUlid:'01CON…ED',docs:1,chunks:2,notes:3},
            {id:'01SIBLING',name:'Sibling',path:'/tmp/sibling',shortUlid:'01SIB…NG',docs:4,chunks:5,notes:6}
          ];
          const overview={
            connected_project_id:'01CONNECTED',
            preferences:{bypass_delete_confirmation:false},
            database_diagnostics:{freelist_bytes:128},
            storage_impact:{
              filesystem:{free_bytes:4096},
              local_snapshot:{estimated_retained_bytes:1024,fits_available_space:true},
              compaction:{conservative_temporary_bytes:2048,fits_available_space:true,estimated_reclaimable_bytes:128},
              memory_estimate_bytes:null,
              memory_notice:'RAM use cannot be reliably estimated from stored bytes.'
            },
            storage_budget:{
              warning_only:true,
              project_footprint:{files:2,bytes:3072},
              warnings:[{code:'free-space-threshold',message:'Local free disk space is below its review threshold.'}],
              notice:'Warnings ask for review only. Nothing was changed.'
            }
          };
          const fetched=[]; const put=[];
          const posted=[];
          window.apiFetch=async path=>{fetched.push(path);return path==='/api/maintenance/overview'?overview:null;};
          window.apiPut=async (path,body)=>{put.push([path,body]);return {bypass_delete_confirmation:true};};
          window.apiPost=async (path,body)=>{
            posted.push([path,body]);
            if(path==='/api/ops/hard-prune/plan') return {
              approval_digest:'digest',candidate_count:1,
              selection:{expired_tombstone_note_ids:[7],expired_operation_ids:[],unprotected_backup_paths:[]},
              preferences:{bypass_delete_confirmation:false},
              storage_impact:overview.storage_impact
            };
            if(path==='/api/ops/hard-prune/apply') return {started:true};
            return null;
          };

          openDock(nodes[0]);
          const card=document.getElementById('dr-maintenance-card');
          const out={card_visible:card.style.display!=="none"};
          await openMaintenanceReview();
          await new Promise(r=>setTimeout(r,30));
          out.panel_title=document.getElementById('mo-title').textContent;
          out.panel_text=document.getElementById('mo-body').textContent;
          document.getElementById('mt-bypass').checked=true;
          maintenanceBypassToggled();
          out.typed_gate=!!document.getElementById('mt-ack');
          document.getElementById('mt-ack').value='ENABLING THIS FEATURE MEANS I AGREE BRAINCELL IS NOT RESPONSIBLE SINCE I WAS ADVISED OF RISKS';
          syncMaintenanceAcknowledgement();
          out.enable_ready=!document.getElementById('mt-enable').disabled;
          await enableMaintenanceBypass();
          out.put=put;
          document.getElementById('hp-keep').value='0';
          await analyzeHardPrune();
          out.review_digest=document.getElementById('hp-review').textContent.includes('digest');
          document.getElementById('hp-typed').value='DELETE WITHOUT LOCAL RECOVERY SNAPSHOT';
          syncHardPruneApply();
          out.apply_ready=!document.getElementById('hp-apply').disabled;
          await startHardPrune();
          out.posted=posted;

          closeModal(); activeProjectId='01SIBLING'; openDock(nodes[1]);
          out.sibling_card_hidden=document.getElementById('dr-maintenance-card').style.display==='none';
          return JSON.stringify(out);
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
    assert result["card_visible"] is True
    assert result["panel_title"] == "Storage & lifecycle"
    assert "Review comes before any permanent cleanup" in result["panel_text"]
    assert "Connected Project local state" in result["panel_text"]
    assert "Storage review needed" in result["panel_text"]
    assert result["typed_gate"] is True
    assert result["enable_ready"] is True
    assert result["put"] == [[
        "/api/preferences/maintenance",
        {
            "bypass_delete_confirmation": True,
            "acknowledgement": (
                "ENABLING THIS FEATURE MEANS I AGREE BRAINCELL IS NOT RESPONSIBLE "
                "SINCE I WAS ADVISED OF RISKS"
            ),
        },
    ]]
    assert result["review_digest"] is True
    assert result["apply_ready"] is True
    assert result["posted"] == [
        ["/api/ops/hard-prune/plan", {
            "project_id": "01CONNECTED",
            "keep_backups": 0,
            "expire_operations_days": None,
            "expire_tombstones_days": None,
        }],
        ["/api/ops/hard-prune/apply", {
            "project_id": "01CONNECTED",
            "keep_backups": 0,
            "expire_operations_days": None,
            "expire_tombstones_days": None,
            "approval_digest": "digest",
            "confirmation_phrase": "DELETE WITHOUT LOCAL RECOVERY SNAPSHOT",
            "create_local_snapshot": False,
        }],
    ]
    assert result["sibling_card_hidden"] is True
