# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
test_gui_layout.py — Served-HTML regression tests for the locked GUI layout
(bottom inspector dock + full-height live-feed rail) and the Pass-2 command
surface. All assertions run against the page served at GET / by create_app —
no browser, no live server.
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


# ── A/B: the locked layout ────────────────────────────────────────────────────

class TestLockedLayout:
    def test_top_level_row_main_column_plus_feed_rail(self, tmp_path):
        """The app splits into a left .main column and a right feed rail."""
        html = _page(tmp_path)
        assert '<div class="main">' in html, "Missing the left .main column"
        assert 'id="feed-rail"' in html, 'Missing id="feed-rail"'
        assert '<aside class="rail"' in html, "Feed rail must be an aside.rail"

    def test_feed_rail_wired_to_api_feed(self, tmp_path):
        """The rail polls GET /api/feed via feedPoll on an interval."""
        html = _page(tmp_path)
        assert "/api/feed" in html, "Page JS must reference /api/feed"
        assert "feedPoll" in html, "Missing the feedPoll poller"
        assert "startFeedPoll" in html, "Missing the feed poll starter"
        assert 'id="feed-list"' in html, "Missing the feed list container"
        assert 'id="feed-building"' in html, "Missing the building banner"
        assert 'id="feed-newpill"' in html, "Missing the '▲ N new' pill"
        assert "feedFlushNew" in html, "New pill must flush the buffered rows"

    def test_feed_rail_collapse_toggle_persisted(self, tmp_path):
        """Rail collapse toggles via toggleRail and persists in sessionStorage."""
        html = _page(tmp_path)
        assert "toggleRail" in html, "Missing the rail collapse toggle"
        assert "bcRailCollapsed" in html, "Rail state must persist in sessionStorage"
        assert ".rail.collapsed" in html, "Missing the collapsed-rail styling"
        assert 'id="rail-reopen"' in html, "Collapsed rail needs a reopen control"

    def test_collapsed_rail_has_edge_expand_tab(self, tmp_path):
        """A vertical expand tab pins to the right edge when the rail is
        collapsed — the primary, obvious way to reopen the feed."""
        html = _page(tmp_path)
        assert 'id="rail-tab"' in html, "Missing the right-edge expand tab"
        assert '"rail-tab" style="display:none" onclick="toggleRail()"' in html, (
            "Expand tab must reopen via the same toggleRail path"
        )
        assert "▸ Live feed" in html, "Expand tab must be labeled '▸ Live feed'"
        assert ".rail-tab{position:fixed" in html, (
            "Expand tab must be fixed to the viewport edge, outside #stage"
        )
        assert "writing-mode:vertical-rl" in html, "Expand tab must be vertical"

    def test_doc_rows_lead_with_preview_text(self, tmp_path):
        """Feed DOC rows show the memory TEXT (preview) as the body; the .jsonl
        doc key (title) demotes to the meta line beside '+N chunks'."""
        html = _page(tmp_path)
        assert "r.preview" in html, "DOC row render must use the preview field"
        assert "pv||r.title" in html, (
            "Empty preview must fall back to the title as body"
        )

    def test_inspector_is_a_bottom_dock(self, tmp_path):
        """The inspector is a bottom dock in the left column, id=drawer kept."""
        html = _page(tmp_path)
        assert '<div class="dock" id="drawer">' in html, (
            "Inspector must be the bottom dock (class=dock) keeping id=drawer"
        )
        assert "openDock" in html, "Missing the openDock cell-click handler"
        assert "closeDock" in html, "Missing the closeDock collapse handler"
        # Esc-to-close ships
        assert "Escape" in html, "Missing the Esc-to-close key handling"
        # Horizontal columns
        for col in ("c-head", "c-stats", "c-search", "c-notes"):
            assert col in html, f"Missing dock column {col}"

    def test_dock_preserves_drawer_functionality(self, tmp_path):
        """All pre-dock inspector controls survive: rebuild/clear/auto-build/
        search/recent-notes and their handlers."""
        html = _page(tmp_path)
        for marker in ("reingestSelected", "confirmClearSelected", "scheduleSelected",
                       "drawerSearch", "loadDrawerNotes",
                       'id="dr-q"', 'id="dr-hits-list"', 'id="dr-notes-list"',
                       'id="dr-docs"', 'id="dr-chunks"', 'id="dr-notes"'):
            assert marker in html, f"Dock lost inspector functionality: {marker}"

    def test_hover_signal_neon_gradient_and_tracked_id(self, tmp_path):
        """Hover is JS-tracked (hoveredId) and renders the #nucHover gradient."""
        html = _page(tmp_path)
        assert 'id="nucHover"' in html, "Missing the #nucHover radial gradient"
        assert "hoveredId" in html, "Hover must be JS-tracked via hoveredId"
        assert "nucHover" in html

    def test_family_ring_helper_and_cell_cursor(self, tmp_path):
        """Cells wear their family's hue via famRing; cursor is pointer."""
        html = _page(tmp_path)
        assert "famRing" in html, "Missing the family-ring color helper"
        assert ".cell-g{cursor:pointer}" in html, (
            "Cells must show cursor:pointer as the click affordance"
        )

    def test_drag_click_threshold_raised(self, tmp_path):
        """The click-vs-drag threshold is 8px so clicks stop misfiring as drags."""
        html = _page(tmp_path)
        assert ">8)dragMoved=true" in html, "dragMoved threshold must be 8px"

    def test_no_global_memory_map_target(self, tmp_path):
        """The native map does not present a shared/global memory target."""
        html = _page(tmp_path)
        assert "GLOBAL BRAIN" not in html

    def test_legend_leads_with_click_and_explicit_pool(self, tmp_path):
        html = _page(tmp_path)
        assert "Click a cell" in html, "Legend must lead with the click affordance"
        assert "Commands → Pool" in html


