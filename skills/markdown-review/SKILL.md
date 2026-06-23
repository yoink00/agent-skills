---
name: markdown-review
version: 1.0.0
description: Edit a markdown document while the user watches changes live in a browser, then collect the user's inline comments back as JSON. Use when iterating on a markdown doc, draft, spec, proposal, plan, or report that the user wants to review interactively — i.e. when the user asks to "review this doc", "edit and let me comment", "show me the changes live", "iterate on this markdown with me", or wants to leave feedback on a document without copy-pasting. Backed by the bundled mdedit.py tool.
license: MIT
metadata:
  requires: python>=3.10
  platform: posix
  dependencies: none (Python standard library only)
---

# Markdown Review

Interactive, human-in-the-loop markdown editing. The bundled `mdedit.py` script
opens a browser viewer that:

1. **Shows your edits live** — every edit is applied through the tool and
   streamed to the browser, with changed blocks flashing and a Rendered/Changes
   (before/after diff) toggle.
2. **Collects the user's comments** — the user selects text to attach inline
   comments, then clicks "Send to LLM". Those comments come back to you as JSON,
   so there is no copy-paste round trip.

## Requirements

- **Python 3.10+** — the script is pure standard library, so there is nothing to
  `pip install`. It runs with the system `python3`.
- **POSIX only** — the daemon backgrounds itself with `fork`, so it does not run
  on native Windows (use WSL there).
- **Works offline** — the browser viewer's two JS libraries (`marked` and
  `highlight.js`) are vendored under `vendor/` and served by the daemon, so no
  network is needed. If `vendor/` is absent the HTML falls back to a CDN. To
  refresh or re-pin the vendored libraries, run `./update-vendor.sh` (versions
  are defined by `VENDOR_ASSETS` in `mdedit.py`).

## Locating the script

`mdedit.py` is bundled next to this `SKILL.md`. Before running any command,
point a `MDEDIT` variable at it — using the directory of this skill file so it
works wherever the skill was installed (e.g. `.agents/skills/markdown-review/`,
`.claude/skills/markdown-review/`, `~/.config/opencode/skills/markdown-review/`):

```bash
# Set once per session. SKILL_DIR is the folder containing this SKILL.md.
MDEDIT="<path-to-this-skill-dir>/mdedit.py"
python3 "$MDEDIT" <subcommand> ...
```

All examples below use `$MDEDIT`. Substitute the absolute path to the bundled
`mdedit.py` for your install location.

## When to use this skill

Use it whenever the user wants to **review a markdown document as you iterate on
it**, rather than reading diffs in chat. Typical triggers:

- "Draft X and let me comment on it."
- "Edit this doc and show me the changes."
- "Open this in the reviewer."
- The user has left comments in the reviewer and wants you to address them.

Do **not** use it for ordinary file edits the user is not reviewing visually —
use the normal edit tools for those.

## Workflow

1. **Open the document.** This spawns the viewer (auto-opens a browser tab the
   first time):

   ```bash
   python3 "$MDEDIT" open path/to/doc.md
   ```

   If the file does not exist it is created empty. Skip this step if you go
   straight to `edit` — the first `edit` opens the viewer too.

2. **Apply edits.** Edits use search/replace semantics: `--old` must occur
   exactly once in the current document (or pass `--replace-all`). Each edit is
   pushed to the browser immediately.

   ```bash
   python3 "$MDEDIT" edit path/to/doc.md \
     --old "Two weeks." --new "Three weeks, with a buffer for review."
   ```

   To match or insert a line break, write `\n` in `--old`/`--new` — backslash
   escapes (`\n`, `\t`, `\r`, `\\`) are interpreted, so `--old "a\nb"` spans
   two lines. (Use `\n` inside the JSON for `--edits-json`, where JSON decodes
   its own escapes.) For several edits in one go, pass a JSON array on stdin (lower latency, one
   browser update):

   ```bash
   python3 "$MDEDIT" edit path/to/doc.md --edits-json - <<'JSON'
   [
     {"old": "old text A", "new": "new text A"},
     {"old": "old text B", "new": "new text B", "replace_all": true}
   ]
   JSON
   ```

   The command prints JSON: `{ "applied": [...], "errors": [...], "ok": bool }`.
   If `ok` is false, an edit's `old` was missing or ambiguous — read the
   `errors[].error` message, add more surrounding context to `old`, and retry.
   The edited document is written to disk as edits apply.

3. **Request review.** When you have finished a round of edits and want the
   user's feedback, block on `review`. It waits until the user clicks
   "Send to LLM" (or the timeout), then prints the comments as JSON to stdout:

   ```bash
   python3 "$MDEDIT" review path/to/doc.md
   ```

   Tell the user (in chat) that the reviewer is open and you are waiting for
   them to add comments and click **Send to LLM**.

