#!/usr/bin/env python3
"""
mdedit.py — LLM-driven markdown editing & review tool.

Designed to be driven by an LLM harness (Claude, opencode, etc.) via simple
CLI sub-commands, in the spirit of `plannotator annotate`.

Two user requirements drive the design:

  1. "As a user I want to visualise the changes to a document as they are made
      so I can more easily review a document as it is iterated on."
      → Every edit is applied through this tool, which pushes a live before/after
        diff to a browser tab. Changed blocks flash; a toggle switches between a
        clean rendered view and an inline diff view.

  2. "As a user I want to be able to comment on a document an LLM has written and
      return those comments back to the LLM so the review process is quicker and
      requires fewer copy and pastes."
      → The `review` command opens (or focuses) the browser, lets the user select
        text and attach comments, then blocks until the user clicks "Send to LLM"
        (or closes the tab) and prints the collected comments as JSON to stdout.

Architecture
------------
A lightweight background HTTP server (one per document, keyed by absolute path)
holds the live session state: the current document text, an ordered list of edit
records (each with old/new text + a unified diff), and a queue of user comments.

The CLI sub-commands are thin clients that talk to that server over localhost.
The first `edit`/`open` for a document auto-spawns the daemon.

Sub-commands
------------
    mdedit.py open        <file.md>                      # open/focus the viewer
    mdedit.py edit        <file.md> --old <s> --new <s>  # apply 1 search/replace edit
    mdedit.py edit        <file.md> --edits-json <path|-> # apply many edits at once
    mdedit.py review      <file.md> [--json] [--timeout N]# block; print comments JSON
    mdedit.py resolve     <file.md> --id N | --all       # clear addressed comments
    mdedit.py clear-diffs <file.md> [--all]              # prune the Changes-view history
    mdedit.py status      <file.md>                      # print session state JSON
    mdedit.py stop        <file.md>                      # shut the session down

Rounds
------
A "round" is one LLM edit pass. The first edit after the user submits a review
opens the next round, and (by default) earlier rounds' diffs are pruned from the
Changes view so it shows only the latest pass. Diffs are grouped by round in the
browser; the user can collapse rounds, clear old ones, and attach comments to
specific diff lines (those come back with `source: "diff"` and a `round`).

Edits use search/replace semantics: `old` must occur exactly once in the current
document (unless --replace-all). This is intentionally the same contract as a
typical LLM "edit file" tool, so it slots straight into a harness.

No third-party Python dependencies — 3.10+ standard library only. Markdown,
syntax highlighting and diff rendering happen client-side via two JS libraries
(marked, highlight.js). These are vendored under vendor/ and served by the
daemon so the viewer works offline; if a vendored file is missing the HTML falls
back to the CDN. Run update-vendor.sh to (re)download the pinned versions.
"""

from __future__ import annotations

import argparse
import difflib
import http.client
import http.server
import json
import os
import re
import signal
import socket
import socketserver
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_PORT = 7575
# Where we record running sessions so CLI clients can find the daemon.
STATE_DIR = Path(os.environ.get("MDEDIT_STATE_DIR", Path.home() / ".cache" / "mdedit"))

# Front-end libraries. These are loaded by the browser viewer to render markdown
# (marked) and syntax-highlight code (highlight.js). They are vendored next to
# this script under vendor/ so the viewer works fully offline; if a vendored
# file is missing we fall back to the CDN URL. `update-vendor.sh` (re)downloads
# the pinned versions below into vendor/. Keep this manifest in sync with that
# script — it is the single source of truth for versions, filenames and URLs.
VENDOR_DIR = Path(__file__).resolve().parent / "vendor"

# local filename -> CDN URL (used as fallback and by update-vendor.sh)
VENDOR_ASSETS = {
    "marked.min.js":
        "https://cdn.jsdelivr.net/npm/marked@12/marked.min.js",
    "highlight.min.js":
        "https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11/highlight.min.js",
    "highlight-onedark.min.css":
        "https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11/styles/base16/onedark.min.css",
}

# Browser MIME types for the vendored assets we serve.
_VENDOR_MIME = {".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8"}


def _asset_url(filename: str) -> str:
    """Local /vendor URL if the file is vendored, else the CDN fallback URL."""
    if (VENDOR_DIR / filename).is_file():
        return f"/vendor/{filename}"
    return VENDOR_ASSETS[filename]


