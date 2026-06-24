"""End-to-end browser tests for editing comments and replying to comments.

These drive the viewer with a headless browser via Playwright, covering the
two comment interactions that unit tests can't reach:

  * **Edit** — clicking the Edit button turns a comment body into an inline
    textarea; saving updates the comment (and, on the live page, persists it
    to the running session).
  * **Reply** — clicking the Reply button opens an inline form; submitting
    adds a threaded reply under the comment. Replies survive the
    share→export→import→live round-trip.

The share-page tests are daemon-free (they drive the standalone HTML); the
live-session tests spawn the real mdedit daemon and verify persistence via
the CLI `status` / `import-comments` commands.

Requires Playwright + chromium:
    pip install playwright && python -m playwright install chromium
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent
MDEDIT = SKILL_DIR / "mdedit.py"

playwright = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

# frontend is imported only to build the share HTML; no daemon needed.
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))
import frontend  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _snapshot(
    name="share-doc.md", text="# Shared Plan\n\nShip feature X in two weeks.\n"
):
    return {
        "name": name,
        "version": 1,
        "submitted": False,
        "current_round": 1,
        "original_text": text,
        "current_text": text,
        "edits": [],
        "comments": [],
    }


def _write_share_html(tmp_path: Path, snapshot: dict | None = None) -> Path:
    html = frontend.build_share_html(snapshot or _snapshot())
    out = tmp_path / "share.share.html"
    out.write_text(html, encoding="utf-8")
    return out


def _set_author(page, name="Alice"):
    """Dismiss the author prompt with the given name."""
    page.fill("#author-input", name)
    page.click("#author-save")
    page.wait_for_selector("#author-prompt.hidden", state="hidden")


def _add_general_comment(page, body="original note"):
    """Add a general comment via the sidebar box and wait for its card."""
    page.fill("#general-input", body)
    page.click("#general-add")
    page.wait_for_selector(".comment-card")


def _env(state_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["MDEDIT_STATE_DIR"] = str(state_dir)
    env["MDEDIT_IDLE_TIMEOUT"] = "0"
    return env


def _state_file(state_dir: Path) -> dict | None:
    files = list(state_dir.glob("*/daemon.json"))
    if not files:
        files = list(state_dir.glob("*.json"))
    if not files:
        return None
    return json.loads(files[0].read_text())


def _spawn_daemon(state_dir: Path, doc: Path) -> dict:
    env = _env(state_dir)
    subprocess.run(
        [sys.executable, str(MDEDIT), "--no-browser", "open", str(doc)],
        check=True,
        capture_output=True,
        env=env,
        timeout=15,
    )
    for _ in range(50):
        info = _state_file(state_dir)
        if info:
            return info
        time.sleep(0.1)
    raise RuntimeError("daemon state file never appeared")


def _stop(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)


# ---------------------------------------------------------------------------
# Edit comment — share page (no daemon)
# ---------------------------------------------------------------------------


def test_edit_comment_share_page_updates_body(tmp_path):
    """Clicking Edit, changing the textarea, and Save updates the visible
    comment body in-place."""
    html_path = _write_share_html(tmp_path)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(html_path.as_uri())
        page.wait_for_selector("#md-render")
        _set_author(page, "Alice")
        _add_general_comment(page, "original wording")

        # Enter edit mode.
        page.click(".comment-card .edit-btn")
        page.wait_for_selector(".comment-card .edit-area")
        area = page.locator(".comment-card .edit-area")
        assert area.input_value() == "original wording"
        # Clear and type a new body.
        area.fill("revised wording")
        page.click(".comment-card .edit-actions .save")

        # The edit form is replaced by the updated body text.
        page.wait_for_function(
            "() => document.querySelector('.comment-card .body')"
            ".textContent.includes('revised wording')",
            timeout=5000,
        )
        body = page.locator(".comment-card .body").inner_text()
        assert "revised wording" in body
        assert "original wording" not in body
        browser.close()


def test_edit_comment_cancel_restores_body(tmp_path):
    """Cancelling an edit restores the original body without saving."""
    html_path = _write_share_html(tmp_path)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(html_path.as_uri())
        page.wait_for_selector("#md-render")
        _set_author(page, "Alice")
        _add_general_comment(page, "keep me")

        page.click(".comment-card .edit-btn")
        page.wait_for_selector(".comment-card .edit-area")
        page.locator(".comment-card .edit-area").fill("throwaway")
        page.click(".comment-card .edit-actions .cancel")

        # Original body is restored.
        page.wait_for_function(
            "() => document.querySelector('.comment-card .body')"
            ".textContent.includes('keep me')",
            timeout=5000,
        )
        assert "throwaway" not in page.locator(".comment-card .body").inner_text()
        browser.close()


def test_edit_comment_reflected_in_export(tmp_path):
    """An edited comment body is what gets exported."""
    html_path = _write_share_html(tmp_path)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(html_path.as_uri())
        page.wait_for_selector("#md-render")
        _set_author(page, "Alice")
        _add_general_comment(page, "original")

        page.click(".comment-card .edit-btn")
        page.wait_for_selector(".comment-card .edit-area")
        page.locator(".comment-card .edit-area").fill("final wording")
        page.click(".comment-card .edit-actions .save")
        page.wait_for_function(
            "() => document.querySelector('.comment-card .body')"
            ".textContent.includes('final wording')",
            timeout=5000,
        )

        page.click("#send-btn")
        page.wait_for_selector("#export-box.open")
        exported = json.loads(page.locator("#export-json").input_value())
        assert len(exported["comments"]) == 1
        assert exported["comments"][0]["body"] == "final wording"
        browser.close()


# ---------------------------------------------------------------------------
# Reply to comment — share page (no daemon)
# ---------------------------------------------------------------------------


def test_reply_comment_share_page(tmp_path):
    """Replying to a comment adds a threaded item under the card with the
    author's name and body."""
    html_path = _write_share_html(tmp_path)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(html_path.as_uri())
        page.wait_for_selector("#md-render")
        _set_author(page, "Alice")
        _add_general_comment(page, "main thread")

        # Open the reply form and submit.
        page.click(".comment-card .reply-btn")
        page.wait_for_selector(".comment-card .reply-form")
        page.locator(".comment-card .reply-form textarea").fill("agreed, let's do it")
        page.click(".comment-card .reply-form .send")

        # The reply renders under the comment.
        page.wait_for_selector(".comment-card .reply-item", timeout=5000)
        reply = page.locator(".comment-card .reply-item")
        assert reply.locator(".reply-author").inner_text() == "Alice"
        assert "agreed" in reply.locator(".reply-body").inner_text()
        browser.close()


