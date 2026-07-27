# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
test_gui_tutorial.py — Served-HTML regression tests for the guided tour
(first-run onboarding) and the numbered happy-path toolbar.

Same offline TestClient idiom as test_gui.py: the assertions pin the SHIPPED
markup + embedded JS (the tour layer, its wiring, and the numbered button
copy). Runtime behavior (autostart truth table, step bounds, done-flag
semantics) is exercised separately via the node-stub route — extracting the
tour section verbatim from gui_template.INDEX_HTML and running it under node
with DOM/localStorage stubs — because the embedded JS has zero static
analysis otherwise.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def _app(tmp_path: Path, *, allow_writes: bool = False):
    """Create a GUI app over the store at tmp_path/braincell.db."""
    from braincell.gui import create_app
    return create_app(db_path=tmp_path / "braincell.db", allow_writes=allow_writes)


def _page(tmp_path: Path) -> str:
    with TestClient(_app(tmp_path)) as client:
        return client.get("/").text


# ── Numbered happy-path toolbar ───────────────────────────────────────────────

class TestNumberedToolbar:
    """The setup path is numbered (1 add → 2 grouping); build-only is
    deliberately un-numbered — the Add-project wizard already builds, so a
    '2 · Build memory' would teach a redundant rebuild."""

    def test_numbered_happy_path_labels(self, tmp_path):
        html = _page(tmp_path)
        assert "1 · ✚ Add project" in html, "Missing numbered '1 · ✚ Add project'"
        assert "2 · ＋ Pool" in html, "Missing numbered Pool action"
        assert "Family recall" not in html

    def test_add_project_still_first_and_primary(self, tmp_path):
        """Numbering must not displace the existing primary-button contract."""
        html = _page(tmp_path)
        toolbar = html.split('<div class="toolbar">', 1)[1]
        first_button = toolbar.split("</button>", 1)[0]
        assert 'id="add-repo-btn"' in first_button
        assert 'class="btn primary" id="add-repo-btn"' in html

    def test_build_only_unnumbered_in_secondary_group(self, tmp_path):
        """Build memory (no MCP) keeps its copy un-numbered, AFTER the
        numbered group's separator."""
        html = _page(tmp_path)
        assert "⬇ Build memory (no MCP)" in html
        assert "· ⬇ Build memory" not in html, "Build-only must NOT be numbered"
        toolbar = html.split('<div class="toolbar">', 1)[1].split("</div>", 1)[0]
        assert '<span class="tb-sep">' in toolbar, "Missing the group separator"
        assert toolbar.index("tb-sep") < toolbar.index('id="build-btn"'), (
            "build-btn must sit in the secondary group (after the separator)"
        )

    def test_help_button_replays_tour(self, tmp_path):
        html = _page(tmp_path)
        assert 'id="help-btn"' in html, "Missing the ? Help replay button"
        assert "? Help" in html
        assert 'onclick="tourStart()"' in html, "? Help must call tourStart()"


# ── Tour layer markup + wiring ────────────────────────────────────────────────

class TestTourMarkup:
    def test_tour_layer_present_with_ring_and_card(self, tmp_path):
        html = _page(tmp_path)
        for marker in ('id="tour"', 'id="tour-ring"', 'id="tour-card"',
                       'id="tour-dots"', 'id="tour-title"', 'id="tour-body"',
                       'id="tour-cta"', 'id="tour-back"', 'id="tour-next"'):
            assert marker in html, f"Missing tour markup: {marker}"

    def test_tour_layer_outside_stage(self, tmp_path):
        """The draw() loop rebuilds #stage's innerHTML every frame — the tour
        must live outside it. The served stage svg is empty, so any content
        inside it would be a regression on its own."""
        html = _page(tmp_path)
        assert '<svg class="stage" id="stage"></svg>' in html, (
            "#stage must ship empty — persistent UI lives outside it"
        )

    def test_tour_is_non_blocking_spotlight(self, tmp_path):
        """The layer ignores pointer events (only the card is interactive) and
        the ring's giant box-shadow provides the dim + cutout."""
        html = _page(tmp_path)
        assert "#tour{position:fixed;inset:0;z-index:28;pointer-events:none}" in html
        assert "0 0 0 9999px" in html, "Missing the spotlight cutout box-shadow"
        assert "pointer-events:all" in html.split("#tour-card", 1)[1][:400], (
            "The card must re-enable pointer events"
        )

    def test_tour_nav_handlers_wired(self, tmp_path):
        html = _page(tmp_path)
        assert 'onclick="tourNext()"' in html
        assert 'onclick="tourBack()"' in html
        assert 'onclick="tourEnd(false)"' in html, "Skip must end the tour"
        assert "Skip tour" in html

    def test_esc_order_modal_then_tour_then_dock(self, tmp_path):
        """Esc layering: an open modal closes first, then the tour, then the
        inspector dock."""
        html = _page(tmp_path)
        chunk = html.split('e.key!=="Escape"', 1)[1][:500]
        assert "tourActive()" in chunk, "Esc handler must consider the tour"
        assert chunk.index("closeModal") < chunk.index("tourActive"), (
            "modal must close before the tour"
        )
        assert chunk.index("tourActive") < chunk.index("closeDock"), (
            "the tour must close before the dock"
        )


