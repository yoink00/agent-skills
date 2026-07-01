"""Fast in-process unit tests for the mdedit HTTP handler (server.py).

These stand up the real :class:`Server` bound to an ephemeral loopback port in
a background thread — no ``fork``, no subprocess, no Playwright — so the request
routing, status codes, error handling, the blocking ``/api/review-wait`` route,
and the cross-origin POST guard can all be exercised in milliseconds.

The browser-driven suites (test_comment_highlights.py, test_share.py, …) cover
the end-to-end UI; this file pins down the HTTP contract directly.
"""

import http.client
import json
import sys
import threading
import time
from pathlib import Path

import pytest

SKILL_DIR = "skills/markdown-review"
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

import mdedit  # noqa: E402
import server  # noqa: E402
from frontend import build_html  # noqa: E402
from model import Session  # noqa: E402
from server import Handler, Server  # noqa: E402

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture
def httpd(tmp_path):
    """Start the real Server in-process on an ephemeral port; tear down after."""
    doc = tmp_path / "doc.md"
    doc.write_text("# Title\n\nHello world.\n")

    session = Session(doc)

    class Bound(Handler):
        pass

    Bound.session = session
    Bound.html = build_html(doc.name)

    srv = Server(("127.0.0.1", 0), Bound)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield {"server": srv, "session": session, "port": port}
    finally:
        srv.shutdown()
        srv.server_close()


