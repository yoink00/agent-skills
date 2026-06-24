# markdown-review

An agent skill for **interactive, human-in-the-loop markdown editing**. The
agent edits a document; you watch the changes land live in a browser, leave
inline comments, and click **Send to LLM** to return them — no copy-paste.

It is built around `mdedit.py`, a runnable entry point with **no third-party
dependencies** (Python 3.10+ standard library only). The implementation is
split into focused modules alongside it: `model.py` (session state),
`frontend.py` (the browser viewer), `server.py` (the HTTP daemon) and
`cliutil.py` (CLI text helpers). You still drive everything through
`mdedit.py`; the split just makes the pieces independently testable.

## What it does

- Streams every agent edit to a browser tab, flashing changed blocks.
- Toggle between a clean **Rendered** view and a **Changes** (before/after diff)
  view, with diffs grouped into review **rounds**.
- Lets you select text — in either view — and attach a comment. Comments come
  back to the agent as structured JSON.
- Re-blocks each review round so the agent waits for fresh feedback rather than
  re-reading stale comments.
- **Share for offline review** — download a standalone HTML copy of the
  document (with the full commenting UI) that a colleague can open in any
  browser without a server. Their comments export as JSON and import back into
  the live session.

## Requirements

- **Python 3.10+** (standard library only — nothing to install).
- **POSIX** host (uses `fork`; on Windows use WSL).
- **Works offline** — the viewer's `marked` and `highlight.js` are vendored
  under `vendor/` and served locally. Run `./update-vendor.sh` to refresh or
  re-pin them; if `vendor/` is missing the page falls back to a CDN.

## Usage

The agent drives the tool; see [`SKILL.md`](./SKILL.md) for the full workflow
and command reference. Quick taste:

```bash
MDEDIT=./mdedit.py
python3 "$MDEDIT" open doc.md
python3 "$MDEDIT" edit doc.md --old "old text" --new "new text"
python3 "$MDEDIT" review doc.md      # blocks until you click "Send to LLM"
python3 "$MDEDIT" resolve doc.md --all
python3 "$MDEDIT" share doc.md > doc.share.html   # offline review copy
python3 "$MDEDIT" import-comments doc.md --from colleague.comments.json
python3 "$MDEDIT" stop doc.md
```

## License

[MIT](./LICENSE).
