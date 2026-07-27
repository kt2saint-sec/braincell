# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
test_gui_active_memory_ui.py — Served-HTML regression tests for the SPA side of
active-project memory (plan Phase C):

- C1: activeProjectId state (?active= → seed → null), isLaunch(), scopeParams()
  following the active project and re-seeding family federation.
- C2: header active-project chip + dropdown (⌂ launch marker, RO sibling tag,
  global-mode "All projects").
- C3: map ACTIVE treatment (persistent emerald ring + ACTIVE label; click =
  activate + inspect).
- C4: read-only sibling inspector (disable-not-hide, explanatory titles) and
  the "Not built yet — Build memory" empty state.
- C5: global-mode feed filter (Active · All) with cursor reset on change.
- §6.3: the "siblings read 0" counts banner is deleted (honest counts, B2).

All assertions run against the page served at GET / by create_app — no browser.
The embedded-JS syntax gate extracts the shipped <script> verbatim and runs
`node --check` on it (skipped when node is absent).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _page(tmp_path: Path, *, allow_writes: bool = False) -> str:
    """Serve GET / from a fresh app and return the HTML text."""
    from braincell.gui import create_app
    app = create_app(db_path=tmp_path / "braincell.db", allow_writes=allow_writes)
    with TestClient(app) as client:
        r = client.get("/")
    assert r.status_code == 200
    return r.text


# ── C1: state + scope params ─────────────────────────────────────────────────

class TestActiveState:
    def test_active_state_symbols_ship(self, tmp_path):
        html = _page(tmp_path)
        assert "activeProjectId" in html, "Missing the activeProjectId SPA state"
        assert "function isLaunch()" in html, "Missing the isLaunch() helper"

    def test_active_init_chain_url_param_then_seed(self, tmp_path):
        """Init: ?active= URL param → launch seed → null, exactly once."""
        html = _page(tmp_path)
        assert 'get("active")' in html, "Missing the ?active= URL param read"
        assert "_urlActive||(cfg&&cfg.launch_project_id)||seedProjectId||null" in html, (
            "Active init must be ?active= → launch_project_id → seed → null"
        )
        assert "_activeInit" in html, (
            "Active init must run once — later loadAll() calls must not clobber a switch"
        )

    def test_scope_params_follow_the_connected_project(self, tmp_path):
        """Ordinary Memory Map scope stays pinned to the connected project."""
        html = _page(tmp_path)
        assert '"&projects="+encodeURIComponent(seedProjectId||"")' in html

    def test_scope_params_has_no_implicit_pool_branch(self, tmp_path):
        """Pool reads are explicit Commands actions, not an implicit scope."""
        html = _page(tmp_path)
        assert '"&seed="+encodeURIComponent(activeProjectId)' not in html

    def test_url_rides_active_param_via_replace_state(self, tmp_path):
        """setActiveProject updates ?active= via history.replaceState (shareable tab)."""
        html = _page(tmp_path)
        assert "history.replaceState" in html, "Missing the history.replaceState URL update"
        assert 'u.searchParams.set("active",activeProjectId)' in html
        assert 'u.searchParams.delete("active")' in html, (
            "Clearing the active project must drop the ?active= param"
        )


# ── C2: header chip + dropdown ───────────────────────────────────────────────

