"""Browser front-end for mdedit: vendored asset config plus the single-page
HTML/CSS/JS viewer.

The viewer is a static HTML page rendered by :func:`build_html`. It loads two
vendored JS/CSS libraries (marked, highlight.js) so it works fully offline; if
a vendored file is absent the page falls back to its CDN URL. The manifest
(``VENDOR_ASSETS``) is the single source of truth consumed by
``mdedit.py vendor-manifest`` and ``update-vendor.sh``.

This module is pure (no I/O beyond stat-ing the vendor dir) so the asset
fallback logic and HTML shape are unit-testable directly.
"""

from __future__ import annotations

import json
from pathlib import Path

# Front-end libraries. These are loaded by the browser viewer to render markdown
# (marked) and syntax-highlight code (highlight.js). They are vendored next to
# this script under vendor/ so the viewer works fully offline; if a vendored
# file is missing we fall back to the CDN URL. `update-vendor.sh` (re)downloads
# the pinned versions below into vendor/. Keep this manifest in sync with that
# script — it is the single source of truth for versions, filenames and URLs.
VENDOR_DIR = Path(__file__).resolve().parent / "vendor"

# local filename -> CDN URL (used as fallback and by update-vendor.sh)
VENDOR_ASSETS = {
    "marked.min.js": "https://cdn.jsdelivr.net/npm/marked@12/marked.min.js",
    "highlight.min.js": "https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11/highlight.min.js",
    "highlight-onedark.min.css": "https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11/styles/base16/onedark.min.css",
}

# Browser MIME types for the vendored assets we serve.
_VENDOR_MIME = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


def _asset_url(filename: str) -> str:
    """Local /vendor URL if the file is vendored, else the CDN fallback URL."""
    if (VENDOR_DIR / filename).is_file():
        return f"/vendor/{filename}"
    return VENDOR_ASSETS[filename]


