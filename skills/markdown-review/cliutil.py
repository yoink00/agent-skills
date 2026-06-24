"""Small text helpers for the mdedit CLI.

``_unescape_cli`` interprets backslash escapes in a single ``--old`` / ``--new``
value so multi-line search strings passed through a shell actually match. Pure
and trivially unit-testable.
"""

from __future__ import annotations

import re

# Common C-style / shell backslash escapes we honour for --old/--new. Unknown
# escapes are left untouched (see _unescape_cli) so genuine backslashes survive.
BACKSLASH_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "0": "\0",
    "\\": "\\",
    '"': '"',
    "'": "'",
}

_ESCAPE_RE = re.compile(r"\\(.)", re.DOTALL)


def unescape_cli(s: str) -> str:
    """Interpret backslash escapes in a single --old / --new CLI value.

    Shells pass a backslash-n inside double quotes as two literal characters,
    so a multi-line search value never matches the file's real newlines and the
    edit fails with 'old text not found'. Decode the common escapes here;
    unknown escapes are left untouched so genuine backslashes survive.
    --edits-json is not processed (JSON already decodes its own escapes).
    """
    return _ESCAPE_RE.sub(lambda m: BACKSLASH_ESCAPES.get(m.group(1), m.group(0)), s)