class TestActiveChip:
    def test_chip_markup_between_search_and_scope_toggle(self, tmp_path):
        html = _page(tmp_path)
        assert 'id="active-chip"' in html, 'Missing id="active-chip"'
        assert 'id="active-dd"' in html, "Missing the dropdown container"
        assert 'id="active-wrap"' in html, "Missing the chip wrapper"
        # placement: after the searchbar, before the scope toggle
        chip_pos = html.index('id="active-chip"')
        assert html.index('id="global-q"') < chip_pos < html.index('id="scope-seg"'), (
            "Chip must sit between the searchbar and the scope toggle"
        )

    def test_chip_handlers_ship(self, tmp_path):
        html = _page(tmp_path)
        for fn in ("renderActiveChip", "openActiveDropdown", "setActiveProject"):
            assert fn in html, f"Missing {fn}"

    def test_launch_marker_and_ro_tag(self, tmp_path):
        """⌂ renders only when active == launch; RO tags a sibling view."""
        html = _page(tmp_path)
        assert 'class="ac-home"' in html, "Missing the ⌂ launch-project marker"
        assert 'class="ac-ro"' in html, "Missing the RO sibling tag"
        assert ">RO</span>" in html
        assert "activeProjectId===seedProjectId" in html, (
            "⌂ must key on active == launch seed"
        )

    def test_global_mode_reads_all_projects(self, tmp_path):
        """With no active project the chip reads 'All projects ▾' (fixes G5)."""
        html = _page(tmp_path)
        assert "All projects ▾" in html
        assert "setActiveProject(null)" in html, (
            "Global-mode dropdown must offer the unfiltered All-projects entry"
        )

    def test_dropdown_escapes_names_and_paths(self, tmp_path):
        """Every server-controlled string in the dropdown goes through esc()."""
        html = _page(tmp_path)
        assert "${esc(n.name)}" in html
        assert "${esc(n.path)}" in html
        assert "esc(n.id)" in html, "Project ULIDs in onclick must be escaped"

    def test_switch_reruns_the_open_drawer(self, tmp_path):
        """setActiveProject repaints the chip and re-runs the open drawer view."""
        html = _page(tmp_path)
        m = re.search(r"function setActiveProject\(pid\)\{(.*?)\n\}", html, re.S)
        assert m, "setActiveProject not found"
        body = m.group(1)
        assert "renderActiveChip()" in body
        assert "loadDrawerNotes()" in body
        assert "drawerSearch()" in body
        assert "draw()" in body, "setActiveProject must repaint the map"


# ── C3: map ACTIVE treatment ─────────────────────────────────────────────────

class TestMapActiveTreatment:
    def test_active_ring_and_label_in_draw(self, tmp_path):
        html = _page(tmp_path)
        assert "cell-active-label" in html, "Missing the ACTIVE label class"
        assert ">ACTIVE</text>" in html, "Missing the ACTIVE label text"
        assert "act=activeProjectId===nd.id" in html, (
            "draw() must compute the active cell"
        )
        # persistent emerald ring — distinct stroke from hover's neon rgba(201,255,233,…)
        assert 'stroke="rgba(24,201,138,.85)"' in html, (
            "Missing the persistent emerald active ring"
        )

    def test_click_activates_before_inspecting(self, tmp_path):
        """Clicking a cell = setActiveProject + openDock (one gesture, one concept)."""
        html = _page(tmp_path)
        assert "setActiveProject(nd.id);openDock(nd);" in html, (
            "Cell click must activate before opening the inspector"
        )


# ── C4: read-only sibling inspector + not-built empty state ──────────────────

class TestInspectorReadOnly:
    def test_action_buttons_have_stable_ids(self, tmp_path):
        html = _page(tmp_path)
        assert 'id="dr-rebuild-btn"' in html, "Rebuild button needs a stable id"
        assert 'id="dr-clear-btn"' in html, "Clear button needs a stable id"

    def test_ro_view_disables_with_explanatory_title(self, tmp_path):
        """Sibling views disable (never hide) Rebuild/Clear/Auto-build + forget."""
        html = _page(tmp_path)
        assert "paintInspectorRo" in html, "Missing the RO repaint helper"
        assert (
            "read-only view — launch braincell gui on this folder to manage it"
            in html
        ), "RO controls must explain WHY they are unavailable"
        m = re.search(r"function paintInspectorRo\(\)\{(.*?)\n\}", html, re.S)
        assert m, "paintInspectorRo not found"
        body = m.group(1)
        assert "isLaunch()" in body, "RO state must key on isLaunch()"
        assert "dr-sched-sel" in body, "Auto-build select must be disabled too"
        assert "b.disabled=ro" in body, "Buttons must be disabled, not hidden"

    def test_per_note_forget_disabled_on_sibling_view(self, tmp_path):
        """The per-note ✕ renders disabled (cursor:not-allowed + title) off-launch."""
        html = _page(tmp_path)
        assert "launchView" in html
        assert "cursor:not-allowed" in html, (
            "Sibling-view forget must render disabled, not vanish"
        )

    def test_not_built_empty_state_wired(self, tmp_path):
        """A sibling 404 ('not built') maps to the honest Build-memory empty state."""
        html = _page(tmp_path)
        assert "notBuiltHtml" in html, "Missing the not-built empty-state helper"
        assert "Not built yet — <b>Build memory</b> to absorb this folder." in html
        assert "apiFetchView" in html, (
            "Drawer fetches must surface the 404 not-built detail"
        )
        assert "if(r.status===404)return {notBuilt:true};" in html


