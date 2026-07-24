# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
test_gui_mcp_controls_ui.py — Served-HTML regression tests for the Phase-2 GUI
surfacing: the inspector-dock MCP status & controls block, the Commands-modal
"Deregister MCP" regroup, the header embedder status chip, and the Build gate
(refuse-with-fix when the embedder is down). All assertions run against the
page served at GET / by create_app — no browser, no live server.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def _page(tmp_path: Path, *, allow_writes: bool = False) -> str:
    """Serve GET / from a fresh app and return the HTML text."""
    from braincell.gui import create_app
    app = create_app(db_path=tmp_path / "braincell.db", allow_writes=allow_writes)
    with TestClient(app) as client:
        r = client.get("/")
    assert r.status_code == 200
    return r.text


# ── Inspector-dock MCP status & controls block ────────────────────────────────

class TestDockMcpBlock:
    def test_mcp_block_markup_present(self, tmp_path):
        """The c-stats column carries the MCP status line, both buttons, and
        the restart note."""
        html = _page(tmp_path)
        for needle in ('id="dr-mcp-status"', 'id="dr-mcp-actions"',
                       'id="dr-mcp-register-btn"', 'id="dr-mcp-deregister-btn"',
                       'id="dr-mcp-note"'):
            assert needle in html, f"Missing MCP block markup: {needle}"

    def test_mcp_buttons_wired_to_handlers(self, tmp_path):
        html = _page(tmp_path)
        assert 'onclick="mcpRegisterSelected()"' in html, "Register MCP button unwired"
        assert 'onclick="mcpDeregisterSelected()"' in html, "Deregister MCP button unwired"
        for fn in ("function mcpStatusText", "function paintMcpBlock",
                   "function mcpRegisterSelected", "function mcpDeregisterSelected",
                   "function doDeregisterSelected", "async function mcpDeregister"):
            assert fn in html, f"Missing {fn}"

    def test_register_reuses_add_project_wizard_register_step(self, tmp_path):
        """Register MCP prefills the path + jumps to the existing wizard step
        — no new install flow."""
        html = _page(tmp_path)
        assert "arPath=nd.path;arProjectId=nd.id" in html, (
            "Register MCP must prefill the wizard with the cell's path"
        )
        assert "arStepInstall();" in html

    def test_deregister_reuses_api_uninstall(self, tmp_path):
        """One shared POST — the dock modal and the Commands row both land on
        /api/uninstall via mcpDeregister."""
        html = _page(tmp_path)
        assert 'apiPost("/api/uninstall",{path,client,scope,disarm})' in html
        # dock modal controls
        for needle in ('id="dm-client"', 'id="dm-scope"', 'id="dm-disarm"'):
            assert needle in html, f"Missing deregister modal control {needle}"

    def test_restart_instruction_sits_in_the_dock(self, tmp_path):
        """The honest answer where a user looks for a restart button: reconnect
        in the client (/mcp) — the GUI cannot restart the MCP server."""
        html = _page(tmp_path)
        assert "To restart the MCP server, reconnect in your client" in html
        assert "run <b>/mcp</b> in Claude Code" in html
        assert "The GUI cannot restart it; it runs inside your MCP client." in html

    def test_status_line_sources_and_paint(self, tmp_path):
        """/api/status.mcp answers for the launch project (path match); other
        cells use /api/projects[].mcp_registered; unknown stays honest."""
        html = _page(tmp_path)
        assert "status.mcp.path&&nd.path===status.mcp.path" in html, (
            "Launch-project detail must key on the status.mcp path"
        )
        assert "typeof p.mcp_registered" in html, (
            "mcp_registered must be carried from /api/projects into the node model"
        )
        assert "Registration unknown" in html, (
            "A missing mcp_registered must render as unknown, never as a guess"
        )
        assert "st.textContent=mcpStatusText(nd)" in html, (
            "Status line must paint via textContent (inert sink)"
        )

    def test_buttons_disabled_not_hidden_read_only(self, tmp_path):
        """Read-only launches disable the two buttons with the explanatory
        title (wdis convention); the status line still renders."""
        html = _page(tmp_path)
        block = html[html.index("function paintMcpBlock"):
                     html.index("function mcpRegisterSelected")]
        assert "b.disabled=true" in block, "Buttons must be disabled, not hidden"
        assert "read-only: launch with --allow-writes" in block


# ── Commands modal regroup ────────────────────────────────────────────────────

