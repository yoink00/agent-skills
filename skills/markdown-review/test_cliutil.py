"""Unit tests for the mdedit CLI text helpers (cliutil.py).

These pin down the backslash-escape decoding for --old/--new, which exists
precisely because shells mangle multi-line search strings (see the
fix/mdedit-cli-newline-escapes history).
"""

import sys

SKILL_DIR = "skills/markdown-review"
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

from cliutil import BACKSLASH_ESCAPES, unescape_cli  # noqa: E402


class TestKnownEscapes:
    def test_newline(self):
        assert unescape_cli(r"line1\nline2") == "line1\nline2"

    def test_tab(self):
        assert unescape_cli(r"a\tb") == "a\tb"

    def test_carriage_return(self):
        assert unescape_cli(r"a\rb") == "a\rb"

    def test_all_known_escapes_decode(self):
        for seq, want in BACKSLASH_ESCAPES.items():
            assert unescape_cli("\\" + seq) == want

    def test_multiple_escapes_in_one_string(self):
        assert unescape_cli(r"a\nb\tc") == "a\nb\tc"


class TestUnknownEscapes:
    def test_unknown_escape_left_untouched(self):
        # \x, \y, \z are not known escapes, so the backslashes survive verbatim.
        assert unescape_cli(r"\x\y\z") == r"\x\y\z"

    def test_unknown_letter_escape_preserved(self):
        assert unescape_cli(r"\x41") == r"\x41"

    def test_trailing_lone_backslash_preserved(self):
        assert unescape_cli("abc\\") == "abc\\"


class TestNoEscapes:
    def test_plain_string_unchanged(self):
        assert unescape_cli("nothing here") == "nothing here"

    def test_empty_string(self):
        assert unescape_cli("") == ""

    def test_real_newlines_preserved(self):
        # Already-real newlines must pass through untouched.
        assert unescape_cli("a\nb") == "a\nb"
