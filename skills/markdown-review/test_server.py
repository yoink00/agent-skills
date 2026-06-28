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