def test_reply_cancel_removes_form(tmp_path):
    """Cancelling a reply removes the form without adding a reply."""
    html_path = _write_share_html(tmp_path)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(html_path.as_uri())
        page.wait_for_selector("#md-render")
        _set_author(page, "Alice")
        _add_general_comment(page, "main thread")

        page.click(".comment-card .reply-btn")
        page.wait_for_selector(".comment-card .reply-form")
        page.locator(".comment-card .reply-form textarea").fill("never mind")
        page.click(".comment-card .reply-form .cancel")

        # No reply item appears.
        page.wait_for_timeout(500)
        assert page.locator(".comment-card .reply-item").count() == 0
        browser.close()


def test_reply_included_in_export(tmp_path):
    """Replies are carried in the exported JSON so they round-trip back to
    a live session."""
    html_path = _write_share_html(tmp_path)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(html_path.as_uri())
        page.wait_for_selector("#md-render")
        _set_author(page, "Alice")
        _add_general_comment(page, "main thread")

        page.click(".comment-card .reply-btn")
        page.wait_for_selector(".comment-card .reply-form")
        page.locator(".comment-card .reply-form textarea").fill("a reply note")
        page.click(".comment-card .reply-form .send")
        page.wait_for_selector(".comment-card .reply-item")

        page.click("#send-btn")
        page.wait_for_selector("#export-box.open")
        exported = json.loads(page.locator("#export-json").input_value())
        assert len(exported["comments"]) == 1
        c = exported["comments"][0]
        assert "replies" in c
        assert len(c["replies"]) == 1
        assert c["replies"][0]["body"] == "a reply note"
        assert c["replies"][0]["author"] == "Alice"
        browser.close()


# ---------------------------------------------------------------------------
# Edit comment — live session (daemon + CLI)
# ---------------------------------------------------------------------------