class TestCommandsModalRegroup:
    def test_uninstall_row_renamed_deregister_mcp(self, tmp_path):
        html = _page(tmp_path)
        assert '<div class="k">Deregister MCP</div>' in html, (
            "The uninstall row must be titled Deregister MCP (NAMINGS)"
        )
        assert '<div class="k">uninstall</div>' not in html, (
            "'uninstall' must not survive as GUI copy"
        )
        assert 'onclick="cmdUninstall()">Deregister MCP<' in html, (
            "The action button must read Deregister MCP"
        )

    def test_deregister_grouped_with_restart_gui_under_mcp_label(self, tmp_path):
        """Both rows sit under one 'MCP status & controls' mo-label, mirroring
        the dock's mental model."""
        html = _page(tmp_path)
        label = html.index("MCP status &amp; controls")
        dereg = html.index('<div class="k">Deregister MCP</div>')
        restart = html.index('<div class="k">restart GUI</div>')
        maint = html.index("Maintenance tools")
        assert maint < label < dereg < restart, (
            "Order must be: Maintenance tools … [MCP label] Deregister MCP, restart GUI"
        )

    def test_deregister_row_still_write_gated(self, tmp_path):
        html = _page(tmp_path)
        assert '${wdis()} onclick="cmdUninstall()"' in html, (
            "Deregister MCP must be disabled (never hidden) in read-only mode"
        )

    def test_hook_toggle_stays_separate_from_mcp_group(self, tmp_path):
        """Family-recall hook is deliberately NOT MCP state — the toolbar
        toggle stays where it is."""
        html = _page(tmp_path)
        assert 'id="hook-btn"' in html
        assert 'onclick="toggleHook()"' in html


# ── Embedder status chip ──────────────────────────────────────────────────────

class TestEmbedderChip:
    def test_chip_markup_and_painter(self, tmp_path):
        html = _page(tmp_path)
        assert 'id="chip-embedder"' in html, "Missing the embedder header chip"
        assert 'id="chip-embedder-txt"' in html
        assert "function paintEmbedderChip" in html
        assert 'onclick="embedderChipClick()"' in html, (
            "Chip must be clickable — the fix affordance when the embedder is down"
        )
        assert "#chip-embedder.bad" in html, "Missing the embedder-down chip styling"

    def test_fix_modal_carries_the_remediation(self, tmp_path):
        html = _page(tmp_path)
        assert "function openEmbedderFixModal" in html
        assert "ollama pull ${esc(e.model||" in html, (
            "Fix modal must name the exact pull command, model escaped"
        )
        assert "Install Ollama" in html
        assert "esc(e.detail||" in html, (
            "Server-controlled detail must go through esc() in the modal"
        )

    def test_chip_text_paints_via_textcontent(self, tmp_path):
        """Chip text/title are inert sinks — no innerHTML with raw model/detail."""
        html = _page(tmp_path)
        painter = html[html.index("function paintEmbedderChip"):
                       html.index("function embedderChipClick")]
        assert "txt.textContent=" in painter
        assert "innerHTML" not in painter


# ── Build gate (refuse-with-fix when the embedder is down) ────────────────────

class TestBuildGate:
    def test_require_embedder_defined_and_explicit_false_only(self, tmp_path):
        """Gate fires only on embedder.ok===false — an absent field (older
        server, test app) must never block a build."""
        html = _page(tmp_path)
        assert "function requireEmbedder()" in html
        gate = html[html.index("function requireEmbedder()"):]
        gate = gate[:gate.index("}\n")]
        assert "e&&!e.ok" in gate, "Gate must require an explicit not-ok answer"

    def test_all_build_entrypoints_gated(self, tmp_path):
        """Toolbar Build modal, the ingest funnel (covers per-cell Rebuild and
        family-build), and both wizard build entries refuse when down."""
        html = _page(tmp_path)
        assert html.count("if(!requireEmbedder())return;") >= 4, (
            "Expected gates in openIngestModal, startIngest, arGoBuild, arStepBuild"
        )
        for fn in ("function openIngestModal", "async function startIngest",
                   "function arGoBuild", "async function arStepBuild"):
            body = html[html.index(fn):]
            body = body[:body.index("\n}")]
            assert "requireEmbedder()" in body, f"{fn} is not embedder-gated"

    def test_refusal_names_the_fix(self, tmp_path):
        html = _page(tmp_path)
        assert "Embedder not ready — Build refused. Install Ollama, then run: ollama pull" in html, (
            "The refusal toast must carry the exact remediation"
        )
