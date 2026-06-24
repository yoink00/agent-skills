#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
# demo-highlight.sh — one-shot harness to manually test comment highlighting
# (doc view + diff view) in mdedit's live reviewer.
#
# Run it, then switch to the browser tab that opens and:
#   1. On "Rendered":  select a phrase → comment  (→ blue highlight)
#   2. On "Changes":   select part of a + line → comment (→ green highlight)
#                     select part of a - line → comment (→ red highlight)
#   3. Click "Send to LLM" to see the comments come back as JSON here.
#
# Usage:
#   ./demo-highlight.sh                # use a temp doc under /tmp
#   ./demo-highlight.sh path/to/doc.md # use your own doc
#   DOC=/path MDEDIT=/path/mdedit.py ./demo-highlight.sh   # override bits
#
# Clean up with:  python3 "$MDEDIT" stop "$DOC"   (or just kill the daemon)
# ──────────────────────────────────────────────────────────────────────────
set -euo pipefail

# This script lives inside the skill directory, so mdedit.py is right here.
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MDEDIT="${MDEDIT:-$SKILL_DIR/mdedit.py}"
DOC="${1:-${DOC:-/tmp/mdedit-highlight-demo.md}}"

echo "▶ mdedit:   $MDEDIT"
echo "▶ document: $DOC"
echo

# Fresh sample doc with a few repeatable phrases to select.
cat > "$DOC" <<'MD'
# Highlight Demo

We should ship feature X in two weeks.
The quick brown fox jumps over the lazy dog.

## Notes

- Reduce error rates and improve logging.
- Ship the migration plan.
MD
echo "✓ wrote sample doc"

# Stop any previous session on this path so we start clean.
python3 "$MDEDIT" stop "$DOC" >/dev/null 2>&1 || true

# Open the viewer (auto-opens a browser tab).
python3 "$MDEDIT" open "$DOC"
echo "✓ viewer open — a browser tab should have opened"
echo

# Apply a couple of edits so the Changes view has + and - lines to comment on.
python3 "$MDEDIT" edit "$DOC" \
  --old "two weeks" --new "three weeks, with a buffer for review" >/dev/null

python3 "$MDEDIT" edit "$DOC" \
  --old "Reduce error rates and improve logging." \
  --new "Cut error rates by half and add structured logs." >/dev/null

echo "✓ applied 2 edits — switch to the 'Changes' tab to see them"
echo
echo "──────────────────────────────────────────────────────────────────"
echo "Now in the browser:"
echo "  • Rendered tab → select e.g. 'the lazy dog' or 'feature X' → comment"
echo "    (expect a BLUE highlight)"
echo "  • Changes tab  → select part of a green '+' line → comment"
echo "                   select part of a red   '-' line → comment"
echo "    (expect GREEN / RED tints matching the line)"
echo "  • Highlights add/clear live as you add or delete comments."
echo "  • When done, click 'Send to LLM' — comments print as JSON below."
echo "──────────────────────────────────────────────────────────────────"
echo

# Block until the user sends comments back.
python3 "$MDEDIT" review "$DOC" --json

echo
echo "✓ review complete. Stopping session…"
python3 "$MDEDIT" stop "$DOC" >/dev/null 2>&1 || true
