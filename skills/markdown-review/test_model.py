"""Fast unit tests for the mdedit session model.

Unlike test_idle_shutdown.py / test_comment_highlights.py, these exercise the
model (Session, EditRecord, Comment) directly — no daemon, no HTTP, no
subprocess — so they run in milliseconds and pin down the round/edit/comment
state machine precisely.

The model is imported by adding the skill directory to sys.path (it is a
standalone script, not an installed package).
"""

import sys
import threading
import time

import pytest

SKILL_DIR = "skills/markdown-review"
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

from model import (  # noqa: E402
    Comment,
    EditRecord,
    Session,
    _read,
    _unified_diff,
    _write,
)


@pytest.fixture
def session(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("# Title\n\nHello world.\n")
    return Session(doc)


# ---------------------------------------------------------------------------
# EditRecord / Comment dataclasses
# ---------------------------------------------------------------------------


class TestEditRecord:
    def test_to_dict_roundtrip(self):
        rec = EditRecord(index=2, old="a", new="b", diff="@@@", round=3)
        d = rec.to_dict()
        assert d == {
            "index": 2,
            "old": "a",
            "new": "b",
            "diff": "@@@",
            "round": 3,
            "ts": d["ts"],
        }
        assert isinstance(d["ts"], float)

    def test_defaults(self):
        rec = EditRecord(index=0, old="a", new="b", diff="")
        assert rec.round == 1
        assert rec.ts > 0


class TestComment:
    def test_to_dict_roundtrip(self):
        c = Comment(id=1, body="hi", quote="q", source="diff", round=2)
        d = c.to_dict()
        assert d["id"] == 1
        assert d["body"] == "hi"
        assert d["quote"] == "q"
        assert d["context_before"] == ""
        assert d["context_after"] == ""
        assert d["source"] == "diff"
        assert d["round"] == 2
        assert d["author"] == "You"
        assert d["stale"] is False

    def test_defaults(self):
        c = Comment(id=1, body="x")
        assert c.quote == ""
        assert c.source == "doc"
        assert c.round == 0
        assert c.author == "You"
        assert c.stale is False


# ---------------------------------------------------------------------------
# apply_edit — validation
# ---------------------------------------------------------------------------


class TestApplyEditValidation:
    def test_simple_replace(self, session):
        rec = session.apply_edit("Hello world.", "Hello universe.")
        assert session.current_text == "# Title\n\nHello universe.\n"
        assert rec.old == "Hello world."
        assert rec.new == "Hello universe."
        assert rec.index == 0
        assert rec.round == 1
        assert "+Hello universe." in rec.diff
        assert "-Hello world." in rec.diff

    def test_persists_to_disk(self, session):
        session.apply_edit("Hello world.", "Hello universe.")
        assert session.path.read_text() == "# Title\n\nHello universe.\n"

    def test_empty_old_rejected(self, session):
        with pytest.raises(ValueError, match="must not be empty"):
            session.apply_edit("", "x")

    def test_not_found_rejected(self, session):
        with pytest.raises(ValueError, match="not found"):
            session.apply_edit("nope", "x")

    def test_ambiguous_old_rejected(self, tmp_path):
        doc = tmp_path / "d.md"
        doc.write_text("dup dup")  # two occurrences, no surrounding text
        s = Session(doc)
        with pytest.raises(ValueError, match="ambiguous"):
            s.apply_edit("dup", "x")

    def test_replace_all_replaces_every_occurrence(self, tmp_path):
        doc = tmp_path / "d.md"
        doc.write_text("dup dup")
        s = Session(doc)
        s.apply_edit("dup", "X", replace_all=True)
        assert s.current_text == "X X"
        assert len(s.edits) == 1

    def test_single_occurrence_replaced(self, tmp_path):
        # With exactly one occurrence the default (non-replace-all) path works;
        # count>1 would be rejected as ambiguous (see test_ambiguous_old_rejected).
        doc = tmp_path / "d.md"
        doc.write_text("keep change keep")
        s = Session(doc)
        s.apply_edit("change", "edit")
        assert s.current_text == "keep edit keep"

    def test_bumps_version_and_activity(self, session):
        v0 = session.version
        a0 = session.last_activity
        time.sleep(0.01)
        session.apply_edit("Hello", "Hi")
        assert session.version == v0 + 1
        assert session.last_activity > a0

    def test_edit_index_increments(self, session):
        session.apply_edit("Hello world.", "A.")
        session.apply_edit("A.", "B.")
        assert [e.index for e in session.edits] == [0, 1]


# ---------------------------------------------------------------------------
# Round bookkeeping
# ---------------------------------------------------------------------------


class TestRounds:
    def test_no_new_round_until_submit(self, session):
        session.apply_edit("Hello", "Hi")
        session.apply_edit("world", "earth")
        assert session.current_round == 1
        assert all(e.round == 1 for e in session.edits)

    def test_first_edit_after_submit_opens_new_round(self, session):
        session.apply_edit("Hello", "Hi")  # round 1
        session.submit()
        session.reset_submitted()
        session.apply_edit("Hi", "Hey")  # opens round 2
        assert session.current_round == 2
        assert session.edits[-1].round == 2

    def test_auto_clear_drops_earlier_rounds(self, session):
        session.apply_edit("Hello", "Hi")  # round 1
        session.submit()
        session.reset_submitted()
        session.apply_edit("Hi", "Hey")  # round 2 -> clears round 1
        rounds = {e.round for e in session.edits}
        assert rounds == {2}

    def test_auto_clear_disabled_keeps_history(self, session):
        session.apply_edit("Hello", "Hi")  # round 1
        session.submit()
        session.reset_submitted()
        session.apply_edit("Hi", "Hey", auto_clear=False)  # round 2
        rounds = sorted(e.round for e in session.edits)
        assert rounds == [1, 2]

    def test_only_first_post_submit_edit_changes_round(self, session):
        session.submit()
        session.reset_submitted()
        session.apply_edit("Hello", "Hi")  # round 2
        session.apply_edit("Hi", "Hey")  # still round 2
        assert session.current_round == 2
        assert all(e.round == 2 for e in session.edits)


# ---------------------------------------------------------------------------
# clear_diffs
# ---------------------------------------------------------------------------


class TestClearDiffs:
    def test_keep_current_keeps_current_round(self, session):
        session.apply_edit("Hello", "Hi")  # round 1
        session.submit()
        session.reset_submitted()
        session.apply_edit("Hi", "Hey")  # round 2 (clears round 1)
        removed = session.clear_diffs(keep_current=True)
        assert removed == 0  # nothing older than current remains
        assert all(e.round == 2 for e in session.edits)

    def test_keep_current_removes_older_when_history_present(self, session):
        session.apply_edit("Hello", "Hi")  # round 1
        session.submit()
        session.reset_submitted()
        session.apply_edit("Hi", "Hey", auto_clear=False)  # round 2, keeps r1
        removed = session.clear_diffs(keep_current=True)
        assert removed == 1
        assert all(e.round == 2 for e in session.edits)

    def test_clear_all(self, session):
        session.apply_edit("Hello", "Hi")
        removed = session.clear_diffs(keep_current=False)
        assert removed == 1
        assert session.edits == []

    def test_clear_all_when_empty(self, session):
        assert session.clear_diffs(keep_current=False) == 0

    def test_clear_does_not_touch_text(self, session):
        session.apply_edit("Hello", "Hi")
        session.clear_diffs(keep_current=False)
        assert "Hi" in session.current_text

    def test_clear_bumps_version_only_when_changed(self, session):
        v0 = session.version
        assert session.clear_diffs() == 0
        assert session.version == v0  # nothing removed -> no bump
        session.apply_edit("Hello", "Hi")
        v1 = session.version
        session.clear_diffs(keep_current=False)
        assert session.version == v1 + 1


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


class TestComments:
    def test_add_comment_assigns_sequential_ids(self, session):
        c1 = session.add_comment("first")
        c2 = session.add_comment("second", quote="q", source="diff", round=1)
        assert c1.id == 1
        assert c2.id == 2
        assert c2.quote == "q"
        assert c2.source == "diff"
        assert c2.round == 1
        assert len(session.comments) == 2

    def test_add_comment_bumps_version(self, session):
        v0 = session.version
        session.add_comment("x")
        assert session.version == v0 + 1

    def test_delete_comment(self, session):
        c = session.add_comment("x")
        assert session.delete_comment(c.id) is True
        assert session.comments == []

    def test_delete_missing_comment_returns_false(self, session):
        session.add_comment("x")
        assert session.delete_comment(999) is False
        assert len(session.comments) == 1

    def test_delete_bumps_version_only_when_changed(self, session):
        session.add_comment("x")
        v = session.version
        session.delete_comment(999)
        assert session.version == v  # no change

    def test_add_comment_with_author(self, session):
        c = session.add_comment("note", author="Alice")
        assert c.author == "Alice"
        assert c.to_dict()["author"] == "Alice"

    def test_add_comment_default_author(self, session):
        c = session.add_comment("note")
        assert c.author == "You"

    def test_add_comment_with_stale(self, session):
        c = session.add_comment("note", quote="old text", stale=True)
        assert c.stale is True
        assert c.to_dict()["stale"] is True

    def test_add_comment_default_stale_false(self, session):
        c = session.add_comment("note")
        assert c.stale is False

    def test_import_comments_merges_new(self, session):
        summary = session.import_comments(
            [{"body": "hi", "quote": "q", "source": "doc", "author": "Alice"}]
        )
        assert summary["imported"] == 1
        assert summary["skipped_duplicates"] == 0
        assert [c.author for c in session.comments] == ["Alice"]

    def test_import_comments_skips_duplicates_by_identity(self, session):
        session.import_comments([{"body": "hi", "quote": "q", "author": "Alice"}])
        summary = session.import_comments(
            [{"body": "hi", "quote": "q", "author": "Alice"}]
        )
        assert summary["imported"] == 0
        assert summary["skipped_duplicates"] == 1
        # Same body from a different author is kept.
        summary2 = session.import_comments(
            [{"body": "hi", "quote": "q", "author": "Bob"}]
        )
        assert summary2["imported"] == 1

    def test_import_comments_flags_stale_quotes(self, session):
        # Quote not present in the document text → stale.
        summary = session.import_comments(
            [{"body": "gone", "quote": "no such text", "author": "Alice"}]
        )
        assert summary["imported"] == 1
        assert summary["stale"] == 1
        assert session.comments[0].stale is True
        assert session.comments[0].id in summary["stale_ids"]

    def test_edit_comment_updates_body(self, session):
        c = session.add_comment("original")
        updated = session.edit_comment(c.id, "revised")
        assert updated is not None
        assert updated.body == "revised"
        assert session.comments[0].body == "revised"

    def test_edit_comment_missing_returns_none(self, session):
        session.add_comment("x")
        assert session.edit_comment(999, "new") is None

    def test_edit_comment_bumps_version(self, session):
        c = session.add_comment("x")
        v = session.version
        session.edit_comment(c.id, "y")
        assert session.version == v + 1

    def test_edit_comment_notifies_change(self, session):
        calls = []
        session.on_change = lambda: calls.append(1)
        c = session.add_comment("x")
        calls.clear()  # reset after add
        session.edit_comment(c.id, "y")
        assert len(calls) == 1


class TestReplies:
    def test_add_reply_to_comment(self, session):
        c = session.add_comment("original")
        r = session.add_reply(c.id, "a note", author="Alice")
        assert r is not None
        assert r.body == "a note"
        assert r.author == "Alice"
        assert r.id == 1
        assert len(session.comments[0].replies) == 1

    def test_add_reply_default_author(self, session):
        c = session.add_comment("x")
        r = session.add_reply(c.id, "note")
        assert r.author == "You"

    def test_add_reply_missing_comment_returns_none(self, session):
        assert session.add_reply(999, "note") is None

    def test_add_reply_increments_reply_id(self, session):
        c = session.add_comment("x")
        r1 = session.add_reply(c.id, "first")
        r2 = session.add_reply(c.id, "second")
        assert r1.id == 1
        assert r2.id == 2

    def test_add_reply_bumps_version(self, session):
        c = session.add_comment("x")
        v = session.version
        session.add_reply(c.id, "note")
        assert session.version == v + 1

    def test_add_reply_notifies_change(self, session):
        calls = []
        session.on_change = lambda: calls.append(1)
        c = session.add_comment("x")
        calls.clear()
        session.add_reply(c.id, "note")
        assert len(calls) == 1

    def test_reply_serialized_in_snapshot(self, session):
        c = session.add_comment("x", quote="Hello")
        session.add_reply(c.id, "reply body", author="Bob")
        snap = session.snapshot()
        comment_dict = snap["comments"][0]
        assert len(comment_dict["replies"]) == 1
        assert comment_dict["replies"][0]["body"] == "reply body"
        assert comment_dict["replies"][0]["author"] == "Bob"

    def test_restore_round_trip_with_replies(self, tmp_path):
        """snapshot → restore → re-snapshot preserves replies."""
        doc = tmp_path / "doc.md"
        doc.write_text("# Hello world\n")
        s1 = Session(doc)
        c = s1.add_comment("original", quote="Hello")
        s1.add_reply(c.id, "reply 1", author="Alice")
        s1.add_reply(c.id, "reply 2", author="Bob")
        snap1 = s1.snapshot()

        s2 = Session(doc)
        s2.restore(snap1)
        snap2 = s2.snapshot()

        assert snap2["comments"][0]["replies"] == snap1["comments"][0]["replies"]

    def test_import_comments_carries_replies(self, session):
        summary = session.import_comments(
            [
                {
                    "body": "main",
                    "quote": "",
                    "author": "Alice",
                    "replies": [
                        {"body": "reply text", "author": "Bob"},
                    ],
                }
            ]
        )
        assert summary["imported"] == 1
        assert len(session.comments[0].replies) == 1
        assert session.comments[0].replies[0].body == "reply text"
        assert session.comments[0].replies[0].author == "Bob"


# ---------------------------------------------------------------------------
# submit / reset lifecycle
# ---------------------------------------------------------------------------


class TestSubmitLifecycle:
    def test_submit_sets_flag(self, session):
        assert session.submitted is False
        session.submit()
        assert session.submitted is True

    def test_reset_clears_flag(self, session):
        session.submit()
        session.reset_submitted()
        assert session.submitted is False

    def test_reset_arms_next_round(self, session):
        session.reset_submitted()
        session.apply_edit("Hello", "Hi")
        assert session.current_round == 2

    def test_submit_without_reset_does_not_arm_round(self, session):
        session.submit()
        session.apply_edit("Hello", "Hi")  # no reset -> still round 1
        assert session.current_round == 1


# ---------------------------------------------------------------------------
# touch
# ---------------------------------------------------------------------------


def test_touch_bumps_activity_without_state_change(session):
    v = session.version
    old = session.last_activity
    time.sleep(0.01)
    session.touch()
    assert session.last_activity > old
    assert session.version == v  # touch must not bump version


# ---------------------------------------------------------------------------
# wait_idle
# ---------------------------------------------------------------------------


class TestWaitIdle:
    def test_blocks_for_the_poll_window(self, session):
        start = time.monotonic()
        session.wait_idle(0.2)
        assert time.monotonic() - start >= 0.18

    def test_returns_idle_seconds_since_activity(self, session):
        # No activity during the wait → idle time is at least the poll window.
        idle = session.wait_idle(0.1)
        assert isinstance(idle, float)
        assert idle >= 0.1

    def test_touch_resets_reported_idle(self, session):
        time.sleep(0.15)
        # A touch partway through the wait pulls last_activity forward, so the
        # reported idle ends up smaller than the full poll window.
        threading.Timer(0.05, session.touch).start()
        idle = session.wait_idle(0.2)
        assert idle < 0.2

    def test_does_not_bump_version(self, session):
        v = session.version
        session.wait_idle(0.05)
        assert session.version == v


# ---------------------------------------------------------------------------
# waiters
# ---------------------------------------------------------------------------


class TestWaiters:
    def test_wait_for_version_returns_immediately_when_newer(self, session):
        session.apply_edit("Hello", "Hi")  # version -> 1
        assert session.wait_for_version(0, timeout=1.0) == 1

    def test_wait_for_version_times_out(self, session):
        start = time.monotonic()
        v = session.wait_for_version(session.version, timeout=0.3)
        assert time.monotonic() - start >= 0.25
        assert v == session.version

    def test_wait_for_version_wakes_on_edit(self, session):
        start_v = session.version

        def apply_later():
            time.sleep(0.1)
            session.apply_edit("Hello", "Hi")

        threading.Thread(target=apply_later).start()
        v = session.wait_for_version(start_v, timeout=2.0)
        assert v > start_v

    def test_wait_for_version_wakes_on_submit(self, session):
        def submit_later():
            time.sleep(0.1)
            session.submit()

        threading.Thread(target=submit_later).start()
        # Even though version didn't change, submit should wake the waiter.
        v = session.wait_for_version(session.version, timeout=2.0)
        assert session.submitted is True
        assert v == session.version

    def test_wait_for_submit_returns_false_on_timeout(self, session):
        assert session.wait_for_submit(timeout=0.2) is False

    def test_wait_for_submit_returns_true_after_submit(self, session):
        threading.Timer(0.1, session.submit).start()
        assert session.wait_for_submit(timeout=2.0) is True


# ---------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------


class TestSnapshots:
    def test_snapshot_shape(self, session):
        session.apply_edit("Hello", "Hi")
        snap = session.snapshot()
        assert snap["name"] == "doc.md"
        assert snap["version"] == 1
        assert snap["submitted"] is False
        assert snap["current_round"] == 1
        assert snap["original_text"] == "# Title\n\nHello world.\n"
        assert snap["current_text"] == "# Title\n\nHi world.\n"
        assert len(snap["edits"]) == 1
        assert snap["edits"][0]["old"] == "Hello"
        assert snap["comments"] == []

    def test_comments_payload_shape(self, session):
        session.apply_edit("Hello", "Hi")
        session.add_comment("note", quote="Hi")
        payload = session.comments_payload()
        assert payload["path"] == str(session.path)
        assert payload["submitted"] is False
        assert payload["current_round"] == 1
        assert payload["edit_count"] == 1
        assert len(payload["comments"]) == 1
        assert payload["comments"][0]["body"] == "note"

    def test_snapshot_is_a_copy(self, session):
        snap = session.snapshot()
        snap["edits"].append("mutated")
        # Mutating the snapshot must not touch live state.
        assert len(session.edits) == 0


# ---------------------------------------------------------------------------
# diff / file helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_unified_diff_marks_additions_and_deletions(self):
        diff = _unified_diff("a\nb\n", "a\nc\n", "doc.md")
        assert "-b" in diff
        assert "+c" in diff
        assert "a/doc.md" in diff
        assert "b/doc.md" in diff

    def test_unified_diff_identical_is_empty(self):
        assert _unified_diff("x\n", "x\n", "d") == ""

    def test_read_missing_file_returns_empty(self, tmp_path):
        assert _read(tmp_path / "nope.md") == ""

    def test_read_write_roundtrip(self, tmp_path):
        f = tmp_path / "f.md"
        _write(f, "hello\n")
        assert _read(f) == "hello\n"


# ---------------------------------------------------------------------------
# restore()
# ---------------------------------------------------------------------------


class TestRestore:
    def test_restore_round_trip(self, tmp_path):
        """snapshot → restore → re-snapshot produces identical output."""
        doc = tmp_path / "doc.md"
        doc.write_text("# Original\n\nHello world.\n")
        s1 = Session(doc)
        s1.apply_edit("Hello", "Goodbye")
        s1.add_comment("nice edit", quote="Goodbye")
        s1.submit()
        s1.reset_submitted()  # arms round 2
        snap1 = s1.snapshot()

        # Fresh session, restore from snapshot.
        s2 = Session(doc)
        s2.restore(snap1)
        snap2 = s2.snapshot()

        assert snap2["edits"] == snap1["edits"]
        assert snap2["comments"] == snap1["comments"]
        assert snap2["current_text"] == snap1["current_text"]
        assert snap2["original_text"] == snap1["original_text"]
        assert snap2["version"] == snap1["version"]
        assert snap2["current_round"] == snap1["current_round"]
        assert snap2["submitted"] == snap1["submitted"]

    def test_restore_reconstructs_comment_seq(self, tmp_path):
        """After restore, _comment_seq continues past restored ids."""
        doc = tmp_path / "doc.md"
        doc.write_text("hello\n")
        s1 = Session(doc)
        s1.add_comment("c1")
        s1.add_comment("c2")
        snap = s1.snapshot()

        s2 = Session(doc)
        s2.restore(snap)
        c3 = s2.add_comment("c3")
        assert c3.id == 3  # continues past restored ids 1 and 2

    def test_restore_clears_new_round_pending(self, tmp_path):
        """A restored session starts without _new_round_pending."""
        doc = tmp_path / "doc.md"
        doc.write_text("hello\n")
        s1 = Session(doc)
        s1.reset_submitted()  # arms next round
        snap = s1.snapshot()

        s2 = Session(doc)
        s2.restore(snap)
        s2.apply_edit("hello", "hi")
        assert s2.current_round == 1  # did NOT advance to round 2

    def test_restore_empty_snapshot(self, tmp_path):
        """Restoring from a minimal snapshot does not crash."""
        doc = tmp_path / "doc.md"
        doc.write_text("hello\n")
        s = Session(doc)
        s.restore({"current_text": "restored\n", "original_text": "orig\n"})
        assert s.current_text == "restored\n"
        assert s.original_text == "orig\n"
        assert s.edits == []
        assert s.comments == []


# ---------------------------------------------------------------------------
# reconcile_disk()
# ---------------------------------------------------------------------------


class TestReconcileDisk:
    def test_reseeds_text_from_disk(self, session):
        session.apply_edit("Hello", "Hi")  # an edit now in history
        session.reconcile_disk("# Completely different\n")
        assert session.current_text == "# Completely different\n"
        assert session.original_text == "# Completely different\n"

    def test_clears_stale_diff_history(self, session):
        session.apply_edit("Hello", "Hi")
        assert session.edits
        session.reconcile_disk("# Something else entirely\n")
        assert session.edits == []

    def test_flags_comments_whose_quote_is_gone(self, session):
        c = session.add_comment("note", quote="Hello")
        assert c.stale is False
        session.reconcile_disk("# No match here\n")
        assert session.comments[0].stale is True

    def test_keeps_matching_comments_fresh(self, session):
        session.add_comment("note", quote="Hello")
        session.reconcile_disk("# Hello again\n")
        assert session.comments[0].stale is False

    def test_keeps_comments_themselves(self, session):
        session.add_comment("feedback", quote="Hello")
        session.reconcile_disk("# rewritten\n")
        # The comment body survives even when its quote no longer matches.
        assert session.comments[0].body == "feedback"

    def test_bumps_version_and_notifies(self, session):
        calls = []
        session.on_change = lambda: calls.append(1)
        v0 = session.version
        session.reconcile_disk("new\n")
        assert session.version == v0 + 1
        assert calls == [1]


# ---------------------------------------------------------------------------
# on_change callback
# ---------------------------------------------------------------------------


class TestOnChange:
    def test_on_change_fires_on_mutations(self, session):
        calls = []
        session.on_change = lambda: calls.append(1)

        session.apply_edit("Hello", "Hi")
        session.add_comment("note")
        session.delete_comment(1)
        session.submit()
        session.reset_submitted()
        session.import_comments([{"body": "imported"}])
        session.clear_diffs()

        assert len(calls) == 6

    def test_on_change_does_not_fire_on_touch(self, session):
        calls = []
        session.on_change = lambda: calls.append(1)
        session.touch()
        assert calls == []

    def test_on_change_does_not_fire_on_snapshots(self, session):
        calls = []
        session.on_change = lambda: calls.append(1)
        session.snapshot()
        session.comments_payload()
        assert calls == []

    def test_on_change_default_is_none(self, session):
        # Without setting on_change, mutations should not crash.
        session.apply_edit("Hello", "Hi")
        session.add_comment("note")