# ── C5: global-mode feed filter ──────────────────────────────────────────────

class TestFeedFilter:
    def test_filter_markup_ships(self, tmp_path):
        html = _page(tmp_path)
        assert 'id="feed-scope"' in html, "Missing the feed filter container"
        assert 'id="feed-scope-active"' in html
        assert 'id="feed-scope-all"' in html
        assert 'id="feed-scope-name"' in html, (
            "Global-mode rail header must carry the active project's name"
        )

    def test_default_is_all_and_global_mode_only(self, tmp_path):
        html = _page(tmp_path)
        assert 'let feedScope="all"' in html, "Filter must default to All"
        m = re.search(r"function feedFilterParams\(\)\{(.*?)\n\}", html, re.S)
        assert m, "feedFilterParams not found"
        assert 'status.mode==="global"' in m.group(1), (
            "The feed filter must apply in global mode only"
        )

    def test_poll_url_carries_the_filter(self, tmp_path):
        html = _page(tmp_path)
        assert "${feedFilterParams()}" in html, (
            "feedPoll must append the projects= filter"
        )

    def test_filter_change_resets_cursors(self, tmp_path):
        """Filter (or filtered-target) change re-tails the stream from cursor 0."""
        html = _page(tmp_path)
        assert "feedResetStream" in html
        m = re.search(r"function feedResetStream\(\)\{(.*?)\n\}", html, re.S)
        assert m, "feedResetStream not found"
        assert "feedCursors={after_note:0,after_doc:0}" in m.group(1)
        m2 = re.search(r"function setFeedScope\(s\)\{(.*?)\n\}", html, re.S)
        assert m2, "setFeedScope not found"
        assert "feedResetStream()" in m2.group(1)


# ── §6.3: counts banner deleted ──────────────────────────────────────────────

class TestCountsBannerGone:
    def test_counts_banner_markup_and_handlers_removed(self, tmp_path):
        """Honest sibling counts (B2) replace the 'siblings read 0' banner."""
        html = _page(tmp_path)
        assert 'id="counts-banner"' not in html, "Counts banner markup must be gone"
        assert "paintCountsBanner" not in html
        assert "dismissCountsBanner" not in html
        assert "sibling projects read 0" not in html


# ── Embedded-JS hygiene gates ────────────────────────────────────────────────

def _extract_script(html: str) -> str:
    m = re.search(r"<script>\n(.*)</script>", html, re.S)
    assert m, "embedded <script> block not found"
    return m.group(1)


class TestEmbeddedJsHygiene:
    @pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
    def test_embedded_script_passes_node_check(self, tmp_path):
        """The shipped <script> must parse — it has zero other static analysis."""
        js = _extract_script(_page(tmp_path))
        js_file = tmp_path / "gui_script.js"
        js_file.write_text(js)
        proc = subprocess.run(
            ["node", "--check", str(js_file)], capture_output=True, text=True
        )
        assert proc.returncode == 0, f"node --check failed:\n{proc.stderr}"

    def test_no_var_or_console_log(self, tmp_path):
        """Repo convention (the GUI rules): no `var`, no console.log."""
        js = _extract_script(_page(tmp_path))
        assert not re.search(r"\bvar\s+[A-Za-z_$]", js), "`var` crept into the script"
        assert "console.log" not in js, "console.log crept into the script"
