# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""gui_template.py — the BrainCell Memory-Map single-page app (HTML+CSS+JS).

Self-contained: no CDN, no build step, no external fonts. Served verbatim by
gui.py at GET /. All data comes from the /api/* endpoints at runtime.
"""

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BrainCell — Map</title>
<style>
/* ══ Self-contained. DARK "Doom regalia" theme — silver/platinum chrome,
      gold regal accents, living green cells, distinct pool hues. ══ */
:root{
  --void:#070b09; --bg2:#0a0f0c;
  --panel:rgba(14,20,16,.62); --surface:rgba(18,25,20,.7); --surface2:rgba(12,17,13,.6);
  /* emerald / ivory / silver theme (2026-07-23) — the --gold* token NAMES are
     legacy from the gold era and now hold the emerald accent ramp. */
  --hair:rgba(24,201,138,.16); --hair2:rgba(24,201,138,.32);
  --ink:#f4f1e4; --mut:#a7ad99; --faint:#606a5b;
  --gold:#18c98a; --gold-h:#5cf0bf; --gold-d:#0b8f5e;
  --emerald:#18c98a; --leaf:#39d98e; --emerald-d:#04301c;
  --white:#f6f4ea;
  /* silver/platinum chrome tokens (owner request — more white & silver) */
  --silver:#c8cfd8; --silver-h:#eef2f6; --silver-d:#7d8794; --steel:rgba(200,207,216,.5);
  --glow-g:rgba(24,201,138,.5); --glow-e:rgba(24,201,138,.42); --glow-s:rgba(200,207,216,.4);
  --soft:rgba(24,201,138,.22);
  --disp:"Space Grotesk","Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;
  --sans:"Inter","SF Pro Text","Segoe UI",system-ui,-apple-system,sans-serif;
  --mono:ui-monospace,"SF Mono","JetBrains Mono",monospace;
  --shadow:0 40px 90px -34px rgba(0,0,0,.85),0 10px 30px -14px rgba(0,0,0,.7);
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;color:var(--ink);font-family:var(--sans);font-size:14px;line-height:1.5;
  -webkit-font-smoothing:antialiased;overflow:hidden;
  background:
    radial-gradient(1000px 560px at 82% -8%, rgba(200,207,216,.10), transparent 58%),
    radial-gradient(820px 520px at 6% 10%, rgba(24,201,138,.10), transparent 56%),
    linear-gradient(160deg,#0a0f0c,#070b09 60%,#050806);
  background-attachment:fixed}
#cyto{position:fixed;inset:-25%;z-index:0;filter:url(#goo) blur(15px) saturate(1.25);opacity:.5;pointer-events:none}
.bgb{position:absolute;border-radius:50%;mix-blend-mode:screen}
#grain{position:fixed;inset:0;z-index:1;pointer-events:none;opacity:.04;mix-blend-mode:overlay;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}
#vig{position:fixed;inset:0;z-index:1;pointer-events:none;background:radial-gradient(130% 100% at 50% 0%,transparent 52%,rgba(0,0,0,.6) 100%)}

.app{position:relative;z-index:2;height:100vh;display:flex;flex-direction:column;padding:0 22px 18px}
header{display:flex;align-items:center;gap:16px;padding:22px 4px 14px}
.mark{display:flex;align-items:center;gap:13px}
.glyph{width:40px;height:40px;filter:drop-shadow(0 0 10px var(--glow-g))}
.word{font-family:var(--disp);font-size:26px;font-weight:700;letter-spacing:-.02em;line-height:1;color:var(--silver-h)}
.word .c{background:linear-gradient(96deg,var(--gold-d),var(--gold) 40%,var(--gold-h) 74%,#fffdf6);-webkit-background-clip:text;background-clip:text;color:transparent}
.tag{font-family:var(--disp);font-size:11px;color:var(--silver);margin-top:6px;letter-spacing:.24em;text-transform:uppercase;font-weight:500;opacity:.85}
.searchbar{margin-left:8px;display:flex;align-items:center;gap:8px;background:var(--surface);border:1px solid rgba(200,207,216,.22);border-radius:999px;padding:7px 14px;backdrop-filter:blur(10px);width:280px;box-shadow:inset 0 1px 0 rgba(255,255,255,.07)}
.searchbar input{border:0;background:none;outline:0;font:inherit;font-size:13px;color:var(--ink);width:100%}
.searchbar input::placeholder{color:var(--faint)}
.searchbar svg{flex:0 0 auto;color:var(--silver);opacity:.7}
.status{margin-left:auto;display:flex;gap:9px;flex-wrap:wrap;justify-content:flex-end}
.chip{display:inline-flex;align-items:center;gap:8px;font-size:11.5px;color:var(--mut);background:var(--panel);border:1px solid var(--hair);border-radius:999px;padding:6px 13px;backdrop-filter:blur(10px);font-variant-numeric:tabular-nums;box-shadow:inset 0 1px 0 rgba(255,255,255,.05)}
.chip b{color:var(--silver-h);font-weight:600}
.dot{width:7px;height:7px;border-radius:50%;background:var(--emerald);box-shadow:0 0 9px var(--glow-e)}
.chip.w .dot{background:var(--gold);box-shadow:0 0 9px var(--glow-g)}
.chip.ro .dot{background:var(--silver-d);box-shadow:0 0 9px var(--glow-s)}

.stage-wrap{position:relative;flex:1;min-height:0;border-radius:22px;overflow:hidden;
  background:linear-gradient(180deg,rgba(16,22,18,.5),rgba(8,12,10,.36));
  border:1px solid rgba(200,207,216,.14);
  box-shadow:var(--shadow),inset 0 1px 0 rgba(255,255,255,.06);
  backdrop-filter:blur(6px)}
.stage-wrap::before{content:"";position:absolute;inset:0;border-radius:inherit;padding:1px;pointer-events:none;
  background:linear-gradient(135deg,var(--glow-g),rgba(200,207,216,.18) 40%,rgba(24,201,138,.15) 68%,transparent 88%);
  -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);-webkit-mask-composite:xor;mask-composite:exclude}
svg.stage{width:100%;height:100%;display:block;touch-action:none;cursor:grab}
svg.stage:active{cursor:grabbing}
.toolbar{position:absolute;top:14px;left:14px;display:flex;gap:8px;z-index:3}
.legend{position:absolute;left:14px;bottom:14px;z-index:3;color:var(--mut);font-size:11.5px;background:var(--panel);border:1px solid rgba(200,207,216,.14);border-radius:12px;padding:9px 13px;backdrop-filter:blur(10px);max-width:350px;line-height:1.6;box-shadow:inset 0 1px 0 rgba(255,255,255,.05)}
.legend b{color:var(--silver-h);font-weight:600}
.btn{font-family:var(--disp);font-size:12.5px;font-weight:500;color:var(--silver-h);cursor:pointer;transition:.16s;background:rgba(200,207,216,.06);border:1px solid rgba(200,207,216,.24);border-radius:10px;padding:8px 14px;backdrop-filter:blur(8px);box-shadow:inset 0 1px 0 rgba(255,255,255,.08)}
.btn:hover{background:rgba(200,207,216,.14);border-color:var(--silver);transform:translateY(-1px)}
.btn.primary{color:#04301c;font-weight:600;border:none;background:linear-gradient(120deg,var(--gold-d),var(--gold) 55%,var(--gold-h));box-shadow:0 8px 22px -8px var(--glow-g),inset 0 1px 0 rgba(255,255,255,.4)}
.btn.primary:hover{box-shadow:0 12px 30px -8px var(--glow-g),inset 0 1px 0 rgba(255,255,255,.5)}

/* scope toggle — segmented control, native to the existing chrome tokens */
.scope-seg{display:inline-flex;align-items:center;margin-left:10px;background:rgba(200,207,216,.06);border:1px solid rgba(200,207,216,.24);border-radius:10px;padding:2px;backdrop-filter:blur(8px);box-shadow:inset 0 1px 0 rgba(255,255,255,.08)}
.scope-seg button{font-family:var(--disp);font-size:11.5px;font-weight:500;color:var(--silver);cursor:pointer;background:none;border:0;border-radius:8px;padding:6px 11px;transition:.16s;white-space:nowrap}
.scope-seg button:hover:not(:disabled){color:var(--silver-h)}
.scope-seg button.active{color:#04301c;font-weight:600;background:linear-gradient(120deg,var(--gold-d),var(--gold) 55%,var(--gold-h));box-shadow:0 6px 16px -8px var(--glow-g),inset 0 1px 0 rgba(255,255,255,.4)}
.scope-seg button:disabled{opacity:.4;cursor:not-allowed}

/* pool-distinct membrane labels use pool hue via CSS var; default gold fallback */
.mem-label{font-family:var(--disp);font-weight:600;font-size:13px;fill:var(--silver-h);paint-order:stroke;stroke:rgba(0,0,0,.5);stroke-width:3px}
.mem-count{font-family:var(--sans);font-size:10.5px;fill:var(--silver)}
.link{stroke-width:1.1}
.cell-g{cursor:grab}.cell-g:active{cursor:grabbing}
.cell-label{font-family:var(--sans);font-size:11px;font-weight:500;fill:var(--silver-h);paint-order:stroke;stroke:rgba(0,0,0,.55);stroke-width:3px;text-anchor:middle}
.pool-btn{cursor:pointer}
.pool-btn rect{fill:url(#poolg)}
.pool-btn text{font-family:var(--disp);font-size:11px;font-weight:600;fill:#04301c}
.pool-btn.disabled{cursor:not-allowed;opacity:.45}
.pool-btn.disabled rect{fill:rgba(200,207,216,.12)}
.pool-btn.disabled text{fill:var(--silver-d)}
.org-label{font-family:var(--disp);font-weight:700;font-size:12px;fill:var(--silver-h);text-anchor:middle;letter-spacing:.06em;paint-order:stroke;stroke:rgba(0,0,0,.5);stroke-width:3px}

/* ── empty/loading overlay ── */
#overlay{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;z-index:6;pointer-events:none;transition:opacity .35s}
#overlay.hidden{opacity:0}
.overlay-inner{text-align:center;color:var(--silver);font-size:14px;background:var(--panel);border:1px solid rgba(200,207,216,.14);border-radius:16px;padding:24px 32px;backdrop-filter:blur(12px);box-shadow:var(--shadow)}
.overlay-inner b{display:block;font-family:var(--disp);font-size:18px;color:var(--silver-h);margin-bottom:8px}
.overlay-inner code{color:var(--gold);font-family:var(--mono);font-size:12px}

.drawer{position:absolute;top:0;right:0;height:100%;width:340px;transform:translateX(102%);transition:transform .28s cubic-bezier(.2,.8,.2,1);z-index:5;
  background:linear-gradient(180deg,rgba(14,20,16,.96),rgba(9,13,10,.94));backdrop-filter:blur(16px);border-left:1px solid rgba(200,207,216,.12);box-shadow:-24px 0 60px -34px #000;display:flex;flex-direction:column}
.drawer.open{transform:none}
.dr-hd{padding:18px 18px 12px;border-bottom:1px solid rgba(200,207,216,.1)}
.dr-hd .close{float:right;cursor:pointer;color:var(--faint);font-size:18px;line-height:1}
.dr-hd .close:hover{color:var(--silver)}
.dr-name{font-family:var(--disp);font-weight:600;font-size:16px;letter-spacing:-.01em;display:flex;align-items:center;gap:9px;color:var(--silver-h)}
.dr-ulid{color:var(--faint);font-family:var(--mono);font-size:11px;margin-top:5px}
.dr-path{color:var(--silver-d);font-size:11px;margin-top:2px;word-break:break-all}
.dr-stats{display:flex;gap:8px;margin-top:12px}
.stat{flex:1;background:rgba(200,207,216,.05);border:1px solid rgba(200,207,216,.12);border-radius:10px;padding:8px 10px;text-align:center;box-shadow:inset 0 1px 0 rgba(255,255,255,.06)}
.stat b{font-family:var(--disp);display:block;font-size:16px;color:var(--silver-h)}
.stat span{font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.08em}
.dr-body{flex:1;overflow:auto;padding:14px 18px}
.dr-search{display:flex;gap:8px;margin-bottom:12px}
.dr-search input{flex:1;border:1px solid rgba(200,207,216,.18);background:rgba(0,0,0,.3);color:var(--ink);border-radius:9px;padding:8px 11px;font:inherit;font-size:12.5px;outline:0}
.dr-search input:focus{border-color:var(--silver)}
.sec{font-family:var(--disp);font-size:10.5px;font-weight:600;text-transform:uppercase;letter-spacing:.14em;color:var(--silver);margin:6px 0 8px}
.note{background:rgba(255,255,255,.03);border:1px solid rgba(200,207,216,.1);border-radius:10px;padding:10px 12px;margin-bottom:8px;box-shadow:inset 0 1px 0 rgba(255,255,255,.04)}
.note .k{font-size:10px;color:var(--gold);text-transform:uppercase;letter-spacing:.08em;font-weight:600}
.note .c{font-size:12.5px;color:var(--ink);margin-top:3px}
.note .m{font-size:10.5px;color:var(--faint);margin-top:5px;font-variant-numeric:tabular-nums}
.fam-tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.ftag{font-size:11px;color:var(--silver-h);background:rgba(200,207,216,.08);border:1px solid rgba(200,207,216,.18);border-radius:999px;padding:3px 10px;display:flex;align-items:center;gap:5px}
.ftag-x{cursor:pointer;opacity:.6;font-size:12px;line-height:1}.ftag-x:hover{opacity:1;color:var(--gold)}
.warn-note{font-size:11.5px;color:var(--gold-h);background:rgba(24,201,138,.07);border:1px solid rgba(24,201,138,.2);border-radius:8px;padding:7px 10px;margin-bottom:10px}

/* ── modal (new pool / ingest / confirm) ── */
#modal-root{position:fixed;inset:0;z-index:30;display:none;align-items:center;justify-content:center;background:rgba(4,7,5,.66);backdrop-filter:blur(6px)}
#modal-root.open{display:flex}
.modal{width:460px;max-width:92vw;max-height:82vh;display:flex;flex-direction:column;border-radius:18px;overflow:hidden;
  background:linear-gradient(180deg,rgba(16,22,18,.98),rgba(9,13,10,.97));border:1px solid rgba(200,207,216,.18);box-shadow:var(--shadow)}
.mo-hd{padding:16px 18px 10px;border-bottom:1px solid rgba(200,207,216,.1)}
.mo-title{font-family:var(--disp);font-weight:600;font-size:16px;color:var(--silver-h)}
.mo-sub{font-size:11.5px;color:var(--mut);margin-top:3px}
.mo-body{padding:14px 18px;overflow:auto;flex:1}
.mo-ft{display:flex;gap:8px;justify-content:flex-end;padding:12px 18px;border-top:1px solid rgba(200,207,216,.1)}
.mo-input{width:100%;border:1px solid rgba(200,207,216,.18);background:rgba(0,0,0,.3);color:var(--ink);border-radius:9px;padding:9px 12px;font:inherit;font-size:13px;outline:0}
.mo-input:focus{border-color:var(--silver)}
.mo-label{font-family:var(--disp);font-size:10.5px;font-weight:600;text-transform:uppercase;letter-spacing:.14em;color:var(--silver);margin:12px 0 6px}
.fs-bar{display:flex;gap:6px;align-items:center;margin-bottom:8px}
.fs-path{flex:1;font-family:var(--mono);font-size:11px;color:var(--gold-h);background:rgba(0,0,0,.3);border:1px solid rgba(200,207,216,.14);border-radius:8px;padding:7px 10px;word-break:break-all}
.fs-list{border:1px solid rgba(200,207,216,.12);border-radius:10px;background:rgba(0,0,0,.22);max-height:230px;overflow:auto}
.fs-item{display:flex;align-items:center;gap:8px;padding:7px 12px;font-size:12.5px;color:var(--ink);cursor:pointer;border-bottom:1px solid rgba(200,207,216,.05)}
.fs-item:last-child{border-bottom:none}
.fs-item:hover{background:rgba(200,207,216,.08)}
.fs-item svg{flex:0 0 auto;color:var(--gold);opacity:.8}
.fs-empty{padding:12px;color:var(--faint);font-size:12px}
.btn.danger{color:#ffd7d7;border-color:rgba(220,80,80,.4);background:rgba(220,80,80,.08)}
.btn.danger:hover{background:rgba(220,80,80,.18);border-color:rgba(220,80,80,.7)}
.dr-actions{display:flex;flex-wrap:wrap;gap:7px;margin-top:12px}
.dr-actions .btn{font-size:11.5px;padding:6px 11px}
.dr-sched{display:flex;align-items:center;gap:8px;margin-top:10px;font-size:11.5px;color:var(--mut)}
.dr-sched select{border:1px solid rgba(200,207,216,.18);background:rgba(0,0,0,.35);color:var(--ink);border-radius:8px;padding:5px 8px;font:inherit;font-size:12px;outline:0}
#chip-job{cursor:default}
#chip-job .dot{background:var(--gold);box-shadow:0 0 9px var(--glow-g);animation:pulse 1.1s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.joblog{font-family:var(--mono);font-size:10.5px;line-height:1.5;color:var(--mut);background:rgba(0,0,0,.3);border:1px solid rgba(200,207,216,.1);border-radius:8px;padding:8px 10px;max-height:150px;overflow:auto;white-space:pre-wrap;margin-top:10px}
.overlay-inner .btn{margin-top:12px;pointer-events:all}
.toastwrap{position:fixed;top:20px;right:20px;z-index:40;display:flex;flex-direction:column;gap:8px}
.toast{display:flex;align-items:center;gap:9px;font-size:12.5px;color:var(--ink);padding:11px 15px;border-radius:12px;background:linear-gradient(180deg,#12180f,#0b0f0a);border:1px solid rgba(200,207,216,.16);box-shadow:var(--shadow);animation:rise .3s ease}
.toast.err{background:linear-gradient(180deg,#1a0c0c,#0f0808);border-color:rgba(220,80,80,.3)}
@keyframes rise{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}}
.spark{position:fixed;width:5px;height:5px;border-radius:50%;pointer-events:none;z-index:25}
</style>
</head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <filter id="goo"><feGaussianBlur in="SourceGraphic" stdDeviation="12" result="b"/><feColorMatrix in="b" mode="matrix" values="1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 0 20 -9"/></filter>
  <radialGradient id="nucG" cx="42%" cy="36%"><stop offset="0" stop-color="#5cf0bf"/><stop offset="46%" stop-color="#12b981"/><stop offset="100%" stop-color="#04301c"/></radialGradient>
  <radialGradient id="orgG" cx="42%" cy="34%"><stop offset="0" stop-color="#ffffff"/><stop offset="28%" stop-color="#eef2f6"/><stop offset="52%" stop-color="#c8cfd8"/><stop offset="72%" stop-color="#18c98a"/><stop offset="100%" stop-color="#0a5c3a"/></radialGradient>
  <linearGradient id="poolg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0b8f5e"/><stop offset=".5" stop-color="#18c98a"/><stop offset="1" stop-color="#5cf0bf"/></linearGradient>
  <symbol id="bcCell" viewBox="0 0 48 48">
    <circle cx="18" cy="24" r="12.5" fill="none" stroke="#c8cfd8" stroke-width="2.2"/>
    <circle cx="30" cy="24" r="12.5" fill="none" stroke="#c8cfd8" stroke-width="2.2"/>
    <circle cx="18" cy="24" r="4.8" fill="url(#nucG)"/><circle cx="30" cy="24" r="4.8" fill="url(#nucG)"/>
    <g stroke="#eef2f6" stroke-width="1.5" stroke-linecap="round"><path d="M24 10 l0 -5"/><path d="M39 15 l4 -3"/><path d="M9 15 l-4 -3"/></g>
  </symbol>
</svg>

<div id="cyto"></div><div id="grain"></div><div id="vig"></div>

<div class="app">
  <header>
    <div class="mark">
      <svg class="glyph" viewBox="0 0 48 48"><use href="#bcCell"/></svg>
      <div><div class="word">Brain<span class="c">Cell</span></div><div class="tag">memory map</div></div>
    </div>
    <label class="searchbar">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>
      <input id="global-q" placeholder="Search all memory…" oninput="draw()">
    </label>
    <div class="scope-seg" id="scope-seg" role="group" aria-label="Memory scope">
      <button id="scope-project" onclick="setScope('project')" title="Only the active project's memory">This project</button>
      <button id="scope-family" onclick="setScope('family')" title="Federated family recall from the active project">Family</button>
      <button id="scope-all" class="active" onclick="setScope('all')" title="All projects — namespace-wide memory">All</button>
    </div>
    <div class="status" id="status-chips">
      <span class="chip"><span class="dot"></span>Mode <b id="c-mode">—</b></span>
      <span class="chip">Projects <b id="c-proj">—</b></span>
      <span class="chip">Pools <b id="c-pool">—</b></span>
      <span class="chip" id="chip-writes" style="display:none"></span>
      <span class="chip" id="chip-job" style="display:none"><span class="dot"></span><b id="chip-job-txt">ingesting…</b></span>
    </div>
  </header>

  <div class="stage-wrap">
    <div class="toolbar">
      <button class="btn primary" onclick="openIngestModal()">⬇ Ingest project</button>
      <button class="btn" onclick="newPool()">＋ New pool</button>
      <button class="btn" id="add-repo-btn" onclick="openAddRepoModal()">✚ Add repo</button>
      <button class="btn" id="hook-btn" onclick="toggleHook()">◌ Family recall: …</button>
      <button class="btn" id="cmd-btn" onclick="openCommandsModal()" title="Every braincell command — what it does and where to run it">⌘ Commands</button>
      <button class="btn" onclick="relax()">↻ Re-tidy</button>
    </div>
    <svg class="stage" id="stage"></svg>
    <div class="legend">
      <b>⬇ Ingest project</b> picks a folder and absorbs its memory · <b>drag a cell into a membrane</b> to add it to that pool ·
      <b>drag it out</b> to remove · <b>click a cell</b> to inspect, re-ingest, clear, or schedule ·
      <b>click a pool's ◉</b> to fuse it into the global brain.
      <br><b>New pools save when you drop the first cell in.</b>
      <br><b>◉ Family recall</b> arms the proactive hook — braincell surfaces related notes at the
      start of every Claude Code turn. Installed disarmed; this is the switch.
      <br><b>⌘ Commands</b> lists every braincell command with instructions, plus the
      maintenance tools (consolidate, reflect, contradictions, backup, undo…).
    </div>

    <div id="overlay">
      <div class="overlay-inner"><b id="overlay-title">Loading…</b><span id="overlay-msg"></span></div>
    </div>

    <aside class="drawer" id="drawer">
      <div class="dr-hd">
        <span class="close" onclick="closeDrawer()">✕</span>
        <div class="dr-name"><svg width="20" height="20" viewBox="0 0 48 48"><use href="#bcCell"/></svg><span id="dr-name">—</span></div>
        <div class="dr-ulid" id="dr-ulid"></div>
        <div class="dr-path" id="dr-path"></div>
        <div class="fam-tags" id="dr-fams"></div>
        <div class="dr-stats">
          <div class="stat"><b id="dr-docs">0</b><span>docs</span></div>
          <div class="stat"><b id="dr-chunks">0</b><span>chunks</span></div>
          <div class="stat"><b id="dr-notes">0</b><span>notes</span></div>
        </div>
        <div class="dr-actions" id="dr-actions" style="display:none">
          <button class="btn" onclick="reingestSelected()">⟳ Re-ingest now</button>
          <button class="btn danger" onclick="confirmClearSelected()">✕ Clear memory</button>
        </div>
        <div class="dr-sched" id="dr-sched" style="display:none">
          <span>Auto-ingest:</span>
          <select id="dr-sched-sel" onchange="scheduleSelected(this.value)">
            <option value="0">off</option>
            <option value="60">hourly</option>
            <option value="1440">daily</option>
            <option value="10080">weekly</option>
          </select>
          <span id="dr-sched-note" style="color:var(--faint)"></span>
        </div>
      </div>
      <div class="dr-body">
        <div class="dr-search">
          <input id="dr-q" placeholder="Search this project's memory…" onkeydown="if(event.key==='Enter')drawerSearch()">
          <button class="btn" onclick="drawerSearch()">Go</button>
        </div>
        <div class="sec">Search results</div>
        <div id="dr-hits-list"></div>
        <div class="sec" style="margin-top:14px">Recent notes</div>
        <div id="dr-notes-list"></div>
      </div>
    </aside>
  </div>
</div>

<div class="toastwrap" id="toasts"></div>

<div id="modal-root" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <div class="mo-hd"><div class="mo-title" id="mo-title">—</div><div class="mo-sub" id="mo-sub"></div></div>
    <div class="mo-body" id="mo-body"></div>
    <div class="mo-ft" id="mo-ft"></div>
  </div>
</div>

<script>
"use strict";
/* ════════ DISTINCT POOL PALETTE — ≥8 clearly-separate hues (owner request) ════════ */
/* Deterministic: family[i] → FAM_HUE[i % FAM_HUE.length] */
const FAM_HUE = [
  [24,201,138],   /* 0 emerald (theme primary — was gold pre-2026-07-23 recolor) */
  [227,179,65],   /* 1 gold    */
  [90,169,255],   /* 2 azure   */
  [169,139,255],  /* 3 violet  */
  [255,122,156],  /* 4 rose    */
  [242,168,60],   /* 5 amber   */
  [47,214,198],   /* 6 cyan/teal */
  [200,207,216],  /* 7 silver  */
];
function famFill(fi){const h=FAM_HUE[fi%FAM_HUE.length];return`rgba(${h[0]},${h[1]},${h[2]},.13)`;}
function famRim(fi) {const h=FAM_HUE[fi%FAM_HUE.length];return`rgba(${h[0]},${h[1]},${h[2]},.6)`;}
function famLinkStroke(fi){const h=FAM_HUE[fi%FAM_HUE.length];return`rgba(${h[0]},${h[1]},${h[2]},.28)`;}
function famLabelFill(fi){const h=FAM_HUE[fi%FAM_HUE.length];return`rgb(${h[0]},${h[1]},${h[2]})`;}

/* animated background medium colours */
const MEDIUM=[[24,201,138],[92,240,191],[10,70,44],[246,244,234],[200,207,216]];

/* ════════ APP STATE ════════ */
const stage=document.getElementById("stage");
let W=0,H=0,nodes=[],families=[],org={x:0,y:0,r:36},drag=null,selected=null;
let status={allow_writes:false,global_brain:{exists:false,path:""},mode:"project"};
let _loading=true,_initDone=false;

/* ════════ API HELPERS ════════ */
/* A4: when the server launched with an auth token it lives in the tab's URL
   (?t=…). Carry it on every API call so the guarded /api/* routes accept us. */
const BC_TOKEN=new URLSearchParams(location.search).get("t");
function withTok(url){
  if(!BC_TOKEN)return url;
  return url+(url.includes("?")?"&":"?")+"t="+encodeURIComponent(BC_TOKEN);
}
async function apiFetch(url){
  try{
    const r=await fetch(withTok(url));
    if(!r.ok){console.error("API",r.status,url);return null;}
    return await r.json();
  }catch(e){console.error("fetch err",url,e);return null;}
}
async function apiPost(url,body){
  try{
    const r=await fetch(withTok(url),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    if(!r.ok){
      let msg=r.statusText;try{const j=await r.json();msg=j.detail||JSON.stringify(j);}catch(_){}
      throw new Error(`${r.status}: ${msg}`);
    }
    return await r.json();
  }catch(e){throw e;}
}

/* ════════ SCOPE TOGGLE (This project / Family / All) ════════ */
/* Governs what memory the drawer's notes + search show, mapping to the EXISTING
   /api/notes|/api/search params — no new backend capability:
     · 'all'     → namespace-wide (no projects filter, no federate)
     · 'project' → projects=<active project>   (existing scoped filter)
     · 'family'  → federate=true               (existing seed-based fan-out)
   Seed presence comes from /api/config. Without a launch seed (global-mode)
   both 'project' and 'family' are meaningless → disabled, scope pinned to 'all'.
   State is in-memory (persists across drawer/tab switches for the session); an
   initial ?scope= is honoured when allowed. */
let scopeMode="all", seedProjectId=null, federateAvailable=false;
(function(){const s=new URLSearchParams(location.search).get("scope");
  if(s==="project"||s==="family"||s==="all")scopeMode=s;})();

function applyScopeAvailability(){
  const bp=document.getElementById("scope-project"),bf=document.getElementById("scope-family");
  if(!federateAvailable){
    bp.disabled=true;bf.disabled=true;
    bp.title="Launch BrainCell on a project folder to scope to it";
    bf.title="Launch BrainCell on a project folder to enable family recall";
    scopeMode="all";
  } else {
    bp.disabled=false;bf.disabled=false;
    bp.title="Only the active project's memory";
    bf.title="Federated family recall from the active project";
    /* default to per-project when we know the seed (preserves the drawer's
       per-project view); an explicit ?scope= above still wins */
    if(!new URLSearchParams(location.search).get("scope"))scopeMode="project";
  }
  syncScopeUi();
}
function syncScopeUi(){
  ["all","project","family"].forEach(m=>{
    const b=document.getElementById("scope-"+m);
    if(b)b.classList.toggle("active",scopeMode===m);
  });
}
function setScope(m){
  const b=document.getElementById("scope-"+m);
  if(!b||b.disabled)return;
  scopeMode=m;syncScopeUi();
  /* re-run the open drawer view so the scope change is immediately visible */
  if(selected){
    loadDrawerNotes(selected);
    const dq=document.getElementById("dr-q");
    if(dq&&dq.value.trim())drawerSearch();
  }
}
/* Query fragment for the active scope. Family sends federate=true WITHOUT
   projects= — the API ignores projects when federate=true (documented sharp
   edge), so the two are never combined. */
function scopeParams(nodeId){
  if(scopeMode==="family"&&federateAvailable)return "&federate=true";
  if(scopeMode==="project")return "&projects="+encodeURIComponent(nodeId||seedProjectId||"");
  return "";
}

/* ════════ INITIAL DATA LOAD ════════ */
async function loadAll(){
  showOverlay("Loading…","");
  const [st,projs,fams,cfg]=await Promise.all([
    apiFetch("/api/status"),
    apiFetch("/api/projects"),
    apiFetch("/api/families"),
    apiFetch("/api/config"),
  ]);
  if(st) status={...status,...st, global_brain: st.global_brain || {exists:false,path:""}};
  if(cfg){seedProjectId=cfg.seed_project_id||null;federateAvailable=!!cfg.federate_available;}
  applyScopeAvailability();
  buildModel(projs||[],fams||[]);
  updateStatusChips();
  _loading=false;
  if(status.mode==="global" && !(status.global_brain&&status.global_brain.exists)){
    showOverlay("No global brain yet","Run <code>braincell build --mode global</code>, then reload.");
  } else if(!nodes.length){
    showOverlay("No projects yet",status.allow_writes
      ?`Pick a project folder and BrainCell will absorb its memory.<br><button class="btn primary" onclick="openIngestModal()">⬇ Ingest your first project</button>`
      :"Run braincell build <path> to index your first project.");
  } else {
    hideOverlay();
  }
  loadSchedules();
  loadHookState();
  /* resume the job chip if an ingest is already running (e.g. page reload) */
  if(status.allow_writes){
    const js=await apiFetch("/api/ingest/status");
    if(js&&js.job&&js.job.state==="running")watchJob();
    opsResume();  /* likewise resume a running maintenance job's poller */
  }
  if(!_initDone){_initDone=true;initNodes();loop();}
  else draw();
}

function buildModel(projs,rawFams){
  /* Build node list from /api/projects */
  const oldById={};nodes.forEach(n=>{oldById[n.id]={x:n.x,y:n.y,vx:n.vx,vy:n.vy};});
  nodes=projs.map(p=>{
    const base=oldById[p.project_id]||{x:W/2+(Math.random()*160-80),y:H/2+(Math.random()*120-60),vx:0,vy:0};
    const pathStr=p.path||"";
    let basename=pathStr.replace(/\/$/,"").split("/").pop()||pathStr||p.project_id;
    const uid=p.project_id||"";
    const shortUlid=uid.length>8?uid.slice(0,6)+"…"+uid.slice(-2):(uid||"?");
    return{id:uid,name:basename,path:pathStr,shortUlid,docs:p.docs||0,chunks:p.chunks||0,notes:p.notes||0,x:base.x,y:base.y,vx:base.vx,vy:base.vy,r:17,pin:false};
  });
  /* Build families — membership by project_id or path match */
  const byId={};nodes.forEach(n=>{byId[n.id]=n;});
  const byPath={};nodes.forEach(n=>{if(n.path)byPath[n.path]=n;});
  families=rawFams.map(f=>{
    const memberSet=new Set();
    (f.members||[]).forEach(m=>{
      if(m.project_id&&byId[m.project_id]){memberSet.add(m.project_id);}
      else if(m.path&&byPath[m.path]){memberSet.add(byPath[m.path].id);}
    });
    return{name:f.name,members:memberSet};
  });
  refreshCounts();
}

async function refreshFamilies(){
  const fams=await apiFetch("/api/families");
  if(!fams)return;
  const byId={};nodes.forEach(n=>{byId[n.id]=n;});
  const byPath={};nodes.forEach(n=>{if(n.path)byPath[n.path]=n;});
  families=fams.map(f=>{
    const memberSet=new Set();
    (f.members||[]).forEach(m=>{
      if(m.project_id&&byId[m.project_id]){memberSet.add(m.project_id);}
      else if(m.path&&byPath[m.path]){memberSet.add(byPath[m.path].id);}
    });
    return{name:f.name,members:memberSet};
  });
  refreshCounts();
}

/* ════════ SIM: step / centroid / famRadius ════════ */
function initNodes(){
  const cx=W/2,cy=H/2;
  nodes.forEach((nd,i)=>{
    nd.x=cx+Math.cos(i/Math.max(nodes.length,1)*6.28)*180+(Math.random()*40-20);
    nd.y=cy+Math.sin(i/Math.max(nodes.length,1)*6.28)*130+(Math.random()*40-20);
    nd.vx=0;nd.vy=0;
  });
  org={x:W-120,y:H/2,r:38};
}
function centroid(f){let x=0,y=0,n=0;nodes.forEach(nd=>{if(f.members.has(nd.id)){x+=nd.x;y+=nd.y;n++;}});return n?{x:x/n,y:y/n,n}:null;}
function famRadius(f,c){let r=52+c.n*14;nodes.forEach(nd=>{if(f.members.has(nd.id))r=Math.max(r,Math.hypot(nd.x-c.x,nd.y-c.y)+34);});return r;}
function famOf(id){return families.filter(f=>f.members.has(id)).map(f=>f.name);}

function step(){
  for(let i=0;i<nodes.length;i++)for(let j=i+1;j<nodes.length;j++){
    const a=nodes[i],b=nodes[j];let dx=a.x-b.x,dy=a.y-b.y,d2=dx*dx+dy*dy||1,d=Math.sqrt(d2),f=2600/d2,ux=dx/d,uy=dy/d;
    a.vx+=ux*f;a.vy+=uy*f;b.vx-=ux*f;b.vy-=uy*f;}
  families.forEach(f=>{const c=centroid(f);if(!c)return;nodes.forEach(nd=>{if(f.members.has(nd.id)){nd.vx+=(c.x-nd.x)*.012;nd.vy+=(c.y-nd.y)*.012;}});});
  nodes.forEach(nd=>{
    nd.vx+=((W/2-60)-nd.x)*.004;nd.vy+=(H/2-nd.y)*.004;
    if(nd===drag){nd.vx=nd.vy=0;return;}
    nd.vx*=.82;nd.vy*=.82;nd.x+=nd.vx*.14;nd.y+=nd.vy*.14;
    nd.x=Math.max(40,Math.min(W-190,nd.x));nd.y=Math.max(40,Math.min(H-40,nd.y));});
}

/* ════════ DRAW ════════ */
function draw(){
  const q=(document.getElementById("global-q").value||"").trim().toLowerCase();
  let s="";
  /* membrane goo fills */
  s+=`<g filter="url(#goo)">`;
  families.forEach((f,fi)=>{const c=centroid(f);if(!c)return;s+=`<circle cx="${c.x.toFixed(1)}" cy="${c.y.toFixed(1)}" r="${famRadius(f,c).toFixed(1)}" fill="${famFill(fi)}"/>`;});
  s+=`</g>`;
  /* crisp membrane rims — each pool's own hue */
  families.forEach((f,fi)=>{const c=centroid(f);if(!c)return;s+=`<circle cx="${c.x.toFixed(1)}" cy="${c.y.toFixed(1)}" r="${famRadius(f,c).toFixed(1)}" fill="none" stroke="${famRim(fi)}" stroke-width="1.3"/>`;});
  /* member→centroid links in pool hue */
  families.forEach((f,fi)=>{const c=centroid(f);if(!c)return;nodes.forEach(nd=>{if(f.members.has(nd.id))
    s+=`<line x1="${nd.x.toFixed(1)}" y1="${nd.y.toFixed(1)}" x2="${c.x.toFixed(1)}" y2="${c.y.toFixed(1)}" stroke="${famLinkStroke(fi)}" stroke-width="1.1"/>`;});});
  /* membrane labels + pool-now button */
  families.forEach((f,fi)=>{const c=centroid(f);if(!c)return;const r=famRadius(f,c),ly=c.y-r+2;
    const labelColor=famLabelFill(fi);
    s+=`<text class="mem-label" x="${c.x.toFixed(1)}" y="${ly.toFixed(1)}" text-anchor="middle" fill="${labelColor}">${esc(f.name)}</text>`;
    s+=`<text class="mem-count" x="${c.x.toFixed(1)}" y="${(ly+15).toFixed(1)}" text-anchor="middle">${c.n} cell${c.n!==1?"s":""}</text>`;
    const poolDisabled=!status.allow_writes||!status.global_brain||!status.global_brain.exists;
    const disAttr=poolDisabled?` class="pool-btn disabled"`:` class="pool-btn"`;
    const titleAttr=poolDisabled
      ?(!status.allow_writes?` title="read-only: launch with --allow-writes"`:`title="no global brain — run braincell build --mode global"`)
      :``;
    s+=`<g${disAttr}${titleAttr} transform="translate(${(c.x-34).toFixed(1)},${(ly+22).toFixed(1)})" onclick="poolFamily(${fi})"><rect width="68" height="22" rx="11"/><text x="34" y="15" text-anchor="middle">◉ Pool now</text></g>`;});
  /* global organism — silver specular rim over gold core (owner request) */
  s+=`<g>`;
  s+=`<circle cx="${org.x}" cy="${org.y}" r="${org.r+20}" fill="rgba(200,207,216,.06)"/>`;
  s+=`<circle cx="${org.x}" cy="${org.y}" r="${org.r+13}" fill="none" stroke="rgba(200,207,216,.22)" stroke-width="1"/>`;
  s+=`<circle cx="${org.x}" cy="${org.y}" r="${org.r+7}" fill="none" stroke="rgba(238,242,246,.4)" stroke-width="1.3"/>`;
  s+=`<circle cx="${org.x}" cy="${org.y}" r="${org.r+2}" fill="none" stroke="rgba(24,201,138,.6)" stroke-width="1.5"/>`;
  s+=`<circle cx="${org.x}" cy="${org.y}" r="${org.r}" fill="url(#orgG)"/>`;
  s+=`<text class="org-label" x="${org.x}" y="${org.y+org.r+18}">GLOBAL BRAIN</text>`;
  s+=`</g>`;
  /* project cells: green nucleus, silver rim with subtle inner-highlight */
  nodes.forEach(nd=>{
    const dim=q&&!(nd.name.toLowerCase().includes(q));const op=dim?.22:1;const sel=selected===nd.id;
    s+=`<g class="cell-g" data-id="${esc(nd.id)}" transform="translate(${nd.x.toFixed(1)},${nd.y.toFixed(1)})" opacity="${op}">`+
       `<circle r="${nd.r+7}" fill="${sel?'rgba(200,207,216,.12)':'transparent'}"/>`+
       `<circle r="${nd.r}" fill="none" stroke="rgba(200,207,216,.55)" stroke-width="1.8"/>`+
       `<circle r="${nd.r-2}" fill="none" stroke="rgba(238,242,246,.18)" stroke-width="1"/>`+
       `<circle r="${nd.r-4}" fill="url(#nucG)"/>`+
       `<text class="cell-label" y="${nd.r+15}">${esc(nd.name)}</text></g>`;});
  stage.innerHTML=s;
}
function loop(){step();draw();requestAnimationFrame(loop);}

/* ════════ POINTER / DRAG ════════ */
function svgPt(e){const m=stage.getScreenCTM().inverse();return new DOMPoint(e.clientX,e.clientY).matrixTransform(m);}
let dragMoved=false;
stage.addEventListener("pointerdown",e=>{const g=e.target.closest(".cell-g");if(!g)return;const nd=nodes.find(n=>n.id===g.dataset.id);if(!nd)return;drag=nd;nd.pin=true;dragMoved=false;stage.setPointerCapture(e.pointerId);});
stage.addEventListener("pointermove",e=>{if(!drag)return;const p=svgPt(e);const dx=p.x-drag.x,dy=p.y-drag.y;if(Math.hypot(dx,dy)>4)dragMoved=true;drag.x=p.x;drag.y=p.y;});
stage.addEventListener("pointerup",async e=>{
  if(!drag)return;const nd=drag;drag=null;nd.pin=false;
  if(!dragMoved){openDrawer(nd);return;}
  /* determine which families the cell was in / is now over */
  let hitFi=-1;
  families.forEach((f,fi)=>{const c=centroid(f);if(!c)return;if(Math.hypot(nd.x-c.x,nd.y-c.y)<=famRadius(f,c))hitFi=fi;});
  const prevFams=[...families.filter(f=>f.members.has(nd.id)).map(f=>f.name)];
  if(hitFi>=0){
    const f=families[hitFi];
    if(!f.members.has(nd.id)){
      /* optimistic add */
      f.members.add(nd.id);
      burstAt(e.clientX,e.clientY);
      refreshCounts();
      if(status.allow_writes){
        try{
          await apiPost("/api/family",{action:"add",name:f.name,paths:[nd.path]});
          toast(`Added ${nd.name} → ${f.name}`);
        }catch(err){
          /* revert optimistic */
          f.members.delete(nd.id);
          refreshCounts();
          toast(`Failed to add to pool: ${err.message}`,"err");
        }
        await refreshFamilies();
      } else {
        toast(`Added ${nd.name} → ${f.name} (read-only mode; changes not saved)`);
      }
    }
  } else {
    /* dragged outside all membranes → remove from all */
    const toRemove=families.filter(f=>f.members.has(nd.id));
    if(toRemove.length){
      toRemove.forEach(f=>f.members.delete(nd.id));
      refreshCounts();
      if(status.allow_writes){
        for(const f of toRemove){
          try{await apiPost("/api/family",{action:"rm",name:f.name,paths:[nd.path]});}
          catch(err){toast(`Failed to remove from ${f.name}: ${err.message}`,"err");}
        }
        await refreshFamilies();
      } else {
        toast(`Removed ${nd.name} from pools (read-only; not saved)`);
      }
    }
  }
});

/* ════════ MODAL PLUMBING ════════ */
function openModal(title,sub,bodyHtml,footHtml){
  document.getElementById("mo-title").textContent=title;
  document.getElementById("mo-sub").innerHTML=sub||"";
  document.getElementById("mo-body").innerHTML=bodyHtml;
  document.getElementById("mo-ft").innerHTML=footHtml;
  document.getElementById("modal-root").classList.add("open");
}
function closeModal(){document.getElementById("modal-root").classList.remove("open");}

/* ════════ FOLDER BROWSER (server-side /api/fs + native GNOME picker) ════════ */
let fsCur="";
/* D1: native zenity picker is an enhancement over /api/fs, never a replacement.
   Once the server reports it unavailable, stop offering it for the rest of
   the session — every fsHtml() render checks this flag. */
let nativePickerDisabled=false;
function fsHtml(){
  return `<div class="fs-bar">
    <div class="fs-path" id="fs-path">…</div>
    <button class="btn" onclick="fsUp()" title="Up one level">↑</button>
    <button class="btn" onclick="fsGo('')" title="Home">⌂</button>
    ${nativePickerDisabled?"":`<button class="btn" id="fs-native-btn" onclick="pickFolderNative()" title="Open the native OS folder picker">📁 Browse (native)…</button>`}
  </div>
  <div class="fs-list" id="fs-list"><div class="fs-empty">Loading…</div></div>`;
}
async function pickFolderNative(){
  const btn=document.getElementById("fs-native-btn");
  if(btn)btn.disabled=true;
  try{
    const res=await apiPost("/api/pick-folder",{});
    if(res.path){
      fsCur=res.path;fsParent=null;
      const pe=document.getElementById("fs-path");if(pe)pe.textContent=res.path;
      const list=document.getElementById("fs-list");
      if(list)list.innerHTML=`<div class="fs-empty">Selected via native picker.</div>`;
    } else if(res.unavailable){
      nativePickerDisabled=true;
      if(btn)btn.remove();
      toast(res.reason?`Native picker unavailable (${res.reason}) — use the folder browser below`:"Native picker unavailable — use the folder browser below","err");
      fsGo(fsCur||"");
    }
    /* {cancelled:true} → no-op, just fall through to re-enable the button */
  }catch(err){
    toast(`Native picker failed: ${err.message}`,"err");
  }finally{
    if(btn&&!nativePickerDisabled)btn.disabled=false;
  }
}
async function fsGo(path){
  const data=await apiFetch("/api/fs?path="+encodeURIComponent(path||""));
  if(!data){document.getElementById("fs-list").innerHTML=`<div class="fs-empty">Cannot open that folder.</div>`;return;}
  fsCur=data.path;fsParent=data.parent;
  const pe=document.getElementById("fs-path");if(pe)pe.textContent=data.path;
  const list=document.getElementById("fs-list");if(!list)return;
  list.innerHTML=data.dirs.length
    ?data.dirs.map(d=>`<div class="fs-item" onclick="fsGo('${esc(d.path).replace(/'/g,"\\'")}')">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>
        ${esc(d.name)}</div>`).join("")
    :`<div class="fs-empty">No sub-folders — use "Select this folder".</div>`;
}
let fsParent=null;
function fsUp(){if(fsParent)fsGo(fsParent);}

/* ════════ INGEST PROJECT (modal → job → poll) ════════ */
function requireWrites(){
  if(!status.allow_writes){toast("Read-only: relaunch with --allow-writes (or use braincell-map)","err");return false;}
  return true;
}
function openIngestModal(){
  if(!requireWrites())return;
  openModal("Ingest a project","Pick the folder your project lives in — BrainCell will absorb its memory.",
    fsHtml(),
    `<button class="btn" onclick="closeModal()">Cancel</button>
     <button class="btn primary" onclick="startIngest(fsCur,null)">⬇ Ingest this folder</button>`);
  fsGo("");
}
let _pendingPool=null,_jobPoll=null;
async function startIngest(path,poolName){
  if(!path){toast("Pick a folder first","err");return;}
  try{
    await apiPost("/api/ingest",{path});
    _pendingPool=poolName||null;
    closeModal();
    toast(`Ingesting ${path.split("/").pop()}…`);
    watchJob();
  }catch(err){toast(`Ingest failed to start: ${err.message}`,"err");}
}
function watchJob(){
  if(_jobPoll)return;
  const chip=document.getElementById("chip-job"),txt=document.getElementById("chip-job-txt");
  chip.style.display="";
  _jobPoll=setInterval(async()=>{
    const data=await apiFetch("/api/ingest/status");
    const job=data&&data.job;
    if(!job){return;}
    const base=(job.path||"").split("/").pop();
    if(job.state==="running"){txt.textContent=`ingesting ${base}…`;return;}
    clearInterval(_jobPoll);_jobPoll=null;chip.style.display="none";
    if(job.state==="done"){
      toast(`Ingest complete: ${base}`);
      const pool=_pendingPool;_pendingPool=null;
      await loadAll();
      if(pool){
        const nd=nodes.find(n=>n.path===job.path);
        if(nd){
          try{
            await apiPost("/api/family",{action:"add",name:pool,paths:[nd.path]});
            toast(`Added ${nd.name} → ${pool}`);
            await refreshFamilies();
          }catch(err){toast(`Pool link failed: ${err.message}`,"err");}
        }
      }
    } else {
      _pendingPool=null;
      const tail=(job.log||[]).slice(-6).join("\n");
      toast(`Ingest failed (${base})`,"err");
      openModal("Ingest failed",esc(job.path),
        `<div class="joblog">${esc(tail||"no output captured")}</div>`,
        `<button class="btn" onclick="closeModal()">Close</button>`);
    }
  },1500);
}

/* ════════ NEW POOL (modal: name + optional project folder) ════════ */
function newPool(){
  openModal("New pool","Name the pool. Optionally pick a project folder to ingest straight into it.",
    `<div class="mo-label">Pool name</div>
     <input class="mo-input" id="np-name" placeholder="e.g. web-stack" autofocus>
     <div class="mo-label">Project folder (optional)</div>
     ${fsHtml()}`,
    `<button class="btn" onclick="closeModal()">Cancel</button>
     <button class="btn" onclick="createPoolOnly()">Create empty pool</button>
     <button class="btn primary" onclick="createPoolAndIngest()">Create &amp; ingest folder</button>`);
  fsGo("");
  setTimeout(()=>{const el=document.getElementById("np-name");if(el)el.focus();},50);
}
function _poolNameFromModal(){
  const el=document.getElementById("np-name");
  const n=el?el.value.trim():"";
  if(!n){toast("Give the pool a name","err");return null;}
  return n;
}
function createPoolOnly(){
  const n=_poolNameFromModal();if(!n)return;
  if(!families.some(f=>f.name===n))families.push({name:n,members:new Set()});
  closeModal();
  toast(`Created pool "${n}" — drop cells in to persist`);
  refreshCounts();
}
async function createPoolAndIngest(){
  const n=_poolNameFromModal();if(!n)return;
  if(!requireWrites())return;
  if(!fsCur){toast("Pick a project folder (or use Create empty pool)","err");return;}
  if(!families.some(f=>f.name===n))families.push({name:n,members:new Set()});
  refreshCounts();
  await startIngest(fsCur,n);
}
function relax(){nodes.forEach(n=>{n.vx=(Math.random()*2-1)*6;n.vy=(Math.random()*2-1)*6;});}

/* ════════ ADD-REPO WIZARD (pick → build → install → family, D6 fixed order) ════════ */
let arStep=0,arPath="",arProjectId=null,arClient="claude",_arBuildPoll=null;

function openAddRepoModal(){
  if(!requireWrites())return;
  arStep=1;arPath="";arProjectId=null;arClient="claude";
  arStepPick();
}
function arStepPick(){
  arStep=1;
  openModal("Add a repo — 1/4: Pick","Choose the folder for the project BrainCell should remember.",
    fsHtml(),
    `<button class="btn" onclick="closeModal()">Cancel</button>
     <button class="btn primary" onclick="arGoBuild()">Next: Build →</button>`);
  fsGo("");
}
function arGoBuild(){
  if(!fsCur){toast("Pick a folder first","err");return;}
  arPath=fsCur;
  arStepBuild();
}
async function arStepBuild(){
  arStep=2;
  openModal("Add a repo — 2/4: Build",esc(arPath),
    `<div id="ar-build-status" style="font-size:12.5px;color:var(--mut)">Starting…</div>
     <div class="joblog" id="ar-build-log" style="display:none"></div>`,
    `<button class="btn" onclick="closeModal()">Cancel</button>`);
  try{
    await apiPost("/api/ingest",{path:arPath});
  }catch(err){
    const st=document.getElementById("ar-build-status");
    if(st)st.textContent=`Failed to start: ${err.message}`;
    return;
  }
  arWatchBuild();
}
function arWatchBuild(){
  if(_arBuildPoll)return;
  _arBuildPoll=setInterval(async()=>{
    const data=await apiFetch("/api/ingest/status");
    const job=data&&data.job;
    if(!job)return;
    const st=document.getElementById("ar-build-status"),log=document.getElementById("ar-build-log");
    if(job.state==="running"){
      if(st)st.textContent=`Building ${(job.path||"").split("/").pop()}…`;
      return;
    }
    clearInterval(_arBuildPoll);_arBuildPoll=null;
    if(job.state==="done"){
      if(st)st.textContent="Build complete.";
      arStepInstall();
    } else {
      const tail=(job.log||[]).slice(-6).join("\n");
      if(st)st.textContent="Build failed — fix the issue below, then retry.";
      if(log){log.style.display="";log.textContent=tail||"no output captured";}
      openModal("Add a repo — 2/4: Build failed",esc(arPath),
        `<div class="joblog">${esc(tail||"no output captured")}</div>`,
        `<button class="btn" onclick="closeModal()">Close</button>
         <button class="btn primary" onclick="arStepBuild()">Retry</button>`);
    }
  },1500);
}
function arStepInstall(){
  arStep=3;
  openModal("Add a repo — 3/4: Install",
    `Register the braincell MCP server for <b>${esc(arPath.split("/").pop())}</b>.`,
    `<div class="mo-label">MCP client</div>
     <select class="mo-input" id="ar-client">
       <option value="claude">claude</option>
       <option value="codex">codex</option>
       <option value="vscode">vscode</option>
     </select>
     <div class="mo-label">Scope</div>
     <select class="mo-input" id="ar-scope">
       <option value="local" selected>local</option>
       <option value="user">user</option>
       <option value="project">project</option>
     </select>
     <label style="display:flex;gap:8px;align-items:center;font-size:12.5px;color:var(--mut);margin-top:12px">
       <input type="checkbox" id="ar-federate" checked> Enable cross-project federation
     </label>
     <label style="display:flex;gap:8px;align-items:center;font-size:12.5px;color:var(--mut);margin-top:8px" id="ar-hook-row">
       <input type="checkbox" id="ar-no-hook"> Skip family-recall hook
     </label>
     <div id="ar-install-err" class="warn-note" style="display:none;margin-top:10px"></div>`,
    `<button class="btn" onclick="closeModal()">Cancel</button>
     <button class="btn primary" onclick="arDoInstall()">Install →</button>`);
  const cSel=document.getElementById("ar-client");
  if(cSel)cSel.addEventListener("change",arSyncHookRow);
  arSyncHookRow();
}
function arSyncHookRow(){
  const cSel=document.getElementById("ar-client"),row=document.getElementById("ar-hook-row");
  if(!cSel||!row)return;
  row.style.display=cSel.value==="claude"?"":"none";
}
async function arDoInstall(){
  const client=document.getElementById("ar-client").value;
  const scope=document.getElementById("ar-scope").value;
  const federate=document.getElementById("ar-federate").checked;
  const noHook=!!(document.getElementById("ar-no-hook")||{}).checked;
  const errBox=document.getElementById("ar-install-err");
  try{
    const res=await apiPost("/api/install",{path:arPath,client,scope,no_hook:noHook,federate});
    arProjectId=res.project_id;arClient=client;
    arStepFamily();
  }catch(err){
    if(errBox){errBox.style.display="";errBox.textContent=err.message;}
  }
}
async function arStepFamily(){
  arStep=4;
  openModal("Add a repo — 4/4: Family","Optionally group this project with siblings for federated recall.",
    `<div id="ar-fam-body"><div class="fs-empty">Loading projects…</div></div>`,
    `<button class="btn" onclick="arFinish()">Skip</button>
     <button class="btn primary" onclick="arDoFamily()">Add to family →</button>`);
  const projs=await apiFetch("/api/projects");
  const body=document.getElementById("ar-fam-body");
  if(!body)return;
  const siblings=(projs||[]).filter(p=>p.project_id!==arProjectId);
  body.innerHTML=`<div class="mo-label">Family name</div>
    <input class="mo-input" id="ar-fam-name" placeholder="e.g. acme-suite">
    <div class="mo-label">Sibling projects</div>
    <div class="fs-list" style="max-height:160px">${
      siblings.length
        ?siblings.map(p=>`<label class="fs-item" style="cursor:pointer">
             <input type="checkbox" class="ar-fam-sib" value="${esc(p.path||"")}"> ${esc((p.path||"").replace(/\/$/,"").split("/").pop()||p.project_id)}
           </label>`).join("")
        :`<div class="fs-empty">No other projects registered yet.</div>`
    }</div>`;
}
async function arDoFamily(){
  const nameEl=document.getElementById("ar-fam-name");
  const name=nameEl?nameEl.value.trim():"";
  if(!name){toast("Give the family a name (or Skip)","err");return;}
  const paths=[...document.querySelectorAll(".ar-fam-sib:checked")].map(el=>el.value);
  paths.push(arPath);
  try{
    await apiPost("/api/family",{action:"add",name,paths});
    toast(`Family "${name}" created`);
    await refreshFamilies();
  }catch(err){toast(`Family create failed: ${err.message}`,"err");}
  arFinish();
}
function arFinish(){
  openModal("Add repo — done","",
    `<div style="font-size:13px;color:var(--ink)">Restart your MCP client (<b>${esc(arClient)}</b>) so it loads the braincell server.</div>`,
    `<button class="btn primary" onclick="closeModal();loadAll();">Done</button>`);
}

/* ════════ DRAWER ACTIONS: re-ingest / clear / schedule ════════ */
async function reingestSelected(){
  const nd=nodes.find(n=>n.id===selected);if(!nd)return;
  if(!requireWrites())return;
  if(!nd.path){toast("This project has no registered path","err");return;}
  await startIngest(nd.path,null);
}
function confirmClearSelected(){
  const nd=nodes.find(n=>n.id===selected);if(!nd)return;
  if(!requireWrites())return;
  openModal("Clear memory",`Wipe ingested docs &amp; chunks for <b>${esc(nd.name)}</b>? The next ingest re-absorbs everything fresh.`,
    `<label style="display:flex;gap:8px;align-items:center;font-size:12.5px;color:var(--mut)">
       <input type="checkbox" id="clr-notes"> Also delete its memory notes (irreversible)
     </label>`,
    `<button class="btn" onclick="closeModal()">Cancel</button>
     <button class="btn danger" onclick="doClearSelected()">✕ Clear</button>`);
}
async function doClearSelected(){
  const nd=nodes.find(n=>n.id===selected);if(!nd)return;
  const inclNotes=!!(document.getElementById("clr-notes")||{}).checked;
  closeModal();
  try{
    const res=await apiPost("/api/clear",{project_id:nd.id,include_notes:inclNotes});
    toast(`Cleared ${nd.name}: ${res.docs_removed} docs${inclNotes?`, ${res.notes_removed} notes`:""}`);
    await loadAll();
    const nd2=nodes.find(n=>n.id===nd.id);
    if(nd2)openDrawer(nd2);
  }catch(err){toast(`Clear failed: ${err.message}`,"err");}
}
let _schedules=[];
async function loadSchedules(){
  if(!status.allow_writes)return;
  const data=await apiFetch("/api/schedule");
  _schedules=(data&&data.schedules)||[];
}

/* ── Proactive family-recall hook (arm / disarm) ───────────────────────────────
   The hook is INSTALLED by `braincell install` (or the Add-repo wizard) but ships
   DISARMED; this toggle is the only in-GUI way to arm it. The arm state is a flag
   FILE on disk, so the server's `armed` is ground truth — never assume the button
   label is in sync, always repaint from the response.
   /api/hook is POST-only for every action INCLUDING "status" (gui_install.py), and
   it mounts only under --allow-writes, hence the read-only guard below. */
let _hookArmed=null;   /* null = unknown/unavailable, else boolean */

function paintHookBtn(){
  const b=document.getElementById("hook-btn");
  if(!b)return;
  if(!status.allow_writes){
    b.textContent="◌ Family recall";
    b.disabled=true;
    b.title="read-only: launch with --allow-writes";
    return;
  }
  b.disabled=false;
  if(_hookArmed===null){
    b.textContent="◌ Family recall: ?";
    b.title="Could not read the hook state.";
  }else if(_hookArmed){
    b.textContent="◉ Family recall: ON";
    b.title="Armed — related notes are injected at the start of every Claude Code turn. Click to disarm.";
  }else{
    b.textContent="◌ Family recall: OFF";
    b.title="Disarmed — the hook is a transparent no-op. Click to arm.";
  }
}

async function loadHookState(){
  if(!status.allow_writes){paintHookBtn();return;}
  try{
    const r=await apiPost("/api/hook",{action:"status"});
    _hookArmed=!!(r&&r.armed);
  }catch(e){
    _hookArmed=null;   /* endpoint absent or errored — show unknown, never crash init */
  }
  paintHookBtn();
}

async function toggleHook(){
  if(!status.allow_writes)return;
  const want=_hookArmed?"off":"on";
  try{
    const r=await apiPost("/api/hook",{action:want});
    _hookArmed=!!(r&&r.armed);
    paintHookBtn();
    toast(_hookArmed
      ?"Family recall ARMED — restart Claude Code sessions to pick it up."
      :"Family recall disarmed.");
  }catch(e){
    await loadHookState();   /* resync from disk rather than trusting the failed action */
    toast("Hook toggle failed: "+e.message);
  }
}
function syncSchedUi(nd){
  const box=document.getElementById("dr-sched"),sel=document.getElementById("dr-sched-sel"),note=document.getElementById("dr-sched-note");
  if(!status.allow_writes||!nd.path){box.style.display="none";return;}
  box.style.display="";
  const s=_schedules.find(s=>s.path===nd.path);
  sel.value=s?String(s.interval_minutes):"0";
  note.textContent=s&&s.last_run?("last run "+new Date(s.last_run*1000).toLocaleString()):(s?"runs while the map is open":"");
}
async function scheduleSelected(minutes){
  const nd=nodes.find(n=>n.id===selected);if(!nd||!nd.path)return;
  try{
    const res=await apiPost("/api/schedule",{path:nd.path,interval_minutes:parseInt(minutes,10)||0});
    _schedules=res.schedules||[];
    toast(parseInt(minutes,10)>0?`Auto-ingest ${nd.name}: every ${minutes} min (while the map is open)`:`Auto-ingest off for ${nd.name}`);
    syncSchedUi(nd);
  }catch(err){toast(`Schedule failed: ${err.message}`,"err");}
}

/* ════════ POOL FAMILY (fusion sparks + POST /api/pool) ════════ */
async function poolFamily(fi){
  const f=families[fi];
  if(!f)return;
  if(!status.allow_writes){toast("Read-only: launch with --allow-writes","err");return;}
  if(!status.global_brain||!status.global_brain.exists){toast("No global brain — run `braincell build --mode global`","err");return;}
  if(!f.members.size){toast(`Pool "${f.name}" is empty — add cells first`,"err");return;}
  /* spark animation */
  const rect=stage.getBoundingClientRect();
  nodes.forEach(nd=>{if(!f.members.has(nd.id))return;
    for(let i=0;i<4;i++){const s=document.createElement("div");s.className="spark";
      const rgb=[[246,244,234],[200,207,216],[24,201,138]][i%3];
      s.style.background=`rgb(${rgb})`;s.style.boxShadow=`0 0 7px rgba(${rgb},.7)`;
      const sx=rect.left+nd.x,sy=rect.top+nd.y,ex=rect.left+org.x,ey=rect.top+org.y;
      s.style.left=sx+"px";s.style.top=sy+"px";document.body.appendChild(s);
      s.animate([{transform:"translate(0,0)",opacity:1},{transform:`translate(${ex-sx}px,${ey-sy}px)`,opacity:.2}],
        {duration:700+Math.random()*400,easing:"cubic-bezier(.4,0,.2,1)"}).onfinish=()=>s.remove();}});
  try{
    const res=await apiPost("/api/pool",{family:f.name});
    const pooled=res.pooled||[];
    const totDocs=pooled.reduce((a,p)=>a+p.docs_copied,0);
    const totChunks=pooled.reduce((a,p)=>a+p.chunks_copied,0);
    const totNotes=pooled.reduce((a,p)=>a+p.notes_copied,0);
    const skippedN=(res.skipped||[]).length;
    toast(`Pooled "${f.name}" → global · ${totDocs} docs, ${totChunks} chunks, ${totNotes} notes copied · ${skippedN} skipped`);
    /* refresh project counts (chunks/docs may have changed) */
    const newProjs=await apiFetch("/api/projects");
    if(newProjs){
      const byId={};newProjs.forEach(p=>{byId[p.project_id]=p;});
      nodes.forEach(nd=>{const p=byId[nd.id];if(p){nd.docs=p.docs||0;nd.chunks=p.chunks||0;nd.notes=p.notes||0;}});
    }
  }catch(err){toast(`Pool failed: ${err.message}`,"err");}
}

/* ════════ INSPECTOR DRAWER ════════ */
function openDrawer(nd){
  selected=nd.id;
  document.getElementById("dr-name").textContent=nd.name;
  document.getElementById("dr-ulid").textContent=nd.shortUlid;
  document.getElementById("dr-path").textContent=nd.path;
  document.getElementById("dr-docs").textContent=nd.docs;
  document.getElementById("dr-chunks").textContent=nd.chunks.toLocaleString();
  document.getElementById("dr-notes").textContent=nd.notes;
  renderFamTags(nd);
  document.getElementById("dr-actions").style.display=status.allow_writes?"":"none";
  syncSchedUi(nd);
  document.getElementById("dr-hits-list").innerHTML="";
  document.getElementById("drawer").classList.add("open");
  loadDrawerNotes(nd.id);
}
function closeDrawer(){selected=null;document.getElementById("drawer").classList.remove("open");}

function renderFamTags(nd){
  const fams=famOf(nd.id);
  document.getElementById("dr-fams").innerHTML=fams.length
    ?fams.map(fn=>{
        const fi=families.findIndex(f=>f.name===fn);
        const h=FAM_HUE[fi%FAM_HUE.length];
        const col=`rgb(${h[0]},${h[1]},${h[2]})`;
        return `<span class="ftag" style="border-color:${col};color:${col}">◇ ${esc(fn)}<span class="ftag-x" onclick="removeFamTag('${esc(nd.id)}','${esc(fn)}')">✕</span></span>`;
      }).join("")
    :`<span class="ftag" style="opacity:.55">no pool</span>`;
}

async function removeFamTag(nodeId,famName){
  const nd=nodes.find(n=>n.id===nodeId);if(!nd)return;
  const f=families.find(f=>f.name===famName);
  if(!f)return;
  if(status.allow_writes){
    try{
      await apiPost("/api/family",{action:"rm",name:famName,paths:[nd.path]});
      f.members.delete(nodeId);
      refreshCounts();
      await refreshFamilies();
      renderFamTags(nd);
    }catch(err){toast(`Failed: ${err.message}`,"err");}
  } else {
    f.members.delete(nodeId);
    refreshCounts();
    renderFamTags(nd);
    toast("Removed from pool (read-only; not saved)");
  }
}

async function loadDrawerNotes(projectId){
  const data=await apiFetch(`/api/notes?k=20${scopeParams(projectId)}`);
  if(!data)return;
  let html="";
  if(data.warning)html+=`<div class="warn-note">${esc(data.warning)}</div>`;
  if(!data.notes||!data.notes.length){
    document.getElementById("dr-notes-list").innerHTML=html+`<div style="color:var(--faint);font-size:12px">No notes yet.</div>`;
    return;
  }
  html+=data.notes.map(n=>{
    const ts=(n.created_at||"").slice(0,10);
    /* forget = the GUI face of `braincell forget` (soft-delete). Write-gated:
       the button renders only when writes are on (/api/forget is unmounted
       read-only, so a click would 404 anyway). */
    const del=status.allow_writes&&n.id!=null
      ?`<span class="ftag-x" style="float:right" title="Forget this note (soft-delete — hidden from recall, kept for audit)" onclick="confirmForgetNote(${Number(n.id)},'${esc(n.project_id||"").replace(/'/g,"\\'")}')">✕</span>`
      :"";
    return `<div class="note"><div class="k">${esc(n.kind||"note")}${del}</div><div class="c">${esc(n.content)}</div><div class="m">conf ${n.confidence!=null?n.confidence:"—"} · ${ts}</div></div>`;
  }).join("");
  document.getElementById("dr-notes-list").innerHTML=html;
}

/* ── forget (wired to the existing POST /api/forget endpoint) ── */
function confirmForgetNote(noteId,projectId){
  if(!requireWrites())return;
  openModal("Forget note",`Soft-delete note <b>${Number(noteId)}</b>? It disappears from recall but is kept (tombstoned) for audit.`,
    "",
    `<button class="btn" onclick="closeModal()">Cancel</button>
     <button class="btn danger" onclick="doForgetNote(${Number(noteId)},'${esc(projectId).replace(/'/g,"\\'")}')">✕ Forget</button>`);
}
async function doForgetNote(noteId,projectId){
  closeModal();
  try{
    const r=await apiPost("/api/forget",{note_id:noteId,project:projectId});
    toast(r.deleted?`Note ${noteId} forgotten`:`Note ${noteId} not deleted (already gone?)`,r.deleted?undefined:"err");
    if(selected)loadDrawerNotes(selected);
  }catch(err){toast(`Forget failed: ${err.message}`,"err");}
}

async function drawerSearch(){
  const nd=nodes.find(n=>n.id===selected);if(!nd)return;
  const q=document.getElementById("dr-q").value.trim();
  if(!q){document.getElementById("dr-hits-list").innerHTML="";return;}
  const data=await apiFetch(`/api/search?q=${encodeURIComponent(q)}&k=20&mode=hybrid${scopeParams(nd.id)}`);
  const el=document.getElementById("dr-hits-list");
  if(!data){el.innerHTML=`<div style="color:var(--faint);font-size:12px">Search unavailable.</div>`;return;}
  let html="";
  if(data.warning)html+=`<div class="warn-note">${esc(data.warning)}</div>`;
  if(!data.hits||!data.hits.length){el.innerHTML=html+`<div style="color:var(--faint);font-size:12px">No results.</div>`;return;}
  html+=data.hits.map(h=>`<div class="note"><div class="k">${esc(h.title||h.doc_key||"")}${h.fts_matched?" ✓":""}</div><div class="c">${esc(h.snippet)}</div><div class="m">score ${h.score.toFixed(4)}${h.cosine!=null?" · cos "+h.cosine.toFixed(3):""}</div></div>`).join("");
  el.innerHTML=html;
}

/* ════════ COMMANDS PANEL (every braincell command, with instructions) ════════
   One modal = the complete command surface. Three groups:
     · Maintenance tools — new endpoints (/api/ops/*, /api/backup, /api/memory)
       reusing the SAME core functions the CLI calls. Long/LLM ops run as a
       background job polled via /api/ops/status (one at a time, like ingest).
     · Already on the map — commands the GUI exposes elsewhere; listed with a
       pointer so the surface is complete.
     · CLI-only — serve/gui/register, with the reason each stays in the CLI.
   Write-gated controls are DISABLED (never hidden) in read-only mode, with the
   standard explanatory title. Destructive runs go through an inline confirm
   strip (no nested modal). */
let _cmdPending=null,_opsPoll=null;

function wdis(){return status.allow_writes?"":' disabled title="read-only: launch with --allow-writes"';}
function cmdProjOptions(){
  return nodes.map(n=>`<option value="${esc(n.id)}" data-path="${esc(n.path)}"${n.id===selected?" selected":""}>${esc(n.name)}</option>`).join("");
}
function cmdSelProj(){const s=document.getElementById("cmd-proj");return s?s.value:"";}
function cmdSelPath(){
  const s=document.getElementById("cmd-proj");
  const o=s&&s.selectedOptions&&s.selectedOptions[0];
  return o?(o.dataset.path||""):"";
}

function openCommandsModal(){
  const noProj=nodes.length?"":`<div class="fs-empty">No projects registered yet — ingest one first.</div>`;
  openModal("⌘ Commands","Every braincell command — what it does, and where to run it.",
    `<div class="mo-label">Project (target for the maintenance tools)</div>
     <select class="mo-input" id="cmd-proj">${cmdProjOptions()}</select>${noProj}

     <div id="cmd-confirm" class="warn-note" style="display:none;margin-top:10px">
       <div id="cmd-confirm-msg"></div>
       <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:8px">
         <button class="btn" onclick="cmdConfirmNo()">Cancel</button>
         <button class="btn danger" onclick="cmdConfirmGo()">Proceed</button>
       </div>
     </div>

     <div class="mo-label">Maintenance tools</div>

     <div class="note"><div class="k">consolidate</div>
       <div class="c">Finds near-duplicate notes by embedding similarity. Dry-run lists the clusters;
       Apply merges each (keeps the newest, tombstones the rest) after writing a pre-merge backup —
       reversible via <b>memory undo</b> below.</div>
       <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:6px;font-size:11.5px;color:var(--mut)">
         thr <input class="mo-input" id="cmd-cons-th" value="0.9" style="width:60px;padding:4px 8px">
         <label><input type="checkbox" id="cmd-cons-apply"> Apply (destructive)</label>
         <label><input type="checkbox" id="cmd-cons-llm"> LLM merge body</label>
         <button class="btn"${wdis()} onclick="cmdConsolidate()">Run</button>
       </div></div>

     <div class="note"><div class="k">reflect</div>
       <div class="c">Asks a local Ollama model to synthesize ONE higher-level note per cluster of
       related notes; sources are superseded + tombstoned. Dry-run previews the clusters; Apply
       writes (pre-backup + undoable). Slow — runs as a background job.</div>
       <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:6px;font-size:11.5px;color:var(--mut)">
         thr <input class="mo-input" id="cmd-refl-th" value="0.85" style="width:60px;padding:4px 8px">
         since-days <input class="mo-input" id="cmd-refl-since" placeholder="all" style="width:60px;padding:4px 8px">
         <label><input type="checkbox" id="cmd-refl-apply"> Apply (destructive)</label>
         <button class="btn"${wdis()} onclick="cmdReflect()">Run</button>
       </div></div>

     <div class="note"><div class="k">contradictions</div>
       <div class="c">READ-ONLY audit: pairs up embedding-close active notes and asks a local LLM
       whether each pair contradicts. Deliberately has no auto-fix — resolve findings yourself with
       supersede/forget. Slow with the LLM; tick "list only" to skip judging.</div>
       <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:6px;font-size:11.5px;color:var(--mut)">
         limit <input class="mo-input" id="cmd-ctr-limit" value="50" style="width:60px;padding:4px 8px">
         <label><input type="checkbox" id="cmd-ctr-nollm"> list only (no LLM)</label>
         <button class="btn"${wdis()} onclick="cmdContradictions()">Run</button>
       </div></div>

     <div class="note"><div class="k">reembed-notes</div>
       <div class="c">Backfills embeddings for notes saved while the embedder was down (NULL vector —
       invisible to semantic recall until re-embedded). Needs Ollama up. Background job.</div>
       <div style="margin-top:6px"><button class="btn"${wdis()} onclick="cmdReembed()">Run</button></div></div>

     <div class="note"><div class="k">backup</div>
       <div class="c">Writes a clean, read-consistent snapshot of the opened brain (SQLite VACUUM INTO)
       next to the database — safe while in use; the source is never modified.</div>
       <div style="margin-top:6px"><button class="btn"${wdis()} onclick="cmdBackup()">⛁ Back up now</button></div></div>

     <div class="note"><div class="k">memory log / undo</div>
       <div class="c">Recorded merge operations (consolidate/reflect applies). Undo restores each
       note's exact pre-merge state; notes changed since are skipped, never clobbered.</div>
       <div style="margin-top:6px"><button class="btn" onclick="cmdMemLog()">Load log</button></div>
       <div class="fs-list" id="cmd-mem-list" style="max-height:150px;margin-top:6px"></div></div>

     <div class="note"><div class="k">uninstall</div>
       <div class="c">Removes the project's braincell MCP registration from a client (and, for Claude
       Code, the family-recall hook). The brain data itself is untouched. VS Code removal is manual —
       the server returns instructions.</div>
       <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:6px;font-size:11.5px;color:var(--mut)">
         <select class="mo-input" id="cmd-un-client" style="width:auto;padding:4px 8px">
           <option value="claude">claude</option><option value="codex">codex</option><option value="vscode">vscode</option>
         </select>
         <select class="mo-input" id="cmd-un-scope" style="width:auto;padding:4px 8px">
           <option value="local">local</option><option value="user">user</option><option value="project">project</option>
         </select>
         <label><input type="checkbox" id="cmd-un-disarm"> also disarm hook</label>
         <button class="btn danger"${wdis()} onclick="cmdUninstall()">Uninstall</button>
       </div></div>

     <div id="cmd-op-status" style="font-size:12px;color:var(--gold-h);margin-top:8px"></div>
     <div class="joblog" id="cmd-op-log" style="display:none"></div>

     <div class="mo-label">Already on the map</div>
     <div class="note"><div class="k">build / sync</div><div class="c">Ingest a repo's transcripts into its brain (sync = the same incremental run) → toolbar <b>⬇ Ingest project</b>, or a cell's <b>⟳ Re-ingest now</b>.</div></div>
     <div class="note"><div class="k">search / recall</div><div class="c">search = ranked document chunks; recall = curated memory notes → click a cell: the drawer's search box and Recent notes (scope toggle applies).</div></div>
     <div class="note"><div class="k">forget</div><div class="c">Soft-delete one note → the ✕ on any note in the drawer's Recent notes (writes on).</div></div>
     <div class="note"><div class="k">family / pool</div><div class="c">Group projects and fuse them into the global brain → <b>＋ New pool</b>, drag cells in/out, click a pool's <b>◉ Pool now</b>.</div></div>
     <div class="note"><div class="k">install</div><div class="c">Wire a repo into an MCP client (build → register → family) → toolbar <b>✚ Add repo</b> wizard.</div></div>
     <div class="note"><div class="k">hook</div><div class="c">Arm/disarm proactive family recall at the start of each Claude Code turn → toolbar <b>◌/◉ Family recall</b> toggle.</div></div>
     <div class="note"><div class="k">stats / clear / schedule</div><div class="c">Store counts live in each cell's drawer header; <b>✕ Clear memory</b> and <b>Auto-ingest</b> sit right beside them.</div></div>

     <div class="mo-label">Run from the CLI</div>
     <div class="note"><div class="k">serve</div><div class="c">Runs the MCP stdio server process for a client — it is launched BY the MCP client (via install), not from a browser tab.</div></div>
     <div class="note"><div class="k">gui</div><div class="c">Starts this very app (<b>braincell gui --allow-writes</b> / braincell-map) — it cannot launch itself.</div></div>
     <div class="note"><div class="k">register</div><div class="c">Mints a project ULID without ingesting — subsumed here by Ingest project / Add repo, which register automatically.</div></div>`,
    `<button class="btn" onclick="closeModal()">Close</button>`);
  if(_opsPoll===null)opsResume();
}

/* inline confirm strip — destructive runs pass through here */
function cmdConfirm(msg,fn){
  _cmdPending=fn;
  const box=document.getElementById("cmd-confirm"),m=document.getElementById("cmd-confirm-msg");
  if(!box||!m){if(fn)fn();return;}
  m.textContent=msg;box.style.display="";box.scrollIntoView({block:"nearest"});
}
function cmdConfirmGo(){
  const f=_cmdPending;_cmdPending=null;
  const box=document.getElementById("cmd-confirm");if(box)box.style.display="none";
  if(f)f();
}
function cmdConfirmNo(){
  _cmdPending=null;
  const box=document.getElementById("cmd-confirm");if(box)box.style.display="none";
}

/* start a maintenance job + poll /api/ops/status into the modal's log area */
async function runOp(op,body){
  if(!requireWrites())return;
  try{
    await apiPost("/api/ops/"+op,body);
    toast(`${op} started…`);
    opsWatch();
  }catch(err){toast(`${op} failed to start: ${err.message}`,"err");}
}
async function opsResume(){
  if(!status.allow_writes)return;
  const data=await apiFetch("/api/ops/status");
  if(data&&data.job&&data.job.state==="running")opsWatch();
}
function opsWatch(){
  if(_opsPoll)return;
  _opsPoll=setInterval(async()=>{
    const data=await apiFetch("/api/ops/status");
    const job=data&&data.job;
    if(!job)return;
    const st=document.getElementById("cmd-op-status"),log=document.getElementById("cmd-op-log");
    if(st)st.textContent=`${job.name}: ${job.state}`;
    if(log){log.style.display="";log.textContent=(job.log||[]).join("\n");log.scrollTop=log.scrollHeight;}
    if(job.state!=="running"){
      clearInterval(_opsPoll);_opsPoll=null;
      toast(job.state==="done"?`${job.name} finished`:`${job.name} FAILED — see the output log`,
            job.state==="done"?undefined:"err");
      if(job.state==="done")loadAll();
    }
  },1200);
}

function cmdConsolidate(){
  const pid=cmdSelProj();if(!pid){toast("Pick a project first","err");return;}
  const th=parseFloat((document.getElementById("cmd-cons-th")||{}).value)||0.9;
  const apply=!!(document.getElementById("cmd-cons-apply")||{}).checked;
  const llm=!!(document.getElementById("cmd-cons-llm")||{}).checked;
  if(apply){
    cmdConfirm("Merge near-duplicate notes for this project? A pre-merge backup is written first; the run is undoable via memory undo.",
      ()=>runOp("consolidate",{project_id:pid,threshold:th,apply:true,llm}));
    return;
  }
  runOp("consolidate",{project_id:pid,threshold:th,apply:false,llm:false});
}
function cmdReflect(){
  const pid=cmdSelProj();if(!pid){toast("Pick a project first","err");return;}
  const th=parseFloat((document.getElementById("cmd-refl-th")||{}).value)||0.85;
  const sinceRaw=((document.getElementById("cmd-refl-since")||{}).value||"").trim();
  const since=sinceRaw?parseInt(sinceRaw,10):null;
  const apply=!!(document.getElementById("cmd-refl-apply")||{}).checked;
  const body={project_id:pid,threshold:th,since_days:(since&&since>0)?since:null,apply};
  if(apply){
    cmdConfirm("Synthesize higher-level notes and supersede + tombstone their sources? A pre-reflect backup is written first; the run is undoable via memory undo.",
      ()=>runOp("reflect",body));
    return;
  }
  runOp("reflect",body);
}
function cmdContradictions(){
  const pid=cmdSelProj();if(!pid){toast("Pick a project first","err");return;}
  const limit=parseInt((document.getElementById("cmd-ctr-limit")||{}).value,10)||50;
  const noLlm=!!(document.getElementById("cmd-ctr-nollm")||{}).checked;
  runOp("contradictions",{project_id:pid,limit,no_llm:noLlm});
}
function cmdReembed(){
  const pid=cmdSelProj();if(!pid){toast("Pick a project first","err");return;}
  runOp("reembed-notes",{project_id:pid});
}
async function cmdBackup(){
  if(!requireWrites())return;
  try{
    const r=await apiPost("/api/backup",{});
    toast(`Backup written: ${r.path}`);
  }catch(err){toast(`Backup failed: ${err.message}`,"err");}
}
async function cmdMemLog(){
  const pid=cmdSelProj();if(!pid){toast("Pick a project first","err");return;}
  const el=document.getElementById("cmd-mem-list");if(!el)return;
  const data=await apiFetch("/api/memory?project_id="+encodeURIComponent(pid));
  if(!data){el.innerHTML=`<div class="fs-empty">Memory log unavailable (writes off?).</div>`;return;}
  const ops=data.operations||[];
  el.innerHTML=ops.length
    ?ops.map(o=>`<div class="fs-item" style="cursor:default">
        <span style="flex:1">#${Number(o.id)} ${esc(o.kind)} · ${esc(o.created_at||"")} · ${Number(o.note_count)} note(s)${o.undone_at?" · UNDONE":""}</span>
        ${o.undone_at?"":`<button class="btn"${wdis()} onclick="cmdMemUndo(${Number(o.id)})">Undo</button>`}
      </div>`).join("")
    :`<div class="fs-empty">No recorded merge operations for this project.</div>`;
}
function cmdMemUndo(opId){
  const pid=cmdSelProj();if(!pid)return;
  cmdConfirm(`Undo merge operation #${opId}? Each note's pre-merge state is restored; notes changed since the merge are skipped, never clobbered.`,
    async()=>{
      try{
        const r=await apiPost("/api/memory/undo",{op_id:opId,project_id:pid});
        toast(`Undid ${r.kind} #${opId}: ${(r.restored||[]).length} restored`+((r.skipped_changed||[]).length?`, ${r.skipped_changed.length} skipped (changed since)`:""));
        cmdMemLog();
      }catch(err){toast(`Undo failed: ${err.message}`,"err");}
    });
}
function cmdUninstall(){
  const path=cmdSelPath();if(!path){toast("Pick a project first","err");return;}
  if(!requireWrites())return;
  const client=(document.getElementById("cmd-un-client")||{}).value||"claude";
  const scope=(document.getElementById("cmd-un-scope")||{}).value||"local";
  const disarm=!!(document.getElementById("cmd-un-disarm")||{}).checked;
  cmdConfirm(`Remove this project's braincell MCP registration from ${client}? The brain data itself is untouched.`,
    async()=>{
      try{
        const r=await apiPost("/api/uninstall",{path,client,scope,disarm});
        toast(`Uninstalled from ${client}: MCP ${r.mcp_removed?"removed":"not removed"}, hook entries removed: ${r.hook_removed}`);
      }catch(err){toast(`Uninstall failed: ${err.message}`,"err");}
    });
}

/* ════════ STATUS CHIPS ════════ */
function updateStatusChips(){
  document.getElementById("c-mode").textContent=status.mode||"—";
  const wr=document.getElementById("chip-writes");
  if(status.allow_writes){
    wr.style.display="";
    wr.innerHTML=`<span class="dot chip-w" style="background:var(--gold);box-shadow:0 0 9px var(--glow-g)"></span>writes <b>on</b>`;
  } else {
    wr.style.display="";
    wr.innerHTML=`<span class="dot" style="background:var(--silver-d);box-shadow:0 0 9px var(--glow-s)"></span>read-only`;
  }
}
function refreshCounts(){
  document.getElementById("c-proj").textContent=nodes.length;
  document.getElementById("c-pool").textContent=families.length;
}

/* ════════ OVERLAY ════════ */
function showOverlay(title,msg){
  document.getElementById("overlay-title").textContent=title;
  document.getElementById("overlay-msg").innerHTML=msg;
  const ov=document.getElementById("overlay");
  ov.classList.remove("hidden");ov.style.pointerEvents="all";
}
function hideOverlay(){
  const ov=document.getElementById("overlay");
  ov.classList.add("hidden");ov.style.pointerEvents="none";
}

/* ════════ TOAST ════════ */
function toast(msg,type){
  const t=document.createElement("div");
  t.className=type==="err"?"toast err":"toast";
  t.innerHTML=`<span class="dot" style="${type==="err"?"background:#e05050":""}"></span><span>${esc(msg)}</span>`;
  document.getElementById("toasts").appendChild(t);
  setTimeout(()=>t.remove(),4800);
}

/* ════════ BURST / SPARKS ════════ */
function burstAt(x,y){
  for(let i=0;i<7;i++){
    const s=document.createElement("div");s.className="spark";
    const rgb=[[246,244,234],[200,207,216],[24,201,138]][i%3];
    s.style.background=`rgb(${rgb})`;s.style.boxShadow=`0 0 7px rgba(${rgb},.7)`;
    const a=Math.random()*6.28,d=16+Math.random()*22;
    s.style.left=x+"px";s.style.top=y+"px";
    document.body.appendChild(s);
    s.animate([{transform:"translate(0,0) scale(1)",opacity:1},{transform:`translate(${Math.cos(a)*d}px,${Math.sin(a)*d}px) scale(0)`,opacity:0}],{duration:520,easing:"ease-out"}).onfinish=()=>s.remove();
  }
}

/* ════════ ESCAPE HELPER ════════ */
function esc(s){return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");}

/* ════════ ANIMATED BACKGROUND MEDIUM ════════ */
(function medium(){
  const cyto=document.getElementById("cyto");
  [[0,44,-6,-12],[1,38,64,-8],[2,34,26,68],[3,24,70,60],[4,30,42,24]].forEach(([ci,sz,x,y],i)=>{
    const b=document.createElement("div");b.className="bgb";
    b.style.width=b.style.height=sz+"vw";b.style.left=x+"vw";b.style.top=y+"vw";
    b.style.background=`radial-gradient(circle, rgb(${MEDIUM[ci].join(",")}), transparent 62%)`;
    cyto.appendChild(b);
    b.animate([{transform:"translate(0,0) scale(1)"},{transform:`translate(${Math.random()*8-4}vw,${Math.random()*8-3}vw) scale(1.14)`},{transform:"translate(0,0) scale(1)"}],
      {duration:30000+i*4000,iterations:Infinity,easing:"ease-in-out"});
  });
})();

/* ════════ SIZE + BOOT ════════ */
function size(){const r=stage.getBoundingClientRect();W=r.width;H=r.height;stage.setAttribute("viewBox",`0 0 ${W} ${H}`);}
addEventListener("resize",()=>{size();if(_initDone&&nodes.length){initNodes();}});
size();
loadAll();
</script>
</body>
</html>
"""
