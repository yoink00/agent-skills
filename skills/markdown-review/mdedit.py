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
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# The single-page browser viewer (HTML/CSS/JS) and the vendored front-end
# asset manifest live in frontend.py. Re-exported here because the HTTP
# handler serves vendored assets and the daemon renders the page.
from frontend import (
    _VENDOR_MIME,
    VENDOR_ASSETS,
    VENDOR_DIR,
    build_html,
)
from model import Session

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_PORT = 7575
# Where we record running sessions so CLI clients can find the daemon.
STATE_DIR = Path(os.environ.get("MDEDIT_STATE_DIR", Path.home() / ".cache" / "mdedit"))

# The vendored front-end libraries (marked, highlight.js), their MIME map and
# the single-page HTML/CSS/JS viewer now live in frontend.py (imported above).
# That keeps the ~700-line embedded front-end out of this file.


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
            self._serve_vendor(path[len("/vendor/") :])
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        p = urlparse(self.path)
        path = p.path
        if path == "/api/edit":
            data = self._read_body()
            edits = data.get("edits")
            if edits is None:
                edits = [
                    {
                        "old": data.get("old", ""),
                        "new": data.get("new", ""),
                        "replace_all": data.get("replace_all", False),
                    }
                ]
            applied, errors = [], []
            for i, e in enumerate(edits):
                try:
                    rec = self.session.apply_edit(
                        e.get("old", ""),
                        e.get("new", ""),
                        bool(e.get("replace_all", False)),
                    )
                    applied.append(rec.to_dict())
                except ValueError as exc:
                    errors.append({"index": i, "error": str(exc)})
            self._json(
                {
                    "applied": applied,
                    "errors": errors,
                    "version": self.session.version,
                    "ok": not errors,
                },
                200 if not errors else 422,
            )
        elif path == "/api/comment":
            data = self._read_body()
            c = self.session.add_comment(
                data.get("body", ""),
                data.get("quote", ""),
                data.get("context_before", ""),
                data.get("context_after", ""),
                data.get("source", "doc"),
                int(data.get("round", 0) or 0),
            )
            self._json(c.to_dict())
        elif path == "/api/comment/delete":
            data = self._read_body()
            ok = self.session.delete_comment(int(data.get("id", -1)))
            self._json({"ok": ok})
        elif path == "/api/diffs/clear":
            data = self._read_body()
            keep_current = bool(data.get("keep_current", True))
            removed = self.session.clear_diffs(keep_current=keep_current)
            self._json(
                {
                    "ok": True,
                    "removed": removed,
                    "current_round": self.session.current_round,
                }
            )
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


def _request(
    port: int, method: str, path: str, body: dict | None = None, timeout: float = 30.0
) -> tuple[int, dict]:
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
    sf.write_text(
        json.dumps(
            {
                "port": port,
                "path": str(path.resolve()),
                "pid": os.getpid(),
                "started": time.time(),
            }
        )
    )

    if open_browser:
        threading.Timer(
            0.4, lambda: webbrowser.open(f"http://127.0.0.1:{port}")
        ).start()

    # Idle auto-shutdown.
    idle_timeout = float(os.environ.get("MDEDIT_IDLE_TIMEOUT", "300"))
    if idle_timeout > 0:
        threading.Thread(
            target=_idle_reaper, args=(session, httpd, idle_timeout), daemon=True
        ).start()

    # Clean shutdown on SIGTERM / SIGHUP so `kill <pid>` removes the state file
    # via the `finally` block below.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(
            sig, lambda *_: threading.Thread(target=httpd.shutdown, daemon=True).start()
        )

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
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "0": "\0",
    "\\": "\\",
    '"': '"',
    "'": "'",
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
    return _ESCAPE_RE.sub(lambda m: _BACKSLASH_ESCAPES.get(m.group(1), m.group(0)), s)


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