# ── Tour steps: copy + teaching points ────────────────────────────────────────

class TestTourSteps:
    def _steps(self, tmp_path) -> str:
        html = _page(tmp_path)
        return html.split("const TOUR_STEPS", 1)[1].split("];", 1)[0]

    def test_seven_cards(self, tmp_path):
        steps = self._steps(tmp_path)
        assert steps.count("title:") == 7, "The tour ships exactly 7 cards"

    def test_anchors_are_stable_dom_only(self, tmp_path):
        """Anchors must be stable DOM (toolbar/header/containers) — never
        selectors into #stage, whose children are rebuilt every frame."""
        steps = self._steps(tmp_path)
        for sel in ('"#add-repo-btn"', '"#build-btn"', '"#active-chip"',
                    '"#new-family-btn"', '".stage-wrap"',
                    '"#feed-rail"'):
            assert sel in steps, f"Missing stable anchor {sel}"
        assert '"#stage"' not in steps, "Never anchor to #stage internals"
        assert ".cell-g" not in steps, "Never anchor to map cells"

    def test_core_teaching_points(self, tmp_path):
        steps = self._steps(tmp_path)
        # (a) connected project vs viewed directories
        assert "connected Project" in steps
        assert "never copy memory" in steps
        # (b) Add project vs Build memory (no MCP)
        assert "Connects BrainCell" in steps
        assert "wires nothing into an MCP client" in steps
        # Pool is membership-only and live
        assert "intentional live cross-project" in steps
        assert "curated notes accrue as you work" in steps

    def test_namings_canon_in_tour_copy(self, tmp_path):
        """Tour copy draws from the canonical terminology: 'project folder',
        Build, Register MCP, Family, Pool — never 'repo' or 'Ingest'."""
        steps = self._steps(tmp_path)
        assert "project folder" in steps
        assert " repo" not in steps, "'repo' is deprecated copy"
        assert "Ingest" not in steps, "'Ingest' is deprecated copy ('Build')"

    def test_cta_hands_off_to_wizard_and_is_write_gated(self, tmp_path):
        """Step-2's 'do it live' CTA ends the tour and opens the real wizard;
        the CTA render goes through the standard wdis() write gate."""
        html = _page(tmp_path)
        assert "tourEnd(true);openAddRepoModal()" in html
        cta_render = html.split('getElementById("tour-cta")', 1)[1][:300]
        assert "wdis()" in cta_render, "Tour CTAs must use the wdis() gate"


# ── First-run trigger + replay ────────────────────────────────────────────────

class TestTourTriggers:
    def test_autostart_consumes_config_and_flags(self, tmp_path):
        """Autostart honors ?tour=1 (force) / ?tour=0 (suppress),
        /api/config.suggest_tour, and the bcTourDone localStorage flag."""
        html = _page(tmp_path)
        assert "suggest_tour" in html, "SPA must consume /api/config.suggest_tour"
        assert "bcTourDone" in html, "Missing the done-flag"
        assert 'get("tour")' in html, "Missing the ?tour= URL override"
        assert "maybeAutoStartTour" in html
        assert "tourShouldAutoStart" in html

    def test_done_flag_set_by_tour_end(self, tmp_path):
        """Finish AND Skip both funnel through tourEnd, which sets the flag —
        a skipper is never re-ambushed."""
        html = _page(tmp_path)
        end_body = html.split("function tourEnd", 1)[1][:700]
        assert 'setItem("bcTourDone"' in end_body, (
            "tourEnd must set bcTourDone (covers Finish and Skip)"
        )
        assert 'delete("tour")' in end_body, (
            "tourEnd must strip ?tour= so a reload doesn't re-force"
        )

    def test_help_button_never_write_gated(self, tmp_path):
        """The tour is educational — replay stays enabled in read-only mode
        (only per-step write CTAs are gated), so help-btn must not be in the
        paintWriteButtons list."""
        html = _page(tmp_path)
        pwb = html.split("function paintWriteButtons", 1)[1][:600]
        assert "help-btn" not in pwb

    def test_overlay_state_extracted_and_tour_aware(self, tmp_path):
        """The empty-state overlay logic lives in paintOverlayState (shared by
        loadAll and tourEnd) and yields to an active tour."""
        html = _page(tmp_path)
        assert "function paintOverlayState()" in html
        pos = html.split("function paintOverlayState()", 1)[1][:300]
        assert "tourActive()" in pos, (
            "The overlay must yield while the tour's welcome card is up"
        )
