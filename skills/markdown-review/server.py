"""HTTP daemon for mdedit: the request handler, the session registry, the
HTTP client helpers used by the CLI, and the daemon lifecycle (spawn, idle
shutdown, signal handling).

One daemon serves one document, keyed by absolute path. The first CLI command
for a document auto-spawns the daemon (forked, detached); later CLI clients
discover it via a state file under ``STATE_DIR`` and talk to it over localhost.

Pure standard library. Depends on the session model (``model.Session``) and the
front-end (``frontend.build_html`` plus the vendored-asset config).
"""

from __future__ import annotations

import http.client
import http.server
import json
import os
import signal
import socket
import socketserver
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from frontend import (
    _VENDOR_MIME,
    VENDOR_ASSETS,
    VENDOR_DIR,
    build_html,
    build_share_html,
)
from model import Session

DEFAULT_PORT = 7575
# Where we record running sessions so CLI clients can find the daemon.
STATE_DIR = Path(os.environ.get("MDEDIT_STATE_DIR", Path.home() / ".cache" / "mdedit"))


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
        elif path == "/api/share":
            html = build_share_html(self.session.snapshot()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(html)
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
                data.get("author", "You"),
                bool(data.get("stale", False)),
            )
            self._json(c.to_dict())
        elif path == "/api/comment/delete":
            data = self._read_body()
            ok = self.session.delete_comment(int(data.get("id", -1)))
            self._json({"ok": ok})
        elif path == "/api/comment/edit":
            data = self._read_body()
            c = self.session.edit_comment(int(data.get("id", -1)), data.get("body", ""))
            if c is not None:
                self._json(c.to_dict())
            else:
                self._json({"ok": False, "error": "comment not found"}, 404)
        elif path == "/api/comment/reply":
            data = self._read_body()
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
            data = self._read_body()
            # Accept either {"comments": [...]} or a bare [...].
            payload = data.get("comments", []) if isinstance(data, dict) else data
            if not isinstance(payload, list):
                self._json({"ok": False, "error": "expected a comments array"}, 422)
                return
            summary = self.session.import_comments(payload)
            self._json({"ok": True, **summary})
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


def _state_key(path: Path) -> str:
    """Return the filesystem-safe key derived from a document's absolute path."""
    return str(path.resolve()).replace(os.sep, "_").replace(":", "_")


def _state_file(path: Path) -> Path:
    """Return the per-document daemon state file: ``<STATE_DIR>/<key>/daemon.json``."""
    key = _state_key(path)
    d = STATE_DIR / key
    d.mkdir(parents=True, exist_ok=True)
    return d / "daemon.json"


def _legacy_state_file(path: Path) -> Path:
    """Return the old-style flat state file for backward-compat reads."""
    key = _state_key(path)
    return STATE_DIR / f"{key}.json"


def _session_file(path: Path) -> Path:
    """Return the per-document session snapshot file: ``<STATE_DIR>/<key>/session.json``."""
    key = _state_key(path)
    d = STATE_DIR / key
    d.mkdir(parents=True, exist_ok=True)
    return d / "session.json"


def _read_state_file(path: Path) -> Path | None:
    """Return the existing state file (new or legacy) for *path*, or None."""
    sf = _state_file(path)
    if sf.exists():
        return sf
    legacy = _legacy_state_file(path)
    if legacy.exists():
        return legacy
    return None


def _save_session(session: Session) -> None:
    """Write the session snapshot to disk (write-through persistence)."""
    sf = _session_file(session.path)
    try:
        snap = session.snapshot()
        sf.write_text(json.dumps(snap), encoding="utf-8")
    except OSError:
        pass


def _load_session(path: Path) -> dict | None:
    """Read the saved session snapshot for *path*, or None if not found."""
    sf = _session_file(path)
    if not sf.exists():
        return None
    try:
        return json.loads(sf.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _purge_session(path: Path) -> bool:
    """Delete the saved session snapshot for *path*. Returns True if removed."""
    sf = _session_file(path)
    try:
        if sf.exists():
            sf.unlink()
            return True
    except OSError:
        pass
    return False


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
        with session._cond:
            session._cond.wait(timeout=check_interval)
            idle = time.time() - session.last_activity
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
