"""Regression tests for inline comment highlighting (doc view + diff view).

These drive the real mdedit daemon with a headless browser (Playwright). They
reproduce a bug where commenting on two phrases that live in the *same* text
node only highlighted the first: ``wrapQuotes`` took one snapshot of text
nodes and reused it across quotes, so after wrapping the first quote the
snapshot pointed at a detached node (``parentNode == null``) and wrapping the
second threw, silently skipping it.

Requires Playwright + chromium:
    pip install playwright && python -m playwright install chromium
"""

import http.client
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

MDEDIT = Path(__file__).resolve().parent / "mdedit.py"

playwright = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _env(state_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["MDEDIT_STATE_DIR"] = str(state_dir)
    env["MDEDIT_IDLE_TIMEOUT"] = "0"  # never auto-shutdown during tests
    return env


def _state_file(state_dir: Path) -> dict | None:
    files = list(state_dir.glob("*.json"))
    if not files:
        return None
    return json.loads(files[0].read_text())


def _spawn(state_dir: Path, doc: Path) -> dict:
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


def _edit(doc: Path, state_dir: Path, old: str, new: str) -> None:
    """Apply a search/replace edit via the CLI so the diff view gets content."""
    env = _env(state_dir)
    subprocess.run(
        [sys.executable, str(MDEDIT), "edit", str(doc), "--old", old, "--new", new],
        check=True,
        capture_output=True,
        env=env,
        timeout=15,
    )


def _post_comment(port: int, **fields) -> dict:
    body = json.dumps(fields)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3.0)
    conn.request(
        "POST",
        "/api/comment",
        body=body,
        headers={"Content-Type": "application/json"},
    )
    resp = conn.getresponse()
    data = json.loads(resp.read().decode())
    conn.close()
    return data


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def session(tmp_path):
    """Spawn a daemon on a fresh doc; yield (port, url); tear down."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    doc = tmp_path / "doc.md"
    doc.write_text(
        "# Highlight Test\n\n"
        "The quick brown fox jumps over the lazy dog.\n"
        "Ship feature X in two weeks.\n"
    )
    info = _spawn(state_dir, doc)
    port = info["port"]
    pid = info["pid"]
    try:
        yield {
            "port": port,
            "url": f"http://127.0.0.1:{port}",
            "doc": doc,
            "state_dir": state_dir,
        }
    finally:
        _stop(pid)


def _count_marks(page, root_selector: str, quote: str) -> int:
    """Count <mark class="has-comment"> under `root_selector` whose text is
    exactly `quote`."""
    return page.eval_on_selector_all(
        f"{root_selector} mark.has-comment",
        """(marks, quote) => marks.filter(m => m.textContent === quote).length""",
        quote,
    )


def test_doc_view_highlights_two_phrases_in_same_text_node(session):
    """Two doc comments whose quotes share one rendered text node must both
    be highlighted (the original bug only highlighted the first)."""
    port = session["port"]
    url = session["url"]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url)
        page.wait_for_selector("#md-render")

        # Both quotes live in the same <p> text node:
        #   "The quick brown fox jumps over the lazy dog."
        _post_comment(port, body="c1", quote="quick brown fox", source="doc")
        _post_comment(port, body="c2", quote="the lazy dog", source="doc")

        # The page's poll loop picks up the version bump and re-applies
        # highlights. Wait for each quote's mark to appear.
        page.wait_for_function(
            """(quote) => [...document.querySelectorAll('#md-render mark.has-comment')]
                          .filter(m => m.textContent === quote).length > 0""",
            arg="quick brown fox",
            timeout=5000,
        )
        page.wait_for_function(
            """(quote) => [...document.querySelectorAll('#md-render mark.has-comment')]
                          .filter(m => m.textContent === quote).length > 0""",
            arg="the lazy dog",
            timeout=5000,
        )

        assert _count_marks(page, "#md-render", "quick brown fox") >= 1
        assert _count_marks(page, "#md-render", "the lazy dog") >= 1

        browser.close()


def test_diff_view_highlights_two_phrases_on_same_line(session):
    """Two diff comments whose quotes share one diff line must both be
    highlighted, scoped to the round they belong to."""
    port = session["port"]
    url = session["url"]
    doc = session["doc"]
    state_dir = session["state_dir"]

    # Produce a single added line containing two selectable phrases.
    _edit(
        doc,
        state_dir,
        old="Ship feature X in two weeks.",
        new="Ship feature X in three weeks with a buffer.",
    )

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url)
        # Diff view is hidden by default; we only need the DOM present.
        page.wait_for_selector("#diff-render", state="attached")

        # Both quotes occur on the same added diff line of round 1.
        _post_comment(port, body="d1", quote="three weeks", source="diff", round=1)
        _post_comment(port, body="d2", quote="a buffer", source="diff", round=1)

        page.wait_for_function(
            """(quote) => [...document.querySelectorAll('#diff-render mark.has-comment')]
                          .filter(m => m.textContent === quote).length > 0""",
            arg="three weeks",
            timeout=5000,
        )
        page.wait_for_function(
            """(quote) => [...document.querySelectorAll('#diff-render mark.has-comment')]
                          .filter(m => m.textContent === quote).length > 0""",
            arg="a buffer",
            timeout=5000,
        )

        assert _count_marks(page, "#diff-render", "three weeks") >= 1
        assert _count_marks(page, "#diff-render", "a buffer") >= 1

        browser.close()