VALSTRO_CSS = r"""
:root {
  --bg-1000:#05080A; --bg-900:#0C1217; --bg-800:#151C22; --bg-700:#21282E;
  --bg-600:#30373D; --bg-500:#6E7881; --bg-400:#8B959E; --bg-300:#AFBBC5;
  --bg-200:#D2D9DF; --bg-100:#F0F7FC;
  --bb-500:#0071F0; --bb-400:#1C85FD; --bb-300:#7DB9FD; --bb-200:#B8D9FE;
  --bb-800:#002B5D; --bb-900:#002752;
  --green:#33E180; --yellow:#F7DDA1; --red:#EA6C6C; --red-500:#FD4545;
  --add-bg:rgba(51,225,128,0.14); --add-bd:rgba(51,225,128,0.45);
  --del-bg:rgba(234,108,108,0.16); --del-bd:rgba(234,108,108,0.45);
  --brand-gradient:linear-gradient(90deg,#65B1EF 0%,#744FDA 100%);
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
html{font-size:15px;}
body{
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  background:var(--bg-900);color:var(--bg-100);height:100vh;display:flex;flex-direction:column;overflow:hidden;
}

/* top bar */
#top-bar{
  display:flex;align-items:center;gap:14px;padding:10px 20px;background:var(--bg-800);
  border-bottom:1px solid var(--bg-700);flex-shrink:0;
}
#top-bar .logo-bar{width:36px;height:3px;background:var(--brand-gradient);border-radius:2px;}
#top-bar .doc-name{font-size:0.95rem;font-weight:600;color:var(--bg-100);}
#top-bar .meta{font-size:0.74rem;color:var(--bg-400);}
#top-bar .spacer{margin-left:auto;}

.toggle-group{display:flex;border:1px solid var(--bg-600);border-radius:6px;overflow:hidden;}
.toggle-group button{
  background:var(--bg-800);border:none;color:var(--bg-300);padding:6px 14px;font-size:0.78rem;
  cursor:pointer;transition:background .12s,color .12s;
}
.toggle-group button:hover{background:var(--bg-700);color:var(--bg-100);}
.toggle-group button.active{background:var(--bb-800);color:var(--bb-200);}

#send-btn{
  background:var(--bb-500);border:none;color:#fff;padding:7px 16px;border-radius:6px;font-size:0.82rem;
  font-weight:600;cursor:pointer;transition:background .12s;
}
#send-btn:hover{background:var(--bb-400);}
#send-btn:disabled{background:var(--bg-700);color:var(--bg-500);cursor:default;}

/* layout */
#body{flex:1;display:flex;overflow:hidden;}
#content-area{flex:1;overflow-y:auto;padding:34px 48px;position:relative;}
#content-area::-webkit-scrollbar{width:7px;}
#content-area::-webkit-scrollbar-thumb{background:var(--bg-700);border-radius:3px;}

/* comments sidebar */
#comments-panel{
  width:330px;min-width:260px;flex-shrink:0;background:var(--bg-1000);
  border-left:1px solid var(--bg-700);display:flex;flex-direction:column;overflow:hidden;
}
#comments-header{padding:13px 16px;border-bottom:1px solid var(--bg-700);font-size:0.78rem;
  font-weight:600;letter-spacing:0.06em;text-transform:uppercase;color:var(--bg-300);
  display:flex;align-items:center;gap:8px;}
#comments-header .count{margin-left:auto;background:var(--bb-800);color:var(--bb-200);
  border-radius:10px;padding:1px 8px;font-size:0.72rem;}
#comments-list{flex:1;overflow-y:auto;padding:10px;}
#comments-list::-webkit-scrollbar{width:6px;}
#comments-list::-webkit-scrollbar-thumb{background:var(--bg-700);border-radius:3px;}
.comment-card{background:var(--bg-800);border:1px solid var(--bg-700);border-radius:7px;
  padding:10px 12px;margin-bottom:9px;}
.comment-card .quote{font-size:0.76rem;color:var(--bg-400);border-left:2px solid var(--bb-500);
  padding-left:8px;margin-bottom:7px;font-style:italic;white-space:pre-wrap;
  max-height:5.2em;overflow:hidden;}
.comment-card .quote.empty{border-left-color:var(--bg-600);color:var(--bg-500);}
.comment-card .body{font-size:0.86rem;color:var(--bg-100);white-space:pre-wrap;line-height:1.5;}
.comment-card .del{float:right;background:none;border:none;color:var(--bg-500);cursor:pointer;
  font-size:0.95rem;line-height:1;padding:0 2px;}
.comment-card .del:hover{color:var(--red);}
#comments-empty{color:var(--bg-500);font-size:0.82rem;text-align:center;padding:24px 12px;line-height:1.6;}

/* selection popover */
#sel-popover{
  position:absolute;display:none;z-index:50;background:var(--bg-700);border:1px solid var(--bb-500);
  border-radius:8px;padding:10px;width:300px;box-shadow:0 10px 30px rgba(0,0,0,.5);
}
#sel-popover.open{display:block;}
#sel-popover .sel-quote{font-size:0.74rem;color:var(--bg-300);border-left:2px solid var(--bb-500);
  padding-left:7px;margin-bottom:8px;font-style:italic;max-height:4em;overflow:hidden;white-space:pre-wrap;}
#sel-popover textarea{width:100%;height:64px;resize:vertical;background:var(--bg-900);
  border:1px solid var(--bg-600);border-radius:5px;color:var(--bg-100);font-size:0.84rem;
  padding:7px;outline:none;font-family:inherit;}
#sel-popover textarea:focus{border-color:var(--bb-400);}
#sel-popover .row{display:flex;gap:6px;margin-top:8px;justify-content:flex-end;}
#sel-popover button{border:none;border-radius:5px;padding:5px 12px;font-size:0.78rem;cursor:pointer;}
#sel-popover .save{background:var(--bb-500);color:#fff;}
#sel-popover .save:hover{background:var(--bb-400);}
#sel-popover .cancel{background:var(--bg-600);color:var(--bg-200);}
#sel-popover .cancel:hover{background:var(--bg-500);}

/* markdown render */
#md-render{max-width:880px;line-height:1.7;}
#md-render h1,#md-render h2,#md-render h3,#md-render h4,#md-render h5,#md-render h6{
  color:var(--bg-100);font-weight:600;margin:1.6em 0 .5em;line-height:1.3;}
#md-render h1{font-size:2rem;border-bottom:2px solid var(--bg-700);padding-bottom:.3em;}
#md-render h2{font-size:1.45rem;border-bottom:1px solid var(--bg-700);padding-bottom:.25em;}
#md-render h3{font-size:1.2rem;color:var(--bb-200);}
#md-render h4{font-size:1.05rem;color:var(--bg-200);}
#md-render p{margin:.75em 0;}
#md-render a{color:var(--bb-300);text-decoration:none;}
#md-render a:hover{text-decoration:underline;}
#md-render ul,#md-render ol{margin:.75em 0 .75em 1.5em;}
#md-render li{margin:.25em 0;}
#md-render blockquote{margin:1em 0;padding:.75em 1em;border-left:3px solid var(--bb-500);
  background:var(--bg-800);border-radius:0 5px 5px 0;color:var(--bg-200);}
#md-render hr{border:none;border-top:1px solid var(--bg-700);margin:2em 0;}
#md-render :not(pre)>code{background:var(--bg-700);color:var(--bb-200);padding:.15em .4em;
  border-radius:4px;font-size:.88em;font-family:"JetBrains Mono","Fira Code",Consolas,monospace;}
#md-render pre{background:var(--bg-1000);border:1px solid var(--bg-700);border-radius:7px;
  padding:1.1em 1.3em;overflow-x:auto;margin:1em 0;font-size:.88em;line-height:1.6;}
#md-render pre code{background:none;color:inherit;padding:0;
  font-family:"JetBrains Mono","Fira Code",Consolas,monospace;}
#md-render table{border-collapse:collapse;width:100%;margin:1em 0;font-size:.9em;}
#md-render th{background:var(--bg-700);color:var(--bg-100);padding:8px 12px;text-align:left;
  font-weight:600;border:1px solid var(--bg-600);}
#md-render td{padding:7px 12px;border:1px solid var(--bg-700);color:var(--bg-200);}
#md-render tr:nth-child(even) td{background:var(--bg-800);}
#md-render img{max-width:100%;border-radius:6px;border:1px solid var(--bg-600);}
.hljs{background:transparent !important;}

/* changed-block flash */
@keyframes changed-flash{
  0%{background:rgba(0,113,240,.18);box-shadow:0 0 0 3px rgba(0,113,240,.10);}
  100%{background:transparent;box-shadow:none;}
}
.changed{border-radius:4px;background:rgba(0,113,240,.18);box-shadow:0 0 0 3px rgba(0,113,240,.10);
  transition:background 1.5s ease-out,box-shadow 1.5s ease-out;}
.changed.fading{background:transparent;box-shadow:none;}

/* diff view */
#diff-render{display:none;max-width:980px;}
#diff-render .edit-block{margin-bottom:22px;border:1px solid var(--bg-700);border-radius:8px;overflow:hidden;}
#diff-render .edit-head{background:var(--bg-800);padding:7px 14px;font-size:0.76rem;color:var(--bg-400);
  border-bottom:1px solid var(--bg-700);display:flex;gap:10px;align-items:center;}
#diff-render .edit-head .n{background:var(--bb-800);color:var(--bb-200);border-radius:4px;
  padding:1px 7px;font-weight:600;}
.diff-line{font-family:"JetBrains Mono","Fira Code",Consolas,monospace;font-size:0.82rem;
  line-height:1.55;white-space:pre-wrap;word-break:break-word;padding:1px 14px;}
.diff-line.add{background:var(--add-bg);border-left:3px solid var(--add-bd);color:var(--green);}
.diff-line.del{background:var(--del-bg);border-left:3px solid var(--del-bd);color:var(--red);}
.diff-line.ctx{color:var(--bg-400);border-left:3px solid transparent;}
.diff-line.hunk{color:var(--bb-300);background:var(--bg-800);border-left:3px solid var(--bb-800);}
#diff-empty{color:var(--bg-500);font-size:0.9rem;padding:30px 0;}

/* round grouping in the diff view */
#diff-toolbar{display:flex;align-items:center;gap:12px;margin-bottom:16px;}
#diff-toolbar button{background:var(--bg-700);border:1px solid var(--bg-600);color:var(--bg-200);
  border-radius:6px;padding:5px 12px;font-size:0.78rem;cursor:pointer;}
#diff-toolbar button:hover{background:var(--bg-600);color:var(--bg-100);border-color:var(--bb-500);}
#diff-toolbar .hint{font-size:0.74rem;color:var(--bg-500);}
.round-group{margin-bottom:18px;border:1px solid var(--bg-700);border-radius:8px;overflow:hidden;}
.round-group.current{border-color:var(--bb-800);}
.round-head{background:var(--bg-800);padding:8px 14px;font-size:0.78rem;color:var(--bg-300);
  display:flex;gap:10px;align-items:center;cursor:pointer;user-select:none;}
.round-head:hover{background:var(--bg-700);}
.round-head .caret{transition:transform .12s;color:var(--bg-500);}
.round-group:not(.collapsed) .round-head .caret{transform:rotate(90deg);}
.round-head .n{background:var(--bb-800);color:var(--bb-200);border-radius:4px;
  padding:1px 8px;font-weight:600;}
.round-head .round-meta{color:var(--bg-500);font-size:0.74rem;}
.round-head .badge{margin-left:auto;background:var(--green);color:var(--bg-1000);
  border-radius:10px;padding:1px 9px;font-size:0.7rem;font-weight:600;}
.round-group.collapsed .round-body{display:none;}
.round-body .edit-block{margin:0;border:none;border-top:1px solid var(--bg-800);border-radius:0;}
.round-body .edit-block:first-child{border-top:none;}

/* comment source tag */
.comment-card .src-tag{display:inline-block;font-size:0.66rem;font-weight:600;letter-spacing:.04em;
  text-transform:uppercase;border-radius:4px;padding:1px 6px;margin-bottom:6px;}
.comment-card .src-tag.diff{background:var(--bb-800);color:var(--bb-200);}

/* toast */
#toast{position:fixed;top:16px;left:50%;transform:translateX(-50%) translateY(-8px);
  padding:9px 18px;background:var(--bg-700);border:1px solid var(--bb-500);border-radius:6px;
  font-size:0.86rem;color:var(--bb-200);opacity:0;transition:opacity .2s,transform .2s;
  pointer-events:none;z-index:500;}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0);}
#toast.ok{border-color:var(--green);color:var(--green);}

/* highlightable selection hint */
::selection{background:rgba(0,113,240,.35);}

/* inline highlights for document text that has a comment attached */
#md-render mark.has-comment{
  background:linear-gradient(180deg,rgba(0,113,240,.32),rgba(0,113,240,.22));
  color:inherit;border-radius:3px;padding:0 1px;cursor:help;
  border-bottom:2px solid rgba(0,113,240,.7);
}
#md-render mark.has-comment.flash{animation:cmflash 1.1s ease;}
@keyframes cmflash{0%{background:rgba(0,113,240,.65);}100%{background:rgba(0,113,240,.22);}}

/* inline highlights for diff lines that have a comment attached */
#diff-render mark.has-comment{
  background:rgba(0,113,240,.30);color:inherit;border-radius:2px;padding:0 1px;
  outline:1px solid rgba(0,113,240,.55);outline-offset:-1px;
}
#diff-render .diff-line.add mark.has-comment{background:rgba(0,170,90,.34);outline-color:rgba(0,170,90,.7);}
#diff-render .diff-line.del mark.has-comment{background:rgba(224,90,75,.40);outline-color:rgba(224,90,75,.75);}
"""


