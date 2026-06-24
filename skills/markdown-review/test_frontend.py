"""Unit tests for the mdedit front-end module (frontend.py).

Covers the vendored-asset fallback resolution and the shape/safety of the
single-page HTML produced by build_html — both pure and fast, no daemon.
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
    def test_all_assets_have_known_mime(self):
        for fname in frontend.VENDOR_ASSETS:
            assert frontend.Path(fname).suffix in frontend._VENDOR_MIME, fname

    def test_all_urls_are_https(self):
        for fname, url in frontend.VENDOR_ASSETS.items():
            assert url.startswith("https://"), (fname, url)

    def test_expected_assets_present(self):
        # build_html hard-codes references to these three; guard against drift.
        assert {
            "marked.min.js",
            "highlight.min.js",
            "highlight-onedark.min.css",
        } <= set(frontend.VENDOR_ASSETS)


# ---------------------------------------------------------------------------
# _asset_url fallback
# ---------------------------------------------------------------------------


class TestAssetUrl:
    def test_local_path_when_vendored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(frontend, "VENDOR_DIR", tmp_path)
        (tmp_path / "marked.min.js").write_text("x")
        assert frontend._asset_url("marked.min.js") == "/vendor/marked.min.js"

    def test_cdn_fallback_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(frontend, "VENDOR_DIR", tmp_path)
        url = frontend._asset_url("marked.min.js")
        assert url == frontend.VENDOR_ASSETS["marked.min.js"]
        assert url.startswith("https://")

    def test_unknown_asset_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(frontend, "VENDOR_DIR", tmp_path)
        with pytest.raises(KeyError):
            frontend._asset_url("does-not-exist.js")


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
        # All three assets are referenced (local or CDN depending on vendor/).
        for fname in frontend.VENDOR_ASSETS:
            assert fname in html

    def test_contains_key_ui_anchors(self):
        # Guard the IDs the JS hooks onto; renaming one silently breaks the UI.
        html = frontend.build_html("x.md")
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

    def test_name_independence(self):
        # Different names produce different titles but the same shell.
        a = frontend.build_html("a.md")
        b = frontend.build_html("b.md")
        assert "a.md" in a and "b.md" in b
        # Same length modulo the name difference is a good shell-stability proxy.
        base_a = a.replace("a.md", "X")
        base_b = b.replace("b.md", "X")
        assert base_a == base_b