# ── C: Pass-2 command surface ─────────────────────────────────────────────────

class TestPass2Commands:
    def test_pool_card_wired(self, tmp_path):
        html = _page(tmp_path)
        assert "cmdPoolMembership" in html
        assert "cmdLivePool" in html
        assert "/api/pools" in html
        assert 'id="cmd-pool-name"' in html
        assert 'id="cmd-pool-query"' in html
        assert "Create Pool" in html
        assert "Add to Pool" in html
        assert "Decouple from Pool" in html
        assert "Search Pool" in html
        assert "Recall from Pool" in html

    def test_skills_card_wired(self, tmp_path):
        html = _page(tmp_path)
        assert "cmdSkills" in html, "Missing the cmdSkills handler"
        assert "/api/skills" in html, "cmdSkills must post to /api/skills"
        assert "your copy left untouched" in html, (
            "Conflict rows must explain the never-clobber outcome"
        )

    def test_automatic_pool_recall_card_is_project_explicit(self, tmp_path):
        html = _page(tmp_path)
        assert "/api/automatic-pool-recall" in html
        assert "Automatic Pool recall" in html
        assert "Disabled by default" in html
        assert 'id="cmd-auto-pool"' in html
        assert 'id="cmd-auto-scope"' in html

    def test_restart_card_wired_and_scoped_to_gui(self, tmp_path):
        html = _page(tmp_path)
        assert "cmdRestart" in html, "Missing the cmdRestart handler"
        assert "/api/restart" in html, "cmdRestart must post to /api/restart"
        assert "/mcp" in html, (
            "Restart copy must say the MCP server restarts in the client (/mcp)"
        )
        assert "GUI only" in html or "GUI server process only" in html or "THIS GUI" in html

    def test_build_modal_has_reembed_without_global_mode(self, tmp_path):
        html = _page(tmp_path)
        assert 'id="ing-reembed"' in html, "Missing the --reembed checkbox"
        assert 'id="ing-global"' not in html, "Global build must not be a GUI option"
        assert "startIngestFromModal" in html, (
            "Build button must route through startIngestFromModal"
        )

    def test_skills_are_a_separate_project_action(self, tmp_path):
        html = _page(tmp_path)
        assert 'id="ar-global"' not in html, "Project MCP connection must not target global memory"
        assert 'id="ar-skills"' not in html
        assert "Add skills" in html
        assert "Remove skills" in html

    def test_reflect_model_and_contradictions_threshold_inputs(self, tmp_path):
        html = _page(tmp_path)
        assert 'id="cmd-refl-model"' in html, "Missing the reflect model input"
        assert 'id="cmd-ctr-th"' in html, "Missing the contradictions threshold input"

    def test_load_log_button_write_gated(self, tmp_path):
        """The load-log button carries ${wdis()} (its GET /api/memory endpoint
        mounts only under --allow-writes)."""
        html = _page(tmp_path)
        assert '${wdis()} onclick="cmdMemLog()"' in html, (
            "Load log must be disabled (never hidden) in read-only mode"
        )


class TestToolbarTooltips:
    def test_retidy_button_has_tooltip(self, tmp_path):
        """Every toolbar action explains itself on hover — Re-tidy was the one
        button shipping without a title (owner-reported, 2026-07-25)."""
        html = _page(tmp_path)
        assert 'onclick="relax()" title="' in html, (
            "Re-tidy must carry a title= tooltip like its toolbar neighbors"
        )
