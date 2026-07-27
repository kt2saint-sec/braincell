# SPDX-License-Identifier: AGPL-3.0-or-later
"""Served-HTML regression tests for project-only Memory Map behavior.

These renderer-level assertions supplement native Qt acceptance.  They guard
the shipped SPA against reintroducing an ordinary cross-project memory view or
a global/all-project feed control.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _page(tmp_path: Path, *, allow_writes: bool = False) -> str:
    from braincell.gui import create_app

    app = create_app(db_path=tmp_path / "braincell.db", allow_writes=allow_writes)
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    return response.text


class TestConnectedProjectUi:
    def test_connected_project_chip_is_not_a_project_switcher(self, tmp_path):
        html = _page(tmp_path)
        marker = '<button class="active-chip" id="active-chip"'
        start = html.index(marker)
        button = html[start:html.index("</button>", start)]
        assert "Connected Project" in button
        assert "onclick" not in button

    def test_ordinary_scope_sends_no_project_selector(self, tmp_path):
        html = _page(tmp_path)
        scope = re.search(r"function scopeParams\(\)\{(.*?)\n\}", html, re.S)
        assert scope
        assert 'return "";' in scope.group(1)
        assert '"&projects="' not in scope.group(1)

    def test_catalog_entry_explains_its_memory_is_not_open(self, tmp_path):
        html = _page(tmp_path)
        assert "This catalog entry is not open for ordinary memory reads." in html
        assert "Use <b>Search Pool</b> or <b>Recall from Pool</b>" in html

    def test_sibling_inspector_disables_memory_writes(self, tmp_path):
        html = _page(tmp_path)
        body = re.search(r"function paintInspectorRo\(\)\{(.*?)\n\}", html, re.S)
        assert body
        assert "selected!==seedProjectId" in body.group(1)
        assert "b.disabled=ro" in body.group(1)
        assert "dr-sched-sel" in body.group(1)

    def test_maintenance_selector_is_connected_project_only(self, tmp_path):
        html = _page(tmp_path)
        body = re.search(r"function cmdProjOptions\(\)\{(.*?)\n\}", html, re.S)
        assert body
        assert "nodes.filter(n=>n.id===seedProjectId)" in body.group(1)


class TestPoolUi:
    def test_explicit_pool_controls_ship(self, tmp_path):
        html = _page(tmp_path)
        for label in ("Create Pool", "Add to Pool", "Decouple from Pool", "Search Pool", "Recall from Pool"):
            assert label in html
        assert "/api/pools/${kind}" in html

    def test_no_global_or_all_project_scope_control_ships(self, tmp_path):
        html = _page(tmp_path)
        assert 'id="scope-all"' not in html
        assert 'id="scope-family"' not in html
        assert "All projects ▾" not in html
        assert 'id="feed-scope-all"' not in html


class TestRendererSyntax:
    def test_embedded_javascript_parses_when_node_is_available(self, tmp_path):
        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not installed")
        html = _page(tmp_path)
        script = re.search(r"<script>(.*)</script>", html, re.S)
        assert script, "Memory Map page has no embedded script"
        source = tmp_path / "memory-map.js"
        source.write_text(script.group(1), encoding="utf-8")
        result = subprocess.run(
            [node, "--check", str(source)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
