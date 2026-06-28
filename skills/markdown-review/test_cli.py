"""Fast unit tests for the mdedit CLI command functions (mdedit.py).

These cover the input-validation and no-daemon branches of each ``cmd_*``
function — the paths that return before spawning/talking to the daemon — so
they run in milliseconds without forking or network. The end-to-end daemon
paths are exercised by the browser/subprocess suites.

The skill's modules use flat absolute imports (it is a standalone script), so
the skill directory is put on ``sys.path`` like the other test files.
"""

import json
import sys
from pathlib import Path

import pytest

SKILL_DIR = "skills/markdown-review"
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

import mdedit  # noqa: E402
import server  # noqa: E402


def _args(argv):
    return mdedit.build_parser().parse_args(argv)


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Redirect both mdedit.STATE_DIR and server.STATE_DIR to a tmp dir.

    ``mdedit`` binds ``STATE_DIR`` by value at import (``from server import
    STATE_DIR``), and the server helpers reference the server-module global, so
    both must be patched to keep tests off the user's real ``~/.cache/mdedit``.
    """
    state = tmp_path / "state"
    monkeypatch.setattr(mdedit, "STATE_DIR", state)
    monkeypatch.setattr(server, "STATE_DIR", state)
    return state


# ---------------------------------------------------------------------------
# cmd_edit — validation (no daemon reached)
# ---------------------------------------------------------------------------


class TestEditValidation:
    def test_missing_file_returns_error(self, tmp_path, capsys):
        rc = mdedit.cmd_edit(
            _args(["edit", str(tmp_path / "nope.md"), "--old", "a", "--new", "b"])
        )
        assert rc == 1
        assert "file not found" in capsys.readouterr().err

    def test_missing_old_and_new_returns_error(self, tmp_path, capsys):
        doc = tmp_path / "d.md"
        doc.write_text("hello\n")
        rc = mdedit.cmd_edit(_args(["edit", str(doc)]))
        assert rc == 1
        assert "provide --old and --new" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_resume --list
# ---------------------------------------------------------------------------


class TestResumeList:
    def test_empty_state_lists_nothing(self, isolated_state, capsys):
        rc = mdedit.cmd_resume(_args(["resume", "--list"]))
        assert rc == 0
        assert json.loads(capsys.readouterr().out) == {"sessions": []}

    def test_lists_saved_session_fields(self, isolated_state, capsys):
        sd = isolated_state / "somekey"
        sd.mkdir(parents=True)
        (sd / "session.json").write_text(
            json.dumps(
                {
                    "path": "/abs/doc.md",
                    "name": "doc.md",
                    "current_round": 2,
                    "comments": [{}, {}],
                    "edits": [{}, {}, {}],
                    "version": 5,
                }
            )
        )
        rc = mdedit.cmd_resume(_args(["resume", "--list"]))
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data["sessions"]) == 1
        row = data["sessions"][0]
        assert row["path"] == "/abs/doc.md"
        assert row["round"] == 2
        assert row["comments"] == 2
        assert row["edits"] == 3
        assert row["version"] == 5
        assert row["exists_on_disk"] is False

    def test_missing_file_returns_error(self, isolated_state, capsys):
        rc = mdedit.cmd_resume(_args(["resume", str(Path("/does/not/exist.md"))]))
        assert rc == 1
        assert "file not found" in capsys.readouterr().err

    def test_no_saved_session_returns_error(self, isolated_state, tmp_path, capsys):
        doc = tmp_path / "d.md"
        doc.write_text("hi\n")
        rc = mdedit.cmd_resume(_args(["resume", str(doc)]))
        assert rc == 1
        assert "no saved session" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_share — no-daemon disk fallback
# ---------------------------------------------------------------------------


class TestShareNoDaemon:
    def test_reads_disk_and_emits_html(self, isolated_state, tmp_path, capsys):
        doc = tmp_path / "d.md"
        doc.write_text("# Hello world\n")
        rc = mdedit.cmd_share(_args(["share", str(doc)]))
        assert rc == 0
        out = capsys.readouterr().out
        assert out.startswith("<!DOCTYPE html>")
        assert "Hello world" in out

    def test_missing_file_returns_error(self, isolated_state, tmp_path, capsys):
        rc = mdedit.cmd_share(_args(["share", str(tmp_path / "nope.md")]))
        assert rc == 1
        assert "file not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_status / cmd_stop / cmd_import_comments — no running session
# ---------------------------------------------------------------------------


class TestNoSessionCommands:
    def test_status_reports_not_running(self, isolated_state, tmp_path, capsys):
        doc = tmp_path / "d.md"
        doc.write_text("hi\n")
        rc = mdedit.cmd_status(_args(["status", str(doc)]))
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["running"] is False

    def test_stop_with_no_session_reports_note(self, isolated_state, tmp_path, capsys):
        doc = tmp_path / "d.md"
        doc.write_text("hi\n")
        rc = mdedit.cmd_stop(_args(["stop", str(doc)]))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert out["note"] == "no running session"

    def test_import_comments_needs_running_session(
        self, isolated_state, tmp_path, capsys
    ):
        doc = tmp_path / "d.md"
        doc.write_text("hi\n")
        rc = mdedit.cmd_import_comments(
            _args(["import-comments", str(doc), "--from", "-"])
        )
        assert rc == 1
        assert "no running session" in capsys.readouterr().err
