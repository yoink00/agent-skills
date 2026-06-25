"""End-to-end browser tests for LaTeX math and Mermaid diagram rendering.

These verify the rendering pipeline added to ``_core_viewer_js()``:

  * ``$$...$$`` / ``$...$`` math delimiters survive ``marked.parse()`` (not
    mangled into HTML) and are passed to KaTeX's ``renderMathInElement``,
  * `````mermaid`` code fences produce ``<pre class="mermaid">`` elements (not
    highlighted ``<pre><code>`` blocks) and are picked up by ``mermaid.run()``,
  * regular code fences are still syntax-highlighted by highlight.js,
  * the page degrades gracefully when the CDN libraries fail to load.

The tests run against the standalone share page (which shares the *same*
``_core_viewer_js()`` as the live viewer, so the rendering logic is covered for
both). The share page is loaded from a ``file://`` URL — no daemon needed.

KaTeX and Mermaid load from CDN in production. To keep these tests fully
offline (no network dependency, no CI flakiness), Playwright route interception
serves *minimal stub implementations* of those libraries. The stubs record that
they were called and simulate rendering (mark elements processed, insert a
stand-in ``<svg>``), which is enough to verify our integration glue — the real
libraries' correctness is upstream's concern.

Requires Playwright + chromium:
    pip install playwright && python -m playwright install chromium
"""

import re
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent

playwright = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))
import frontend  # noqa: E402

# ---------------------------------------------------------------------------
# Stub CDN libraries
# ---------------------------------------------------------------------------

# Minimal KaTeX auto-render stub. The real renderMathInElement walks the DOM
# looking for $...$ / $$...$$ delimiters and replaces them with rendered HTML.
# Our stub records that it was called (with which options) and wraps any text
# node containing '$' in a <span class="katex"> so tests can confirm math
# content reached the renderer.
_KATEX_STUB_JS = """
window.renderMathInElement = function(elem, opts) {
  window.__katexCalls = (window.__katexCalls || 0) + 1;
  window.__katexOpts = opts;
  if (!elem) return;
  var walker = document.createTreeWalker(elem, NodeFilter.SHOW_TEXT);
  var nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  nodes.forEach(function(node) {
    if (node.nodeValue.indexOf('$') === -1) return;
    var span = document.createElement('span');
    span.className = 'katex';
    span.setAttribute('data-raw', node.nodeValue);
    span.textContent = node.nodeValue;
    node.parentNode.replaceChild(span, node);
  });
};
"""

# Minimal Mermaid stub. The real mermaid.run() finds ``pre.mermaid`` elements,
# parses their text, and inserts an SVG. Our stub records the call, marks each
# element with ``data-processed`` (so our guard selector skips it next time),
# and inserts a stand-in ``<svg>``.
_MERMAID_STUB_JS = """
window.mermaid = {
  initialize: function(opts) { window.__mermaidInitOpts = opts; },
  run: function(opts) {
    window.__mermaidCalls = (window.__mermaidCalls || 0) + 1;
    window.__mermaidRunOpts = opts;
    var els = document.querySelectorAll(opts && opts.querySelector || 'pre.mermaid');
    els.forEach(function(el) {
      el.setAttribute('data-processed', '');
      var svg = document.createElement('svg');
      svg.setAttribute('class', 'mermaid-rendered');
      svg.setAttribute('width', '100');
      svg.setAttribute('height', '50');
      el.appendChild(svg);
    });
    return Promise.resolve();
  }
};
"""


def _intercept_cdn(page, *, block=False):
    """Route CDN requests for KaTeX/Mermaid to stub scripts (or abort them).

    Intercepts ALL requests and filters by URL substring: the KaTeX
    auto-render script lives at ``.../katex@0.16/dist/contrib/auto-render.min.js``
    whose last path segment does NOT contain "katex", so a naive
    ``**/*katex*`` glob (which matches on the final path segment) would miss
    it. Filtering on the full URL string is more robust.

    With ``block=True`` the CDN requests are aborted entirely, simulating an
    offline / blocked-CDN scenario — the page's guard clauses should prevent
    any crash and the document text remains visible.
    """

    def handler(route):
        url = route.request.url
        is_cdn = "katex" in url or "mermaid" in url
        if not is_cdn:
            route.continue_()
            return
        if block:
            route.abort()
            return
        if "katex" in url and ".css" in url:
            route.fulfill(status=200, content_type="text/css", body="")
        elif "katex" in url:
            route.fulfill(
                status=200, content_type="text/javascript", body=_KATEX_STUB_JS
            )
        elif "mermaid" in url:
            route.fulfill(
                status=200, content_type="text/javascript", body=_MERMAID_STUB_JS
            )

    # Match only CDN requests (by hostname), not file:// page loads. This is
    # critical because pytest's tmp_path directory names are derived from the
    # test function name — a test named ``test_mermaid_*`` produces a path
    # containing "mermaid", which a naive substring/keyword regex would match,
    # causing the stub to be served as the page itself.
    page.route(re.compile(r"cdn\.jsdelivr\.net"), handler)