4. **Act on the comments.** The JSON looks like:

   ```json
   {
     "submitted": true,
     "current_round": 2,
     "edit_count": 3,
     "comment_count": 2,
     "comments": [
       {
         "id": 1,
         "body": "This goal is too vague — quantify feature X.",
         "quote": "Deliver feature X",
         "context_before": "## Goals\n\n- ",
         "context_after": "\n- Improve performance",
         "source": "doc",
         "round": 0
       },
       {
         "id": 2,
         "body": "Why drop this line?",
         "quote": "- Reduce error rates",
         "source": "diff",
         "round": 1
       }
     ]
   }
   ```

   - `quote` is the exact text the comment is anchored to (empty for a general
     comment). Use `quote` + `context_before`/`context_after` to locate the spot
     to revise, then apply more `edit` commands.
   - `source` tells you where the comment was made: `"doc"` (the rendered view —
     anchored to document text) or `"diff"` (the Changes view — anchored to a
     diff line from the given `round`). For a `"diff"` comment, `round` is the
     round whose change the user is reacting to.
   - An empty `comments` array with `submitted: true` means the user approved
     with no changes.
   - **Resolve each comment once you have addressed it** so it does not come
     back on the next review round. Pass the comment `id`(s):

     ```bash
     python3 "$MDEDIT" resolve path/to/doc.md --id 1 --id 2
     ```

     Use `--all` to clear every outstanding comment at once. Only resolve a
     comment after you have actually applied the edit it asked for — leave any
     comment you are deferring so the user still sees it next round.

   - Loop: apply edits → resolve addressed comments → `review` again — as many
     rounds as the user wants. Each `review` blocks again until the user clicks
     **Send to LLM**, so you will not get stale, already-actioned comments back.

## Rounds and the review cycle

The intended cycle is: _LLM writes → user comments → LLM updates → user reviews
the updates and comments → LLM updates → … until the user is happy._ To support
this, edits are grouped into **rounds**:

- A **round** is one LLM edit pass. The first `edit` you apply after the user
  submits a `review` automatically opens the next round.
- When a new round opens, the previous rounds' diffs are pruned from the
  Changes view by default, so the user sees only what changed in the latest
  pass. The document text is never affected — only the diff history.
- In the browser's **Changes** view, diffs are grouped under collapsible
  **Round N** headers (newest first, latest expanded). The user can select any
  diff line and comment on it; that comment returns to you with
  `source: "diff"` and the `round` it belongs to.
- You normally don't manage rounds explicitly — they advance on their own each
  review cycle. If you ever want to prune the diff history yourself, use
  `clear-diffs` (keeps the current round) or `clear-diffs --all` (wipes all).
  The user can also click **Clear old rounds** in the browser.

5. **Finish.** When the user is happy, shut the session down:

   ```bash
   python3 "$MDEDIT" stop path/to/doc.md
   ```

## Command reference

| Command       | Purpose                                                      | Blocking? |
| ------------- | ------------------------------------------------------------ | --------- |
| `open`        | Open/focus the viewer for a document.                        | No        |
| `edit`        | Apply one or more search/replace edits; pushes a live diff.  | No        |
| `review`      | Wait for the user to send comments, then print them as JSON. | **Yes**   |
| `resolve`     | Clear comments you have addressed (`--id N` or `--all`).     | No        |
| `clear-diffs` | Prune the Changes-view diff history (`--all` wipes all).     | No        |
| `status`      | Print session state (version, edit/comment counts) as JSON.  | No        |
| `stop`        | Shut the session daemon down.                                | No        |

Useful flags:

- `--no-browser` (top-level): do not auto-open a browser tab. Use in headless
  contexts.
- `review --timeout N`: wait at most N seconds (0 = wait forever, the default).
- `review --json`: suppress the human-readable stderr notice; only emit JSON.

## Tips

- One session exists per document path; repeated `open`/`edit` reuse it. The
  user can keep the tab open across many edit→review rounds.
- Run `edit` commands one logical change at a time so the user sees each change
  flash distinctly, or batch with `--edits-json` when changes are tightly
  related.
- Always prefer this skill's `edit` over directly writing the file when the user
  is reviewing live, so the browser stays in sync and the diff history is built.
- After each `review`, `resolve` the comments you actioned before editing or
  reviewing again — otherwise the user has to delete them manually in the
  browser. `review` re-arms itself each round, so it will block for fresh input
  rather than returning the same comments.
- Comments with `source: "diff"` are the user reacting to a specific change you
  made in a given `round`. Read them as "feedback on your last edit" rather than
  "feedback on the document as written", and reply with a follow-up edit.
- The script writes edits to the file on disk, so the document is always
  persisted — there is nothing extra to save.
