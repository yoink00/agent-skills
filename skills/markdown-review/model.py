"""Session model for mdedit.

Holds all live state for one document under review plus the diff/file
helpers the model depends on. Pure standard library, fully unit-testable
without spawning the HTTP daemon.

See ``mdedit.py`` for the architectural overview.
"""

from __future__ import annotations

import difflib
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class EditRecord:
    """A single applied search/replace edit and its rendered diff."""

    index: int
    old: str
    new: str
    diff: str  # unified diff (text) for this single edit
    round: int = 1  # which review round this edit belongs to
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "old": self.old,
            "new": self.new,
            "diff": self.diff,
            "round": self.round,
            "ts": self.ts,
        }


@dataclass
class Comment:
    """A user comment, optionally anchored to a text selection or a diff line."""

    id: int
    body: str
    quote: str = ""  # the selected text the comment is anchored to
    context_before: str = ""  # a little surrounding context for the LLM
    context_after: str = ""
    source: str = "doc"  # "doc" (rendered view) or "diff" (Changes view)
    round: int = 0  # round the diff comment refers to (0 = n/a)
    author: str = "You"  # who wrote the comment (for multi-reviewer support)
    stale: bool = False  # true if the quote text no longer exists in the doc
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "body": self.body,
            "quote": self.quote,
            "context_before": self.context_before,
            "context_after": self.context_after,
            "source": self.source,
            "round": self.round,
            "author": self.author,
            "stale": self.stale,
            "ts": self.ts,
        }