def build_html(name: str) -> str:
    """The single-page front-end. State is fetched from the server via JSON."""
    css = VALSTRO_CSS
    safe_name = name.replace("<", "&lt;").replace('"', "&quot;")
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{safe_name} — mdedit</title>
<link rel="stylesheet" href="{_asset_url("highlight-onedark.min.css")}">
<script src="{_asset_url("highlight.min.js")}"></script>
<script src="{_asset_url("marked.min.js")}"></script>
<style>{css}</style>
</head><body>

<div id="top-bar">
  <div class="logo-bar"></div>
  <span class="doc-name" id="doc-name">{safe_name}</span>
  <span class="meta" id="doc-meta"></span>
  <span class="spacer"></span>
  <div class="toggle-group">
    <button id="view-rendered" class="active">Rendered</button>
    <button id="view-diff">Changes</button>
  </div>
  <button id="send-btn" title="Return all comments to the LLM">Send to LLM</button>
</div>

<div id="body">
  <div id="content-area">
    <div id="md-render"></div>
    <div id="diff-render"></div>
    <div id="sel-popover">
      <div class="sel-quote" id="sel-quote"></div>
      <textarea id="sel-input" placeholder="Comment on the selected text…"></textarea>
      <div class="row">
        <button class="cancel" id="sel-cancel">Cancel</button>
        <button class="save" id="sel-save">Add comment</button>
      </div>
    </div>
  </div>
  <div id="comments-panel">
    <div id="comments-header">Comments <span class="count" id="comment-count">0</span></div>
    <div id="comments-list">
      <div id="comments-empty">
        Select any text in the document to attach a comment,
        or use the box below for a general note.
      </div>
    </div>
    <div style="padding:10px;border-top:1px solid var(--bg-700);">
      <textarea id="general-input" placeholder="Add a general comment…"
        style="width:100%;height:54px;resize:vertical;background:var(--bg-800);
        border:1px solid var(--bg-600);border-radius:5px;color:var(--bg-100);
        font-size:0.84rem;padding:7px;outline:none;font-family:inherit;"></textarea>
      <button id="general-add" style="margin-top:7px;width:100%;background:var(--bg-700);
        border:1px solid var(--bg-600);color:var(--bg-200);border-radius:5px;padding:6px;
        font-size:0.8rem;cursor:pointer;">Add general comment</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
