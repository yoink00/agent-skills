"""End-to-end browser tests for the standalone share page.

These drive the self-contained share HTML (produced by ``build_share_html``)
with a headless browser via Playwright — no daemon, no network. They cover the
offline review loop that unit tests can't reach:

  * the page renders the embedded document from a ``file://`` URL,
  * the author prompt stamps comments with a name,
  * adding a doc-anchored comment works in-memory,
  * clicking "Export comments" opens a JSON box and a Download button saves a file,
  * that JSON round-trips back through ``mdedit.py import-comments``.

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
    # The prompt hides (gains .hidden -> display:none) once the name is saved.
    page.wait_for_selector("#author-prompt.hidden", state="hidden")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_share_page_renders_offline(tmp_path):
    """The standalone page loads from file:// and renders the document text,
    with no server connection."""
    html_path = _write_share_html(tmp_path)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(html_path.as_uri())
        # The embedded document text is rendered into #md-render.
        page.wait_for_selector("#md-render")
        page.wait_for_function(
            "() => document.getElementById('md-render').textContent.includes('feature X')",
            timeout=5000,
        )
        # The review-only banner is present.
        assert page.locator("#share-banner").is_visible()
        # The export button exists (not a "Send to LLM" live button).
        assert page.locator("#send-btn").inner_text() == "Export comments"
        browser.close()


def test_author_prompt_attributes_comments(tmp_path):
    """Comments added after the author prompt carry the entered name."""
    html_path = _write_share_html(tmp_path)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(html_path.as_uri())
        page.wait_for_selector("#md-render")
        _set_author(page, "Carol")

        # Add a general comment via the sidebar box.
        page.fill("#general-input", "Looks good overall")
        page.click("#general-add")
        page.wait_for_selector(".comment-card")

        # The comment card shows the author.
        author = page.locator(".comment-card .author").inner_text()
        assert author == "Carol"
        assert "Looks good overall" in page.locator(".comment-card .body").inner_text()
        browser.close()


def test_doc_anchored_comment_in_memory(tmp_path):
    """Selecting document text and commenting works without a server."""
    html_path = _write_share_html(tmp_path)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(html_path.as_uri())
        page.wait_for_selector("#md-render")
        _set_author(page, "Dan")

        # Select a known phrase in the rendered document via the Selection
        # API, then dispatch the mouseup that opens the popover.
        phrase = "feature X"
        page.evaluate(
            """(phrase) => {
              const md = document.getElementById('md-render');
              const walker = document.createTreeWalker(md, NodeFilter.SHOW_TEXT);
              while (walker.nextNode()) {
                const idx = walker.currentNode.nodeValue.indexOf(phrase);
                if (idx >= 0) {
                  const range = document.createRange();
                  range.setStart(walker.currentNode, idx);
                  range.setEnd(walker.currentNode, idx + phrase.length);
                  const sel = window.getSelection();
                  sel.removeAllRanges();
                  sel.addRange(range);
                  break;
                }
              }
              document.getElementById('content-area')
                .dispatchEvent(new MouseEvent('mouseup', {bubbles:true}));
            }""",
            phrase,
        )
        # The selection popover opens.
        page.wait_for_selector("#sel-popover.open")
        page.fill("#sel-input", "be more specific")
        page.click("#sel-save")

        page.wait_for_selector(".comment-card")
        card = page.locator(".comment-card").first
        assert "be more specific" in card.locator(".body").inner_text()
        # The quote is anchored to the selected text.
        assert phrase in card.locator(".quote").inner_text()
        browser.close()


def test_export_downloads_json_with_author(tmp_path):
    """Export comments opens a JSON box (no auto-download); the Download JSON
    button then saves a file carrying the author and full comment shape."""
    html_path = _write_share_html(tmp_path)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(html_path.as_uri())
        page.wait_for_selector("#md-render")
        _set_author(page, "Eve")

        page.fill("#general-input", "ship it")
        page.click("#general-add")
        page.wait_for_selector(".comment-card")

        # Export opens the copy/paste box; it must NOT auto-download, so we
        # wait for the box to appear rather than for a download event.
        page.click("#send-btn")
        page.wait_for_selector("#export-box.open")
        box_text = page.locator("#export-json").input_value()
        assert json.loads(box_text)["comments"][0]["author"] == "Eve"

        # The explicit Download JSON button is what triggers the file save.
        with page.expect_download(timeout=5000) as dl_info:
            page.click("#download-json")
        download = dl_info.value
        assert download.suggested_filename == "share-doc.comments.json"

        # Read the downloaded JSON.
        save_to = tmp_path / "exported.comments.json"
        download.save_as(str(save_to))
        data = json.loads(save_to.read_text())

        assert data["doc_name"] == "share-doc.md"
        assert "exported_at" in data
        assert len(data["comments"]) == 1
        c = data["comments"][0]
        assert c["body"] == "ship it"
        assert c["author"] == "Eve"
        assert c["source"] == "doc"
        # Every comment carries the fields import-comments expects.
        for key in (
            "body",
            "quote",
            "context_before",
            "context_after",
            "source",
            "round",
            "author",
        ):
            assert key in c, key

        browser.close()


def test_export_no_automatic_download(tmp_path):
    """Clicking Export comments must not trigger a download on its own."""
    html_path = _write_share_html(tmp_path)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(html_path.as_uri())
        page.wait_for_selector("#md-render")
        _set_author(page, "Eve")
        page.fill("#general-input", "ship it")
        page.click("#general-add")
        page.wait_for_selector(".comment-card")

        # Expect NO download within 1s of clicking Export comments.
        download_registered = []
        page.on("download", lambda d: download_registered.append(d))
        page.click("#send-btn")
        page.wait_for_selector("#export-box.open")
        page.wait_for_timeout(1000)
        assert download_registered == []
        browser.close()


def test_live_import_from_paste_box_merges_comments(tmp_path):
    """The live viewer's Import ▾ dropdown opens a paste box whose JSON is
    POSTed to /api/import and merged into the session, deduped against
    existing comments."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    doc = tmp_path / "live.md"
    doc.write_text("# Plan\n\nShip feature X.\n")
    env = _env(state_dir)

    info = _spawn_daemon(state_dir, doc)
    url = f"http://127.0.0.1:{info['port']}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(url)
            page.wait_for_selector("#md-render")
            # Add one comment directly so dedup has something to hit.
            page.fill("#general-input", "original note")
            page.click("#general-add")
            page.wait_for_selector(".comment-card")

            # Open the Import dropdown and pick the paste option.
            page.click("#import-btn")
            page.wait_for_selector("#import-menu.open")
            page.click("#import-menu-text")
            page.wait_for_selector("#import-box.open")

            payload = json.dumps(
                {
                    "comments": [
                        {
                            "body": "from friend",
                            "quote": "",
                            "context_before": "",
                            "context_after": "",
                            "source": "doc",
                            "round": 0,
                            "author": "Frank",
                        },
                        # Duplicate of the existing general note → skipped.
                        {
                            "body": "original note",
                            "quote": "",
                            "context_before": "",
                            "context_after": "",
                            "source": "doc",
                            "round": 0,
                            "author": "You",
                        },
                    ],
                }
            )
            page.fill("#import-json", payload)
            page.click("#import-apply")
            # One existing + one imported = two cards.
            page.wait_for_function(
                "() => document.querySelectorAll('.comment-card').length === 2"
            )
            # Import box auto-closes on success.
            page.wait_for_selector("#import-box.open", state="hidden")
            browser.close()

        # The imported comment is persisted server-side.
        status = subprocess.run(
            [sys.executable, str(MDEDIT), "status", str(doc)],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        assert json.loads(status.stdout)["comment_count"] == 2
    finally:
        _stop(info["pid"])


# ---------------------------------------------------------------------------
# Round-trip: exported JSON → mdedit.py import-comments → live session
# ---------------------------------------------------------------------------


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


def test_exported_json_imports_back_into_live_session(tmp_path):
    """The JSON downloaded from the share page can be fed straight into
    ``mdedit.py import-comments`` and shows up in the live session."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    doc = tmp_path / "live.md"
    doc.write_text("# Shared Plan\n\nShip feature X in two weeks.\n")

    # 1. Produce a share HTML straight from the running session's text, open it,
    #    add a comment, and export it.
    snapshot = _snapshot(text=doc.read_text())
    html_path = _write_share_html(tmp_path, snapshot)
    exported = tmp_path / "reviewer.comments.json"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(html_path.as_uri())
        page.wait_for_selector("#md-render")
        _set_author(page, "Frank")
        page.fill("#general-input", "needs a timeline")
        page.click("#general-add")
        page.wait_for_selector(".comment-card")
        page.click("#send-btn")
        page.wait_for_selector("#export-box.open")
        with page.expect_download(timeout=5000) as dl_info:
            page.click("#download-json")
        dl_info.value.save_as(str(exported))
        browser.close()

    # 2. Spawn the live daemon and import the exported comments.
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
        assert summary["skipped_duplicates"] == 0

        # 3. The comment is now in the live session.
        status = subprocess.run(
            [sys.executable, str(MDEDIT), "status", str(doc)],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        assert json.loads(status.stdout)["comment_count"] == 1

        # 4. Re-importing the same file is a no-op (dedup).
        result2 = subprocess.run(
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
        summary2 = json.loads(result2.stdout)
        assert summary2["imported"] == 0
        assert summary2["skipped_duplicates"] == 1
    finally:
        _stop(info["pid"])
