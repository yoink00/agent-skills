"""Unit tests for the mdedit front-end module (frontend.py).

Covers the CDN asset config and the shape/safety of the single-page HTML
produced by build_html — both pure and fast, no daemon.
"""

import sys

import pytest

SKILL_DIR = "skills/markdown-review"
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

import frontend  # noqa: E402
from frontend import _script_json  # noqa: E402

# ---------------------------------------------------------------------------
# Asset manifest sanity
# ---------------------------------------------------------------------------


class TestManifest:
    def test_all_urls_are_https(self):
        for fname, url in frontend.CDN_ASSETS.items():
            assert url.startswith("https://"), (fname, url)

    def test_expected_assets_present(self):
        # build_html / build_share_html hard-code references to these three.
        assert {
            "marked.min.js",
            "highlight.min.js",
            "highlight-onedark.min.css",
        } <= set(frontend.CDN_ASSETS)


# ---------------------------------------------------------------------------
# _script_json — safe JSON for <script> interpolation
# ---------------------------------------------------------------------------


class TestScriptJson:
    def test_simple_string_is_json(self):
        assert _script_json("doc.md") == '"doc.md"'

    def test_neutralises_script_close(self):
        # The literal `</script>` must not survive into the output.
        assert "</script>" not in _script_json("</script>")
        assert "</" not in _script_json("</x>")

    def test_angle_brackets_and_amp_escaped(self):
        out = _script_json("<a>&b")
        assert "<" not in out and ">" not in out and "&" not in out
        # and the JS escapes decode back to the originals.
        assert r"\u003ca\u003e\u0026b" in out

    def test_decodes_to_identical_value(self):
        # Whatever goes in must come out unchanged once the JS/JSON parses it.
        import json

        for val in [
            "doc.md",
            "</script><script>",
            'a"b\\c',
            "line1\nline2",
            "\u2028paragraph\u2029",
            '<weird & " name >.md',
        ]:
            assert json.loads(_script_json(val)) == val

    def test_handles_non_string(self):
        import json

        assert json.loads(_script_json({"a": [1, 2]})) == {"a": [1, 2]}


# ---------------------------------------------------------------------------
# build_html
# ---------------------------------------------------------------------------


class TestBuildHtml:
    def test_is_a_complete_html_document(self):
        html = frontend.build_html("doc.md")
        assert html.startswith("<!DOCTYPE html>")
        assert html.rstrip().endswith("</html>")

    def test_injects_document_name_into_title(self):
        html = frontend.build_html("plan.md")
        assert "plan.md — mdedit</title>" in html

    def test_escapes_html_in_displayed_name(self):
        # The name is rendered into HTML contexts (<title> and the #doc-name
        # span); markup in the name must be escaped there. (In the JS `DOC_NAME`
        # literal it is escaped further still — see test_js_doc_name_neutralises_script_breakout.)
        name = '<img src=x>"evil".md'
        html = frontend.build_html(name)
        assert "<title>&lt;img src=x>&quot;evil&quot;.md — mdedit</title>" in html
        assert (
            '<span class="doc-name" id="doc-name">&lt;img src=x>&quot;evil&quot;.md</span>'
            in html
        )

    def test_doc_name_passed_to_js_safely(self):
        # The front-end receives the name as a JSON-encoded literal, so any
        # quote/backslash in the name is escaped rather than breaking the JS.
        html = frontend.build_html('a"b\\c.md')
        # json.dumps keeps the quotes/escapes intact within the JS literal.
        assert '"a\\"b\\\\c.md"' in html

    def test_js_doc_name_neutralises_script_breakout(self):
        # Regression: a document name containing `</script>` must NOT appear
        # literally inside the <script> block, or the HTML parser would
        # terminate the element early and parse the rest as markup (an
        # injection vector via the file name). The `<` is escaped to \u003c.
        evil = "</script><script>alert(1)</script>"
        html = frontend.build_html(evil)
        assert "</script><script>alert(1)</script>" not in html
        assert "DOC_NAME" in html  # the literal is still emitted
        # The escaped form decodes (in JS) back to the original name.
        assert r"\u003c/script\u003e" in html

    def test_embeds_css_and_asset_urls(self):
        html = frontend.build_html("x.md")
        assert frontend.VALSTRO_CSS.strip()[:60] in html
        # All three assets are referenced from CDN.
        for url in frontend.CDN_ASSETS.values():
            assert url in html

    def test_contains_key_ui_anchors(self):
        # Guard the IDs the JS hooks onto; renaming one silently breaks the UI.
        html = frontend.build_html("x.md")
        for anchor in (
            'id="md-render"',
            'id="diff-render"',
            'id="comments-list"',
            'id="send-btn"',
            'id="sel-popover"',
            'id="view-rendered"',
            'id="view-diff"',
            'id="import-btn"',
            'id="import-menu-text"',
            'id="import-menu-file"',
            'id="import-box"',
            'id="import-apply"',
            'id="import-file"',
        ):
            assert anchor in html, anchor

    def test_has_import_dropdown(self):
        # The live (originating) viewer is where exported comments come back.
        html = frontend.build_html("x.md")
        assert "Import \u25be" in html  # Import ▾ button
        assert "Import Comments\u2026" in html
        assert "Import from File\u2026" in html

    def test_name_independence(self):
        # Different names produce different titles but the same shell.
        a = frontend.build_html("a.md")
        b = frontend.build_html("b.md")
        assert "a.md" in a and "b.md" in b
        # Same length modulo the name difference is a good shell-stability proxy.
        base_a = a.replace("a.md", "X")
        base_b = b.replace("b.md", "X")
        assert base_a == base_b