const DOC_NAME = {json.dumps(name)};

marked.setOptions({{ gfm:true, breaks:false }});
const renderer = new marked.Renderer();
renderer.code = function(code, lang) {{
  const language = (lang||'').toLowerCase().trim();
  try {{
    const h = (language && hljs.getLanguage(language))
      ? hljs.highlight(code, {{language}}).value : hljs.highlightAuto(code).value;
    return `<pre><code class="hljs language-${{language||'plaintext'}}">${{h}}</code></pre>`;
  }} catch(_) {{ return `<pre><code class="hljs">${{escapeHtml(code)}}</code></pre>`; }}
}};
marked.use({{ renderer }});

function escapeHtml(s){{return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}}

const mdRender   = document.getElementById('md-render');
const diffRender = document.getElementById('diff-render');
const toastEl    = document.getElementById('toast');
let toastTimer = null;
function toast(msg, kind){{
  toastEl.textContent = msg;
  toastEl.classList.toggle('ok', kind==='ok');
  toastEl.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer=setTimeout(()=>toastEl.classList.remove('show'), kind==='ok'?2800:2200);
}}

// ── State ────────────────────────────────────────────────────────────
let state = {{ version:-1, current_text:'', edits:[], comments:[], submitted:false }};
let view = 'rendered';