class Session:
    """
    Holds all live state for one document under review.

    Thread-safe: every mutation takes ``self.lock``. A Condition lets the /poll
    long-poll and the /review-wait blocking endpoint sleep until something
    interesting happens (a new edit, a new comment, or the user hitting "Send").
    """

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self._cond = threading.Condition(self.lock)

        self.original_text: str = _read(path)
        self.current_text: str = self.original_text
        self.edits: list[EditRecord] = []
        self.comments: list[Comment] = []
        self._comment_seq = 0

        # Round bookkeeping. A "round" is one LLM edit pass. The first edit that
        # lands after the user submits a review starts the next round.
        self.current_round: int = 1
        self._new_round_pending: bool = False

        # Bumped on every edit so the browser can hot-reload via long-poll.
        self.version: int = 0
        # Set true when the user clicks "Send to LLM"; unblocks review-wait.
        self.submitted: bool = False
        self.last_activity: float = time.time()

    # -- edits --------------------------------------------------------------

    def apply_edit(
        self, old: str, new: str, replace_all: bool = False, auto_clear: bool = True
    ) -> EditRecord:
        """Apply one search/replace edit. Raises ValueError on a bad match.

        If a new round is pending (the user submitted a review since the last
        edit), this edit opens that round. When ``auto_clear`` is true, diffs
        from all earlier rounds are dropped so the Changes view shows only this
        pass.
        """
        with self._cond:
            text = self.current_text
            if old == "":
                raise ValueError("`old` must not be empty")
            count = text.count(old)
            if count == 0:
                raise ValueError("`old` text not found in document")
            if count > 1 and not replace_all:
                raise ValueError(
                    f"`old` text is ambiguous: found {count} occurrences. "
                    f"Provide more surrounding context or pass --replace-all."
                )

            # Open a new round on the first edit after a submit.
            if self._new_round_pending:
                self.current_round += 1
                self._new_round_pending = False
                if auto_clear:
                    self.edits = [
                        e for e in self.edits if e.round == self.current_round
                    ]

            before = text
            text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
            self.current_text = text

            diff = _unified_diff(before, text, self.path.name)
            rec = EditRecord(
                index=len(self.edits),
                old=old,
                new=new,
                diff=diff,
                round=self.current_round,
            )
            self.edits.append(rec)
            self.version += 1
            self.last_activity = time.time()

            # Persist to disk so the file on disk reflects the live document.
            _write(self.path, text)

            self._cond.notify_all()
            return rec

    def clear_diffs(self, keep_current: bool = True) -> int:
        """Drop edit/diff records. Keeps the current round's diffs by default.

        The document text is untouched — only the visible diff history shrinks.
        Returns the number of edit records removed.
        """
        with self._cond:
            before = len(self.edits)
            if keep_current:
                self.edits = [e for e in self.edits if e.round == self.current_round]
            else:
                self.edits = []
            removed = before - len(self.edits)
            if removed:
                self.version += 1
                self.last_activity = time.time()
                self._cond.notify_all()
            return removed

    # -- comments -----------------------------------------------------------

    def add_comment(
        self,
        body: str,
        quote: str = "",
        before: str = "",
        after: str = "",
        source: str = "doc",
        round: int = 0,
        author: str = "You",
        stale: bool = False,
    ) -> Comment:
        with self._cond:
            self._comment_seq += 1
            c = Comment(
                id=self._comment_seq,
                body=body,
                quote=quote,
                context_before=before,
                context_after=after,
                source=source,
                round=round,
                author=author,
                stale=stale,
            )
            self.comments.append(c)
            self.version += 1
            self.last_activity = time.time()
            self._cond.notify_all()
            return c

    def delete_comment(self, cid: int) -> bool:
        with self._cond:
            n = len(self.comments)
            self.comments = [c for c in self.comments if c.id != cid]
            changed = len(self.comments) != n
            if changed:
                self.version += 1
                self.last_activity = time.time()
                self._cond.notify_all()
            return changed

    def comment_exists(self, **kwargs) -> bool:
        """Check whether a comment with the given field values already exists.

        Used by ``import-comments`` to skip exact duplicates. Only the fields
        passed as keyword arguments are compared.
        """
        with self.lock:
            for c in self.comments:
                if all(getattr(c, k) == v for k, v in kwargs.items()):
                    return True
            return False

    def submit(self) -> None:
        with self._cond:
            self.submitted = True
            self.last_activity = time.time()
            self._cond.notify_all()

    def reset_submitted(self) -> None:
        """Clear the submitted flag so the next ``review`` blocks again.

        Also arms the next round: the LLM has just consumed a review, so its
        next edit should open a fresh round (and clear the prior round's diffs).
        """
        with self._cond:
            self.submitted = False
            self._new_round_pending = True
            self.last_activity = time.time()
            self._cond.notify_all()

    def touch(self) -> None:
        """Bump ``last_activity`` without any state change.

        Called by poll-style GET handlers (/api/poll, /api/comments) so that an
        open browser tab or an in-flight CLI ``review`` keeps the daemon alive.
        """
        with self._cond:
            self.last_activity = time.time()

    # -- waiters ------------------------------------------------------------

    def wait_for_version(self, since: int, timeout: float = 25.0) -> int:
        deadline = time.monotonic() + timeout
        with self._cond:
            while self.version <= since and not self.submitted:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=remaining)
            return self.version

    def wait_for_submit(self, timeout: float | None = None) -> bool:
        """Block until the user clicks Send (returns True) or timeout (False)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while not self.submitted:
                if deadline is None:
                    self._cond.wait(timeout=1.0)
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(timeout=remaining)
            return self.submitted

    # -- snapshots ----------------------------------------------------------

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "path": str(self.path),
                "name": self.path.name,
                "version": self.version,
                "submitted": self.submitted,
                "current_round": self.current_round,
                "original_text": self.original_text,
                "current_text": self.current_text,
                "edits": [e.to_dict() for e in self.edits],
                "comments": [c.to_dict() for c in self.comments],
            }

    def comments_payload(self) -> dict:
        with self.lock:
            return {
                "path": str(self.path),
                "submitted": self.submitted,
                "current_round": self.current_round,
                "edit_count": len(self.edits),
                "comments": [c.to_dict() for c in self.comments],
            }


# ---------------------------------------------------------------------------
# File / diff helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _unified_diff(before: str, after: str, name: str) -> str:
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{name}",
        tofile=f"b/{name}",
        n=3,
    )
    return "".join(diff)