# ---------------------------------------------------------------------------
# Session model
# ---------------------------------------------------------------------------

@dataclass
class EditRecord:
    """A single applied search/replace edit and its rendered diff."""

    index: int
    old: str
    new: str
    diff: str                       # unified diff (text) for this single edit
    round: int = 1                  # which review round this edit belongs to
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "old": self.old,
            "new": self.new,
            "diff": self.diff,
            "round": self.round,
            "ts": self.ts,
        }


@dataclass
class Comment:
    """A user comment, optionally anchored to a text selection or a diff line."""

    id: int
    body: str
    quote: str = ""                 # the selected text the comment is anchored to
    context_before: str = ""        # a little surrounding context for the LLM
    context_after: str = ""
    source: str = "doc"             # "doc" (rendered view) or "diff" (Changes view)
    round: int = 0                  # round the diff comment refers to (0 = n/a)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "body": self.body,
            "quote": self.quote,
            "context_before": self.context_before,
            "context_after": self.context_after,
            "source": self.source,
            "round": self.round,
            "ts": self.ts,
        }


class Session:
    """
    Holds all live state for one document under review.

    Thread-safe: every mutation takes `self.lock`. A Condition lets the /poll
    long-poll and the /review-wait blocking endpoint sleep until something
    interesting happens (a new edit, a new comment, or the user hitting "Send").
    """

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self._cond = threading.Condition(self.lock)

        self.original_text: str = _read(path)
        self.current_text: str = self.original_text
        self.edits: list[EditRecord] = []
        self.comments: list[Comment] = []
        self._comment_seq = 0

        # Round bookkeeping. A "round" is one LLM edit pass. The first edit that
        # lands after the user submits a review starts the next round.
        self.current_round: int = 1
        self._new_round_pending: bool = False

        # Bumped on every edit so the browser can hot-reload via long-poll.
        self.version: int = 0
        # Set true when the user clicks "Send to LLM"; unblocks review-wait.
        self.submitted: bool = False
        self.last_activity: float = time.time()

    # -- edits --------------------------------------------------------------

    def apply_edit(self, old: str, new: str, replace_all: bool = False,
                   auto_clear: bool = True) -> EditRecord:
        """Apply one search/replace edit. Raises ValueError on a bad match.

        If a new round is pending (the user submitted a review since the last
        edit), this edit opens that round. When `auto_clear` is true, diffs from
        all earlier rounds are dropped so the Changes view shows only this pass.
        """
        with self._cond:
            text = self.current_text
            if old == "":
                raise ValueError("`old` must not be empty")
            count = text.count(old)
            if count == 0:
                raise ValueError("`old` text not found in document")
            if count > 1 and not replace_all:
                raise ValueError(
                    f"`old` text is ambiguous: found {count} occurrences. "
                    f"Provide more surrounding context or pass --replace-all."
                )

            # Open a new round on the first edit after a submit.
            if self._new_round_pending:
                self.current_round += 1
                self._new_round_pending = False
                if auto_clear:
                    self.edits = [e for e in self.edits if e.round == self.current_round]

            before = text
            text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
            self.current_text = text

            diff = _unified_diff(before, text, self.path.name)
            rec = EditRecord(index=len(self.edits), old=old, new=new, diff=diff,
                             round=self.current_round)
            self.edits.append(rec)
            self.version += 1
            self.last_activity = time.time()

            # Persist to disk so the file on disk reflects the live document.
            _write(self.path, text)

            self._cond.notify_all()
            return rec

    def clear_diffs(self, keep_current: bool = True) -> int:
        """Drop edit/diff records. Keeps the current round's diffs by default.

        The document text is untouched — only the visible diff history shrinks.
        Returns the number of edit records removed.
        """
        with self._cond:
            before = len(self.edits)
            if keep_current:
                self.edits = [e for e in self.edits if e.round == self.current_round]
            else:
                self.edits = []
            removed = before - len(self.edits)
            if removed:
                self.version += 1
                self.last_activity = time.time()
                self._cond.notify_all()
            return removed

    # -- comments -----------------------------------------------------------

    def add_comment(self, body: str, quote: str = "",
                    before: str = "", after: str = "",
                    source: str = "doc", round: int = 0) -> Comment:
        with self._cond:
            self._comment_seq += 1
            c = Comment(id=self._comment_seq, body=body, quote=quote,
                        context_before=before, context_after=after,
                        source=source, round=round)
            self.comments.append(c)
            self.version += 1
            self.last_activity = time.time()
            self._cond.notify_all()
            return c

    def delete_comment(self, cid: int) -> bool:
        with self._cond:
            n = len(self.comments)
            self.comments = [c for c in self.comments if c.id != cid]
            changed = len(self.comments) != n
            if changed:
                self.version += 1
                self.last_activity = time.time()
                self._cond.notify_all()
            return changed

    def submit(self) -> None:
        with self._cond:
            self.submitted = True
            self.last_activity = time.time()
            self._cond.notify_all()

    def reset_submitted(self) -> None:
        """Clear the submitted flag so the next `review` blocks again.

        Also arms the next round: the LLM has just consumed a review, so its
        next edit should open a fresh round (and clear the prior round's diffs).
        """
        with self._cond:
            self.submitted = False
            self._new_round_pending = True
            self.last_activity = time.time()
            self._cond.notify_all()

    def touch(self) -> None:
        """Bump `last_activity` without any state change.

        Called by poll-style GET handlers (/api/poll, /api/comments) so that an
        open browser tab or an in-flight CLI `review` keeps the daemon alive.
        """
        with self._cond:
            self.last_activity = time.time()

    # -- waiters ------------------------------------------------------------

    def wait_for_version(self, since: int, timeout: float = 25.0) -> int:
        deadline = time.monotonic() + timeout
        with self._cond:
            while self.version <= since and not self.submitted:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=remaining)
            return self.version

    def wait_for_submit(self, timeout: float | None = None) -> bool:
        """Block until the user clicks Send (returns True) or timeout (False)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while not self.submitted:
                if deadline is None:
                    self._cond.wait(timeout=1.0)
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(timeout=remaining)
            return self.submitted

    # -- snapshots ----------------------------------------------------------

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "path": str(self.path),
                "name": self.path.name,
                "version": self.version,
                "submitted": self.submitted,
                "current_round": self.current_round,
                "original_text": self.original_text,
                "current_text": self.current_text,
                "edits": [e.to_dict() for e in self.edits],
                "comments": [c.to_dict() for c in self.comments],
            }

    def comments_payload(self) -> dict:
        with self.lock:
            return {
                "path": str(self.path),
                "submitted": self.submitted,
                "current_round": self.current_round,
                "edit_count": len(self.edits),
                "comments": [c.to_dict() for c in self.comments],
            }


# ---------------------------------------------------------------------------
# File / diff helpers
# ---------------------------------------------------------------------------

def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _unified_diff(before: str, after: str, name: str) -> str:
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{name}",
        tofile=f"b/{name}",
        n=3,
    )
    return "".join(diff)


# ---------------------------------------------------------------------------
# HTML / front-end
# ---------------------------------------------------------------------------

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
    const n=diffAndPatch(mdRender, html);
    if(n>0) toast('↻ document updated ('+n+' block'+(n!==1?'s':'')+')');
  }} else {{
    mdRender.innerHTML=html;
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


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    session: Session
    html: str

    def log_message(self, *_):
        pass

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- helpers ------------------------------------------------------------

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _serve_vendor(self, filename: str):
        """Serve a vendored front-end asset from VENDOR_DIR.

        Guards against path traversal and only serves files declared in
        VENDOR_ASSETS, so the daemon never exposes arbitrary local files.
        """
        if filename not in VENDOR_ASSETS:
            self._json({"error": "unknown asset"}, 404)
            return
        target = (VENDOR_DIR / filename).resolve()
        # Defence in depth: ensure the resolved path stays inside VENDOR_DIR.
        if VENDOR_DIR.resolve() not in target.parents or not target.is_file():
            self._json({"error": "not found"}, 404)
            return
        try:
            data = target.read_bytes()
        except OSError:
            self._json({"error": "not found"}, 404)
            return
        ctype = _VENDOR_MIME.get(target.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    # -- routing ------------------------------------------------------------

    def do_GET(self):
        p = urlparse(self.path)
        path = p.path
        if path in ("/", "/index.html"):
            body = self.html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/state":
            self._json(self.session.snapshot())
        elif path == "/api/comments":
            self.session.touch()
            self._json(self.session.comments_payload())
        elif path == "/api/poll":
            q = parse_qs(p.query)
            try:
                since = int(q.get("v", ["0"])[0])
            except (ValueError, IndexError):
                since = 0
            self.session.touch()
            ver = self.session.wait_for_version(since, timeout=25.0)
            self._json({"version": ver, "submitted": self.session.submitted})
        elif path == "/api/ping":
            self._json({"ok": True, "path": str(self.session.path)})
        elif path.startswith("/vendor/"):
            self._serve_vendor(path[len("/vendor/"):])
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        p = urlparse(self.path)
        path = p.path
        if path == "/api/edit":
            data = self._read_body()
            edits = data.get("edits")
            if edits is None:
                edits = [{"old": data.get("old", ""), "new": data.get("new", ""),
                          "replace_all": data.get("replace_all", False)}]
            applied, errors = [], []
            for i, e in enumerate(edits):
                try:
                    rec = self.session.apply_edit(
                        e.get("old", ""), e.get("new", ""),
                        bool(e.get("replace_all", False)))
                    applied.append(rec.to_dict())
                except ValueError as exc:
                    errors.append({"index": i, "error": str(exc)})
            self._json({
                "applied": applied,
                "errors": errors,
                "version": self.session.version,
                "ok": not errors,
            }, 200 if not errors else 422)
        elif path == "/api/comment":
            data = self._read_body()
            c = self.session.add_comment(
                data.get("body", ""), data.get("quote", ""),
                data.get("context_before", ""), data.get("context_after", ""),
                data.get("source", "doc"), int(data.get("round", 0) or 0))
            self._json(c.to_dict())
        elif path == "/api/comment/delete":
            data = self._read_body()
            ok = self.session.delete_comment(int(data.get("id", -1)))
            self._json({"ok": ok})
        elif path == "/api/diffs/clear":
            data = self._read_body()
            keep_current = bool(data.get("keep_current", True))
            removed = self.session.clear_diffs(keep_current=keep_current)
            self._json({"ok": True, "removed": removed,
                        "current_round": self.session.current_round})
        elif path == "/api/submit":
            self.session.submit()
            self._json({"ok": True})
        elif path == "/api/submit/reset":
            self.session.reset_submitted()
            self._json({"ok": True})
        elif path == "/api/stop":
            self._json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self._json({"error": "not found"}, 404)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, *_):
        pass


# ---------------------------------------------------------------------------
# Daemon registry — map a document path to a running server's port
# ---------------------------------------------------------------------------

def _state_file(path: Path) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve()).replace(os.sep, "_").replace(":", "_")
    return STATE_DIR / f"{key}.json"


def _find_running(path: Path) -> int | None:
    """Return the port of a live daemon for `path`, or None."""
    sf = _state_file(path)
    if not sf.exists():
        return None
    try:
        info = json.loads(sf.read_text())
        port = int(info["port"])
    except (ValueError, KeyError, OSError):
        return None
    if _ping(port, path):
        return port
    # Stale registry entry
    try:
        sf.unlink()
    except OSError:
        pass
    return None


def _ping(port: int, path: Path) -> bool:
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1.0)
        conn.request("GET", "/api/ping")
        resp = conn.getresponse()
        if resp.status != 200:
            return False
        info = json.loads(resp.read().decode("utf-8"))
        return info.get("path") == str(path.resolve())
    except (OSError, ValueError):
        return False


def _free_port(preferred: int) -> int:
    for candidate in [preferred] + list(range(preferred + 1, preferred + 50)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    # fall back to an ephemeral port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# HTTP client helpers (used by CLI sub-commands)
# ---------------------------------------------------------------------------

def _request(port: int, method: str, path: str, body: dict | None = None,
             timeout: float = 30.0) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    payload = None
    headers = {}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8")
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {"raw": raw}
    return resp.status, data


# ---------------------------------------------------------------------------
# Idle shutdown
# ---------------------------------------------------------------------------

def _idle_reaper(session: Session, server: Server, timeout: float):
    """Background thread that shuts down the server after `timeout` seconds
    of inactivity (no edits, no comments, no polling).

    ``timeout <= 0`` disables the reaper (called from the outside — this
    function is not started in that case).
    """
    check_interval = min(60.0, max(0.5, timeout / 4.0))
    while True:
        with session._cond:
            session._cond.wait(timeout=check_interval)
            idle = time.time() - session.last_activity
        if idle >= timeout:
            threading.Thread(target=server.shutdown, daemon=True).start()
            return


# ---------------------------------------------------------------------------
# Daemon entry point (run in a forked child)
# ---------------------------------------------------------------------------

def _run_daemon(path: Path, port: int, open_browser: bool):
    session = Session(path)
    html = build_html(path.name)

    class Bound(Handler):
        pass
    Bound.session = session
    Bound.html = html

    httpd = Server(("127.0.0.1", port), Bound)

    # Register so CLI clients can find us.
    sf = _state_file(path)
    sf.write_text(json.dumps({"port": port, "path": str(path.resolve()),
                              "pid": os.getpid(), "started": time.time()}))

    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()

    # Idle auto-shutdown.
    idle_timeout = float(os.environ.get("MDEDIT_IDLE_TIMEOUT", "300"))
    if idle_timeout > 0:
        threading.Thread(target=_idle_reaper, args=(session, httpd, idle_timeout),
                         daemon=True).start()

    # Clean shutdown on SIGTERM / SIGHUP so `kill <pid>` removes the state file
    # via the `finally` block below.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: threading.Thread(
            target=httpd.shutdown, daemon=True).start())

    try:
        httpd.serve_forever()
    finally:
        try:
            sf.unlink()
        except OSError:
            pass


def _spawn_daemon(path: Path, open_browser: bool) -> int:
    """Fork a detached daemon for `path`; return its port."""
    port = _free_port(DEFAULT_PORT)

    pid = os.fork()
    if pid == 0:
        # Child: detach and run the server.
        os.setsid()
        # Redirect std streams so the parent's stdout stays clean (it prints JSON).
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        try:
            _run_daemon(path, port, open_browser)
        finally:
            os._exit(0)

    # Parent: wait for the daemon to come up.
    for _ in range(100):
        if _ping(port, path):
            return port
        time.sleep(0.05)
    raise RuntimeError("daemon failed to start")


def _ensure_daemon(path: Path, open_browser: bool) -> int:
    existing = _find_running(path)
    if existing:
        return existing
    return _spawn_daemon(path, open_browser)


# ---------------------------------------------------------------------------
# CLI sub-commands
# ---------------------------------------------------------------------------

_BACKSLASH_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "b": "\b",
    "f": "\f", "v": "\v", "0": "\0",
    "\\": "\\", '"': '"', "'": "'",
}
_ESCAPE_RE = re.compile(r"\\(.)", re.DOTALL)


def _unescape_cli(s: str) -> str:
    """Interpret backslash escapes in a single --old / --new CLI value.

    Shells pass a backslash-n inside double quotes as two literal characters,
    so a multi-line search value never matches the file's real newlines and the
    edit fails with 'old text not found'. Decode the common escapes here;
    unknown escapes are left untouched so genuine backslashes survive.
    --edits-json is not processed (JSON already decodes its own escapes).
    """
    return _ESCAPE_RE.sub(
        lambda m: _BACKSLASH_ESCAPES.get(m.group(1), m.group(0)), s)


def cmd_open(args) -> int:
    path = Path(args.file).resolve()
    if not path.exists():
        path.write_text("", encoding="utf-8")
    port = _ensure_daemon(path, open_browser=not args.no_browser)
    print(json.dumps({"ok": True, "url": f"http://127.0.0.1:{port}", "path": str(path)}))
    return 0


def cmd_edit(args) -> int:
    path = Path(args.file).resolve()
    if not path.exists():
        print(json.dumps({"ok": False, "error": f"file not found: {path}"}), file=sys.stderr)
        return 1

    # Build the edits list.
    if args.edits_json:
        raw = sys.stdin.read() if args.edits_json == "-" else Path(args.edits_json).read_text()
        try:
            edits = json.loads(raw)
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": f"invalid edits JSON: {exc}"}), file=sys.stderr)
            return 1
        if not isinstance(edits, list):
            print(json.dumps({"ok": False, "error": "edits JSON must be a list"}), file=sys.stderr)
            return 1
    else:
        if args.old is None or args.new is None:
            print(json.dumps({"ok": False,
                  "error": "provide --old and --new, or --edits-json"}), file=sys.stderr)
            return 1
        edits = [{"old": _unescape_cli(args.old),
                  "new": _unescape_cli(args.new),
                  "replace_all": args.replace_all}]

    port = _ensure_daemon(path, open_browser=not args.no_browser)
    status, data = _request(port, "POST", "/api/edit", {"edits": edits})
    data["url"] = f"http://127.0.0.1:{port}"
    print(json.dumps(data, indent=2))
    return 0 if data.get("ok") else 1


def cmd_review(args) -> int:
    path = Path(args.file).resolve()
    if not path.exists():
        print(json.dumps({"ok": False, "error": f"file not found: {path}"}), file=sys.stderr)
        return 1

    port = _ensure_daemon(path, open_browser=not args.no_browser)

    if not args.json:
        print(f"Review open at http://127.0.0.1:{port} — "
              f"waiting for you to click “Send to LLM”…", file=sys.stderr)

    # Block until the user clicks Send (or timeout). We poll the server.
    deadline = None if args.timeout <= 0 else time.monotonic() + args.timeout
    while True:
        try:
            status, data = _request(port, "GET", "/api/comments", timeout=5.0)
        except OSError:
            print(json.dumps({"ok": False, "error": "session ended"}),
                  file=sys.stderr)
            return 1
        if data.get("submitted"):
            break
        if deadline is not None and time.monotonic() > deadline:
            break
        time.sleep(0.6)

    try:
        _, payload = _request(port, "GET", "/api/comments", timeout=5.0)
    except OSError:
        print(json.dumps({"ok": False, "error": "session ended"}),
              file=sys.stderr)
        return 1
    result = {
        "path": str(path),
        "submitted": payload.get("submitted", False),
        "current_round": payload.get("current_round", 1),
        "edit_count": payload.get("edit_count", 0),
        "comment_count": len(payload.get("comments", [])),
        "comments": payload.get("comments", []),
    }

    # Reset the submitted flag so a subsequent `review` blocks again instead of
    # returning immediately. Only do this once the user has actually submitted
    # (i.e. we didn't just time out), otherwise we'd clobber a pending submit.
    if result["submitted"]:
        try:
            _request(port, "POST", "/api/submit/reset", {}, timeout=5.0)
        except OSError:
            pass

    print(json.dumps(result, indent=2))
    return 0


def cmd_resolve(args) -> int:
    """Resolve (delete) one or more comments the LLM has addressed."""
    path = Path(args.file).resolve()
    port = _find_running(path)
    if not port:
        print(json.dumps({"ok": False, "error": "no running session"}), file=sys.stderr)
        return 1

    if not args.all and not args.id:
        print(json.dumps({"ok": False,
              "error": "provide one or more --id, or --all"}), file=sys.stderr)
        return 1

    # Determine which ids to resolve.
    if args.all:
        _, data = _request(port, "GET", "/api/comments", timeout=5.0)
        ids = [c.get("id") for c in data.get("comments", [])]
    else:
        ids = args.id

    resolved, missing = [], []
    for cid in ids:
        _, data = _request(port, "POST", "/api/comment/delete", {"id": cid})
        if data.get("ok"):
            resolved.append(cid)
        else:
            missing.append(cid)

    print(json.dumps({
        "ok": not missing,
        "resolved": resolved,
        "missing": missing,
        "url": f"http://127.0.0.1:{port}",
    }, indent=2))
    return 0 if not missing else 1


def cmd_clear_diffs(args) -> int:
    """Clear the diff/round history shown in the Changes view.

    By default keeps the current round's diffs; --all wipes every round. The
    document text itself is never touched.
    """
    path = Path(args.file).resolve()
    port = _find_running(path)
    if not port:
        print(json.dumps({"ok": False, "error": "no running session"}), file=sys.stderr)
        return 1
    _, data = _request(port, "POST", "/api/diffs/clear",
                       {"keep_current": not args.all})
    data["url"] = f"http://127.0.0.1:{port}"
    print(json.dumps(data, indent=2))
    return 0 if data.get("ok") else 1


def cmd_status(args) -> int:
    path = Path(args.file).resolve()
    port = _find_running(path)
    if not port:
        print(json.dumps({"running": False, "path": str(path)}))
        return 0
    _, data = _request(port, "GET", "/api/state")
    print(json.dumps({
        "running": True,
        "url": f"http://127.0.0.1:{port}",
        "version": data.get("version"),
        "submitted": data.get("submitted"),
        "edit_count": len(data.get("edits", [])),
        "comment_count": len(data.get("comments", [])),
    }, indent=2))
    return 0


def cmd_stop(args) -> int:
    path = Path(args.file).resolve()
    port = _find_running(path)
    if not port:
        print(json.dumps({"ok": True, "note": "no running session"}))
        return 0
    try:
        _request(port, "POST", "/api/stop", {})
    except OSError:
        pass
    print(json.dumps({"ok": True}))
    return 0


def cmd_vendor_manifest(args) -> int:
    """Print the front-end asset manifest as JSON.

    `update-vendor.sh` consumes this so the list of files/URLs lives in exactly
    one place (VENDOR_ASSETS) rather than being duplicated in the shell script.
    """
    print(json.dumps({
        "vendor_dir": str(VENDOR_DIR),
        "assets": VENDOR_ASSETS,
    }, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mdedit.py",
        description="LLM-driven markdown editing & review tool.")
    p.add_argument("--no-browser", action="store_true",
                   help="Do not auto-open a browser tab.")
    p.add_argument("--idle-timeout", type=float, default=None,
                   help="Idle shutdown in seconds (default: 300). 0 disables. "
                        "Overrides MDEDIT_IDLE_TIMEOUT.")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("open", help="Open or focus the viewer for a document.")
    sp.add_argument("file")
    sp.set_defaults(func=cmd_open)

    sp = sub.add_parser("edit", help="Apply one or more search/replace edits.")
    sp.add_argument("file")
    sp.add_argument(
        "--old",
        help="Text to find (must occur once unless --replace-all). "
             "Backslash escapes are interpreted (e.g. \\n -> newline).")
    sp.add_argument(
        "--new",
        help="Replacement text. Backslash escapes are interpreted (e.g. \\n -> newline).")
    sp.add_argument("--replace-all", action="store_true",
                    help="Replace every occurrence of --old.")
    sp.add_argument("--edits-json", metavar="PATH",
                    help="Path to a JSON array of {old,new,replace_all} edits, or '-' for stdin.")
    sp.set_defaults(func=cmd_edit)

    sp = sub.add_parser("review", help="Block until the user sends comments; print them as JSON.")
    sp.add_argument("file")
    sp.add_argument("--json", action="store_true",
                    help="Suppress the human-readable stderr notice.")
    sp.add_argument("--timeout", type=float, default=0,
                    help="Max seconds to wait (0 = wait forever).")
    sp.set_defaults(func=cmd_review)

    sp = sub.add_parser("resolve",
                        help="Resolve (delete) comments the LLM has addressed.")
    sp.add_argument("file")
    sp.add_argument("--id", type=int, action="append", metavar="N",
                    help="Comment id to resolve (repeatable).")
    sp.add_argument("--all", action="store_true",
                    help="Resolve every outstanding comment.")
    sp.set_defaults(func=cmd_resolve)

    sp = sub.add_parser("status", help="Print the current session state.")
    sp.add_argument("file")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("clear-diffs",
                        help="Clear the Changes-view diff history (document text is kept).")
    sp.add_argument("file")
    sp.add_argument("--all", action="store_true",
                    help="Clear every round, including the current one.")
    sp.set_defaults(func=cmd_clear_diffs)

    sp = sub.add_parser("stop", help="Shut the session daemon down.")
    sp.add_argument("file")
    sp.set_defaults(func=cmd_stop)

    sp = sub.add_parser("vendor-manifest",
                        help="Print the front-end asset manifest as JSON (used by update-vendor.sh).")
    sp.set_defaults(func=cmd_vendor_manifest)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    # Sub-command level --no-browser fallback: top-level flag may sit before subcmd.
    if not hasattr(args, "no_browser"):
        args.no_browser = False
    # Propagate --idle-timeout to the forked daemon child via env.
    if getattr(args, "idle_timeout", None) is not None:
        os.environ["MDEDIT_IDLE_TIMEOUT"] = str(args.idle_timeout)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
