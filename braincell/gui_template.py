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
  /* dark UA widgets (scrollbars, form controls) — without this Chromium draws
     its light-grey default scrollbars over the dark theme */
  color-scheme:dark;
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
/* themed scrollbars — the standard properties (Chromium 121+/Firefox) plus the
   ::-webkit-scrollbar set for older Chromium; palette-matched, track invisible */
*{scrollbar-width:thin;scrollbar-color:var(--steel) transparent}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--steel);border-radius:8px;border:2px solid transparent;background-clip:padding-box}
::-webkit-scrollbar-thumb:hover{background:var(--silver-d)}
::-webkit-scrollbar-corner{background:transparent}
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

/* top-level ROW: left column (.main = header/toolbar/map/dock) + full-height feed rail */
.app{position:relative;z-index:2;height:100vh;display:flex}
.main{flex:1;min-width:0;display:flex;flex-direction:column;padding:0 16px 14px}
/* flex-wrap: header controls (search, active chip, scope, chips) must wrap
   inside .main at narrow widths — unwrapped they overflowed under the feed
   rail and became unclickable (same defect as the toolbar). */
header{display:flex;flex-wrap:wrap;align-items:center;gap:16px;padding:22px 4px 14px}
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

/* active-project chip + dropdown — whose memory the GUI is viewing (NAMINGS:
   Active project). ⌂ marks the launch project; RO tags read-only sibling views. */
.active-wrap{position:relative;margin-left:10px}
.active-chip{display:inline-flex;align-items:center;gap:7px;font-family:var(--disp);font-size:12px;font-weight:600;color:var(--silver-h);cursor:pointer;background:rgba(24,201,138,.08);border:1px solid rgba(24,201,138,.35);border-radius:999px;padding:7px 14px;backdrop-filter:blur(8px);box-shadow:inset 0 1px 0 rgba(255,255,255,.08);white-space:nowrap;max-width:300px;overflow:hidden;text-overflow:ellipsis;transition:.16s}
.active-chip:hover{border-color:var(--gold);background:rgba(24,201,138,.14)}
.active-chip .ac-home{color:var(--gold-h)}
.ac-ro{font-size:9px;font-weight:700;letter-spacing:.08em;color:var(--silver-d);border:1px solid rgba(200,207,216,.35);border-radius:5px;padding:1px 5px}
.active-dd{position:absolute;top:calc(100% + 6px);left:0;z-index:20;min-width:320px;max-width:440px;max-height:340px;overflow:auto;border-radius:14px;background:linear-gradient(180deg,rgba(16,22,18,.98),rgba(9,13,10,.97));border:1px solid rgba(200,207,216,.18);box-shadow:var(--shadow)}
.ad-item{padding:9px 14px;cursor:pointer;border-bottom:1px solid rgba(200,207,216,.06)}
.ad-item:last-child{border-bottom:none}
.ad-item:hover{background:rgba(200,207,216,.08)}
.ad-item.cur{background:rgba(24,201,138,.1)}
.ad-name{display:block;font-family:var(--disp);font-size:12.5px;font-weight:600;color:var(--silver-h)}
.ad-meta{display:block;font-size:10.5px;color:var(--mut);margin-top:2px;word-break:break-all}

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
/* Toolbar is bounded (left AND right) and wraps: absolutely-positioned chrome
   reserves no flex space, so without a right edge its min-content width jutted
   past the stage and UNDER the opaque feed rail at narrow viewports — buttons
   rendered but elementFromPoint hit the rail (dead controls at ≤1366px).
   pointer-events:none on the container + auto on children keeps the (now
   full-width) toolbar box from blocking map clicks between buttons. */
.toolbar{position:absolute;top:14px;left:14px;right:14px;display:flex;flex-wrap:wrap;gap:8px;z-index:3;pointer-events:none}
.toolbar>*{pointer-events:auto}
/* pointer-events:none — the legend is informational text with zero handlers;
   as absolutely-positioned chrome it must never eat clicks meant for the map
   or the (wrapping) toolbar when a small window brings them into contact. */