// ── View toggle ──────────────────────────────────────────────────────
document.getElementById('view-rendered').addEventListener('click',()=>setView('rendered'));
document.getElementById('view-diff').addEventListener('click',()=>setView('diff'));
function setView(v){{
  view=v;
  document.getElementById('view-rendered').classList.toggle('active',v==='rendered');
  document.getElementById('view-diff').classList.toggle('active',v==='diff');
  mdRender.style.display   = v==='rendered'?'block':'none';
  diffRender.style.display = v==='diff'?'block':'none';
}}

// ── Change highlighting (diff & patch top-level blocks) ──────────────
const changedEls = new Set();
let clearTimer = null;
function scheduleClear(){{
  clearTimeout(clearTimer);
  clearTimer=setTimeout(()=>{{
    changedEls.forEach(el=>{{
      el.classList.add('fading');
      el.addEventListener('transitionend',()=>{{el.classList.remove('changed','fading');changedEls.delete(el);}},{{once:true}});
    }});
  }}, 8000);
}}
function diffAndPatch(container, newHtml){{
  const scratch=document.createElement('div'); scratch.innerHTML=newHtml;
  const oldC=Array.from(container.childNodes), newC=Array.from(scratch.childNodes);
  const max=Math.max(oldC.length,newC.length); let n=0;
  for(let i=0;i<max;i++){{
    const o=oldC[i], w=newC[i];
    if(!o){{const im=w.cloneNode(true);container.appendChild(im);if(im.nodeType===1){{im.classList.add('changed');changedEls.add(im);n++;}}}}
    else if(!w){{container.removeChild(o);n++;}}
    else if(o.nodeType!==w.nodeType||o.nodeName!==w.nodeName||(o.outerHTML||o.textContent)!==(w.outerHTML||w.textContent)){{
      const im=w.cloneNode(true);container.replaceChild(im,o);
      if(im.nodeType===1){{im.classList.add('changed');changedEls.add(im);n++;}}
    }}
  }}
  if(n>0) scheduleClear();
  return n;
}}