# ---------------------------------------------------------------------------
# Test fixtures / helpers
# ---------------------------------------------------------------------------

# A document that exercises every rendering path at once.
_DOC = """\
# Math and Diagrams

Inline math: $a_1 + a_2 = b_3$ and $E = mc^2$.

Display math:

$$\\int_0^1 x^2 \\, dx = \\frac{1}{3}$$

Mermaid diagram:

```mermaid
graph LR
  A --> B
```

Regular code:

```python
x = 1
```
"""


def _snapshot(text=_DOC):
    return {
        "name": "render-test.md",
        "version": 1,
        "submitted": False,
        "current_round": 1,
        "original_text": text,
        "current_text": text,
        "edits": [],
        "comments": [],
    }


def _write_share_html(tmp_path: Path, text=_DOC) -> Path:
    html = frontend.build_share_html(_snapshot(text))
    out = tmp_path / "render.share.html"
    out.write_text(html, encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Mermaid tests
# ---------------------------------------------------------------------------


class TestMermaidRendering:
    def test_mermaid_fence_produces_mermaid_pre_element(self, tmp_path):
        """A ```mermaid fence must become <pre class="mermaid">, not a
        highlighted <pre><code> block. This is pure renderer.code logic —
        it works even before Mermaid JS executes."""
        html_path = _write_share_html(tmp_path)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            _intercept_cdn(page)
            page.goto(html_path.as_uri())
            page.wait_for_selector("#md-render")

            # The mermaid fence produced a <pre class="mermaid">.
            mermaid_el = page.locator("#md-render pre.mermaid")
            page.wait_for_selector("pre.mermaid", timeout=5000)
            assert mermaid_el.count() == 1
            # The diagram source is preserved as text content.
            assert "graph LR" in mermaid_el.inner_text()

            # It did NOT become a highlighted code block.
            assert page.locator("#md-render pre code.hljs.language-mermaid").count() == 0
            browser.close()

    def test_mermaid_run_is_called_and_processes_element(self, tmp_path):
        """After render, mermaid.run() fires and marks the element processed."""
        html_path = _write_share_html(tmp_path)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            _intercept_cdn(page)
            page.goto(html_path.as_uri())
            page.wait_for_selector("#md-render")

            # mermaid.run() was called at least once with our selector.
            page.wait_for_function(
                "() => window.__mermaidCalls > 0 && "
                "window.__mermaidRunOpts && "
                "window.__mermaidRunOpts.querySelector.indexOf('pre.mermaid') >= 0",
                timeout=5000,
            )
            # The element got data-processed + a stand-in SVG. Use
            # state="attached" because the stub SVG is empty (no viewBox)
            # so Playwright considers it non-visible despite width/height.
            page.wait_for_selector(
                "pre.mermaid[data-processed] svg.mermaid-rendered",
                state="attached",
                timeout=5000,
            )
            browser.close()

    def test_mermaid_initialized_with_dark_theme(self, tmp_path):
        """Mermaid is initialized with the dark theme to match the viewer UI."""
        html_path = _write_share_html(tmp_path)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            _intercept_cdn(page)
            page.goto(html_path.as_uri())
            page.wait_for_selector("#md-render")

            page.wait_for_function(
                "() => window.__mermaidInitOpts && "
                "window.__mermaidInitOpts.theme === 'dark'",
                timeout=5000,
            )
            browser.close()


# ---------------------------------------------------------------------------
# Math tests
# ---------------------------------------------------------------------------


class TestMathRendering:
    def test_inline_math_survives_marked_parsing(self, tmp_path):
        """Inline math $...$ must pass through marked.parse() intact. Without
        the extract/restore protection, marked would mangle underscores into
        <em> tags and asterisks into emphasis."""
        html_path = _write_share_html(tmp_path)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            _intercept_cdn(page)
            page.goto(html_path.as_uri())
            page.wait_for_selector("#md-render")

            # The math delimiter and its content survived into the DOM. We
            # check the raw text before KaTeX processes it — but since the
            # stub runs synchronously during renderDoc, the $...$ text is now
            # inside a .katex span's data-raw attribute.
            page.wait_for_selector(".katex", timeout=5000)
            raws = page.eval_on_selector_all(
                ".katex",
                "els => els.map(e => e.getAttribute('data-raw'))",
            )
            joined = " ".join(raws)
            # Underscores survived (not turned into <em>).
            assert "a_1 + a_2 = b_3" in joined
            assert "E = mc^2" in joined
            browser.close()

    def test_display_math_survives_marked_parsing(self, tmp_path):
        """Display math $$...$$ must survive marked.parse() intact."""
        html_path = _write_share_html(tmp_path)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            _intercept_cdn(page)
            page.goto(html_path.as_uri())
            page.wait_for_selector("#md-render")

            page.wait_for_selector(".katex", timeout=5000)
            raws = page.eval_on_selector_all(
                ".katex",
                "els => els.map(e => e.getAttribute('data-raw'))",
            )
            joined = " ".join(raws)
            # The integral survived with its LaTeX commands intact.
            assert "\\int_0^1" in joined
            assert "\\frac{1}{3}" in joined
            browser.close()

    def test_katex_render_called_on_md_render(self, tmp_path):
        """renderMathInElement is called on the #md-render element after render."""
        html_path = _write_share_html(tmp_path)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            _intercept_cdn(page)
            page.goto(html_path.as_uri())
            page.wait_for_selector("#md-render")

            # renderMathInElement was called, with both display ($$) and
            # inline ($) delimiters configured.
            page.wait_for_function(
                "() => window.__katexCalls > 0 && "
                "window.__katexOpts && "
                "window.__katexOpts.delimiters.some(d => d.left === '$$') && "
                "window.__katexOpts.delimiters.some(d => d.left === '$')",
                timeout=5000,
            )
            browser.close()

    def test_math_not_mangled_into_em_tags(self, tmp_path):
        """Regression: without math protection, marked turns a_b into
        <em>a</em>b. Verify no <em> elements appear inside math content."""
        # A doc where the math contains underscored subscripts that marked
        # would interpret as emphasis if it saw them.
        doc = "The sum $x_i + y_j$ equals $z_k$.\n"
        html_path = _write_share_html(tmp_path, doc)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            _intercept_cdn(page)
            page.goto(html_path.as_uri())
            page.wait_for_selector("#md-render")

            page.wait_for_selector(".katex", timeout=5000)
            # No <em> elements were produced from the math underscores.
            assert page.locator("#md-render em").count() == 0
            browser.close()


# ---------------------------------------------------------------------------
# Non-regression: regular code still highlighted
# ---------------------------------------------------------------------------


def test_regular_code_fence_still_highlighted(tmp_path):
    """Non-mermaid code fences must still go through highlight.js."""
    html_path = _write_share_html(tmp_path)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        _intercept_cdn(page)
        page.goto(html_path.as_uri())
        page.wait_for_selector("#md-render")

        # The python fence produced a highlighted code block.
        page.wait_for_selector(
            "#md-render pre code.hljs.language-python", timeout=5000
        )
        browser.close()


# ---------------------------------------------------------------------------
# Graceful degradation: CDN blocked / offline
# ---------------------------------------------------------------------------


def test_page_does_not_crash_when_cdn_blocked(tmp_path):
    """When the CDN is unreachable, the page must not crash: the guard
    clauses in renderMath()/renderMermaid() skip silently, and the document
    text (including raw math delimiters) remains visible."""
    html_path = _write_share_html(tmp_path)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        # Block all CDN requests — simulate offline.
        _intercept_cdn(page, block=True)
        page.goto(html_path.as_uri())
        page.wait_for_selector("#md-render")

        # The document text rendered (marked/highlight.js are inlined, so
        # they work; only KaTeX/Mermaid are missing).
        page.wait_for_function(
            "() => document.getElementById('md-render').textContent.includes('Math and Diagrams')",
            timeout=5000,
        )

        # The mermaid fence is still a <pre class="mermaid"> (just unprocessed).
        assert page.locator("pre.mermaid").count() == 1
        assert page.locator("pre.mermaid[data-processed]").count() == 0

        # The raw math delimiters are visible as text (not rendered, but
        # not lost — the content is still readable).
        md_text = page.locator("#md-render").inner_text()
        assert "a_1 + a_2 = b_3" in md_text

        # No error toast appeared.
        assert not page.locator("#toast.show").is_visible()
        browser.close()