class TestBuildShareHtml:
    """Tests for the standalone share page (build_share_html)."""

    def _snapshot(self, name="doc.md", text="# Hello\n", edits=None, comments=None):
        return {
            "name": name,
            "version": 1,
            "submitted": False,
            "current_round": 1,
            "original_text": text,
            "current_text": text,
            "edits": edits or [],
            "comments": comments or [],
        }

    def test_is_a_complete_html_document(self):
        html = frontend.build_share_html(self._snapshot())
        assert html.startswith("<!DOCTYPE html>")
        assert html.rstrip().endswith("</html>")

    def test_injects_document_name_into_title(self):
        html = frontend.build_share_html(self._snapshot("plan.md"))
        assert "plan.md — review (shared)</title>" in html

    def test_embeds_snapshot_json(self):
        snap = self._snapshot(text="# Hello world\n")
        html = frontend.build_share_html(snap)
        assert "INITIAL_STATE" in html
        assert "Hello world" in html

    def test_has_export_button_not_send(self):
        html = frontend.build_share_html(self._snapshot())
        assert "Export comments" in html
        assert "send-btn" in html  # same ID, different label

    def test_has_author_prompt(self):
        html = frontend.build_share_html(self._snapshot())
        assert "author-prompt" in html
        assert "author-input" in html

    def test_has_export_box(self):
        html = frontend.build_share_html(self._snapshot())
        assert "export-box" in html
        assert "export-json" in html

    def test_has_share_banner(self):
        html = frontend.build_share_html(self._snapshot())
        assert "share-banner" in html

    def test_has_no_import_ui(self):
        # Importing belongs on the originating (live) viewer, which has a
        # server to receive comments; the standalone share page has neither.
        html = frontend.build_share_html(self._snapshot())
        for anchor in (
            'id="import-btn"',
            'id="import-menu"',
            'id="import-box"',
            'id="import-file"',
        ):
            assert anchor not in html, anchor

    def test_no_server_api_calls(self):
        """The share page must not make any fetch('/api/...') calls."""
        html = frontend.build_share_html(self._snapshot())
        assert "/api/state" not in html
        assert "/api/poll" not in html
        assert "/api/comment" not in html
        assert "/api/submit" not in html
        assert "/api/share" not in html

    def test_has_core_viewer_js(self):
        """Both pages share the same core rendering JS."""
        html = frontend.build_share_html(self._snapshot())
        assert "renderComments" in html
        assert "renderDiff" in html
        assert "applyCommentHighlights" in html

    def test_has_local_add_comment(self):
        """The share page defines its own in-memory addComment."""
        html = frontend.build_share_html(self._snapshot())
        assert "addComment" in html
        assert "state.comments.push" in html

    def test_contains_css(self):
        html = frontend.build_share_html(self._snapshot())
        assert frontend.VALSTRO_CSS.strip()[:60] in html

    def test_escapes_html_in_displayed_name(self):
        name = '<img src=x>"evil".md'
        html = frontend.build_share_html(self._snapshot(name))
        assert "<title>&lt;img src=x>&quot;evil&quot;.md" in html

    def test_same_ui_anchors_as_live_page(self):
        """Share page has the same element IDs the core JS hooks onto."""
        html = frontend.build_share_html(self._snapshot())
        for anchor in (
            "#md-render",
            "#diff-render",
            "#comments-list",
            "#send-btn",
            "#sel-popover",
            "view-rendered",
            "view-diff",
        ):
            assert anchor in html, anchor

    def test_references_cdn_assets(self):
        """All front-end assets are loaded from CDN as <script src>/<link>."""
        html = frontend.build_share_html(self._snapshot())
        assert frontend.CDN_ASSETS["highlight-onedark.min.css"] in html
        assert frontend.CDN_ASSETS["highlight.min.js"] in html
        assert frontend.CDN_ASSETS["marked.min.js"] in html