def test_edit_comment_live_session_persists(tmp_path):
    """Editing a comment in the live viewer persists the new body to the
    running session (visible via /api/state)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    doc = tmp_path / "live.md"
    doc.write_text("# Plan\n\nShip feature X.\n")

    info = _spawn_daemon(state_dir, doc)
    url = f"http://127.0.0.1:{info['port']}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(url)
            page.wait_for_selector("#md-render")
            _add_general_comment(page, "original note")

            page.click(".comment-card .edit-btn")
            page.wait_for_selector(".comment-card .edit-area")
            page.locator(".comment-card .edit-area").fill("edited via live page")
            page.click(".comment-card .edit-actions .save")
            page.wait_for_function(
                "() => document.querySelector('.comment-card .body')"
                ".textContent.includes('edited via live page')",
                timeout=5000,
            )
            browser.close()

        # The edit was POSTed to /api/comment/edit and persisted server-side.
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", info["port"], timeout=5)
        conn.request("GET", "/api/state")
        data = json.loads(conn.getresponse().read().decode("utf-8"))
        conn.close()
        assert len(data["comments"]) == 1
        assert data["comments"][0]["body"] == "edited via live page"
        assert data["comments"][0]["replies"] == []
    finally:
        _stop(info["pid"])


# ---------------------------------------------------------------------------
# Reply to comment — live session (daemon + CLI)
# ---------------------------------------------------------------------------


def test_reply_comment_live_session_persists(tmp_path):
    """Replying to a comment in the live viewer persists the reply to the
    running session."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    doc = tmp_path / "live.md"
    doc.write_text("# Plan\n\nShip feature X.\n")

    info = _spawn_daemon(state_dir, doc)
    url = f"http://127.0.0.1:{info['port']}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(url)
            page.wait_for_selector("#md-render")
            _add_general_comment(page, "main thread")

            page.click(".comment-card .reply-btn")
            page.wait_for_selector(".comment-card .reply-form")
            page.locator(".comment-card .reply-form textarea").fill("a live reply")
            page.click(".comment-card .reply-form .send")
            page.wait_for_selector(".comment-card .reply-item")
            browser.close()

        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", info["port"], timeout=5)
        conn.request("GET", "/api/state")
        data = json.loads(conn.getresponse().read().decode("utf-8"))
        conn.close()
        assert len(data["comments"]) == 1
        c = data["comments"][0]
        assert c["body"] == "main thread"
        assert len(c["replies"]) == 1
        assert c["replies"][0]["body"] == "a live reply"
        assert c["replies"][0]["author"] == "You"
    finally:
        _stop(info["pid"])


# ---------------------------------------------------------------------------
# Round-trip: reply on share page → export → import-comments → live session
# ---------------------------------------------------------------------------


def test_reply_round_trip_share_to_live(tmp_path):
    """A reply added on the standalone share page is exported with the
    comment, and survives ``mdedit.py import-comments`` into a live session.

    This models the core workflow: a colleague imports a comment, you (or
    another reviewer) reply offline, export, and the reply comes back with
    the comment into the live session.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    doc = tmp_path / "live.md"
    doc.write_text("# Shared Plan\n\nShip feature X in two weeks.\n")

    # 1. On the share page, add a comment and a reply, then export.
    snapshot = _snapshot(text=doc.read_text())
    html_path = _write_share_html(tmp_path, snapshot)
    exported = tmp_path / "reviewer.comments.json"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(html_path.as_uri())
        page.wait_for_selector("#md-render")
        _set_author(page, "Frank")
        _add_general_comment(page, "needs a timeline")

        page.click(".comment-card .reply-btn")
        page.wait_for_selector(".comment-card .reply-form")
        page.locator(".comment-card .reply-form textarea").fill("agreed, two weeks")
        page.click(".comment-card .reply-form .send")
        page.wait_for_selector(".comment-card .reply-item")

        page.click("#send-btn")
        page.wait_for_selector("#export-box.open")
        with page.expect_download(timeout=5000) as dl_info:
            page.click("#download-json")
        dl_info.value.save_as(str(exported))
        browser.close()

    # 2. Import into a live session and verify the reply survived.
    info = _spawn_daemon(state_dir, doc)
    try:
        env = _env(state_dir)
        result = subprocess.run(
            [
                sys.executable,
                str(MDEDIT),
                "import-comments",
                str(doc),
                "--from",
                str(exported),
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        summary = json.loads(result.stdout)
        assert summary["ok"] is True
        assert summary["imported"] == 1

        # 3. The live session now holds the comment with its reply.
        status = subprocess.run(
            [sys.executable, str(MDEDIT), "status", str(doc)],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        assert json.loads(status.stdout)["comment_count"] == 1

        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", info["port"], timeout=5)
        conn.request("GET", "/api/state")
        data = json.loads(conn.getresponse().read().decode("utf-8"))
        conn.close()
        c = data["comments"][0]
        assert c["body"] == "needs a timeline"
        assert c["author"] == "Frank"
        assert len(c["replies"]) == 1
        assert c["replies"][0]["body"] == "agreed, two weeks"
        assert c["replies"][0]["author"] == "Frank"
    finally:
        _stop(info["pid"])