// ── Render diff view (grouped by round) ──────────────────────────────
const roundCollapse = new Map();   // round number -> bool (user override)
function renderDiff(){{
  if(!state.edits.length){{
    diffRender.innerHTML='<div id="diff-empty">No edits yet. When the assistant '
      +'edits the document, each round of changes appears here.</div>';
    return;
  }}
  // Group edits by round.
  const byRound=new Map();
  for(const e of state.edits){{
    const r=e.round||1;
    if(!byRound.has(r)) byRound.set(r, []);
    byRound.get(r).push(e);
  }}
  const rounds=[...byRound.keys()].sort((a,b)=>b-a);   // newest first
  const current=state.current_round||rounds[0];
  const hasOld=rounds.some(r=>r!==current);

  const parts=[];
  if(hasOld){{
    parts.push('<div id="diff-toolbar"><button id="clear-old-rounds">Clear old rounds</button>'
      +'<span class="hint">'+rounds.length+' rounds shown</span></div>');
  }}
  for(const r of rounds){{
    const edits=byRound.get(r);
    const isCurrent=(r===current);
    // Default: current round expanded, older rounds collapsed; user can override.
    const collapsed=roundCollapse.has(r) ? roundCollapse.get(r) : !isCurrent;
    const blocks=[];
    for(const e of edits){{
      const lines=(e.diff||'').split('\\n');
      const body=[];
      for(const ln of lines){{
        if(ln.startsWith('+++')||ln.startsWith('---')) continue;
        let cls='ctx';
        if(ln.startsWith('@@')) cls='hunk';
        else if(ln.startsWith('+')) cls='add';
        else if(ln.startsWith('-')) cls='del';
        // data-round lets a selection in this view be anchored to its round.
        body.push(`<div class="diff-line ${{cls}}" data-round="${{r}}">${{escapeHtml(ln||' ')}}</div>`);
      }}
      blocks.push(`<div class="edit-block">${{body.join('')}}</div>`);
    }}
    const ts=new Date(edits[edits.length-1].ts*1000).toLocaleTimeString();
    parts.push(
      `<div class="round-group ${{isCurrent?'current':''}} ${{collapsed?'collapsed':''}}" data-round="${{r}}">`
      +`<div class="round-head" data-round="${{r}}">`
      +`<span class="caret">▸</span>`
      +`<span class="n">Round ${{r}}</span>`
      +`<span class="round-meta">${{edits.length}} edit${{edits.length!==1?'s':''}} · ${{ts}}</span>`
      +(isCurrent?'<span class="badge">latest</span>':'')
      +`</div><div class="round-body">${{blocks.join('')}}</div></div>`
    );
  }}
  diffRender.innerHTML=parts.join('');

  // Wire up collapse toggles.
  diffRender.querySelectorAll('.round-head').forEach(h=>{{
    h.addEventListener('click',()=>{{
      const r=+h.dataset.round;
      const grp=h.parentElement;
      const nowCollapsed=grp.classList.toggle('collapsed');
      roundCollapse.set(r, nowCollapsed);
    }});
  }});
  const clearBtn=document.getElementById('clear-old-rounds');
  if(clearBtn) clearBtn.addEventListener('click',clearOldRounds);
  applyDiffCommentHighlights();
}}

async function clearOldRounds(){{
  await fetch('/api/diffs/clear',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{keep_current:true}})}});
  await refresh();
  toast('Cleared older rounds', 'ok');
}}

// ── Render document ──────────────────────────────────────────────────
// Renders only the rendered-document body. The Changes view (renderDiff) and
// the top-bar edit-count meta are driven separately from refresh() so that
// events which bump the version but leave the document text untouched — adding
// or deleting a comment, clearing old rounds, submitting — don't re-patch the
// DOM. Re-patching would otherwise compare blocks still flagged `changed`/
// `fading` against clean re-parsed HTML and re-flash them, making the "changed
// block" highlight (and a spurious "document updated" toast) come back every
// time you comment.
function renderDoc(patch){{
  const html=marked.parse(state.current_text||'');
  if(patch && mdRender.childNodes.length){{
    // Strip any comment-highlight marks first so the block-level diff compares
    // clean marked HTML against clean marked HTML (otherwise blocks carrying
    // <mark> wrappers would be flagged as changed and re-flash on every edit).
    unwrapCommentMarks(mdRender);
    const n=diffAndPatch(mdRender, html);
    if(n>0) toast('↻ document updated ('+n+' block'+(n!==1?'s':'')+')');
  }} else {{
    mdRender.innerHTML=html;
  }}
  applyCommentHighlights();
}}

