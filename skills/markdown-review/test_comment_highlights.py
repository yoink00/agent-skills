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

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from conftest import (  # noqa: E402
    edit_cli,
    post_comment,
    spawn_daemon,
    stop_daemon,
)
from playwright.sync_api import sync_playwright  # noqa: E402

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
    info = spawn_daemon(state_dir, doc)
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
        stop_daemon(pid)


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
        post_comment(port, body="c1", quote="quick brown fox", source="doc")
        post_comment(port, body="c2", quote="the lazy dog", source="doc")

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
    edit_cli(
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
        post_comment(port, body="d1", quote="three weeks", source="diff", round=1)
        post_comment(port, body="d2", quote="a buffer", source="diff", round=1)

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


@pytest.fixture
def make_session(tmp_path):
    """Factory: spawn a daemon on a caller-supplied document and tear it down
    at the end of the test. Returns a callable that yields the session dict."""
    info = {}

    def _make(doc_text):
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        doc = tmp_path / "doc.md"
        doc.write_text(doc_text)
        info.update(spawn_daemon(state_dir, doc))
        return {
            "port": info["port"],
            "url": f"http://127.0.0.1:{info['port']}",
            "doc": doc,
            "state_dir": state_dir,
        }

    yield _make
    if info.get("pid"):
        stop_daemon(info["pid"])


def test_doc_view_highlights_only_the_anchored_occurrence(make_session):
    """Commenting on a short, repeated token ("is") must light up exactly the
    occurrence the user picked — identified by its stored context — and not
    every substring match. Regression: "is" inside "This" used to light up
    alongside the real target."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        session = make_session("# Token test\n\nThe cat is black. This dog is brown.\n")
        page.goto(session["url"])
        page.wait_for_selector("#md-render")

        # "is" appears three times: after "cat" (standalone), inside "This",
        # and after "dog" (standalone). Anchor to the first via its context.
        post_comment(
            session["port"],
            body="c",
            quote="is",
            context_before="cat ",
            context_after=" black",
            source="doc",
        )

        page.wait_for_function(
            "() => document.querySelectorAll('#md-render mark.has-comment').length > 0",
            timeout=5000,
        )
        marks = page.eval_on_selector_all(
            "#md-render mark.has-comment",
            "ms => ms.map(m => ({"
            "  text: m.textContent,"
            "  before: (m.previousSibling && m.previousSibling.nodeValue) || '',"
            "  after: (m.nextSibling && m.nextSibling.nodeValue) || ''"
            "}))",
        )
        assert len(marks) == 1
        assert marks[0]["text"] == "is"
        assert marks[0]["before"].endswith("cat ")
        assert marks[0]["after"].startswith(" black")

        browser.close()


def test_doc_view_highlights_selection_spanning_inline_markup(make_session):
    """A selection that crosses an inline element (``Some **bold** text``
    renders as three text nodes) must highlight as one span. Regression:
    naive single-node indexOf matched nothing because no single text node
    held the whole quote."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        session = make_session("# Inline test\n\nSome **bold** text here.\n")
        page.goto(session["url"])
        page.wait_for_selector("#md-render")

        post_comment(
            session["port"],
            body="c",
            quote="Some bold text",
            source="doc",
        )

        page.wait_for_function(
            "() => [...document.querySelectorAll('#md-render mark.has-comment')]"
            ".some(m => m.textContent === 'Some bold text')",
            timeout=5000,
        )
        assert _count_marks(page, "#md-render", "Some bold text") == 1

        browser.close()


def test_doc_view_highlights_whole_bullet_line(make_session):
    """Selecting a whole bullet line — which the browser serialises with a
    trailing newline — must still highlight. Regression: the trailing newline
    defeated single-node indexOf so nothing lit up."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        session = make_session("# List test\n\n- alpha item\n- beta item\n")
        page.goto(session["url"])
        page.wait_for_selector("#md-render")

        post_comment(
            session["port"],
            body="c",
            quote="alpha item\n",
            source="doc",
        )

        page.wait_for_function(
            "() => [...document.querySelectorAll('#md-render mark.has-comment')]"
            ".some(m => m.textContent === 'alpha item')",
            timeout=5000,
        )
        assert _count_marks(page, "#md-render", "alpha item") == 1

        browser.close()
