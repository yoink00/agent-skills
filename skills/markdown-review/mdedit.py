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
import json
import os
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# CLI sub-commands
# ---------------------------------------------------------------------------
from cliutil import unescape_cli as _unescape_cli

# cmd_vendor_manifest reports the vendored-asset manifest (the single source
# of truth lives in frontend.py and is also read by update-vendor.sh).
from frontend import VENDOR_ASSETS, VENDOR_DIR

# The HTTP daemon and session registry live in server.py; the CLI commands
# below are thin clients that talk to it over localhost.
from server import _ensure_daemon, _find_running, _request


def cmd_open(args) -> int:
    path = Path(args.file).resolve()
    if not path.exists():
        path.write_text("", encoding="utf-8")
    port = _ensure_daemon(path, open_browser=not args.no_browser)
    print(
        json.dumps({"ok": True, "url": f"http://127.0.0.1:{port}", "path": str(path)})
    )
    return 0


def cmd_edit(args) -> int:
    path = Path(args.file).resolve()
    if not path.exists():
        print(
            json.dumps({"ok": False, "error": f"file not found: {path}"}),
            file=sys.stderr,
        )
        return 1

    # Build the edits list.
    if args.edits_json:
        raw = (
            sys.stdin.read()
            if args.edits_json == "-"
            else Path(args.edits_json).read_text()
        )
        try:
            edits = json.loads(raw)
        except ValueError as exc:
            print(
                json.dumps({"ok": False, "error": f"invalid edits JSON: {exc}"}),
                file=sys.stderr,
            )
            return 1
        if not isinstance(edits, list):
            print(
                json.dumps({"ok": False, "error": "edits JSON must be a list"}),
                file=sys.stderr,
            )
            return 1
    else:
        if args.old is None or args.new is None:
            print(
                json.dumps(
                    {"ok": False, "error": "provide --old and --new, or --edits-json"}
                ),
                file=sys.stderr,
            )
            return 1
        edits = [
            {
                "old": _unescape_cli(args.old),
                "new": _unescape_cli(args.new),
                "replace_all": args.replace_all,
            }
        ]

    port = _ensure_daemon(path, open_browser=not args.no_browser)
    status, data = _request(port, "POST", "/api/edit", {"edits": edits})
    data["url"] = f"http://127.0.0.1:{port}"
    print(json.dumps(data, indent=2))
    return 0 if data.get("ok") else 1


def cmd_review(args) -> int:
    path = Path(args.file).resolve()
    if not path.exists():
        print(
            json.dumps({"ok": False, "error": f"file not found: {path}"}),
            file=sys.stderr,
        )
        return 1

    port = _ensure_daemon(path, open_browser=not args.no_browser)

    if not args.json:
        print(
            f"Review open at http://127.0.0.1:{port} — "
            f"waiting for you to click “Send to LLM”…",
            file=sys.stderr,
        )

    # Block until the user clicks Send (or timeout). We poll the server.
    deadline = None if args.timeout <= 0 else time.monotonic() + args.timeout
    while True:
        try:
            status, data = _request(port, "GET", "/api/comments", timeout=5.0)
        except OSError:
            print(json.dumps({"ok": False, "error": "session ended"}), file=sys.stderr)
            return 1
        if data.get("submitted"):
            break
        if deadline is not None and time.monotonic() > deadline:
            break
        time.sleep(0.6)

    try:
        _, payload = _request(port, "GET", "/api/comments", timeout=5.0)
    except OSError:
        print(json.dumps({"ok": False, "error": "session ended"}), file=sys.stderr)
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
        print(
            json.dumps({"ok": False, "error": "provide one or more --id, or --all"}),
            file=sys.stderr,
        )
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

    print(
        json.dumps(
            {
                "ok": not missing,
                "resolved": resolved,
                "missing": missing,
                "url": f"http://127.0.0.1:{port}",
            },
            indent=2,
        )
    )
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
    _, data = _request(port, "POST", "/api/diffs/clear", {"keep_current": not args.all})
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
    print(
        json.dumps(
            {
                "running": True,
                "url": f"http://127.0.0.1:{port}",
                "version": data.get("version"),
                "submitted": data.get("submitted"),
                "edit_count": len(data.get("edits", [])),
                "comment_count": len(data.get("comments", [])),
            },
            indent=2,
        )
    )
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
    print(
        json.dumps(
            {
                "vendor_dir": str(VENDOR_DIR),
                "assets": VENDOR_ASSETS,
            },
            indent=2,
        )
    )
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mdedit.py", description="LLM-driven markdown editing & review tool."
    )
    p.add_argument(
        "--no-browser", action="store_true", help="Do not auto-open a browser tab."
    )
    p.add_argument(
        "--idle-timeout",
        type=float,
        default=None,
        help="Idle shutdown in seconds (default: 300). 0 disables. "
        "Overrides MDEDIT_IDLE_TIMEOUT.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("open", help="Open or focus the viewer for a document.")
    sp.add_argument("file")
    sp.set_defaults(func=cmd_open)

    sp = sub.add_parser("edit", help="Apply one or more search/replace edits.")
    sp.add_argument("file")
    sp.add_argument(
        "--old",
        help="Text to find (must occur once unless --replace-all). "
        "Backslash escapes are interpreted (e.g. \\n -> newline).",
    )
    sp.add_argument(
        "--new",
        help="Replacement text. Backslash escapes are interpreted (e.g. \\n -> newline).",
    )
    sp.add_argument(
        "--replace-all", action="store_true", help="Replace every occurrence of --old."
    )
    sp.add_argument(
        "--edits-json",
        metavar="PATH",
        help="Path to a JSON array of {old,new,replace_all} edits, or '-' for stdin.",
    )
    sp.set_defaults(func=cmd_edit)

    sp = sub.add_parser(
        "review", help="Block until the user sends comments; print them as JSON."
    )
    sp.add_argument("file")
    sp.add_argument(
        "--json", action="store_true", help="Suppress the human-readable stderr notice."
    )
    sp.add_argument(
        "--timeout",
        type=float,
        default=0,
        help="Max seconds to wait (0 = wait forever).",
    )
    sp.set_defaults(func=cmd_review)

    sp = sub.add_parser(
        "resolve", help="Resolve (delete) comments the LLM has addressed."
    )
    sp.add_argument("file")
    sp.add_argument(
        "--id",
        type=int,
        action="append",
        metavar="N",
        help="Comment id to resolve (repeatable).",
    )
    sp.add_argument(
        "--all", action="store_true", help="Resolve every outstanding comment."
    )
    sp.set_defaults(func=cmd_resolve)

    sp = sub.add_parser("status", help="Print the current session state.")
    sp.add_argument("file")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser(
        "clear-diffs",
        help="Clear the Changes-view diff history (document text is kept).",
    )
    sp.add_argument("file")
    sp.add_argument(
        "--all",
        action="store_true",
        help="Clear every round, including the current one.",
    )
    sp.set_defaults(func=cmd_clear_diffs)

    sp = sub.add_parser("stop", help="Shut the session daemon down.")
    sp.add_argument("file")
    sp.set_defaults(func=cmd_stop)

    sp = sub.add_parser(
        "vendor-manifest",
        help="Print the front-end asset manifest as JSON (used by update-vendor.sh).",
    )
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