// Highlight document selections that have a comment attached. Each comment
// whose source is 'doc' carries a `quote` (the exact selected text); we wrap
// every occurrence of that text inside the rendered document in a
// <mark class="has-comment"> so the reader can see at a glance which spans
// have feedback. Idempotent: it unwraps existing marks before re-applying.
function unwrapCommentMarks(root){{
  root.querySelectorAll('mark.has-comment').forEach(m=>{{
    const p=m.parentNode; while(m.firstChild) p.insertBefore(m.firstChild,m);
    p.removeChild(m); p.normalize();
  }});
}}
// Wrap every occurrence of each `quotes` string inside `root`'s text nodes in
// a <mark class="has-comment">. Longest quotes first so shorter substrings
// don't steal their text nodes. Idempotent when the caller unwraps first.
function wrapQuotes(root, quotes){{
  if(!quotes.length) return;
  quotes=[...quotes].sort((a,b)=>b.length-a.length);
  // Collect live text nodes fresh for each quote. Wrapping a quote splits its
  // text node (replaceChild with a fragment), so a snapshot taken once up
  // front would point at detached nodes (parentNode === null) for the second
  // quote — wrapping it would then throw and silently skip every later quote.
  const collect=()=>{{
    const walk=document.createTreeWalker(root,NodeFilter.SHOW_TEXT,{{
      acceptNode(n){{ return n.nodeValue.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT; }}
    }});
    const nodes=[]; while(walk.nextNode()) nodes.push(walk.currentNode);
    return nodes;
  }};
  const wrapNode=(node,q)=>{{
    const text=node.nodeValue;
    let idx=text.indexOf(q);
    if(idx<0) return;
    const frag=document.createDocumentFragment();
    let last=0;
    while(idx>=0){{
      if(idx>last) frag.appendChild(document.createTextNode(text.slice(last,idx)));
      const mk=document.createElement('mark');
      mk.className='has-comment';
      mk.textContent=text.slice(idx,idx+q.length);
      frag.appendChild(mk);
      last=idx+q.length;
      idx=text.indexOf(q,last);
    }}
    if(last<text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    node.parentNode.replaceChild(frag,node);
  }};
  for(const q of quotes){{
    for(const node of collect()){{
      // A fresh collect() returns only live nodes, but guard anyway in case a
      // sibling quote already replaced one earlier in this same pass.
      if(node.parentNode) wrapNode(node,q);
    }}
  }}
}}

// Highlight document-view selections that have a comment attached. Each
// comment whose source is 'doc' carries a `quote` (the exact selected text);
// we wrap every occurrence in the rendered document so the reader can see at
// a glance which spans have feedback.
function applyCommentHighlights(){{
  unwrapCommentMarks(mdRender);
  const quotes=[...new Set(
    state.comments.filter(c=>c.source==='doc'&&c.quote).map(c=>c.quote)
  )];
  wrapQuotes(mdRender, quotes);
}}

// Highlight diff-view selections that have a comment attached. A diff comment
// is anchored to a specific round, so we scope the search to that round's
// group and only wrap within its diff lines.
function applyDiffCommentHighlights(){{
  unwrapCommentMarks(diffRender);
  // Group quotes by round so each is scoped to its own round-group element.
  const byRound=new Map();
  for(const c of state.comments){{
    if(c.source!=='diff'||!c.round||!c.quote) continue;
    if(!byRound.has(c.round)) byRound.set(c.round, new Set());
    byRound.get(c.round).add(c.quote);
  }}
  for(const [round, quotes] of byRound){{
    const grp=diffRender.querySelector('.round-group[data-round="'+round+'"]');
    if(grp) wrapQuotes(grp, [...quotes]);
  }}
}}

// ── Comments ─────────────────────────────────────────────────────────
function renderComments(){{
  const list=document.getElementById('comments-list');
  document.getElementById('comment-count').textContent=state.comments.length;
  if(!state.comments.length){{
    list.innerHTML='<div id="comments-empty">Select any text in the document to attach a comment, '
      +'or use the box below for a general note.</div>';
    return;
  }}
  list.innerHTML=state.comments.map(c=>{{
    const tag=c.source==='diff'
      ? `<span class="src-tag diff">Round ${{c.round}} diff</span>`
      : '';
    const q=c.quote
      ? `<div class="quote">${{escapeHtml(c.quote)}}</div>`
      : `<div class="quote empty">general comment</div>`;
    return `<div class="comment-card"><button class="del" data-id="${{c.id}}" title="Delete">×</button>`
      +`${{tag}}${{q}}<div class="body">${{escapeHtml(c.body)}}</div></div>`;
  }}).join('');
  list.querySelectorAll('.del').forEach(b=>b.addEventListener('click',()=>deleteComment(+b.dataset.id)));
}}

async function addComment(body, quote, before, after, source, round){{
  if(!body.trim()) return;
  await fetch('/api/comment',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{body,quote:quote||'',context_before:before||'',context_after:after||'',
      source:source||'doc',round:round||0}})}});
  await refresh();
  toast('Comment added');
}}
async function deleteComment(id){{
  await fetch('/api/comment/delete',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{id}})}});
  await refresh();
}}

// general comment box
document.getElementById('general-add').addEventListener('click',()=>{{
  const ta=document.getElementById('general-input');
  if(ta.value.trim()){{ addComment(ta.value,'','',''); ta.value=''; }}
}});