# ---------------------------------------------------------------------------
# KaTeX + Mermaid CDN references
# ---------------------------------------------------------------------------


class TestCdnLibraries:
    """Both pages must reference KaTeX and Mermaid from CDN."""

    def test_live_page_has_katex_css(self):
        html = frontend.build_html("x.md")
        assert frontend.KATEX_CSS in html
        assert 'rel="stylesheet"' in html

    def test_live_page_has_katex_js(self):
        html = frontend.build_html("x.md")
        assert frontend.KATEX_JS in html
        assert frontend.KATEX_AUTORENDER_JS in html

    def test_live_page_has_mermaid_js(self):
        html = frontend.build_html("x.md")
        assert frontend.MERMAID_JS in html

    def test_share_page_has_katex_css(self):
        html = frontend.build_share_html(
            {"name": "d.md", "current_text": "", "edits": [], "comments": []}
        )
        assert frontend.KATEX_CSS in html

    def test_share_page_has_katex_js(self):
        html = frontend.build_share_html(
            {"name": "d.md", "current_text": "", "edits": [], "comments": []}
        )
        assert frontend.KATEX_JS in html
        assert frontend.KATEX_AUTORENDER_JS in html

    def test_share_page_has_mermaid_js(self):
        html = frontend.build_share_html(
            {"name": "d.md", "current_text": "", "edits": [], "comments": []}
        )
        assert frontend.MERMAID_JS in html

    def test_share_page_uses_script_src_not_inline_for_cdn_libs(self):
        """CDN libs must be <script src> tags, not inlined."""
        html = frontend.build_share_html(
            {"name": "d.md", "current_text": "", "edits": [], "comments": []}
        )
        assert f'<script src="{frontend.MERMAID_JS}">' in html
        assert f'<script src="{frontend.KATEX_JS}">' in html


# ---------------------------------------------------------------------------
# Mermaid code fence in core JS
# ---------------------------------------------------------------------------


class TestMermaidCodeFence:
    """The core viewer JS must intercept ```mermaid fences and emit
    <pre class=\"mermaid\"> instead of a highlighted code block."""

    def test_core_js_has_mermaid_branch(self):
        js = frontend._core_viewer_js()
        assert "'mermaid'" in js
        assert 'pre class="mermaid"' in js

    def test_core_js_has_mermaid_init(self):
        js = frontend._core_viewer_js()
        assert "mermaid.initialize" in js
        assert "theme: 'dark'" in js

    def test_core_js_has_mermaid_run(self):
        js = frontend._core_viewer_js()
        assert "mermaid.run" in js

    def test_live_page_has_mermaid_branch(self):
        html = frontend.build_html("x.md")
        assert 'pre class="mermaid"' in html

    def test_share_page_has_mermaid_branch(self):
        html = frontend.build_share_html(
            {"name": "d.md", "current_text": "", "edits": [], "comments": []}
        )
        assert 'pre class="mermaid"' in html


# ---------------------------------------------------------------------------
# Math protection logic
# ---------------------------------------------------------------------------


class TestMathProtection:
    """The core viewer JS must extract $...$ / $$...$$ before marked.parse
    and restore them after, so marked doesn't mangle math syntax."""

    def test_core_js_has_extract_math(self):
        js = frontend._core_viewer_js()
        assert "extractMath" in js
        assert "_mathPlaceholders" in js

    def test_core_js_has_restore_math(self):
        js = frontend._core_viewer_js()
        assert "restoreMath" in js

    def test_core_js_has_render_markdown_wrapper(self):
        """renderMarkdown wraps extract -> marked.parse -> restore."""
        js = frontend._core_viewer_js()
        assert "function renderMarkdown" in js
        assert "extractMath" in js
        assert "restoreMath" in js

    def test_core_js_has_katex_auto_render(self):
        js = frontend._core_viewer_js()
        assert "renderMathInElement" in js
        assert "$$" in js  # display delimiter
        assert "delimiters" in js

    def test_render_doc_uses_render_markdown_not_marked_parse(self):
        js = frontend._core_viewer_js()
        # renderDoc should call renderMarkdown, not marked.parse directly.
        assert "renderMarkdown(state.current_text" in js

    def test_core_js_has_mermaid_css(self):
        """VALSTRO_CSS should style Mermaid containers."""
        css = frontend.VALSTRO_CSS
        assert "pre.mermaid" in css

    def test_core_js_has_katex_css(self):
        """VALSTRO_CSS should include KaTeX dark theme adjustments."""
        css = frontend.VALSTRO_CSS
        assert ".katex" in css