.legend{pointer-events:none;position:absolute;left:14px;bottom:14px;z-index:3;color:var(--mut);font-size:11.5px;background:var(--panel);border:1px solid rgba(200,207,216,.14);border-radius:12px;padding:9px 13px;backdrop-filter:blur(10px);max-width:350px;line-height:1.6;box-shadow:inset 0 1px 0 rgba(255,255,255,.05)}
.legend b{color:var(--silver-h);font-weight:600}
.btn{font-family:var(--disp);font-size:12.5px;font-weight:500;color:var(--silver-h);cursor:pointer;transition:.16s;background:rgba(200,207,216,.06);border:1px solid rgba(200,207,216,.24);border-radius:10px;padding:8px 14px;backdrop-filter:blur(8px);box-shadow:inset 0 1px 0 rgba(255,255,255,.08)}
.btn:hover{background:rgba(200,207,216,.14);border-color:var(--silver);transform:translateY(-1px)}
.btn.primary{color:#04301c;font-weight:600;border:none;background:linear-gradient(120deg,var(--gold-d),var(--gold) 55%,var(--gold-h));box-shadow:0 8px 22px -8px var(--glow-g),inset 0 1px 0 rgba(255,255,255,.4)}
.btn.primary:hover{box-shadow:0 12px 30px -8px var(--glow-g),inset 0 1px 0 rgba(255,255,255,.5)}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn:disabled:hover{background:rgba(200,207,216,.06);border-color:rgba(200,207,216,.24);transform:none}

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
.cell-g{cursor:pointer}.cell-g:active{cursor:grabbing}
.cell-label{font-family:var(--sans);font-size:11px;font-weight:500;fill:var(--silver-h);paint-order:stroke;stroke:rgba(0,0,0,.55);stroke-width:3px;text-anchor:middle}
.cell-active-label{font-family:var(--disp);font-size:8.5px;font-weight:700;letter-spacing:.2em;fill:var(--gold-h);text-anchor:middle;paint-order:stroke;stroke:rgba(0,0,0,.55);stroke-width:2.5px}
.pool-btn{cursor:pointer}
.pool-btn rect{fill:url(#poolg)}
.pool-btn text{font-family:var(--disp);font-size:11px;font-weight:600;fill:#04301c}
.pool-btn.disabled{cursor:not-allowed;opacity:.45}
.pool-btn.disabled rect{fill:rgba(200,207,216,.12)}
.pool-btn.disabled text{fill:var(--silver-d)}
.org-label{font-family:var(--disp);font-weight:700;font-size:12px;fill:var(--silver-h);text-anchor:middle;letter-spacing:.06em;paint-order:stroke;stroke:rgba(0,0,0,.5);stroke-width:3px}
/* decorative-but-honest: no global brain yet → the org node dims (title explains) */
.org-missing{opacity:.45}

/* ── empty/loading overlay ── */
#overlay{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;z-index:6;pointer-events:none;transition:opacity .35s}
#overlay.hidden{opacity:0}
.overlay-inner{text-align:center;color:var(--silver);font-size:14px;background:var(--panel);border:1px solid rgba(200,207,216,.14);border-radius:16px;padding:24px 32px;backdrop-filter:blur(12px);box-shadow:var(--shadow)}
.overlay-inner b{display:block;font-family:var(--disp);font-size:18px;color:var(--silver-h);margin-bottom:8px}
.overlay-inner code{color:var(--gold);font-family:var(--mono);font-size:12px}

/* ── INSPECTOR DOCK — bottom of the left column (was a right slide-in drawer) ── */
/* Height is CONTENT-SIZED up to a cap (was a rigid 300px, ~50px shorter than
   the panels' natural content — every column grew a permanent scrollbar and
   read as cut off). Columns wrap instead of forcing a horizontal scrollbar
   when the main column is narrow (rigid bases used to sum past its width). */
.dock{flex:0 1 auto;margin-top:12px;display:none;min-height:0;max-height:min(46vh,440px);border-radius:16px;overflow:hidden;
  background:linear-gradient(180deg,rgba(14,20,16,.96),rgba(9,13,10,.94));backdrop-filter:blur(16px);border:1px solid rgba(200,207,216,.16);box-shadow:var(--shadow)}
.dock.open{display:flex;flex-wrap:wrap;overflow-y:auto}
.dock .col{padding:12px 16px;border-right:1px solid rgba(200,207,216,.1);overflow-y:auto;min-width:0}
.dock .col:last-child{border-right:none}
.dock .c-head{flex:1 0 230px}
.dock .c-stats{flex:1 0 280px}  /* Store stats + MCP block; inherits .col overflow-y:auto so nothing clips */
.mcp-status{font-size:11.5px;color:var(--silver);margin-top:2px;line-height:1.45}
.mcp-note{font-size:10px;color:var(--faint);margin-top:6px;line-height:1.5}
.dock .c-search{flex:1 0 260px}
.dock .c-notes{flex:2 1 320px}
.dock .close{float:right;cursor:pointer;color:var(--faint);font-size:16px;line-height:1}
.dock .close:hover{color:var(--silver)}
.dr-name{font-family:var(--disp);font-weight:600;font-size:16px;letter-spacing:-.01em;display:flex;align-items:center;gap:9px;color:var(--silver-h)}
.dr-ulid{color:var(--faint);font-family:var(--mono);font-size:11px;margin-top:5px}
.dr-path{color:var(--silver-d);font-size:11px;margin-top:2px;word-break:break-all}
.dr-stats{display:flex;gap:8px;margin-top:12px}
.stat{flex:1;background:rgba(200,207,216,.05);border:1px solid rgba(200,207,216,.12);border-radius:10px;padding:8px 10px;text-align:center;box-shadow:inset 0 1px 0 rgba(255,255,255,.06)}
.stat b{font-family:var(--disp);display:block;font-size:16px;color:var(--silver-h)}
.stat span{font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.08em}
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

/* ── modal (new family / build / confirm) ── */
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
/* embedder header chip — quiet when ok; loud + clickable (fix modal) when down */
#chip-embedder{cursor:default}
#chip-embedder.bad{cursor:pointer;color:#ffd7d7;border-color:rgba(220,80,80,.4);background:rgba(220,80,80,.08)}
#chip-embedder.bad .dot{background:#e05050;box-shadow:0 0 9px rgba(224,80,80,.55)}
#chip-embedder.bad b{color:#ffd7d7}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.joblog{font-family:var(--mono);font-size:10.5px;line-height:1.5;color:var(--mut);background:rgba(0,0,0,.3);border:1px solid rgba(200,207,216,.1);border-radius:8px;padding:8px 10px;max-height:150px;overflow:auto;white-space:pre-wrap;margin-top:10px}
.overlay-inner .btn{margin-top:12px;pointer-events:all}
.toastwrap{position:fixed;top:20px;right:20px;z-index:40;display:flex;flex-direction:column;gap:8px}
.toast{display:flex;align-items:center;gap:9px;font-size:12.5px;color:var(--ink);padding:11px 15px;border-radius:12px;background:linear-gradient(180deg,#12180f,#0b0f0a);border:1px solid rgba(200,207,216,.16);box-shadow:var(--shadow);animation:rise .3s ease}
.toast.err{background:linear-gradient(180deg,#1a0c0c,#0f0808);border-color:rgba(220,80,80,.3)}
@keyframes rise{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}}
.spark{position:fixed;width:5px;height:5px;border-radius:50%;pointer-events:none;z-index:25}

/* ── LIVE MEMORY FEED — full-height right rail (flex sibling of .main; OUTSIDE
      #stage so the 60fps draw() rebuild never touches it) ── */
/* Responsive width: a rigid 640px ate HALF of a 1280px viewport and forced the
   main column under it. clamp keeps the full 640px on wide screens (≥~1883px —
   the owner's 1920 maximized view is unchanged) and scales down to a 300px
   floor on laptops so the main column always keeps ~2/3 of the width. */
.rail{flex:0 0 clamp(300px,34vw,640px);min-height:0;display:flex;flex-direction:column;
  background:linear-gradient(180deg,rgba(14,20,16,.92),rgba(9,13,10,.9));backdrop-filter:blur(14px);border-left:1px solid rgba(200,207,216,.14)}
.rail.collapsed{display:none}
.rail-hd{display:flex;align-items:center;gap:9px;padding:16px 18px;font-family:var(--disp);font-size:12.5px;font-weight:600;letter-spacing:.08em;color:var(--silver-h);border-bottom:1px solid rgba(200,207,216,.1)}
.rail-hd .live{width:8px;height:8px;border-radius:50%;background:var(--emerald);box-shadow:0 0 8px var(--glow-e);animation:pulse 1.4s ease-in-out infinite}
.rail-hd .chev{margin-left:auto;color:var(--mut);cursor:pointer;font-size:12px}
.rail-hd .chev:hover{color:var(--silver-h)}
/* global-mode feed filter (Active · All) — 2-state, default All */
.feed-scope{display:inline-flex;align-items:center;gap:6px;margin-left:6px}
.feed-scope-name{font-size:10px;color:var(--mut);letter-spacing:0;text-transform:none;font-weight:500;max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.feed-scope button{font-family:var(--disp);font-size:10px;font-weight:600;color:var(--silver);cursor:pointer;background:rgba(200,207,216,.06);border:1px solid rgba(200,207,216,.2);border-radius:7px;padding:3px 8px;transition:.16s}
.feed-scope button.active{color:#04301c;background:linear-gradient(120deg,var(--gold-d),var(--gold) 55%,var(--gold-h));border-color:transparent}
.feed-scope button:disabled{opacity:.4;cursor:not-allowed}
.rail-building{padding:9px 18px;background:rgba(227,179,65,.08);border-bottom:1px solid rgba(200,207,216,.1);font-size:12px;color:#e3b341}
.newpill{margin:10px auto 0;background:rgba(24,201,138,.16);border:1px solid rgba(24,201,138,.4);color:var(--gold-h);border-radius:20px;padding:4px 14px;font-size:11px;cursor:pointer;width:max-content}
.feed{flex:1;overflow-y:auto;padding:12px 16px;min-height:0}
.frow{border:1px solid rgba(200,207,216,.1);border-radius:10px;padding:10px 13px;margin-bottom:9px;background:rgba(255,255,255,.03)}
.ftop{display:flex;align-items:center;gap:9px;margin-bottom:5px}
.fbadge{font-size:9.5px;font-weight:700;letter-spacing:.5px;padding:2px 8px;border-radius:5px}
.fbadge.note{background:rgba(227,179,65,.16);color:#e3b341}
.fbadge.doc{background:rgba(90,169,255,.14);color:#8fc0ff}
.fproj{font-size:10.5px;color:var(--mut);margin-left:auto}
.fbody{font-size:12.5px;color:var(--ink)}
.fmeta{font-size:10.5px;color:var(--faint);margin-top:5px;font-variant-numeric:tabular-nums}
/* collapsed-rail EXPAND TAB — slim vertical strip pinned to the right edge
   (fixed: outside #stage, untouched by the draw() loop). The pair: collapse
   from the rail header's ⟩ chevron, expand from this tab. */
.rail-tab{position:fixed;right:0;top:50%;transform:translateY(-50%);z-index:8;cursor:pointer;
  writing-mode:vertical-rl;text-orientation:mixed;padding:16px 8px;border-radius:12px 0 0 12px;
  font-family:var(--disp);font-size:12px;font-weight:600;letter-spacing:.1em;color:#04301c;
  background:linear-gradient(180deg,var(--gold-d),var(--gold) 55%,var(--gold-h));
  border:1px solid rgba(24,201,138,.5);border-right:none;
  box-shadow:-8px 0 22px -8px var(--glow-g),inset 0 1px 0 rgba(255,255,255,.35)}
.rail-tab:hover{filter:brightness(1.12)}

/* ── Tutorial dropdown — the discoverable onboarding entry (owner request):
      one labeled toolbar control, two entries: guided tour + Command List.
      The tour entry IS the historical help-btn (same id, same tourStart()). ── */
/* position:FIXED, anchored in JS: an absolute dropdown inside .stage-wrap gets
   cut by its overflow:hidden at short windows (the clipped area then belongs
   to the dock — item unclickable). Fixed escapes the clip, like the tour card. */
.tut-dd{position:fixed;z-index:20;min-width:280px;border-radius:12px;overflow:hidden;
  background:linear-gradient(180deg,rgba(16,22,18,.98),rgba(9,13,10,.97));border:1px solid rgba(200,207,216,.18);box-shadow:var(--shadow)}
.td-item{display:block;width:100%;text-align:left;padding:10px 14px;cursor:pointer;border:none;background:transparent;
  border-bottom:1px solid rgba(200,207,216,.06);color:var(--silver-h);font-family:var(--disp);font-size:12.5px;font-weight:600}
.td-item:last-child{border-bottom:none}
.td-item:hover{background:rgba(200,207,216,.08)}
.td-sub{display:block;font-size:10.5px;color:var(--mut);font-weight:500;margin-top:2px}

/* ── toolbar numbered-group separator (happy path | secondary actions) ── */
.tb-sep{width:1px;align-self:stretch;margin:2px 3px;background:rgba(200,207,216,.22)}

/* ── GUIDED TOUR (coach marks) — fixed body-level layer OUTSIDE #stage so the
      60fps draw() rebuild never touches it. The LAYER ignores pointer events
      (non-blocking: the real UI stays clickable underneath); only the card is
      interactive. The ring's giant box-shadow is the spotlight dim, and its
      .3s transition makes the ring TRAVEL between anchors. z-index 28: above
      the stage chrome (toolbar z3, overlay z6), below modals (30) so a wizard
      opened from a CTA covers it, below toasts (40). ── */
#tour{position:fixed;inset:0;z-index:28;pointer-events:none}
#tour-ring{position:fixed;border:2px solid var(--gold);border-radius:14px;
  box-shadow:0 0 0 9999px rgba(4,7,5,.55),0 0 22px var(--glow-g);
  transition:all .3s ease;pointer-events:none}
#tour-card{position:fixed;width:380px;max-width:92vw;pointer-events:all;
  background:linear-gradient(180deg,rgba(16,22,18,.98),rgba(9,13,10,.97));
  border:1px solid rgba(200,207,216,.18);border-radius:16px;box-shadow:var(--shadow);
  padding:16px 18px;transition:left .3s ease,top .3s ease}
.tour-dots{font-family:var(--disp);font-size:10.5px;font-weight:600;letter-spacing:.14em;color:var(--faint);text-transform:uppercase}
.tour-title{font-family:var(--disp);font-weight:600;font-size:16px;color:var(--silver-h);margin-top:4px}
.tour-body{font-size:12.5px;color:var(--ink);margin-top:8px;line-height:1.55}
.tour-body b{color:var(--gold-h)}
.tour-cta{margin-top:10px}
.tour-nav{display:flex;align-items:center;gap:8px;margin-top:14px}
.tour-nav .btn{font-size:11.5px;padding:6px 11px}
.tour-skip{margin-right:auto;font-size:11.5px;color:var(--faint);cursor:pointer}
.tour-skip:hover{color:var(--silver)}
</style>
</head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <filter id="goo"><feGaussianBlur in="SourceGraphic" stdDeviation="12" result="b"/><feColorMatrix in="b" mode="matrix" values="1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 0 20 -9"/></filter>
  <radialGradient id="nucG" cx="42%" cy="36%"><stop offset="0" stop-color="#5cf0bf"/><stop offset="46%" stop-color="#12b981"/><stop offset="100%" stop-color="#04301c"/></radialGradient>
  <radialGradient id="nucHover" cx="42%" cy="36%"><stop offset="0" stop-color="#c9ffe9"/><stop offset="46%" stop-color="#5cf0bf"/><stop offset="100%" stop-color="#18c98a"/></radialGradient>
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
<div class="main">
  <header>
    <div class="mark">
      <svg class="glyph" viewBox="0 0 48 48"><use href="#bcCell"/></svg>
      <div><div class="word">Brain<span class="c">Cell</span></div><div class="tag">memory map</div></div>
    </div>
    <label class="searchbar">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>
      <input id="global-q" placeholder="Search all memory…" oninput="draw()">
    </label>
    <div class="active-wrap" id="active-wrap">
      <button class="active-chip" id="active-chip" onclick="openActiveDropdown()" title="Active project — whose memory you're viewing. Click to switch.">…</button>
      <div class="active-dd" id="active-dd" style="display:none"></div>
    </div>
    <div class="scope-seg" id="scope-seg" role="group" aria-label="Memory scope">
      <button id="scope-project" onclick="setScope('project')" title="Only the active project's memory">This project</button>
      <button id="scope-family" onclick="setScope('family')" title="Federated family recall from the active project">Family</button>
      <button id="scope-all" class="active" onclick="setScope('all')" title="All projects — namespace-wide memory">All</button>
    </div>
    <div class="status" id="status-chips">
      <span class="chip"><span class="dot"></span>Mode <b id="c-mode">—</b></span>
      <span class="chip">Projects <b id="c-proj">—</b></span>
      <span class="chip">Families <b id="c-pool">—</b></span>
      <span class="chip" id="chip-writes" style="display:none"></span>
      <span class="chip" id="chip-embedder" style="display:none" onclick="embedderChipClick()"><span class="dot"></span><b id="chip-embedder-txt">—</b></span>
      <span class="chip" id="chip-job" style="display:none"><span class="dot"></span><b id="chip-job-txt">building…</b></span>
    </div>
  </header>

  <div class="stage-wrap">
    <!-- Numbered setup path first (Project connection, then optional grouping),
         then a separator, then the un-numbered secondary actions. "Build
         memory (no MCP)" is deliberately NOT numbered: the Add-project wizard
         already builds, so labeling build-only "2" would teach a redundant
         rebuild — it is an ALTERNATIVE to step 1, not its successor. -->
    <div class="toolbar">
      <button class="btn primary" id="add-repo-btn" onclick="openAddRepoModal()" title="Build memory, register the MCP, and optionally join a family">1 · ✚ Add project</button>
      <button class="btn" id="new-family-btn" onclick="newPool()" title="Create a family — an opt-in cross-project recall grouping">2 · ＋ New family</button>
      <span class="tb-sep"></span>
      <button class="btn" id="build-btn" onclick="openIngestModal()" title="Build memory only — no MCP registration">⬇ Build memory (no MCP)</button>
      <button class="btn" id="cmd-btn" onclick="openCommandsModal()" title="Every braincell command — what it does and where to run it">★ Commands</button>
      <button class="btn" onclick="relax()" title="Re-settle the map layout — spreads overlapping cells apart">↻ Re-tidy</button>
      <button class="btn" id="tut-btn" onclick="toggleTutorialMenu()" title="Learn the map — guided tour and command reference">🎓 Tutorial ▾</button>
      <button class="btn" id="rail-reopen" style="display:none" onclick="toggleRail()" title="Reopen the live memory feed">⟨ Live feed</button>
    </div>
    <svg class="stage" id="stage"></svg>
    <div class="legend">
      <b>Click a cell</b> to open its inspector along the bottom · cells wear their <b>family's color</b> (ring) — hover glows neon green ·
      <b>1 · ✚ Add project</b> builds a folder's memory and registers the MCP · <b>⬇ Build memory (no MCP)</b> builds only ·
      <b>drag a cell into a membrane</b> to add it to that family ·
      <b>drag it out</b> to remove ·
      <b>click a family's ◉ Pool now</b> to fuse it into the global brain.
      <br><b>New families save when you drop the first cell in.</b>
      <br><b>★ Commands</b> lists every braincell command with instructions, plus the
      maintenance tools (consolidate, reflect, contradictions, backup, undo…).
      <br><b>? Help</b> replays the guided tour.
    </div>

    <div id="overlay">
      <div class="overlay-inner"><b id="overlay-title">Loading…</b><span id="overlay-msg"></span></div>
    </div>

  </div>

  <!-- Inspector dock — bottom of the left column; populated on cell click.
       Keeps id="drawer" (the inspector's stable anchor) with the dock layout. -->
  <div class="dock" id="drawer">
    <div class="col c-head">
      <span class="close" onclick="closeDock()" title="Collapse the inspector (Esc)">✕</span>
      <div class="dr-name"><svg width="20" height="20" viewBox="0 0 48 48"><use href="#bcCell"/></svg><span id="dr-name">—</span></div>
      <div class="dr-ulid" id="dr-ulid"></div>
      <div class="dr-path" id="dr-path"></div>
      <div class="fam-tags" id="dr-fams"></div>
    </div>
    <div class="col c-stats">
      <div class="sec">Store</div>
      <div class="dr-stats">
        <div class="stat"><b id="dr-docs">0</b><span>docs</span></div>
        <div class="stat"><b id="dr-chunks">0</b><span>chunks</span></div>
        <div class="stat"><b id="dr-notes">0</b><span>notes</span></div>
      </div>
      <div class="dr-actions" id="dr-actions" style="display:none">
        <button class="btn" id="dr-rebuild-btn" onclick="reingestSelected()">⟳ Rebuild now</button>
        <button class="btn danger" id="dr-clear-btn" onclick="confirmClearSelected()">✕ Clear memory</button>
      </div>
      <div class="dr-sched" id="dr-sched" style="display:none">
        <span>Auto-build:</span>
        <select id="dr-sched-sel" onchange="scheduleSelected(this.value)">
          <option value="0">off</option>
          <option value="60">hourly</option>
          <option value="1440">daily</option>
          <option value="10080">weekly</option>
        </select>
        <span id="dr-sched-note" style="color:var(--faint)"></span>
      </div>
      <!-- MCP status & controls — per-project registration state + the honest
           restart instruction (there is no restart button ON PURPOSE: the MCP
           server is a stdio subprocess owned by the MCP client, not this GUI). -->
      <div class="sec" style="margin-top:12px">MCP</div>
      <div class="mcp-status" id="dr-mcp-status">—</div>
      <div class="dr-actions" id="dr-mcp-actions">
        <button class="btn" id="dr-mcp-register-btn" onclick="mcpRegisterSelected()">Register MCP</button>
        <button class="btn" id="dr-mcp-deregister-btn" onclick="mcpDeregisterSelected()">Deregister MCP</button>
      </div>
      <div class="mcp-note" id="dr-mcp-note">To restart the MCP server, reconnect in your client — run <b>/mcp</b> in Claude Code. The GUI cannot restart it; it runs inside your MCP client.</div>
    </div>
    <div class="col c-search">
      <div class="sec">Search this project</div>
      <div class="dr-search">
        <input id="dr-q" placeholder="Search this project's memory…" onkeydown="if(event.key==='Enter')drawerSearch()">
        <button class="btn" onclick="drawerSearch()">Go</button>
      </div>
      <div id="dr-hits-list"></div>
    </div>
    <div class="col c-notes">
      <div class="sec">Recent notes</div>
      <div id="dr-notes-list"></div>
    </div>
  </div>
</div>

<!-- Live memory feed — full-height right rail, flex sibling OUTSIDE the stage wrapper -->
<aside class="rail" id="feed-rail">
  <div class="rail-hd"><span class="live"></span> LIVE MEMORY
    <span class="feed-scope" id="feed-scope" style="display:none">
      <span class="feed-scope-name" id="feed-scope-name"></span>
      <button id="feed-scope-active" onclick="setFeedScope('active')" title="Only the active project's activity">Active</button>
      <button id="feed-scope-all" class="active" onclick="setFeedScope('all')" title="All projects — the namespace-wide stream">All</button>
    </span>
    <span class="chev" onclick="toggleRail()" title="Collapse the live feed — reopen from the ▸ Live feed tab on the right edge">⟩</span></div>
  <div class="rail-building" id="feed-building" style="display:none"></div>
  <div class="newpill" id="feed-newpill" style="display:none" onclick="feedFlushNew()">▲ 0 new</div>
  <div class="feed" id="feed-list"></div>
</aside>
</div>

<!-- Always-visible expand affordance while the rail is collapsed — the primary
     way back; the toolbar "⟨ Live feed" button remains as a secondary path. -->
<div class="rail-tab" id="rail-tab" style="display:none" onclick="toggleRail()" title="Expand the live memory feed">▸ Live feed</div>

<div class="toastwrap" id="toasts"></div>

<!-- Tutorial dropdown — BODY-level like the tour card and toasts: .stage-wrap's
     backdrop-filter makes it the containing block (and clip) for any fixed
     element inside it, and its stacking context can never paint above the
     dock. Anchored to #tut-btn by toggleTutorialMenu(). -->
<div class="tut-dd" id="tut-dd" style="display:none">
  <button class="td-item" id="help-btn" onclick="tourStart()" title="? Help — replay the guided tour">Tutorial<span class="td-sub">the guided tour of the map</span></button>
  <button class="td-item" id="tut-cmds" onclick="openCommandsModal()" title="Every braincell command — what it does and where to run it">Command List<span class="td-sub">every braincell command, explained</span></button>
</div>

<div id="modal-root" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <div class="mo-hd"><div class="mo-title" id="mo-title">—</div><div class="mo-sub" id="mo-sub"></div></div>
    <div class="mo-body" id="mo-body"></div>
    <div class="mo-ft" id="mo-ft"></div>
  </div>
</div>

<!-- Guided tour — coach-mark spotlight + card, body-level (OUTSIDE #stage: the
     draw() loop must never rebuild it). Non-blocking by construction: the
     layer is pointer-events:none, only the card is interactive. Replay entry
     point: the toolbar ? Help button. -->
<div id="tour" style="display:none">
  <div id="tour-ring"></div>
  <div id="tour-card">
    <div class="tour-dots" id="tour-dots"></div>
    <div class="tour-title" id="tour-title"></div>
    <div class="tour-body" id="tour-body"></div>
    <div class="tour-cta" id="tour-cta"></div>
    <div class="tour-nav">
      <span class="tour-skip" onclick="tourEnd(false)">Skip tour</span>
      <button class="btn" id="tour-back" onclick="tourBack()">← Back</button>
      <button class="btn primary" id="tour-next" onclick="tourNext()">Next →</button>
    </div>
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
/* cell outer ring — the cell's first family's hue at the given alpha */
function famRing(fi,a){const h=FAM_HUE[fi%FAM_HUE.length];return`rgba(${h[0]},${h[1]},${h[2]},${a})`;}

/* animated background medium colours */
const MEDIUM=[[24,201,138],[92,240,191],[10,70,44],[246,244,234],[200,207,216]];

/* ════════ APP STATE ════════ */
const stage=document.getElementById("stage");
let W=0,H=0,nodes=[],families=[],org={x:0,y:0,r:36},drag=null,selected=null;
/* JS-tracked hover: CSS :hover is unreliable on map cells (recreated every draw()
   frame), so pointermove records the hovered cell id and draw() renders it. */
let hoveredId=null;
let status={allow_writes:false,global_brain:{exists:false,path:""},mode:"project"};
let _loading=true,_initDone=false;

/* ════════ API HELPERS ════════ */
/* A4: the initial embedded navigation carries the auth token
   (?t=…). Carry it on every API call so the guarded /api/* routes accept us. */
const BC_TOKEN=new URLSearchParams(location.search).get("t");
function withTok(url){
  if(!BC_TOKEN)return url;
  return url+(url.includes("?")?"&":"?")+"t="+encodeURIComponent(BC_TOKEN);
}
/* A 401 means our cookie/token didn't authenticate — usually a renderer that
   outlived its server, or a stale per-instance cookie. Re-mint by loading /
   ONCE (GET / sets a fresh auth cookie), guarded one-shot per renderer session so a
   genuine failure can't loop and — critically — a 401 NEVER renders as an empty
   "wiped" map. Only after a reload still 401s do we surface the toast.
   credentials:"same-origin" ensures the auth cookie rides every call. */
let _staleTokenToasted=false,_reauthing=false;
function staleTokenToast(){
  if(_staleTokenToasted)return;
  _staleTokenToasted=true;
  toast("Session token invalid — relaunch braincell gui for a fresh one","err");
}
function on401(){
  /* true = a re-auth reload was kicked off; caller should bail quietly. */
  if(_reauthing)return true;                       /* reload already underway */
  if(sessionStorage.getItem("bc_reauth")){         /* already retried → real failure */
    staleTokenToast();return false;
  }
  _reauthing=true;
  sessionStorage.setItem("bc_reauth","1");
  location.replace("/");                            /* GET / sets the cookie → reload authenticates */
  return true;
}
function authOk(){sessionStorage.removeItem("bc_reauth");}
async function apiFetch(url){
  try{
    const r=await fetch(withTok(url),{credentials:"same-origin"});
    if(!r.ok){
      if(r.status===401)on401();
      console.error("API",r.status,url);return null;
    }
    authOk();
    return await r.json();
  }catch(e){console.error("fetch err",url,e);return null;}
}
async function apiPost(url,body){
  try{
    const r=await fetch(withTok(url),{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    if(!r.ok){
      if(r.status===401){on401();throw new Error("401: session expired — reloading");}
      let msg=r.statusText;try{const j=await r.json();msg=j.detail||JSON.stringify(j);}catch(_){}
      throw new Error(`${r.status}: ${msg}`);
    }
    authOk();
    return await r.json();
  }catch(e){throw e;}
}
/* Like apiFetch, but surfaces the backend's 404 "not built" answer (a sibling
   project whose brain doesn't exist yet) as {notBuilt:true} instead of
   collapsing it into null — the drawer maps it to an honest empty state. */
async function apiFetchView(url){
  try{
    const r=await fetch(withTok(url),{credentials:"same-origin"});
    if(r.status===404)return {notBuilt:true};
    if(!r.ok){
      if(r.status===401)on401();
      console.error("API",r.status,url);return null;
    }
    authOk();
    return await r.json();
  }catch(e){console.error("fetch err",url,e);return null;}
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
    loadDrawerNotes();
    const dq=document.getElementById("dr-q");
    if(dq&&dq.value.trim())drawerSearch();
  }
}
/* Query fragment for the active scope. Family sends federate=true WITHOUT
   projects= — the API ignores projects when federate=true (documented sharp
   edge), so the two are never combined. Both branches follow the ACTIVE
   project; family additionally re-seeds the fan-out when active ≠ launch. */
function scopeParams(){
  if(scopeMode==="family"&&federateAvailable){
    let p="&federate=true";
    if(activeProjectId&&activeProjectId!==seedProjectId)
      p+="&seed="+encodeURIComponent(activeProjectId);
    return p;
  }
  if(scopeMode==="project")return "&projects="+encodeURIComponent(activeProjectId||seedProjectId||"");
  return "";
}

/* ════════ ACTIVE PROJECT (viewing) vs LAUNCH PROJECT (managing) ════════
   activeProjectId = whose memory the GUI is showing: map focus, inspector,
   drawer notes/search, and — in global mode — the feed filter. Switching it is
   a VIEW change only; writes stay pinned to the launch project's opened store.
   Init: ?active= URL param → launch seed → null (rides the URL like ?scope=,
   so a view is shareable/bookmarkable). */
let activeProjectId=null,_activeInit=false;
const _urlActive=new URLSearchParams(location.search).get("active");
const RO_VIEW_TITLE="read-only view — launch braincell gui on this folder to manage it";
function isLaunch(){
  /* True when the write endpoints act on the memory being viewed. Global mode:
     the opened global db holds every project's rows — nothing is a read-only
     sibling. Seedless project-mode apps (tests/embedded) have no launch
     project to pin to, so they keep today's behavior. */
  if(status.mode!=="project")return true;
  if(!seedProjectId)return true;
  return activeProjectId===seedProjectId;
}
function renderActiveChip(){
  const b=document.getElementById("active-chip");
  if(!b)return;
  if(!activeProjectId){b.innerHTML=`All projects ▾`;return;}
  const nd=nodes.find(n=>n.id===activeProjectId);
  const name=nd?nd.name:activeProjectId;
  const uid=activeProjectId;
  const su=uid.length>8?uid.slice(0,6)+"…"+uid.slice(-2):uid;
  const launch=status.mode==="project"&&seedProjectId&&activeProjectId===seedProjectId;
  const home=launch?`<span class="ac-home" title="Launch project — writes stay here">⌂</span> `:"";
  const ro=(status.mode==="project"&&seedProjectId&&!launch)?` <span class="ac-ro" title="${RO_VIEW_TITLE}">RO</span>`:"";
  b.innerHTML=`${home}${esc(name)} · ${esc(su)}${ro} ▾`;
}
function openActiveDropdown(){
  const dd=document.getElementById("active-dd");
  if(!dd)return;
  if(dd.style.display!=="none"){dd.style.display="none";return;}
  /* launch project pinned first, then by name */
  const rows=[...nodes].sort((a,b)=>{
    if(a.id===seedProjectId)return -1;
    if(b.id===seedProjectId)return 1;
    return a.name.localeCompare(b.name);
  });
  let html="";
  if(status.mode==="global")
    html+=`<div class="ad-item${!activeProjectId?" cur":""}" onclick="setActiveProject(null)"><span class="ad-name">All projects</span><span class="ad-meta">namespace-wide</span></div>`;
  html+=rows.map(n=>{
    const fams=famOf(n.id);
    const launch=status.mode==="project"&&seedProjectId&&n.id===seedProjectId;
    return `<div class="ad-item${n.id===activeProjectId?" cur":""}" onclick="setActiveProject('${esc(n.id).replace(/'/g,"\\'")}')">
      <span class="ad-name">${launch?"⌂ ":""}${esc(n.name)}</span>
      <span class="ad-meta">${esc(n.path)}</span>
      <span class="ad-meta">${Number(n.docs)} docs · ${Number(n.chunks)} chunks · ${Number(n.notes)} notes${fams.length?" · "+fams.map(esc).join(", "):""}</span>
    </div>`;
  }).join("");
  dd.innerHTML=html||`<div class="ad-item" style="cursor:default"><span class="ad-meta">No projects registered yet.</span></div>`;
  dd.style.display="";
}
/* click-away closes the dropdown (clicks inside #active-wrap don't) */
addEventListener("click",e=>{
  const dd=document.getElementById("active-dd");
  if(dd&&dd.style.display!=="none"&&!e.target.closest("#active-wrap"))dd.style.display="none";
});
/* ── Tutorial dropdown (mirrors the active-project dropdown pattern) ── */
function toggleTutorialMenu(){
  const dd=document.getElementById("tut-dd");
  if(dd.style.display!=="none"){dd.style.display="none";return;}
  dd.style.display="";
  /* fixed-position anchor: below the button, flipped above when the window is
     too short, clamped to the right edge — never clipped, never off-screen */
  const b=document.getElementById("tut-btn").getBoundingClientRect();
  const w=dd.offsetWidth,h=dd.offsetHeight;
  dd.style.left=Math.max(8,Math.min(b.left,innerWidth-w-8))+"px";
  dd.style.top=((b.bottom+6+h<=innerHeight-8)?b.bottom+6:Math.max(8,b.top-h-6))+"px";
}
addEventListener("click",e=>{
  const dd=document.getElementById("tut-dd");
  if(!dd||dd.style.display==="none")return;
  /* the trigger's own onclick already toggled — closing here would re-open */
  if(e.target.closest("#tut-btn"))return;
  dd.style.display="none";   /* outside click AND item clicks (post-onclick) */
});
/* fixed-position anchor goes stale when the window resizes — close; the next
   open recomputes it against the button's new position */
addEventListener("resize",()=>{
  const dd=document.getElementById("tut-dd");
  if(dd&&dd.style.display!=="none")dd.style.display="none";
});
function setActiveProject(pid){
  activeProjectId=pid||null;
  const dd=document.getElementById("active-dd");
  if(dd)dd.style.display="none";
  /* ?active= rides the internal URL beside ?scope= and ?t= */
  const u=new URL(location.href);
  if(activeProjectId)u.searchParams.set("active",activeProjectId);
  else u.searchParams.delete("active");
  history.replaceState(null,"",u.toString());
  renderActiveChip();
  feedFilterSync();
  /* re-run the open drawer view immediately (same pattern as setScope);
     following the active node keeps header + notes on one project */
  const dk=document.getElementById("drawer");
  if(dk&&dk.classList.contains("open")){
    const nd=nodes.find(n=>n.id===activeProjectId);
    if(nd&&selected!==nd.id)openDock(nd);
    else{
      paintInspectorRo();
      loadDrawerNotes();
      const dq=document.getElementById("dr-q");
      if(dq&&dq.value.trim())drawerSearch();
    }
  }
  draw();
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
  /* active project init runs ONCE — later loadAll() calls (post-build etc.)
     must not clobber an in-session switch. launch = launch_project_id (alias
     of the seed, /api/config Phase D); ?active= wins for restored state. */
  if(!_activeInit){
    _activeInit=true;
    activeProjectId=_urlActive||(cfg&&cfg.launch_project_id)||seedProjectId||null;
  }
  applyScopeAvailability();
  buildModel(projs||[],fams||[]);
  renderActiveChip();
  feedFilterSync();
  updateStatusChips();
  paintWriteButtons();
  paintEmbedderChip();
  _loading=false;
  paintOverlayState();
  /* first-run guided tour — checked once per page load, after the model is
     built (the predicate needs nodes + allow_writes + /api/config.suggest_tour) */
  maybeAutoStartTour(!!(cfg&&cfg.suggest_tour),!!(cfg&&cfg.tour_seen));
  loadSchedules();
  startFeedPoll();   /* live memory feed rail (idempotent — one interval) */
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
    /* mcp_registered: true|false from /api/projects; anything else = unknown */
    const mcpReg=(typeof p.mcp_registered==="boolean")?p.mcp_registered:null;
    return{id:uid,name:basename,path:pathStr,shortUlid,docs:p.docs||0,chunks:p.chunks||0,notes:p.notes||0,mcp_registered:mcpReg,x:base.x,y:base.y,vx:base.vx,vy:base.vy,r:17,pin:false};
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
  org={x:W-110,y:H/2,r:38};
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
  /* global organism — silver specular rim over gold core (owner request).
     Decorative-but-honest: never clickable, and when no global brain exists it
     dims (.org-missing) with a title saying how to create one. */
  const orgMissing=!(status.global_brain&&status.global_brain.exists);
  s+=orgMissing
    ?`<g class="org-missing"><title>no global brain — run braincell build --mode global</title>`
    :`<g>`;
  s+=`<circle cx="${org.x}" cy="${org.y}" r="${org.r+20}" fill="rgba(200,207,216,.06)"/>`;
  s+=`<circle cx="${org.x}" cy="${org.y}" r="${org.r+13}" fill="none" stroke="rgba(200,207,216,.22)" stroke-width="1"/>`;
  s+=`<circle cx="${org.x}" cy="${org.y}" r="${org.r+7}" fill="none" stroke="rgba(238,242,246,.4)" stroke-width="1.3"/>`;
  s+=`<circle cx="${org.x}" cy="${org.y}" r="${org.r+2}" fill="none" stroke="rgba(24,201,138,.6)" stroke-width="1.5"/>`;
  s+=`<circle cx="${org.x}" cy="${org.y}" r="${org.r}" fill="url(#orgG)"/>`;
  s+=`<text class="org-label" x="${org.x}" y="${org.y+org.r+18}">GLOBAL BRAIN</text>`;
  s+=`</g>`;
  /* project cells: green nucleus + family-colored outer ring.
     Ring = first family's hue (~.85 alpha); no family → dimmed silver (~.35).
     Hover (JS-tracked hoveredId) is the primary interactive signal: neon
     nucleus (#nucHover) + halo + ring alpha 1. Precedence: hover > selected
     > family-base. */
  nodes.forEach(nd=>{
    const dim=q&&!(nd.name.toLowerCase().includes(q));const op=dim?.22:1;
    const sel=selected===nd.id,hov=hoveredId===nd.id,act=activeProjectId===nd.id;
    const fi=families.findIndex(f=>f.members.has(nd.id));
    const ring=fi>=0?famRing(fi,hov?1:.85):`rgba(200,207,216,${hov?1:.35})`;
    let halo="";
    if(hov)halo=`<circle r="${nd.r+9}" fill="rgba(92,240,191,.14)"/><circle r="${nd.r+4}" fill="none" stroke="rgba(201,255,233,.55)" stroke-width="1.4"/>`;
    else if(sel&&!act)halo=`<circle r="${nd.r+7}" fill="rgba(200,207,216,.12)"/>`;
    /* ACTIVE treatment (C3): persistent emerald ring + soft glow — steady,
       distinct from hover's neon flash — plus an ACTIVE label under the name */
    if(act)halo=`<circle r="${nd.r+12}" fill="rgba(24,201,138,.09)"/><circle r="${nd.r+6}" fill="none" stroke="rgba(24,201,138,.85)" stroke-width="1.7"/>`+halo;
    const actLabel=act?`<text class="cell-active-label" y="${nd.r+27}">ACTIVE</text>`:"";
    s+=`<g class="cell-g" data-id="${esc(nd.id)}" transform="translate(${nd.x.toFixed(1)},${nd.y.toFixed(1)})" opacity="${op}">`+
       `<circle r="${nd.r+7}" fill="transparent"/>`+halo+
       `<circle r="${nd.r}" fill="none" stroke="${ring}" stroke-width="1.8"/>`+
       `<circle r="${nd.r-2}" fill="none" stroke="rgba(238,242,246,.18)" stroke-width="1"/>`+
       `<circle r="${nd.r-4}" fill="url(#${hov?"nucHover":"nucG"})"/>`+
       `<text class="cell-label" y="${nd.r+15}">${esc(nd.name)}</text>`+actLabel+`</g>`;});
  stage.innerHTML=s;
}
function loop(){step();draw();requestAnimationFrame(loop);}

/* ════════ POINTER / DRAG ════════ */
function svgPt(e){const m=stage.getScreenCTM().inverse();return new DOMPoint(e.clientX,e.clientY).matrixTransform(m);}
let dragMoved=false;
stage.addEventListener("pointerdown",e=>{const g=e.target.closest(".cell-g");if(!g)return;const nd=nodes.find(n=>n.id===g.dataset.id);if(!nd)return;drag=nd;nd.pin=true;dragMoved=false;hoveredId=null;stage.setPointerCapture(e.pointerId);});
stage.addEventListener("pointermove",e=>{
  if(!drag){
    /* hover tracking — draw() renders hoveredId (CSS :hover can't survive the
       per-frame innerHTML rebuild) */
    const g=e.target.closest(".cell-g");
    hoveredId=g?g.dataset.id:null;
    return;
  }
  /* 8px threshold: below it a press is a CLICK (open inspector), not a drag */
  const p=svgPt(e);const dx=p.x-drag.x,dy=p.y-drag.y;if(Math.hypot(dx,dy)>8)dragMoved=true;drag.x=p.x;drag.y=p.y;});
stage.addEventListener("pointerleave",()=>{hoveredId=null;});
stage.addEventListener("pointerup",async e=>{
  if(!drag)return;const nd=drag;drag=null;nd.pin=false;
  /* click = activate + inspect (one gesture, one concept — C3) */
  if(!dragMoved){setActiveProject(nd.id);openDock(nd);return;}
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
          toast(`Failed to add to family: ${err.message}`,"err");
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
        toast(`Removed ${nd.name} from families (read-only; not saved)`);
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

/* ════════ FOLDER NAVIGATOR (/api/fs + Qt folder dialog) ════════ */
let fsCur="";
/* The Qt dialog is the direct path; /api/fs remains an embedded navigator.
   Once the bridge reports unavailable, every fsHtml() render hides the button. */
let nativePickerDisabled=false;
function fsHtml(){
  return `<div class="fs-bar">
    <div class="fs-path" id="fs-path">…</div>
    <button class="btn" onclick="fsUp()" title="Up one level">↑</button>
    <button class="btn" onclick="fsGo('')" title="Home">⌂</button>
    ${nativePickerDisabled?"":`<button class="btn" id="fs-native-btn" onclick="pickFolderNative()" title="Open the system folder picker">📁 Browse…</button>`}
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
      if(list)list.innerHTML=`<div class="fs-empty">Folder selected.</div>`;
    } else if(res.unavailable){
      nativePickerDisabled=true;
      if(btn)btn.remove();
      toast(res.reason?`Folder picker unavailable (${res.reason}) — use the folder navigator below`:"Folder picker unavailable — use the folder navigator below","err");
      fsGo(fsCur||"");
    }
    /* {cancelled:true} → no-op, just fall through to re-enable the button */
  }catch(err){
    toast(`Folder picker failed: ${err.message}`,"err");
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
/* ── Embedder status (from /api/status.embedder) + Build gate ──────────────────
   Building while the embedder is down "succeeds" with every chunk NULL-embedded
   (invisible to semantic search) — so Build actions refuse-with-fix instead.
   Gate fires ONLY on an explicit embedder.ok===false; an absent field (older
   server, test app) never blocks. The chip repaints from /api/status on every
   loadAll — server is ground truth. */
function paintEmbedderChip(){
  const chip=document.getElementById("chip-embedder"),txt=document.getElementById("chip-embedder-txt");
  if(!chip||!txt)return;
  const e=status.embedder;
  if(!e){chip.style.display="none";return;}
  chip.style.display="";
  if(e.ok){
    chip.classList.remove("bad");
    txt.textContent=e.model?`Embedder ${e.model}`:"Embedder ready";
    chip.title="Local embedder reachable — Build and semantic recall are live.";
  }else{
    chip.classList.add("bad");
    txt.textContent="⚠ Embedder down";
    chip.title=(e.detail||"Embedder unreachable")+" — click for the fix";
  }
}
function embedderChipClick(){
  const e=status.embedder;
  if(e&&!e.ok)openEmbedderFixModal();
}
function openEmbedderFixModal(){
  const e=status.embedder||{};
  openModal("Embedder not ready",esc(e.detail||"The local embedder is unreachable."),
    `<div style="font-size:13px;color:var(--ink)">
       Build and semantic recall need the local embedder — without it a build would
       produce chunks with no vectors, invisible to semantic search.<br><br>
       <b>Fix:</b><br>
       1. Install Ollama — <code>https://ollama.com</code><br>
       2. Pull the model: <code>ollama pull ${esc(e.model||"")}</code><br><br>
       Then reload this page.
     </div>`,
    `<button class="btn" onclick="closeModal()">Close</button>`);
}
function requireEmbedder(){
  const e=status.embedder;
  if(e&&!e.ok){
    toast(`Embedder not ready — Build refused. Install Ollama, then run: ollama pull ${e.model||""}`,"err");
    return false;
  }
  return true;
}
function openIngestModal(){
  if(!requireWrites())return;
  if(!requireEmbedder())return;
  openModal("Build memory (no MCP)","Pick the folder your project lives in — BrainCell absorbs its memory. This does NOT register the MCP server; use 1 · ✚ Add project for the full setup.",
    fsHtml()+
    `<label style="display:flex;gap:8px;align-items:center;font-size:12.5px;color:var(--mut);margin-top:12px">
       <input type="checkbox" id="ing-reembed"> Rebuild embeddings from scratch (--reembed)
     </label>
     <label style="display:flex;gap:8px;align-items:center;font-size:12.5px;color:var(--mut);margin-top:8px">
       <input type="checkbox" id="ing-global"> Build into the global brain (--mode global)
     </label>`,
    `<button class="btn" onclick="closeModal()">Cancel</button>
     <button class="btn primary" onclick="startIngestFromModal()">⬇ Build this folder</button>`);
  fsGo("");
}
function startIngestFromModal(){
  const reembed=!!(document.getElementById("ing-reembed")||{}).checked;
  const globalMode=!!(document.getElementById("ing-global")||{}).checked;
  startIngest(fsCur,null,{reembed,global:globalMode});
}
let _pendingPool=null,_jobPoll=null;
async function startIngest(path,poolName,opts){
  if(!path){toast("Pick a folder first","err");return;}
  if(!requireEmbedder())return;   /* the funnel gate — covers Rebuild + family-build too */
  try{
    const body={path};
    if(opts&&opts.reembed)body.reembed=true;
    if(opts&&opts.global)body.mode="global";
    await apiPost("/api/ingest",body);
    _pendingPool=poolName||null;
    closeModal();
    toast(`Building ${path.split("/").pop()}…`);
    watchJob();
  }catch(err){toast(`Build failed to start: ${err.message}`,"err");}
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
    if(job.state==="running"){txt.textContent=`building ${base}…`;return;}
    clearInterval(_jobPoll);_jobPoll=null;chip.style.display="none";
    if(job.state==="done"){
      toast(`Build complete: ${base}`);
      const pool=_pendingPool;_pendingPool=null;
      await loadAll();
      if(pool){
        const nd=nodes.find(n=>n.path===job.path);
        if(nd){
          try{
            await apiPost("/api/family",{action:"add",name:pool,paths:[nd.path]});
            toast(`Added ${nd.name} → ${pool}`);
            await refreshFamilies();
          }catch(err){toast(`Family link failed: ${err.message}`,"err");}
        }
      }
    } else {
      _pendingPool=null;
      const tail=(job.log||[]).slice(-6).join("\n");
      toast(`Build failed (${base})`,"err");
      openModal("Build failed",esc(job.path),
        `<div class="joblog">${esc(tail||"no output captured")}</div>`,
        `<button class="btn" onclick="closeModal()">Close</button>`);
    }
  },1500);
}

/* ════════ NEW POOL (modal: name + optional project folder) ════════ */
function newPool(){
  openModal("New family","Name the family. Optionally pick a project folder to build straight into it.",
    `<div class="mo-label">Family name</div>
     <input class="mo-input" id="np-name" placeholder="e.g. web-stack" autofocus>
     <div class="mo-label">Project folder (optional)</div>
     ${fsHtml()}`,
    `<button class="btn" onclick="closeModal()">Cancel</button>
     <button class="btn" onclick="createPoolOnly()">Create empty family</button>
     <button class="btn primary" onclick="createPoolAndIngest()">Create &amp; build folder</button>`);
  fsGo("");
  setTimeout(()=>{const el=document.getElementById("np-name");if(el)el.focus();},50);
}
function _poolNameFromModal(){
  const el=document.getElementById("np-name");
  const n=el?el.value.trim():"";
  if(!n){toast("Give the family a name","err");return null;}
  return n;
}
function createPoolOnly(){
  const n=_poolNameFromModal();if(!n)return;
  if(!families.some(f=>f.name===n))families.push({name:n,members:new Set()});
  closeModal();
  toast(`Created family "${n}" — drop cells in to persist`);
  refreshCounts();
}
async function createPoolAndIngest(){
  const n=_poolNameFromModal();if(!n)return;
  if(!requireWrites())return;
  if(!fsCur){toast("Pick a project folder (or use Create empty family)","err");return;}
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
  openModal("Add a project — 1/4: Pick","Choose the folder for the project BrainCell should remember.",
    fsHtml(),
    `<button class="btn" onclick="closeModal()">Cancel</button>
     <button class="btn primary" onclick="arGoBuild()">Next: Build →</button>`);
  fsGo("");
}
function arGoBuild(){
  if(!fsCur){toast("Pick a folder first","err");return;}
  if(!requireEmbedder())return;   /* refuse before leaving the pick step */
  arPath=fsCur;
  arStepBuild();
}
async function arStepBuild(){
  if(!requireEmbedder())return;   /* the retry path re-enters here directly */
  arStep=2;
  openModal("Add a project — 2/4: Build",esc(arPath),
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
      openModal("Add a project — 2/4: Build failed",esc(arPath),
        `<div class="joblog">${esc(tail||"no output captured")}</div>`,
        `<button class="btn" onclick="closeModal()">Close</button>
         <button class="btn primary" onclick="arStepBuild()">Retry</button>`);
    }
  },1500);
}
function arStepInstall(){
  arStep=3;
  openModal("Add a project — 3/4: Register MCP",
    `Register the braincell MCP server for <b>${esc(arPath.split("/").pop())}</b>.`,
    `<div class="mo-label">MCP client</div>
     <select class="mo-input" id="ar-client">
       <option value="claude">claude</option>
       <option value="codex">codex</option>
     </select>
     <div class="mo-label">Scope</div>
     <select class="mo-input" id="ar-scope">
       <option value="local" selected>local</option>
       <option value="project">project</option>
     </select>
     <div id="ar-install-err" class="warn-note" style="display:none;margin-top:10px"></div>`,
    `<button class="btn" onclick="closeModal()">Cancel</button>
     <button class="btn primary" onclick="arDoInstall()">Register MCP →</button>`);
}
async function arDoInstall(){
  const client=document.getElementById("ar-client").value;
  const scope=document.getElementById("ar-scope").value;
  const errBox=document.getElementById("ar-install-err");
  try{
    const res=await apiPost("/api/install",{path:arPath,client,scope});
    arProjectId=res.project_id;arClient=client;
    arStepFamily();
  }catch(err){
    if(errBox){errBox.style.display="";errBox.textContent=err.message;}
  }
}
async function arStepFamily(){
  arStep=4;
  openModal("Add a project — 4/4: Family","Optional — most projects stay isolated. Group this project with siblings only if you want federated family recall across them.",
    `<div id="ar-fam-body"><div class="fs-empty">Loading projects…</div></div>`,
    `<button class="btn" onclick="arFinish()">Skip — keep isolated</button>
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
  openModal("Add project — done","",
    `<div style="font-size:13px;color:var(--ink)">Restart your MCP client (<b>${esc(arClient)}</b>) — or run /mcp in Claude Code — so it loads the braincell server.</div>`,
    `<button class="btn primary" onclick="arDone()">Done</button>`);
}
async function arDone(){
  closeModal();
  await loadAll();
  /* a dock-initiated Register leaves the inspector open — repaint its MCP
     status line from the server's fresh answer */
  if(selected){const nd=nodes.find(n=>n.id===selected);if(nd)openDock(nd);}
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
  openModal("Clear memory",`Wipe built docs &amp; chunks for <b>${esc(nd.name)}</b>? The next build re-absorbs everything fresh.`,
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
    if(nd2)openDock(nd2);
  }catch(err){toast(`Clear failed: ${err.message}`,"err");}
}
let _schedules=[];
async function loadSchedules(){
  if(!status.allow_writes)return;
  const data=await apiFetch("/api/schedule");
  _schedules=(data&&data.schedules)||[];
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
    toast(parseInt(minutes,10)>0?`Auto-build ${nd.name}: every ${minutes} min (while the map is open)`:`Auto-build off for ${nd.name}`);
    syncSchedUi(nd);
  }catch(err){toast(`Schedule failed: ${err.message}`,"err");}
}

/* ════════ POOL FAMILY (fusion sparks + POST /api/pool) ════════ */
async function poolFamily(fi){
  const f=families[fi];
  if(!f)return;
  if(!status.allow_writes){toast("Read-only: launch with --allow-writes","err");return;}
  if(!status.global_brain||!status.global_brain.exists){toast("No global brain — run `braincell build --mode global`","err");return;}
  if(!f.members.size){toast(`Family "${f.name}" is empty — add cells first`,"err");return;}
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

/* ════════ INSPECTOR DOCK (bottom of the left column) ════════ */
function openDock(nd){
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
  paintInspectorRo();
  paintMcpBlock(nd);
  document.getElementById("dr-hits-list").innerHTML="";
  document.getElementById("drawer").classList.add("open");
  size();   /* the dock changes the stage's height — resize the sim viewport */
  loadDrawerNotes();
}
/* C4 — read-only sibling view: active ≠ launch in project mode. The write
   endpoints act on the LAUNCH store only, so managing another project from
   here is structurally impossible; disable (never hide) with the explanatory
   title, mirroring the hook toggle's wdis convention. */
function paintInspectorRo(){
  const ro=!isLaunch();
  [["dr-rebuild-btn","Build this project's memory again now"],
   ["dr-clear-btn","Wipe built docs & chunks — the next build re-absorbs everything fresh"]]
  .forEach(([id,tip])=>{
    const b=document.getElementById(id);
    if(!b)return;
    b.disabled=ro;
    b.title=ro?RO_VIEW_TITLE:tip;
  });
  const sched=document.getElementById("dr-sched-sel");
  if(sched){sched.disabled=ro;sched.title=ro?RO_VIEW_TITLE:"";}
}

/* ── MCP status & controls (inspector dock) ────────────────────────────────────
   Registration state is read-only DETECTION: /api/status.mcp carries per-client
   detail for the launch project (null in global mode); every /api/projects entry
   carries a claude-client mcp_registered summary. The buttons reuse the existing
   flows — Register MCP = the Add-project wizard's Register-MCP step with the
   path prefilled; Deregister MCP = POST /api/uninstall. Both mount only under
   --allow-writes, so read-only disables (never hides) them, while the status
   line still renders. There is deliberately NO restart button: the MCP server
   is a stdio subprocess owned by the MCP client — the honest restart is
   reconnecting in the client (/mcp), and the note says exactly that. */
function mcpStatusText(nd){
  if(status.mcp&&status.mcp.path&&nd.path===status.mcp.path){
    const clients=status.mcp.clients||[];
    if(clients.length)
      return "● Registered for "+clients.map(c=>`${c.client} (${c.scope||"?"})`).join(", ");
    return "○ Not registered";
  }
  if(nd.mcp_registered===true)return "● Registered (claude)";
  if(nd.mcp_registered===false)return "○ Not registered";
  return "○ Registration unknown";
}
function paintMcpBlock(nd){
  const st=document.getElementById("dr-mcp-status");
  if(st)st.textContent=mcpStatusText(nd);   /* textContent — inert, no esc needed */
  [["dr-mcp-register-btn","Register the braincell MCP server for this project — opens the Register-MCP step"],
   ["dr-mcp-deregister-btn","Remove this project's braincell MCP registration from a client — the brain data is untouched"]]
  .forEach(([id,tip])=>{
    const b=document.getElementById(id);
    if(!b)return;
    if(status.allow_writes){b.disabled=false;b.title=tip;}
    else{b.disabled=true;b.title="read-only: launch with --allow-writes";}
  });
}
function mcpRegisterSelected(){
  const nd=nodes.find(n=>n.id===selected);if(!nd)return;
  if(!requireWrites())return;
  if(!nd.path){toast("This project has no registered path","err");return;}
  /* reuse the Add-project wizard at its Register-MCP step, path prefilled */
  arPath=nd.path;arProjectId=nd.id;arClient="claude";
  arStepInstall();
}
/* shared POST — the dock's Deregister modal and the Commands row both land here */
async function mcpDeregister(path,client,scope){
  const r=await apiPost("/api/uninstall",{path,client,scope});
  toast(`Disconnected from ${client}: BrainCell ${r.mcp_removed?"removed":"not removed"}`);
}
function mcpDeregisterSelected(){
  const nd=nodes.find(n=>n.id===selected);if(!nd)return;
  if(!requireWrites())return;
  if(!nd.path){toast("This project has no registered path","err");return;}
  openModal("Deregister MCP",
    `Remove <b>${esc(nd.name)}</b>'s braincell MCP registration from a client. The brain data itself is untouched.`,
    `<div class="mo-label">MCP client</div>
     <select class="mo-input" id="dm-client">
       <option value="claude">claude</option><option value="codex">codex</option><option value="vscode">vscode</option>
     </select>
     <div class="mo-label">Scope</div>
     <select class="mo-input" id="dm-scope">
       <option value="local">local</option><option value="project">project</option>
     </select>`,
    `<button class="btn" onclick="closeModal()">Cancel</button>
     <button class="btn danger" onclick="doDeregisterSelected()">Deregister MCP</button>`);
}
async function doDeregisterSelected(){
  const nd=nodes.find(n=>n.id===selected);if(!nd)return;
  const client=(document.getElementById("dm-client")||{}).value||"claude";
  const scope=(document.getElementById("dm-scope")||{}).value||"local";
  closeModal();
  try{
    /* VS Code removal is manual — the server's 409 instructions surface
       verbatim in the error toast below. */
    await mcpDeregister(nd.path,client,scope);
    await loadAll();   /* repaint registration state from the server's answer */
    const nd2=nodes.find(n=>n.id===nd.id);
    if(nd2)openDock(nd2);
  }catch(err){toast(`Deregister failed: ${err.message}`,"err");}
}
function closeDock(){selected=null;document.getElementById("drawer").classList.remove("open");size();}
/* Esc closes the topmost layer: an open modal first, then the tour, else the
   dock (the tour sits above the dock but below modals — same z-order). */
addEventListener("keydown",e=>{
  if(e.key!=="Escape")return;
  const mr=document.getElementById("modal-root");
  if(mr&&mr.classList.contains("open")){closeModal();return;}
  if(tourActive()){tourEnd(false);return;}
  const dk=document.getElementById("drawer");
  if(dk&&dk.classList.contains("open"))closeDock();
});

function renderFamTags(nd){
  const fams=famOf(nd.id);
  document.getElementById("dr-fams").innerHTML=fams.length
    ?fams.map(fn=>{
        const fi=families.findIndex(f=>f.name===fn);
        const h=FAM_HUE[fi%FAM_HUE.length];
        const col=`rgb(${h[0]},${h[1]},${h[2]})`;
        return `<span class="ftag" style="border-color:${col};color:${col}">◇ ${esc(fn)}<span class="ftag-x" onclick="removeFamTag('${esc(nd.id)}','${esc(fn)}')">✕</span></span>`;
      }).join("")
    :`<span class="ftag" style="opacity:.55">no family</span>`;
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
    toast("Removed from family (read-only; not saved)");
  }
}

/* honest empty state for a sibling whose brain doesn't exist yet (backend 404) */
function notBuiltHtml(){
  return `<div style="color:var(--faint);font-size:12px">Not built yet — <b>Build memory</b> to absorb this folder.</div>`;
}
async function loadDrawerNotes(){
  const data=await apiFetchView(`/api/notes?k=20${scopeParams()}`);
  if(!data)return;
  const list=document.getElementById("dr-notes-list");
  if(data.notBuilt){list.innerHTML=notBuiltHtml();return;}
  let html="";
  if(data.warning)html+=`<div class="warn-note">${esc(data.warning)}</div>`;
  if(!data.notes||!data.notes.length){
    list.innerHTML=html+`<div style="color:var(--faint);font-size:12px">No notes yet.</div>`;
    return;
  }
  const launchView=isLaunch();
  html+=data.notes.map(n=>{
    const ts=(n.created_at||"").slice(0,10);
    /* forget = the GUI face of `braincell forget` (soft-delete). Write-gated:
       the button renders only when writes are on (/api/forget is unmounted
       read-only, so a click would 404 anyway). On a read-only sibling view it
       renders DISABLED (never hidden) — writes act on the launch store only. */
    const del=status.allow_writes&&n.id!=null
      ?(launchView
        ?`<span class="ftag-x" style="float:right" title="Forget this note (soft-delete — hidden from recall, kept for audit)" onclick="confirmForgetNote(${Number(n.id)},'${esc(n.project_id||"").replace(/'/g,"\\'")}')">✕</span>`
        :`<span class="ftag-x" style="float:right;opacity:.35;cursor:not-allowed" title="${RO_VIEW_TITLE}">✕</span>`)
      :"";
    return `<div class="note"><div class="k">${esc(n.kind||"note")}${del}</div><div class="c">${esc(n.content)}</div><div class="m">conf ${n.confidence!=null?n.confidence:"—"} · ${ts}</div></div>`;
  }).join("");
  list.innerHTML=html;
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
    if(selected)loadDrawerNotes();
  }catch(err){toast(`Forget failed: ${err.message}`,"err");}
}

async function drawerSearch(){
  const nd=nodes.find(n=>n.id===selected);if(!nd)return;
  const q=document.getElementById("dr-q").value.trim();
  if(!q){document.getElementById("dr-hits-list").innerHTML="";return;}
  const data=await apiFetchView(`/api/search?q=${encodeURIComponent(q)}&k=20&mode=hybrid${scopeParams()}`);
  const el=document.getElementById("dr-hits-list");
  if(!data){el.innerHTML=`<div style="color:var(--faint);font-size:12px">Search unavailable.</div>`;return;}
  if(data.notBuilt){el.innerHTML=notBuiltHtml();return;}
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
function cmdFamOptions(){
  /* 0 families used to render a bare empty <select> (owner-reported: a tiny
     unlabeled box). Render an explanatory disabled placeholder instead; the
     select itself is also disabled (see openCommandsModal) and cmdPool's
     guard keeps Run from ever posting a blank family. */
  if(!families.length)
    return `<option value="">no families yet — ＋ New family creates one</option>`;
  return families.map(f=>`<option value="${esc(f.name)}">${esc(f.name)}</option>`).join("");
}
function cmdSelProj(){const s=document.getElementById("cmd-proj");return s?s.value:"";}
function cmdSelPath(){
  const s=document.getElementById("cmd-proj");
  const o=s&&s.selectedOptions&&s.selectedOptions[0];
  return o?(o.dataset.path||""):"";
}

function openCommandsModal(){
  const noProj=nodes.length?"":`<div class="fs-empty">No projects registered yet — build one first.</div>`;
  openModal("★ Commands","Every braincell command — what it does, and where to run it.",
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
         model <input class="mo-input" id="cmd-refl-model" placeholder="default" style="width:120px;padding:4px 8px">
         <label><input type="checkbox" id="cmd-refl-apply"> Apply (destructive)</label>
         <button class="btn"${wdis()} onclick="cmdReflect()">Run</button>
       </div></div>

     <div class="note"><div class="k">contradictions</div>
       <div class="c">READ-ONLY audit: pairs up embedding-close active notes and asks a local LLM
       whether each pair contradicts. Deliberately has no auto-fix — resolve findings yourself with
       supersede/forget. Slow with the LLM; tick "list only" to skip judging.</div>
       <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:6px;font-size:11.5px;color:var(--mut)">
         limit <input class="mo-input" id="cmd-ctr-limit" value="50" style="width:60px;padding:4px 8px">
         thr <input class="mo-input" id="cmd-ctr-th" placeholder="0.85" style="width:60px;padding:4px 8px">
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

     <div class="note"><div class="k">pool</div>
       <div class="c">Fuse a family's brains — or every project at once — into the global brain.
       "prune deleted" also removes global rows whose source rows were deleted since the last pool.
       (Each family's <b>◉ Pool now</b> button on the map does the plain single-family run.)</div>
       <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:6px;font-size:11.5px;color:var(--mut)">
         <select class="mo-input" id="cmd-pool-fam" style="width:auto;padding:4px 8px"${families.length?"":' disabled title="No families yet — create one with ＋ New family; the all-projects checkbox still works"'}>${cmdFamOptions()}</select>
         <label><input type="checkbox" id="cmd-pool-all"> all projects</label>
         <label><input type="checkbox" id="cmd-pool-prune"> prune deleted</label>
         <button class="btn"${wdis()} onclick="cmdPool()">Run</button>
       </div></div>

     <div class="note"><div class="k">Project skills</div>
       <div class="c">Adds BrainCell skills inside the Viewed project. Existing edited copies are
       never overwritten or removed.</div>
       <div style="display:flex;gap:8px;align-items:center;margin-top:6px">
         <select class="mo-input" id="cmd-skills-client" style="width:auto;padding:4px 8px">
           <option value="claude">Claude</option><option value="codex">Codex</option>
         </select>
         <button class="btn"${wdis()} onclick="cmdSkills('add')">Add skills</button>
         <button class="btn"${wdis()} onclick="cmdSkills('remove')">Remove skills</button>
       </div>
       <div class="fs-list" id="cmd-skills-list" style="max-height:120px;margin-top:6px;display:none"></div></div>

     <div class="note"><div class="k">memory log / undo</div>
       <div class="c">Recorded merge operations (consolidate/reflect applies). Undo restores each
       note's exact pre-merge state; notes changed since are skipped, never clobbered.</div>
       <div style="margin-top:6px"><button class="btn"${wdis()} onclick="cmdMemLog()">Load log</button></div>
       <div class="fs-list" id="cmd-mem-list" style="max-height:150px;margin-top:6px"></div></div>

     <div id="cmd-op-status" style="font-size:12px;color:var(--gold-h);margin-top:8px"></div>
     <div class="joblog" id="cmd-op-log" style="display:none"></div>

     <div class="mo-label">MCP status &amp; controls</div>

     <div class="note"><div class="k">Deregister MCP</div>
       <div class="c">Removes the Project's BrainCell connection from a client. Project memory is untouched. VS Code removal is manual —
       the server returns instructions. To restart the MCP server, reconnect in your client —
       run /mcp in Claude Code.</div>
       <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:6px;font-size:11.5px;color:var(--mut)">
         <select class="mo-input" id="cmd-un-client" style="width:auto;padding:4px 8px">
           <option value="claude">claude</option><option value="codex">codex</option><option value="vscode">vscode</option>
         </select>
         <select class="mo-input" id="cmd-un-scope" style="width:auto;padding:4px 8px">
           <option value="local">local</option><option value="project">project</option>
         </select>
         <button class="btn danger"${wdis()} onclick="cmdUninstall()">Deregister MCP</button>
       </div></div>

     <div class="note"><div class="k">restart GUI</div>
       <div class="c">Restarts THIS GUI server process only, then reloads the page. The braincell
       MCP server is a separate process — restart it in your MCP client (e.g. /mcp in Claude Code).</div>
       <div style="margin-top:6px"><button class="btn"${wdis()} onclick="cmdRestart()">↻ Restart GUI</button></div></div>

     <div class="mo-label">Already on the map</div>
     <div class="note"><div class="k">build / sync</div><div class="c">Build a project folder's transcripts into its brain (sync = the same incremental run) → toolbar <b>⬇ Build memory (no MCP)</b>, or a cell's <b>⟳ Rebuild now</b>.</div></div>
     <div class="note"><div class="k">search / recall</div><div class="c">search = ranked document chunks; recall = curated memory notes → click a cell: the drawer's search box and Recent notes (scope toggle applies).</div></div>
     <div class="note"><div class="k">forget</div><div class="c">Soft-delete one note → the ✕ on any note in the drawer's Recent notes (writes on).</div></div>
     <div class="note"><div class="k">family / pool</div><div class="c">Group projects and fuse them into the global brain → <b>＋ New family</b>, drag cells in/out, click a family's <b>◉ Pool now</b>.</div></div>
     <div class="note"><div class="k">install</div><div class="c">Wire a project into an MCP client (build → register MCP → family) → toolbar <b>1 · ✚ Add project</b> wizard.</div></div>
     <div class="note"><div class="k">stats / clear / schedule</div><div class="c">Store counts live in each cell's drawer header; <b>✕ Clear memory</b> and <b>Auto-build</b> sit right beside them.</div></div>

     <div class="mo-label">Run from the CLI</div>
     <div class="note"><div class="k">serve</div><div class="c">Runs the MCP stdio server process for a client — it is launched BY the MCP client (via install), not by this desktop app.</div></div>
     <div class="note"><div class="k">gui</div><div class="c">Starts this very app (<b>braincell gui --allow-writes</b> / braincell-map) — it cannot launch itself.</div></div>
     <div class="note"><div class="k">register</div><div class="c">Mints a project ULID without building — subsumed here by Build memory / Add project, which register automatically.</div></div>`,
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
  const model=((document.getElementById("cmd-refl-model")||{}).value||"").trim();
  const apply=!!(document.getElementById("cmd-refl-apply")||{}).checked;
  const body={project_id:pid,threshold:th,since_days:(since&&since>0)?since:null,model:model||null,apply};
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
  const thv=parseFloat(((document.getElementById("cmd-ctr-th")||{}).value||"").trim());
  const noLlm=!!(document.getElementById("cmd-ctr-nollm")||{}).checked;
  runOp("contradictions",{project_id:pid,limit,threshold:isNaN(thv)?null:thv,no_llm:noLlm});
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
/* pool --all/--prune — the maintenance face of POST /api/pool (the map's
   ◉ Pool now button stays the plain single-family run) */
async function cmdPool(){
  if(!requireWrites())return;
  const all=!!(document.getElementById("cmd-pool-all")||{}).checked;
  const fam=((document.getElementById("cmd-pool-fam")||{}).value||"");
  const prune=!!(document.getElementById("cmd-pool-prune")||{}).checked;
  if(!all&&!fam){toast("Pick a family (or tick all projects)","err");return;}
  const go=async()=>{
    try{
      const res=await apiPost("/api/pool",{family:all?null:fam,all_projects:all,prune});
      const pooled=res.pooled||[];
      const totNotes=pooled.reduce((a,p)=>a+(p.notes_copied||0),0);
      /* the endpoint has no top-level "pruned" — the real counts ride each
         PoolStats row (notes_pruned/docs_pruned); summing res.pruned always
         showed "pruned 0" no matter what was actually pruned */
      const pruned=pooled.reduce((a,p)=>a+(p.notes_pruned||0)+(p.docs_pruned||0),0);
      toast(`Pooled ${pooled.length} project(s) → global · ${totNotes} notes copied${prune?` · pruned ${pruned}`:""}`);
      await loadAll();
    }catch(err){toast(`Pool failed: ${err.message}`,"err");}
  };
  if(prune){
    cmdConfirm("Prune removes global-brain rows whose source rows were deleted since the last pool. Run pool with --prune?",go);
    return;
  }
  go();
}
async function cmdSkills(action){
  if(!requireWrites())return;
  const el=document.getElementById("cmd-skills-list");
  const nd=nodes.find(n=>n.id===selected);
  if(!nd||!nd.path){toast("Select a Viewed project first","err");return;}
  const client=((document.getElementById("cmd-skills-client")||{}).value||"claude");
  try{
    const r=await apiPost("/api/skills",{path:nd.path,client,action});
    const rows=Array.isArray(r)?r:((r&&(r.skills||r.results))||[]);
    if(el){
      el.style.display="";
      el.innerHTML=rows.length
        ?rows.map(s=>`<div class="fs-item" style="cursor:default"><span style="flex:1">${esc(s.name)} — ${esc(s.status)}${s.status==="conflict"?" · your copy left untouched — move it, then retry":""} · ${esc(s.path||"")}</span></div>`).join("")
        :`<div class="fs-empty">No skills returned.</div>`;
    }
    const conflicts=rows.filter(s=>s.status==="conflict").length;
    const verb=action==="remove"?"removed":"added";
    toast(conflicts?`Skills ${verb} with ${conflicts} conflict(s) — see the list`:`Skills ${verb} (${rows.length})`);
  }catch(err){toast(`Skills failed: ${err.message}`,"err");}
}
function cmdRestart(){
  if(!requireWrites())return;
  cmdConfirm("Restart the GUI server now? This restarts the GUI only — the MCP server restarts in your client (e.g. /mcp).",()=>{
    apiPost("/api/restart",{}).catch(()=>{});   /* the server may die mid-response */
    toast("Restarting…");
    const t0=Date.now();
    const poll=setInterval(async()=>{
      if(Date.now()-t0>30000){
        clearInterval(poll);
        toast("GUI did not come back within 30 s — restart it from the CLI","err");
        return;
      }
      const st=await apiFetch("/api/status");
      if(st){clearInterval(poll);location.reload();}
    },800);
  });
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
  cmdConfirm(`Remove this project's braincell MCP registration from ${client}? The brain data itself is untouched.`,
    async()=>{
      try{
        await mcpDeregister(path,client,scope);
        await loadAll();   /* refresh mcp_registered so the dock repaints honestly */
      }catch(err){toast(`Deregister failed: ${err.message}`,"err");}
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
/* Read-only affordance: the toolbar write buttons follow the wdis() pattern —
   disabled + explanatory title, instead of the click-then-fail toast. */
function paintWriteButtons(){
  [["add-repo-btn","Build memory, register the MCP, and optionally join a family"],
   ["build-btn","Build memory only — no MCP registration"],
   ["new-family-btn","Create a family — an opt-in cross-project recall grouping"]]
  .forEach(([id,tip])=>{
    const b=document.getElementById(id);
    if(!b)return;
    if(status.allow_writes){b.disabled=false;b.title=tip;}
    else{b.disabled=true;b.title="read-only: launch with --allow-writes";}
  });
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
/* Overlay state for the CURRENT model — extracted from loadAll so tourEnd can
   repaint it (the tour's welcome card supersedes the empty-state overlay while
   the tour runs; skipping with the map still empty brings the overlay back). */
function paintOverlayState(){
  if(tourActive()){hideOverlay();return;}
  if(status.mode==="global" && !(status.global_brain&&status.global_brain.exists)){
    showOverlay("No global brain yet","Run <code>braincell build --mode global</code>, then reload.");
  } else if(!nodes.length){
    showOverlay("No projects yet",status.allow_writes
      ?`Pick a project folder and BrainCell will absorb its memory.<br><button class="btn primary" onclick="openAddRepoModal()">✚ Add your first project</button>`
      :"Run braincell build <path> to index your first project.");
  } else {
    hideOverlay();
  }
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

/* ════════ LIVE MEMORY FEED (right rail — GET /api/feed) ════════
   Polls every 1500 ms with {after_note, after_doc} cursors; merges notes +
   documents newest-first. Scroll-anchored: at top (≤4px) new rows prepend,
   otherwise they buffer behind the "▲ N new" pill. A returned max cursor
   BELOW the stored one means the store was cleared → reset to a fresh tail. */
let feedCursors={after_note:0,after_doc:0};
let feedRows=[],feedBuffer=[],_feedPoll=null;
const FEED_CAP=200;

function feedRowHtml(r){
  if(r.t==="note"){
    const st=r.status&&r.status!=="active"?` · ${esc(r.status)}`:"";
    return `<div class="frow"><div class="ftop"><span class="fbadge note">NOTE · ${esc(r.kind||"note")}${st}</span><span class="fproj">${esc(r.project||"")}</span></div><div class="fbody">${esc(r.content||"")}</div><div class="fmeta">${esc(r.created_at||"")}</div></div>`;
  }
  /* DOC rows lead with the memory TEXT (preview = first-chunk text, ~280
     chars); the .jsonl doc key (title) demotes to the meta line. No preview
     → fall back to the title as body. */
  const pv=String(r.preview||"").trim();
  const body=pv||r.title||"";
  const titleCap=pv&&r.title?`${esc(r.title)} · `:"";
  return `<div class="frow"><div class="ftop"><span class="fbadge doc">DOC</span><span class="fproj">${esc(r.project||"")}</span></div><div class="fbody">${esc(body)}</div><div class="fmeta">${titleCap}+${Number(r.chunks||0)} chunks · ${esc(r.created_at||"")}</div></div>`;
}
function renderFeedPrepend(rows){
  feedRows=rows.concat(feedRows);
  if(feedRows.length>FEED_CAP)feedRows.length=FEED_CAP;   /* prune oldest */
  const list=document.getElementById("feed-list");
  if(list)list.innerHTML=feedRows.map(feedRowHtml).join("");
}
function updateNewPill(){
  const p=document.getElementById("feed-newpill");
  if(!p)return;
  if(feedBuffer.length){p.style.display="";p.textContent=`▲ ${feedBuffer.length} new`;}
  else p.style.display="none";
}
function feedFlushNew(){
  const rows=feedBuffer;feedBuffer=[];
  renderFeedPrepend(rows);updateNewPill();
  const list=document.getElementById("feed-list");
  if(list)list.scrollTop=0;
}
/* ── global-mode feed filter (C5): Active · All, default All ──
   Project-mode behavior unchanged (the opened db is already single-project),
   so the control shows — and the filter applies — in global mode only. */
let feedScope="all",_feedFilterPid=null;
function feedFilterParams(){
  if(status.mode==="global"&&feedScope==="active"&&activeProjectId)
    return "&projects="+encodeURIComponent(activeProjectId);
  return "";
}
function feedResetStream(){
  /* the filtered stream is a different tail — cursors must restart */
  feedCursors={after_note:0,after_doc:0};
  feedRows=[];feedBuffer=[];
  const list=document.getElementById("feed-list");
  if(list)list.innerHTML="";
  updateNewPill();
}
function setFeedScope(s){
  if(feedScope===s)return;
  feedScope=s;
  _feedFilterPid=activeProjectId;
  ["active","all"].forEach(m=>{
    const b=document.getElementById("feed-scope-"+m);
    if(b)b.classList.toggle("active",feedScope===m);
  });
  feedResetStream();
  feedPoll();
}
function feedFilterSync(){
  /* repaint the rail header for the current mode + active project; if the
     Active filter is on, an active-project switch re-tails the stream */
  const wrap=document.getElementById("feed-scope");
  if(!wrap)return;
  if(status.mode!=="global"){wrap.style.display="none";return;}
  wrap.style.display="";
  const nameEl=document.getElementById("feed-scope-name");
  if(nameEl){
    const nd=nodes.find(n=>n.id===activeProjectId);
    nameEl.textContent=nd?nd.name:"";
  }
  const ba=document.getElementById("feed-scope-active");
  if(ba){
    ba.disabled=!activeProjectId;
    ba.title=activeProjectId?"Only the active project's activity":"Pick an active project first";
  }
  if(feedScope==="active"&&_feedFilterPid!==activeProjectId){
    _feedFilterPid=activeProjectId;
    feedResetStream();
    feedPoll();
  }
}
async function feedPoll(){
  const data=await apiFetch(`/api/feed?after_note=${feedCursors.after_note}&after_doc=${feedCursors.after_doc}&k=30${feedFilterParams()}`);
  if(!data)return;
  const bn=document.getElementById("feed-building");
  if(bn){
    if(data.job&&data.job.state==="running"){
      bn.style.display="";
      bn.textContent=`⚙ building ${(data.job.path||"").split("/").pop()}… ${Number(data.job.done||0)}/${Number(data.job.total||0)} docs`;
    } else bn.style.display="none";
  }
  const cur=data.cursors||{};
  const maxNote=Number(cur.note||0),maxDoc=Number(cur.doc||0);
  if(maxNote<feedCursors.after_note||maxDoc<feedCursors.after_doc){
    /* a clear happened — reset; the next poll refetches the fresh tail */
    feedCursors={after_note:0,after_doc:0};
    feedRows=[];feedBuffer=[];
    const list=document.getElementById("feed-list");
    if(list)list.innerHTML="";
    updateNewPill();
    return;
  }
  feedCursors={after_note:maxNote,after_doc:maxDoc};
  const rows=[
    ...(data.notes||[]).map(n=>({t:"note",...n})),
    ...(data.documents||[]).map(d=>({t:"doc",...d})),
  ].sort((a,b)=>String(b.created_at||"").localeCompare(String(a.created_at||"")));
  if(!rows.length)return;
  const list=document.getElementById("feed-list");
  const atTop=!list||list.scrollTop<=4;
  if(atTop){renderFeedPrepend(rows);}
  else{feedBuffer=rows.concat(feedBuffer);updateNewPill();}
}
function startFeedPoll(){
  if(_feedPoll)return;
  feedPoll();
  _feedPoll=setInterval(feedPoll,1500);
}
/* rail collapse — persisted per session; size() lets the map reclaim the space */
function applyRailState(){
  let collapsed=false;
  try{collapsed=!!sessionStorage.getItem("bcRailCollapsed");}catch(_){}
  const rail=document.getElementById("feed-rail"),btn=document.getElementById("rail-reopen"),tab=document.getElementById("rail-tab");
  if(rail)rail.classList.toggle("collapsed",collapsed);
  if(btn)btn.style.display=collapsed?"":"none";
  if(tab)tab.style.display=collapsed?"":"none";   /* right-edge expand tab — primary reopen */
  size();
}
function toggleRail(){
  let collapsed=false;
  try{collapsed=!!sessionStorage.getItem("bcRailCollapsed");}catch(_){}
  try{
    if(collapsed)sessionStorage.removeItem("bcRailCollapsed");
    else sessionStorage.setItem("bcRailCollapsed","1");
  }catch(_){}
  applyRailState();
}

/* ════════ GUIDED TOUR (coach marks) ════════
   First-run walkthrough of the whole flow. Anchors are STABLE DOM only
   (toolbar buttons, the active-project chip, .stage-wrap, #feed-rail) — never
   elements inside #stage, which draw() rebuilds every frame. Auto-start:
   ?tour=1 (the `braincell start` first-run handoff) > ?tour=0 (suppress) >
   bcTourDone localStorage flag > allow_writes && (/api/config.suggest_tour ||
   empty map). The flag is set on Finish AND Skip — a skipper is never
   re-ambushed. All step copy is STATIC (no server-controlled strings, so no
   esc() sinks); a step that ever interpolates a project name must esc() it. */
const TOUR_STEPS=[
  {t:null,title:"Welcome to BrainCell",
   body:`Every <b>project folder</b> you register gets its own brain — a private memory built from its transcripts and documents. This map shows each brain as a cell. Two minutes of tour shows you the whole flow.`},
  {t:"#add-repo-btn",title:"1 · Add project — the full setup",
   body:`Pick a project folder, and BrainCell <b>Builds</b> its memory, <b>Registers the MCP</b> server so Claude Code's braincell tools work inside that folder, and optionally joins it to a <b>Family</b>. One wizard, four steps — this is the button for every folder you work in.`,
   cta:{label:"Open the wizard",run:"tourEnd(true);openAddRepoModal()"}},
  {t:"#build-btn",title:"Build only — no MCP",
   body:`This builds a folder's memory so you can search it <b>from this map and the CLI</b>, but wires nothing into an MCP client. Use it for reference material you only want to search — for folders where you'll actually run Claude Code, use <b>1 · Add project</b> instead.`},
  {t:"#active-chip",title:"This window has ONE active project",
   body:`You launched BrainCell from one project folder — the <b>active project</b>, shown in this chip and marked ACTIVE on the map. &ldquo;This project&rdquo; scopes search and notes to its brain; other cells are separate brains, viewed read-only here. Adding folders never merges memories — cross-project recall is always opt-in.`},
  {t:"#new-family-btn",title:"2 · New family — optional grouping",
   body:`A <b>Family</b> groups related project folders for cross-project recall. Brains stay physically separate — family recall fans out read-only and merges the ranking. Create one here, or drag a cell into a family's membrane on the map. Most projects are fine isolated — skipping this is the default.`},
  {t:".stage-wrap",rect:"right",title:"Pool — the only fuse",
   body:`<b>Pool</b> physically copies a family's brains into the separate global brain — the one action that merges data, and it's always an explicit click (<b>◉ Pool now</b> on a family). Until a global brain is built, that big cell stays dimmed.`},
  {t:"#feed-rail",alt:"#rail-tab",title:"Watch memory happen",
   body:`The live feed streams notes and documents as they land. Project memory starts with built documents; curated notes accrue as you work.<br><br>You're set — start with <b>1 · Add project</b>. Replay this tour anytime from <b>? Help</b>.`},
];
let tourStep=-1,_tourPoll=null,_tourChecked=false;
function tourActive(){return tourStep>=0;}
function tourStart(){
  tourStep=0;
  document.getElementById("tour").style.display="";
  hideOverlay();   /* the welcome card supersedes the empty-state overlay */
  tourShow();
  /* cheap 400ms watchdog keeps the ring glued through layout shifts (dock
     open/close, rail collapse) — one getBoundingClientRect per tick, no
     per-frame work */
  if(!_tourPoll)_tourPoll=setInterval(()=>{if(tourActive())tourReposition();},400);
}
function tourEnd(done){
  tourStep=-1;
  document.getElementById("tour").style.display="none";
  if(_tourPoll){clearInterval(_tourPoll);_tourPoll=null;}
  /* Finish AND Skip both set the flag — never re-ambush next launch. The
     server-side mark is the durable one (native webview localStorage does not
     persist); fire-and-forget, a failure just means one more auto-offer. */
  try{localStorage.setItem("bcTourDone","1");}catch(_){}
  try{apiPost("/api/tour-seen",{}).catch(()=>{});}catch(_){}
  /* strip ?tour=1 so a renderer reload doesn't force a re-run */
  const u=new URL(location.href);
  if(u.searchParams.has("tour")){u.searchParams.delete("tour");history.replaceState(null,"",u.toString());}
  if(done)burstAt(innerWidth/2,innerHeight/2);
  paintOverlayState();   /* empty map + skip → the overlay's CTA comes back */
}
function tourShow(){
  const s=TOUR_STEPS[tourStep];if(!s)return;
  document.getElementById("tour-dots").textContent=`Step ${tourStep+1} of ${TOUR_STEPS.length}`;
  document.getElementById("tour-title").textContent=s.title;
  document.getElementById("tour-body").innerHTML=s.body;
  /* write CTAs follow the standard wdis() gate (disabled + explanatory title
     in read-only) — the tour itself stays fully viewable */
  document.getElementById("tour-cta").innerHTML=s.cta
    ?`<button class="btn primary"${wdis()} onclick="${s.cta.run}">${s.cta.label}</button>`
    :"";
  document.getElementById("tour-back").style.display=tourStep>0?"":"none";
  document.getElementById("tour-next").textContent=
    tourStep===0?"Start the tour →":(tourStep===TOUR_STEPS.length-1?"Finish":"Next →");
  tourReposition();
}
function tourNext(){
  if(tourStep>=TOUR_STEPS.length-1){tourEnd(true);return;}
  tourStep++;tourShow();
}
function tourBack(){if(tourStep>0){tourStep--;tourShow();}}
function tourTargetRect(s){
  const pick=sel=>{
    const el=sel?document.querySelector(sel):null;
    if(!el)return null;
    const r=el.getBoundingClientRect();
    return (r.width||r.height)?r:null;   /* display:none → 0-size → try alt */
  };
  let r=pick(s.t);
  if(!r&&s.alt)r=pick(s.alt);
  if(!r)return null;
  /* rect:"right" — highlight the right slice of a big container (the GLOBAL
     BRAIN sits at the stage's right edge, but it lives INSIDE #stage, so the
     container slice is the closest stable anchor) */
  if(s.rect==="right")return{left:r.left+r.width*.66,top:r.top,width:r.width*.34,height:r.height};
  return{left:r.left,top:r.top,width:r.width,height:r.height};
}
function tourReposition(){
  const s=TOUR_STEPS[tourStep];if(!s)return;
  const ring=document.getElementById("tour-ring"),card=document.getElementById("tour-card");
  const r=tourTargetRect(s);
  const vw=innerWidth,vh=innerHeight;
  if(r){
    const pad=6;
    ring.style.left=(r.left-pad)+"px";ring.style.top=(r.top-pad)+"px";
    ring.style.width=(r.width+pad*2)+"px";ring.style.height=(r.height+pad*2)+"px";
    ring.style.borderWidth="2px";
  } else {
    /* centered step (welcome) — a zero-size cutout keeps the full dim */
    ring.style.left=(vw/2)+"px";ring.style.top=(vh/2)+"px";
    ring.style.width="0px";ring.style.height="0px";
    ring.style.borderWidth="0px";
  }
  const cw=Math.min(380,vw*.92),ch=card.offsetHeight||220;
  let cl,ct;
  if(r){
    cl=Math.max(12,Math.min(r.left,vw-cw-12));
    ct=r.top+r.height+16;
    if(ct+ch>vh-12)ct=Math.max(12,r.top-ch-16);
  } else {
    cl=(vw-cw)/2;ct=Math.max(12,(vh-ch)/2);
  }
  card.style.left=cl+"px";card.style.top=ct+"px";
}
function tourShouldAutoStart(suggest,seenServer){
  const p=new URLSearchParams(location.search).get("tour");
  if(p==="0")return false;
  if(p==="1")return true;   /* explicit handoff wins over the done-flag */
  let done=false;
  try{done=!!localStorage.getItem("bcTourDone");}catch(_){}
  if(done)return false;
  /* Server-persisted flag (/api/config.tour_seen, set by POST /api/tour-seen
     when the tour finishes or is skipped). This is the durable suppressor:
     localStorage alone dies with the native window's non-persistent webview
     profile, and !nodes.length made onboarding unreachable for anyone with a
     populated brain. First run on this machine => guide, once. */
  if(seenServer)return false;
  return status.allow_writes;
}
function maybeAutoStartTour(suggest,seenServer){
  if(_tourChecked)return;   /* later loadAll() calls (post-build) never retrigger */
  _tourChecked=true;
  if(tourShouldAutoStart(suggest,seenServer))tourStart();
}
addEventListener("resize",()=>{if(tourActive())tourReposition();});

/* ════════ SIZE + BOOT ════════ */
function size(){
  const r=stage.getBoundingClientRect();W=r.width;H=r.height;
  stage.setAttribute("viewBox",`0 0 ${W} ${H}`);
  /* keep the GLOBAL BRAIN org node visible in the (possibly narrower) stage */
  org.x=W-110;org.y=H/2;
}
addEventListener("resize",()=>{size();if(_initDone&&nodes.length){initNodes();}});
size();
applyRailState();
loadAll();
</script>
</body>
</html>
"""