// ── Selection → popover ──────────────────────────────────────────────
const pop=document.getElementById('sel-popover');
const selQuoteEl=document.getElementById('sel-quote');
const selInput=document.getElementById('sel-input');
let pendingSel=null;

document.getElementById('content-area').addEventListener('mouseup',(e)=>{{
  if(pop.contains(e.target)) return;
  setTimeout(()=>maybeShowPopover(e),0);
}});
function maybeShowPopover(e){{
  const sel=window.getSelection();
  const text=sel.toString();
  if(!text || !text.trim()){{ hidePopover(); return; }}

  let source='doc', round=0, before='', after='';
  if(view==='rendered'){{
    // capture a little surrounding context from the full document text
    const idx=state.current_text.indexOf(text);
    if(idx>=0){{ before=state.current_text.slice(Math.max(0,idx-60),idx);
                 after=state.current_text.slice(idx+text.length, idx+text.length+60); }}
  }} else {{
    // Changes view: anchor the comment to the round of the selected diff line.
    source='diff';
    let node=sel.anchorNode;
    while(node && node.nodeType!==1) node=node.parentElement;
    const line=node && node.closest ? node.closest('.diff-line, .round-group') : null;
    if(line && line.dataset && line.dataset.round) round=+line.dataset.round;
  }}

  pendingSel={{quote:text,before,after,source,round}};
  selQuoteEl.textContent=(source==='diff'&&round?`[Round ${{round}}] `:'')
    +(text.length>200?text.slice(0,200)+'…':text);
  selInput.value='';
  const area=document.getElementById('content-area');
  const rect=area.getBoundingClientRect();
  let x=e.clientX-rect.left+area.scrollLeft;
  let y=e.clientY-rect.top+area.scrollTop+10;
  x=Math.min(x, area.scrollLeft+area.clientWidth-320);
  pop.style.left=Math.max(8,x)+'px'; pop.style.top=y+'px';
  pop.classList.add('open'); selInput.focus();
}}
function hidePopover(){{ pop.classList.remove('open'); pendingSel=null; }}
document.getElementById('sel-cancel').addEventListener('click',hidePopover);
document.getElementById('sel-save').addEventListener('click',()=>{{
  if(pendingSel && selInput.value.trim()){{
    addComment(selInput.value,pendingSel.quote,pendingSel.before,pendingSel.after,
               pendingSel.source,pendingSel.round);
  }}
  hidePopover();
}});
selInput.addEventListener('keydown',(e)=>{{
  if((e.metaKey||e.ctrlKey)&&e.key==='Enter'){{ document.getElementById('sel-save').click(); }}
  if(e.key==='Escape') hidePopover();
}});

// ── Send to LLM ──────────────────────────────────────────────────────
document.getElementById('send-btn').addEventListener('click',async()=>{{
  const n=state.comments.length;
  await fetch('/api/submit',{{method:'POST'}});
  toast(n>0
    ? '✓ Sent '+n+' comment'+(n!==1?'s':'')+' to the LLM'
    : '✓ Approved — sent to the LLM with no comments', 'ok');
}});

// ── State sync ───────────────────────────────────────────────────────
async function refresh(){{
  const r=await fetch('/api/state'); const s=await r.json();
  const firstLoad = state.version < 0;
  // Only re-patch the rendered document when its text actually changed.
  // Comments, submits and diff-clears bump the version too; patching on those
  // would re-flash every block still carrying the transient `changed` class.
  const docChanged = s.current_text !== state.current_text;
  const verChanged = s.version !== state.version;
  state=s;
  if(firstLoad || docChanged) renderDoc(!firstLoad && docChanged);
  if(firstLoad || verChanged) renderDiff();
  renderComments();
  // Comment add/delete bumps the version but not the document text; re-apply
  // highlights so new selections light up (and deleted ones clear) without
  // re-rendering either view's body.
  applyCommentHighlights();
  applyDiffCommentHighlights();
  document.getElementById('doc-meta').textContent =
    state.edits.length+' edit'+(state.edits.length!==1?'s':'');
}}

async function poll(){{
  while(true){{
    try{{
      const r=await fetch('/api/poll?v='+state.version,{{signal:AbortSignal.timeout(30000)}});
      if(r.ok){{ const d=await r.json(); if(d.version!==state.version) await refresh(); }}
    }}catch(_){{ await new Promise(r=>setTimeout(r,1000)); }}
  }}
}}

refresh().then(poll);
</script>
</body></html>
"""
