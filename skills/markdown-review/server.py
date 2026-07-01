"""HTTP daemon for mdedit: the request handler, the session registry, the
HTTP client helpers used by the CLI, and the daemon lifecycle (spawn, idle
shutdown, signal handling).

One daemon serves one document, keyed by absolute path. The first CLI command
for a document auto-spawns the daemon (forked, detached); later CLI clients
discover it via a state file under ``STATE_DIR`` and talk to it over localhost.

Pure standard library. Depends on the session model (``model.Session``) and
the front-end (``frontend.build_html``).
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import http.server
import json
import os
import signal
import socket
import socketserver
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from frontend import build_html, build_share_html
from model import Session

DEFAULT_PORT = 7575
# Where we record running sessions so CLI clients can find the daemon.
STATE_DIR = Path(os.environ.get("MDEDIT_STATE_DIR", Path.home() / ".cache" / "mdedit"))

# Most filesystems cap a single path component at 255 bytes (NAME_MAX).
# Base64 of a ~190-char path already reaches that, so longer paths fall back
# to a fixed-length SHA-256 digest (see ``_state_key``).
_MAX_STATE_KEY_LEN = 200


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

    def _read_body(self) -> dict | None:
        """Read and JSON-decode the request body.

        Returns the decoded dict, an empty dict when no body was sent, or
        ``None`` when a body *was* sent but could not be parsed as a JSON
        object (so callers can surface a 400 rather than silently treating a
        malformed request as "no edits").
        """
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            return None
        if length < 0:
            return None
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(obj, dict):
            return None
        return obj

    def _is_authorized_local(self) -> bool:
        """Reject cross-origin mutating requests.

        The daemon listens on the loopback interface only, but a malicious page
        visited by the user can still issue "simple" CORS requests (e.g. with
        ``Content-Type: text/plain``) at ``http://127.0.0.1:<port>`` — which the
        browser does not preflight and which the server would otherwise happily
        honour (editing the document, stopping the session, …). Require the
        ``Host`` to name the local address; when the browser advertises an
        ``Origin`` it must be one of the local origins, and ``Sec-Fetch-Site``
        must not be ``cross-site``. CLI clients (no ``Origin``,
        ``Host: 127.0.0.1:<port>``) pass.
        """
        port = 0
        addr = self.server.server_address
        if isinstance(addr, tuple):
            port = addr[1]
        local_hosts = (f"127.0.0.1:{port}", f"[::1]:{port}", f"localhost:{port}")
        # Host is case-insensitive per RFC 7230 §5.4; normalise so a browser
        # or CLI that sends ``Host: Localhost:<port>`` is not wrongly rejected.
        if self.headers.get("Host", "").lower() not in local_hosts:
            return False
        origin = self.headers.get("Origin")
        if origin:
            local_origins = {
                f"http://127.0.0.1:{port}",
                f"http://[::1]:{port}",
                f"http://localhost:{port}",
            }
            if origin not in local_origins:
                return False
        sfs = self.headers.get("Sec-Fetch-Site", "")
        if sfs and sfs not in ("same-origin", "same-site", "none"):
            return False
        return True

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
        elif path == "/api/review-wait":
            # Blocking read used by the CLI ``review`` command. Blocks inside
            # ``wait_for_submit`` (which releases the lock while waiting, so the
            # browser's poll and other reads proceed concurrently) until the
            # user submits or the per-call timeout elapses, then returns the
            # comments payload. ``touch`` on entry keeps the idle reaper alive
            # across a long human-wait. Capped at 25s per call so the CLI loop
            # also enforces its own deadline and detects a dead daemon promptly.
            q = parse_qs(p.query)
            try:
                wait_t = float(q.get("t", ["20"])[0])
            except (ValueError, IndexError):
                wait_t = 20.0
            wait_t = max(0.0, min(wait_t, 25.0))
            self.session.touch()
            self.session.wait_for_submit(timeout=wait_t)
            self._json(self.session.comments_payload())
        elif path == "/api/share":
            html = build_share_html(self.session.snapshot()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(html)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._is_authorized_local():
            self._json({"error": "forbidden"}, 403)
            return
        p = urlparse(self.path)
        path = p.path
        data = self._read_body()
        if data is None:
            self._json({"error": "invalid JSON body"}, 400)
            return
        if path == "/api/edit":
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
            c = self.session.add_comment(
                data.get("body", ""),
                data.get("quote", ""),
                data.get("context_before", ""),
                data.get("context_after", ""),
                data.get("source", "doc"),
                int(data.get("round", 0) or 0),
                data.get("author", "You"),
                bool(data.get("stale", False)),
            )
            self._json(c.to_dict())
        elif path == "/api/comment/delete":
            ok = self.session.delete_comment(int(data.get("id", -1)))
            self._json({"ok": ok})
        elif path == "/api/comment/edit":
            c = self.session.edit_comment(int(data.get("id", -1)), data.get("body", ""))
            if c is not None:
                self._json(c.to_dict())
            else:
                self._json({"ok": False, "error": "comment not found"}, 404)
        elif path == "/api/comment/reply":
            r = self.session.add_reply(
                int(data.get("comment_id", -1)),
                data.get("body", ""),
                data.get("author", "You"),
            )
            if r is not None:
                self._json(r.to_dict())
            else:
                self._json({"ok": False, "error": "comment not found"}, 404)
        elif path == "/api/import":
            payload = data.get("comments", [])
            if not isinstance(payload, list):
                self._json({"ok": False, "error": "expected a comments array"}, 422)
                return
            summary = self.session.import_comments(payload)
            self._json({"ok": True, **summary})
        elif path == "/api/diffs/clear":
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

    def handle_error(self, _request, client_address):
        # ``socketserver`` routes any exception raised inside a request handler
        # to here (see ``ThreadingTCPServer.process_request_thread``); this is
        # the framework's real error hook — a handler-level ``handle_error`` is
        # never called. Don't dump a full traceback (the daemon's std streams
        # are /dev/null in production), but never swallow silently either: a
        # one-liner to stderr keeps routing/handler bugs debuggable in the
        # foreground. (BrokenPipe/ConnectionReset are absorbed earlier in
        # ``Handler.handle`` and never reach here.)
        try:
            exc = sys.exc_info()[1]
            msg = f"{type(exc).__name__}: {exc}" if exc else "unknown error"
        except Exception:
            msg = "unknown error"
        sys.stderr.write(f"mdedit handler error from {client_address}: {msg}\n")


# ---------------------------------------------------------------------------
# Daemon registry — map a document path to a running server's port
# ---------------------------------------------------------------------------


def _state_key(path: Path) -> str:
    """Return a collision-free, filesystem-safe key for a document's absolute path.

    Earlier versions derived the key by replacing path separators with ``_`` —
    but ``/a_b/c`` and ``/a/b_c`` then mapped to the same key, so two different
    documents silently shared one daemon/session state file. URL-safe base64 of
    the resolved path is injective and uses only ``[A-Za-z0-9_-]`` (no path
    separators), so each document gets its own state directory.

    For very long paths the base64 form would exceed NAME_MAX (~255 bytes) and
    ``mkdir`` would fail; those fall back to a fixed-length SHA-256 digest,
    which is still injective for all practical purposes.
    """
    raw = str(path.resolve()).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii")
    if len(encoded) <= _MAX_STATE_KEY_LEN:
        return encoded
    return hashlib.sha256(raw).hexdigest()


def _legacy_state_key(path: Path) -> str:
    """Old separator-replacement key scheme — kept only to read pre-existing state.

    This scheme collides on underscore-bearing paths (see :func:`_state_key`),
    so it is never used for *writing*; it exists purely so sessions saved under
    the old scheme can still be found and resumed after an upgrade.
    """
    return str(path.resolve()).replace(os.sep, "_").replace(":", "_")


def _state_file(path: Path) -> Path:
    """Return the per-document daemon state file: ``<STATE_DIR>/<key>/daemon.json``."""
    key = _state_key(path)
    d = STATE_DIR / key
    d.mkdir(parents=True, exist_ok=True)
    return d / "daemon.json"


def _legacy_state_file(path: Path) -> Path:
    """Return the old-style flat daemon state file for backward-compat reads."""
    key = _legacy_state_key(path)
    return STATE_DIR / f"{key}.json"


def _legacy_daemon_file(path: Path) -> Path:
    """Return the old-style per-directory daemon state file for backward-compat."""
    key = _legacy_state_key(path)
    return STATE_DIR / key / "daemon.json"


def _session_file(path: Path) -> Path:
    """Return the per-document session snapshot file: ``<STATE_DIR>/<key>/session.json``."""
    key = _state_key(path)
    d = STATE_DIR / key
    d.mkdir(parents=True, exist_ok=True)
    return d / "session.json"


def _legacy_session_file(path: Path) -> Path:
    """Return the old-style session snapshot file for backward-compat reads."""
    key = _legacy_state_key(path)
    return STATE_DIR / key / "session.json"


def _read_state_file(path: Path) -> Path | None:
    """Return the existing daemon state file (current or any legacy form) for *path*."""
    for sf in (_state_file(path), _legacy_state_file(path), _legacy_daemon_file(path)):
        if sf.exists():
            return sf
    return None


def _save_session(session: Session) -> None:
    """Write the session snapshot to disk atomically (write-through persistence).

    Writes to a sibling temp file then ``os.replace``s it into place so a crash
    mid-write cannot leave a truncated ``session.json`` (which would silently
    lose the resumable session on the next load).
    """
    sf = _session_file(session.path)
    tmp = sf.parent / (sf.name + ".tmp")
    try:
        snap = session.snapshot()
        tmp.write_text(json.dumps(snap), encoding="utf-8")
        os.replace(tmp, sf)
    except OSError:
        # Don't leave the temp file behind on a failed write (it would
        # accumulate across failures and confuse a later ``os.replace``).
        try:
            tmp.unlink()
        except OSError:
            pass


def _load_session(path: Path) -> dict | None:
    """Read the saved session snapshot for *path* (current or legacy form), or None."""
    for sf in (_session_file(path), _legacy_session_file(path)):
        if sf.exists():
            try:
                return json.loads(sf.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return None
    return None


def _purge_session(path: Path) -> bool:
    """Delete every saved state file for *path* (current + legacy forms).

    Removes both the resumable ``session.json`` and the daemon's
    ``daemon.json`` under the current and legacy key schemes, so ``stop
    --purge`` leaves nothing behind. Returns True if any file was removed.
    """
    removed = False
    for sf in (
        _session_file(path),
        _state_file(path),
        _legacy_session_file(path),
        _legacy_daemon_file(path),
        _legacy_state_file(path),
    ):
        try:
            if sf.exists():
                sf.unlink()
                removed = True
        except OSError:
            pass
    return removed


def _find_running(path: Path) -> int | None:
    """Return the port of a live daemon for `path`, or None."""
    sf = _read_state_file(path)
    if sf is None:
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
        idle = session.wait_idle(check_interval)
        if idle >= timeout:
            threading.Thread(target=server.shutdown, daemon=True).start()
            return


# ---------------------------------------------------------------------------
# Daemon entry point (run in a forked child)
# ---------------------------------------------------------------------------


def _run_daemon(path: Path, port: int, open_browser: bool, restore: bool = False):
    session = Session(path)

    # Restore saved session state if requested and available.
    if restore:
        saved = _load_session(path)
        if saved is not None:
            session.restore(saved)
            # If the document was edited on disk while the daemon was down, the
            # file is the source of truth: re-seed the session text from it,
            # drop the now-invalid diff history, and re-flag stale comments.
            try:
                disk_text = path.read_text(encoding="utf-8")
            except OSError:
                disk_text = session.current_text
            if disk_text != session.current_text:
                session.reconcile_disk(disk_text)

    # Set up write-through persistence.
    session.on_change = lambda: _save_session(session)
    # Save once immediately so the session file exists even before any edits.
    _save_session(session)

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


def _spawn_daemon(path: Path, open_browser: bool, restore: bool = False) -> int:
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
            _run_daemon(path, port, open_browser, restore=restore)
        finally:
            os._exit(0)

    # Parent: wait for the daemon to come up.
    for _ in range(100):
        if _ping(port, path):
            return port
        time.sleep(0.05)
    raise RuntimeError("daemon failed to start")


def _ensure_daemon(path: Path, open_browser: bool, restore: bool = False) -> int:
    existing = _find_running(path)
    if existing:
        return existing
    return _spawn_daemon(path, open_browser, restore=restore)
