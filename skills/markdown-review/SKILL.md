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

- **Python 3.10+** — the tool is pure standard library, so there is nothing to
  `pip install`. It runs with the system `python3`.
- **POSIX only** — the daemon backgrounds itself with `fork`, so it does not run
  on native Windows (use WSL there).
- **Browser libraries from CDN** — the viewer renders markdown, code, math, and
  diagrams using JS libraries (`marked`, `highlight.js`, [KaTeX](https://katex.org),
  [Mermaid](https://mermaid.js.org)) loaded from CDN, so an internet connection
  is required while reviewing.
- **LaTeX math and Mermaid diagrams** — the viewer renders inline math
  (`$E=mc^2$`), display math (`$$...$$`), and ``` ```mermaid ``` ``` diagram
  code fences. These features load [KaTeX](https://katex.org) and
  [Mermaid](https://mermaid.js.org) from CDN, so they require an internet
  connection.

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
         "author": "You",
         "stale": false,
         "round": 0,
         "replies": []
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
   - `author` identifies who wrote the comment (default `"You"` for the live
     viewer; the colleague's name for imported comments).
   - `stale` is `true` if the comment's `quote` text no longer exists in the
     current document (the text was edited after the comment was made). Treat
     the context as potentially shifted.
   - `replies` is a list of threaded replies attached to the comment (each has
     `body`, `author`, and `ts`). They appear in the browser sidebar under the
     parent comment and are included when comments are exported/imported.
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

   - Users can also **edit** any comment (fix typos, refine wording) and
     **reply** to imported comments with a threaded note directly in the
     browser sidebar. Both are UI-only actions — no CLI command is needed.

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

## Sharing for offline review

The viewer has a **Share** button (top bar) that downloads a standalone HTML
file — a self-contained copy of the document with the full commenting UI but
**no server dependency**. The colleague opens it in any browser, adds comments,
and clicks **Export comments** to download a JSON file. You then import those
comments back into the live session.

There is also a **Download** button (top bar) that saves the document's current
markdown source as a plain `.md` file — useful when you just want to send the
raw file to a reviewer rather than a shareable HTML copy.

This is useful when a colleague can't connect to your local server (different
machine, offline, etc.) or when you want to collect feedback from multiple
people in parallel.

### Sharing out

1. The user clicks **Share** in the live viewer (or you run the CLI command):

   ```bash
   python3 "$MDEDIT" share path/to/doc.md > doc.share.html
   ```

   The CLI `share` command writes standalone HTML to stdout. If a session is
   running, the snapshot includes diff history; otherwise it reads the file
   from disk.

2. Send the `.share.html` file to your colleague (email, chat, etc.).

### Collecting comments back

1. Your colleague opens the file in a browser, enters their name, adds
   comments, and clicks **Export comments**, then **Download JSON** (or copies
   the JSON from the box). They send back the `.comments.json` file.

2. Import the comments into the live session. Either from the **live viewer's**
   **Import ▾** button (top bar) — **Import from File…** to pick the
   `.comments.json`, or **Import Comments…** to paste JSON into a box — or from
   the CLI:

   ```bash
   python3 "$MDEDIT" import-comments path/to/doc.md --from colleague.comments.json
   ```

   Use `--from -` to read from stdin instead. The in-browser import and the
   CLI hit the same `/api/import` endpoint, so dedup and stale-flagging behave
   identically.

3. The output JSON reports how many comments were imported, how many were
   skipped as duplicates, and how many were flagged stale:

   ```json
   {
     "ok": true,
     "imported": 3,
     "skipped_duplicates": 1,
     "stale": 1,
     "stale_ids": [5],
     "url": "http://127.0.0.1:7575"
   }
   ```

   - **Duplicates** are detected by `(author, quote, body, source, round)` —
     re-importing the same file is safe, and the same feedback from different
     people is preserved.
   - **Stale** comments reference text that has since been edited. They appear
     with a warning in the viewer and a `"stale": true` flag in the review JSON.
   - Imported comments are added as **pending, unsubmitted** comments — they
     appear immediately in the live viewer's sidebar and as highlights in the
     document, but are **not** sent to you yet. They wait for the user to click
     **Send to LLM**, exactly like comments the user typed directly.

4. **Block on `review` before responding.** Imported comments are *not* acted
   on automatically. After importing, run `review`:

   ```bash
   python3 "$MDEDIT" review path/to/doc.md
   ```

   It blocks until the user reviews the comments in the browser and clicks
   **Send to LLM**, then the comments come back to you as JSON. **Do not act on the
   imported JSON directly** — the user must get the chance to review and gate
   the feedback, just as in a normal round. (Skipping `review` also skips the
   new-round arming that `reset_submitted` does, so response edits would land
   in the wrong round in the Changes view.)

5. Once `review` returns, address the comments with `edit` (which opens a fresh
   round), `resolve` the ones you addressed, and loop as usual. You can import
   from multiple colleagues before a single `review` — each import adds new
   (deduplicated) pending comments.

6. **Finish.** When the user is happy, shut the session down:

   ```bash
   python3 "$MDEDIT" stop path/to/doc.md
   ```

## Resuming a session

Sessions are **automatically persisted** to disk as you work — every edit,
comment, and review round is saved. If the daemon dies (idle timeout, crash,
restart, or `stop`), you can **resume** the session with all diff history,
comments, and round bookkeeping intact.

### Discovering resumable sessions

To see which documents have saved sessions:

```bash
python3 "$MDEDIT" resume --list
```

This prints a JSON array of resumable sessions with their document path, round,
comment count, edit count, and whether the file still exists on disk.

### Resuming

```bash
python3 "$MDEDIT" resume path/to/doc.md
```

This spawns a new daemon with the saved state restored, opens the browser, and
prints a summary JSON:

```json
{
  "ok": true,
  "url": "http://127.0.0.1:7575",
  "path": "/abs/doc.md",
  "restored": true,
  "round": 2,
  "comments": 3,
  "edits": 5
}
```

If a daemon is already running for the document, `resume` reuses it (the saved
state is not re-applied).

### External-edit detection

If the document was modified outside mdedit since the session was saved (e.g.
someone edited it directly), `resume` includes a `warning` field in the output:

```json
{
  "ok": true,
  "restored": true,
  "warning": "document was modified externally since the session was saved",
  "disk_text_length": 1250,
  "saved_text_length": 1234
}
```

When you see this warning, inspect the document to understand what changed. The
saved diff history and comments may reference text that no longer matches. You
can either proceed with the restored session (if the changes are minor) or start
fresh with `open`.

### Stopping without saving / purging

`stop` by default keeps the saved session so you can resume later. To stop and
**permanently delete** the saved session (diffs, comments, rounds):

```bash
python3 "$MDEDIT" stop --purge path/to/doc.md
```

The document file itself is never deleted — only the session metadata.

## Command reference

| Command             | Purpose                                                      | Blocking? |
| ------------------- | ------------------------------------------------------------ | --------- |
| `open`              | Open/focus the viewer for a document.                        | No        |
| `edit`              | Apply one or more search/replace edits; pushes a live diff.  | No        |
| `review`            | Wait for the user to send comments, then print them as JSON. | **Yes**   |
| `resolve`           | Clear comments you have addressed (`--id N` or `--all`).     | No        |
| `clear-diffs`       | Prune the Changes-view diff history (`--all` wipes all).     | No        |
| `share`             | Generate standalone share HTML for offline review (stdout).  | No        |
| `import-comments`   | Import comments from exported JSON into the running session. | No        |
| `status`            | Print session state (version, edit/comment counts) as JSON.  | No        |
| `resume`            | Resume a saved session (restore diffs, comments, rounds).   | No        |
| `stop`              | Shut the session daemon down (`--purge` also deletes the    | No        |
|                     | saved session so it cannot be resumed).                     |          |

Useful flags:

- `--no-browser` (top-level): do not auto-open a browser tab. Use in headless
  contexts.
- `review --timeout N`: wait at most N seconds (0 = wait forever, the default).
- `review --json`: suppress the human-readable stderr notice; only emit JSON.
- `import-comments --from PATH`: path to comment JSON file, or `-` for stdin.
- `resume --list`: list all resumable sessions and exit.
- `stop --purge`: also delete the saved session so it cannot be resumed.

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
  persisted — there is nothing extra to save. Session state (diffs, comments,
  rounds) is also auto-saved; use `resume` to recover after the daemon dies.