def _req(port, method, path, body=None, headers=None, timeout=8.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    payload = None
    hdrs = {}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    conn.request(method, path, body=payload, headers=hdrs)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8")
    conn.close()
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {"raw": raw}
    return resp.status, data


# ---------------------------------------------------------------------------
# State key collision (regression for the underscore-path bug)
# ---------------------------------------------------------------------------


class TestStateKey:
    def test_underscore_paths_do_not_collide(self):
        # /a_b/c and /a/b_c mapped to the same key under the old scheme.
        a = server._state_key(Path("/Users/me/a_b/c.md"))
        b = server._state_key(Path("/Users/me/a/b_c.md"))
        assert a != b

    def test_key_is_filename_safe(self):
        key = server._state_key(Path("/some/path with spaces/d.md"))
        assert "/" not in key
        assert "\x00" not in key

    def test_legacy_scheme_still_collides(self):
        # Documents *why* the legacy scheme was replaced: it is kept only for
        # reading pre-existing state, never for writing.
        a = server._legacy_state_key(Path("/x/a_b/c.md"))
        b = server._legacy_state_key(Path("/x/a/b_c.md"))
        assert a == b


# ---------------------------------------------------------------------------
# GET routing
# ---------------------------------------------------------------------------


class TestGetRouting:
    def test_state_returns_snapshot(self, httpd):
        status, data = _req(httpd["port"], "GET", "/api/state")
        assert status == 200
        assert data["name"] == "doc.md"
        assert "Hello world" in data["current_text"]

    def test_root_serves_html(self, httpd):
        status, raw = _req(httpd["port"], "GET", "/")
        assert status == 200
        assert "raw" in raw  # not JSON → returned as {"raw": ...}

    def test_ping(self, httpd):
        status, data = _req(httpd["port"], "GET", "/api/ping")
        assert status == 200
        assert data["ok"] is True

    def test_unknown_path_is_404(self, httpd):
        status, _ = _req(httpd["port"], "GET", "/api/nope")
        assert status == 404


# ---------------------------------------------------------------------------
# POST routing + error codes
# ---------------------------------------------------------------------------


class TestPostRouting:
    def test_edit_applies_and_returns_applied(self, httpd):
        status, data = _req(
            httpd["port"],
            "POST",
            "/api/edit",
            body={"edits": [{"old": "Hello", "new": "Hi"}]},
        )
        assert status == 200
        assert data["ok"] is True
        assert len(data["applied"]) == 1
        assert httpd["session"].current_text == "# Title\n\nHi world.\n"

    def test_edit_missing_old_returns_422_with_errors(self, httpd):
        status, data = _req(
            httpd["port"],
            "POST",
            "/api/edit",
            body={"edits": [{"old": "nope", "new": "x"}]},
        )
        assert status == 422
        assert data["ok"] is False
        assert data["errors"]
        assert "not found" in data["errors"][0]["error"]

    def test_comment_add_then_list(self, httpd):
        status, data = _req(
            httpd["port"],
            "POST",
            "/api/comment",
            body={"body": "note", "quote": "Hello"},
        )
        assert status == 200
        assert data["id"] == 1
        status, data = _req(httpd["port"], "GET", "/api/comments")
        assert status == 200
        assert len(data["comments"]) == 1

    def test_delete_missing_comment_returns_ok_false(self, httpd):
        status, data = _req(
            httpd["port"], "POST", "/api/comment/delete", body={"id": 999}
        )
        assert status == 200
        assert data["ok"] is False

    def test_edit_missing_comment_is_404(self, httpd):
        status, _ = _req(
            httpd["port"], "POST", "/api/comment/edit", body={"id": 999, "body": "x"}
        )
        assert status == 404

    def test_reply_missing_comment_is_404(self, httpd):
        status, _ = _req(
            httpd["port"],
            "POST",
            "/api/comment/reply",
            body={"comment_id": 999, "body": "x"},
        )
        assert status == 404

    def test_import_non_array_is_422(self, httpd):
        status, data = _req(
            httpd["port"], "POST", "/api/import", body={"comments": "not a list"}
        )
        assert status == 422
        assert data["ok"] is False

    def test_unknown_post_is_404(self, httpd):
        status, _ = _req(httpd["port"], "POST", "/api/nope", body={})
        assert status == 404

    def test_submit_then_reset(self, httpd):
        status, _ = _req(httpd["port"], "POST", "/api/submit", body={})
        assert status == 200
        assert httpd["session"].submitted is True
        status, _ = _req(httpd["port"], "POST", "/api/submit/reset", body={})
        assert httpd["session"].submitted is False
        assert httpd["session"]._new_round_pending is True


class TestMalformedBody:
    def test_malformed_json_body_is_400(self, httpd):
        conn = http.client.HTTPConnection("127.0.0.1", httpd["port"], timeout=5.0)
        conn.request(
            "POST",
            "/api/comment",
            body=b"not json{",
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        resp.read()
        conn.close()
        assert resp.status == 400


# ---------------------------------------------------------------------------
# Cross-origin hardening
# ---------------------------------------------------------------------------


class TestOriginGuard:
    def test_cross_origin_post_is_403(self, httpd):
        # Host is correct but Origin names a foreign site → reject.
        status, data = _req(
            httpd["port"],
            "POST",
            "/api/submit",
            body={},
            headers={"Origin": "https://evil.example"},
        )
        assert status == 403

    def test_cross_site_sec_fetch_is_403(self, httpd):
        status, _ = _req(
            httpd["port"],
            "POST",
            "/api/submit",
            body={},
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert status == 403

    def test_foreign_host_is_403(self, httpd):
        # A DNS-rebinding style request whose Host is not the loopback address.
        conn = http.client.HTTPConnection("127.0.0.1", httpd["port"], timeout=5.0)
        # http.client sets its own Host; override via skip_host + putheader.
        conn.putrequest("POST", "/api/submit", skip_host=True)
        conn.putheader("Host", "rebinding.attacker:1234")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "2")
        conn.endheaders(message_body=b"{}")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        assert resp.status == 403

    def test_same_origin_post_allowed(self, httpd):
        port = httpd["port"]
        status, _ = _req(
            httpd["port"],
            "POST",
            "/api/submit",
            body={},
            headers={
                "Origin": f"http://127.0.0.1:{port}",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        assert status == 200
        assert httpd["session"].submitted is True

    def test_cli_post_with_no_origin_allowed(self, httpd):
        # CLI/http.client sends Host but no Origin/Sec-Fetch-Site → must pass.
        status, _ = _req(httpd["port"], "POST", "/api/submit", body={})
        assert status == 200


# ---------------------------------------------------------------------------
# /api/review-wait — blocking submit waiter
# ---------------------------------------------------------------------------


class TestReviewWait:
    def test_returns_promptly_when_already_submitted(self, httpd):
        httpd["session"].submit()
        status, data = _req(httpd["port"], "GET", "/api/review-wait?t=2")
        assert status == 200
        assert data["submitted"] is True

    def test_blocks_then_returns_unsubmitted(self, httpd):
        start = time.monotonic()
        status, data = _req(httpd["port"], "GET", "/api/review-wait?t=0.4")
        elapsed = time.monotonic() - start
        assert status == 200
        assert data["submitted"] is False
        assert elapsed >= 0.35  # actually waited

    def test_wakes_when_submitted_from_another_thread(self, httpd):
        def submit_later():
            time.sleep(0.2)
            httpd["session"].submit()

        threading.Thread(target=submit_later).start()
        start = time.monotonic()
        status, data = _req(httpd["port"], "GET", "/api/review-wait?t=5")
        elapsed = time.monotonic() - start
        assert status == 200
        assert data["submitted"] is True
        # Woke promptly on submit rather than waiting the full window.
        assert elapsed < 2.0


# ---------------------------------------------------------------------------
# Server.handle_error — socketserver's real error hook (a handler-level
# handle_error is never called by the framework, so the logging must live on
# the Server subclass). Pins down the bug where the logging was attached to a
# dead Handler.handle_error and silently swallowed instead.
# ---------------------------------------------------------------------------


class TestErrorLogging:
    def test_handler_exception_is_logged_not_swallowed(
        self, httpd, monkeypatch, capsys
    ):
        def boom(_self):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(Handler, "do_GET", boom)

        conn = http.client.HTTPConnection("127.0.0.1", httpd["port"], timeout=3.0)
        conn.request("GET", "/api/ping")
        try:
            conn.getresponse().read()
        except http.client.RemoteDisconnected:
            pass
        conn.close()

        # handle_error runs in the server's worker thread; let it flush stderr.
        time.sleep(0.05)
        err = capsys.readouterr().err
        assert "mdedit handler error" in err
        assert "RuntimeError" in err
        assert "kaboom" in err


# ---------------------------------------------------------------------------
# Content-Length robustness — garbage / negative values must surface as a
# clean 400, not crash the handler (leaving the connection to time out).
# Regression for the ``int(Content-Length)`` + ``rfile.read(-5)`` bugs.
# ---------------------------------------------------------------------------


class TestContentLength:
    def _post_with_header(self, port, header_value):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
        conn.putrequest("POST", "/api/comment", skip_host=True)
        conn.putheader("Host", f"127.0.0.1:{port}")
        conn.putheader("Content-Length", header_value)
        conn.endheaders()
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status

    def test_garbage_content_length_returns_400(self, httpd):
        # ``int("not-a-number")`` used to raise ValueError → dropped connection.
        assert self._post_with_header(httpd["port"], "not-a-number") == 400

    def test_negative_content_length_returns_400(self, httpd):
        # ``rfile.read(-5)`` used to raise/hang instead of treating the body as
        # malformed.
        assert self._post_with_header(httpd["port"], "-5") == 400

    def test_non_dict_json_body_returns_400(self, httpd):
        # A bare JSON array is valid JSON but not the expected object shape.
        status, _ = _req(httpd["port"], "POST", "/api/comment", body=[1, 2, 3])
        assert status == 400


# ---------------------------------------------------------------------------
# _is_authorized_local — edge cases for the Host/Origin guards.
# ---------------------------------------------------------------------------


class TestOriginGuardEdgeCases:
    def test_uppercase_host_is_accepted(self, httpd):
        # RFC 7230 §5.4: the Host header's host component is case-insensitive.
        port = httpd["port"]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
        conn.putrequest("POST", "/api/submit", skip_host=True)
        conn.putheader("Host", f"LOCALHOST:{port}")
        conn.putheader("Content-Length", "2")
        conn.endheaders(message_body=b"{}")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        assert resp.status == 200

    def test_missing_host_is_rejected(self, httpd):
        conn = http.client.HTTPConnection("127.0.0.1", httpd["port"], timeout=5.0)
        conn.putrequest("POST", "/api/submit", skip_host=True)
        conn.putheader("Content-Length", "2")
        conn.endheaders(message_body=b"{}")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        assert resp.status == 403

    def test_localhost_origin_accepted(self, httpd):
        port = httpd["port"]
        status, _ = _req(
            httpd["port"],
            "POST",
            "/api/submit",
            body={},
            headers={"Origin": f"http://localhost:{port}"},
        )
        assert status == 200


# ---------------------------------------------------------------------------
# Long-path state key — must not exceed NAME_MAX (~255 bytes).
# ---------------------------------------------------------------------------


class TestStateKeyLongPath:
    def test_long_path_falls_back_to_sha256(self):
        long_path = Path("/" + "a" * 250 + "/doc.md")
        key = server._state_key(long_path)
        assert len(key) <= 255
        # SHA-256 hex digest is exactly 64 chars and only hex digits.
        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)

    def test_short_path_still_uses_base64(self):
        path = Path("/short/path.md")
        import base64 as _b64

        expected = _b64.urlsafe_b64encode(str(path).encode()).decode()
        assert server._state_key(path) == expected

    def test_two_long_paths_do_not_collide(self):
        a = server._state_key(Path("/" + "a" * 250 + "/doc.md"))
        b = server._state_key(Path("/" + "b" * 250 + "/doc.md"))
        assert a != b


# ---------------------------------------------------------------------------
# _save_session — atomic write + tmp cleanup on failure.
# ---------------------------------------------------------------------------


class TestSaveSession:
    def test_atomic_write_creates_session_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(server, "STATE_DIR", tmp_path / "state")
        doc = tmp_path / "doc.md"
        doc.write_text("hi\n")
        session = Session(doc)
        server._save_session(session)
        sf = server._session_file(doc)
        assert sf.exists()
        data = json.loads(sf.read_text())
        assert data["current_text"] == "hi\n"

    def test_cleans_up_tmp_on_write_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(server, "STATE_DIR", tmp_path / "state")
        doc = tmp_path / "doc.md"
        doc.write_text("hi\n")
        session = Session(doc)
        sf = server._session_file(doc)
        tmp = sf.parent / (sf.name + ".tmp")

        def boom(*_a, **_kw):
            raise OSError("disk on fire")

        monkeypatch.setattr("os.replace", boom)
        server._save_session(session)
        assert not tmp.exists()


# ---------------------------------------------------------------------------
# _purge_session — must remove every state file (session + daemon, current +
# legacy), not just the session.json under the current key.
# ---------------------------------------------------------------------------


class TestPurgeSession:
    def test_removes_current_daemon_and_session_files(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        monkeypatch.setattr(server, "STATE_DIR", state)
        doc = tmp_path / "doc.md"
        doc.write_text("hi\n")

        sf = server._state_file(doc)
        sf.write_text('{"port": 1234}')
        session_file = server._session_file(doc)
        session_file.write_text('{"current_text": "hi"}')

        removed = server._purge_session(doc)
        assert removed is True
        assert not sf.exists()
        assert not session_file.exists()

    def test_removes_legacy_daemon_file(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        monkeypatch.setattr(server, "STATE_DIR", state)
        doc = tmp_path / "doc.md"
        doc.write_text("hi\n")

        legacy = server._legacy_daemon_file(doc)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text('{"port": 1234}')

        assert server._purge_session(doc) is True
        assert not legacy.exists()

    def test_returns_false_when_nothing_to_remove(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        monkeypatch.setattr(server, "STATE_DIR", state)
        doc = tmp_path / "doc.md"
        doc.write_text("hi\n")
        assert server._purge_session(doc) is False


# ---------------------------------------------------------------------------
# Legacy-key fallback — sessions saved under the old underscore scheme must
# still be loadable.
# ---------------------------------------------------------------------------


class TestLegacyFallback:
    def test_load_session_finds_legacy_session_file(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        monkeypatch.setattr(server, "STATE_DIR", state)
        doc = tmp_path / "doc.md"

        legacy = server._legacy_session_file(doc)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(
            json.dumps({"current_text": "legacy\n", "original_text": "legacy\n"})
        )

        loaded = server._load_session(doc)
        assert loaded is not None
        assert loaded["current_text"] == "legacy\n"

    def test_read_state_file_finds_legacy_daemon_file(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        monkeypatch.setattr(server, "STATE_DIR", state)
        doc = tmp_path / "doc.md"

        legacy = server._legacy_daemon_file(doc)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text('{"port": 9999}')

        found = server._read_state_file(doc)
        assert found == legacy


# ---------------------------------------------------------------------------
# cmd_review blocking loop against the in-process httpd — exercises the
# deadline/wait_t boundary math (timeout expiry, prompt submit, mid-wait wake)
# without forking a subprocess.
# ---------------------------------------------------------------------------


class TestCmdReviewBlocking:
    def test_timeout_expires_returns_unsubmitted(
        self, monkeypatch, httpd, tmp_path, capsys
    ):
        monkeypatch.setattr(mdedit, "_ensure_daemon", lambda *a, **kw: httpd["port"])
        doc = tmp_path / "doc.md"
        doc.write_text("# Test\n")
        args = mdedit.build_parser().parse_args(
            ["--no-browser", "review", "--json", str(doc), "--timeout", "0.5"]
        )
        start = time.monotonic()
        rc = mdedit.cmd_review(args)
        elapsed = time.monotonic() - start
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        assert result["submitted"] is False
        assert elapsed < 3.0

    def test_returns_promptly_when_already_submitted(
        self, monkeypatch, httpd, tmp_path, capsys
    ):
        monkeypatch.setattr(mdedit, "_ensure_daemon", lambda *a, **kw: httpd["port"])
        httpd["session"].submit()
        doc = tmp_path / "doc.md"
        doc.write_text("# Test\n")
        args = mdedit.build_parser().parse_args(
            ["--no-browser", "review", "--json", str(doc), "--timeout", "5"]
        )
        rc = mdedit.cmd_review(args)
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        assert result["submitted"] is True
        # cmd_review resets the flag so the next review blocks again.
        assert httpd["session"].submitted is False

    def test_wakes_when_submitted_mid_wait(self, monkeypatch, httpd, tmp_path, capsys):
        monkeypatch.setattr(mdedit, "_ensure_daemon", lambda *a, **kw: httpd["port"])

        def submit_later():
            time.sleep(0.3)
            httpd["session"].submit()

        threading.Thread(target=submit_later, daemon=True).start()
        doc = tmp_path / "doc.md"
        doc.write_text("# Test\n")
        args = mdedit.build_parser().parse_args(
            ["--no-browser", "review", "--json", str(doc), "--timeout", "5"]
        )
        start = time.monotonic()
        rc = mdedit.cmd_review(args)
        elapsed = time.monotonic() - start
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        assert result["submitted"] is True
        assert elapsed < 2.0  # woke promptly, did not wait the full 5s
