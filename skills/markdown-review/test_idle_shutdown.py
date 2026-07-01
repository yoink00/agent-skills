"""Tests for idle auto-shutdown and signal handling of the mdedit daemon.

These are integration tests: they spawn the real daemon as a subprocess,
interact with it over HTTP, and assert that it exits when expected.
"""

import os
import signal
import subprocess
import sys
import time

from conftest import (  # noqa: E402
    MDEDIT,
    http_get,
    make_env,
    process_alive,
    spawn_daemon,
    state_file_path,
    wait_for_exit,
)

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIdleShutdown:
    """The daemon shuts itself down after MDEDIT_IDLE_TIMEOUT seconds of
    inactivity."""

    def test_idle_shutdown_fires(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        doc = tmp_path / "idle.md"
        doc.write_text("# Idle\n")

        info = spawn_daemon(state_dir, doc, idle_timeout="2")
        pid = info["pid"]

        # Do nothing; daemon should exit within a few seconds.
        assert wait_for_exit(pid, timeout=10), (
            f"daemon pid {pid} did not exit after idle timeout"
        )

        # State file should be cleaned up.
        time.sleep(0.5)
        assert state_file_path(state_dir) is None, "state file not removed after exit"

    def test_keepalive_extends_life(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        doc = tmp_path / "keep.md"
        doc.write_text("# Keep\n")

        info = spawn_daemon(state_dir, doc, idle_timeout="2")
        pid = info["pid"]
        port = info["port"]

        # Poll /api/comments every ~1s to keep the daemon alive well past the
        # 2s timeout. (We use /api/comments rather than /api/poll because the
        # latter long-polls for up to 25s, which is incompatible with a 2s idle
        # timeout. In production the default is 300s so the browser's 25s
        # poll cycle keeps it alive.)
        deadline = time.time() + 8  # 4x the idle timeout
        while time.time() < deadline:
            try:
                http_get(port, "/api/comments")
            except OSError:
                break
            time.sleep(1.0)

        # Daemon should still be alive.
        assert process_alive(pid), (
            "daemon died despite active polling (keepalive not working)"
        )

        # Clean up.
        try:
            os.kill(pid, signal.SIGTERM)
            wait_for_exit(pid, timeout=5)
        except OSError:
            pass

    def test_disabled_timeout_zero(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        doc = tmp_path / "disabled.md"
        doc.write_text("# Disabled\n")

        info = spawn_daemon(state_dir, doc, idle_timeout="0")
        pid = info["pid"]

        # Wait longer than the default timeout; daemon should still be alive.
        time.sleep(5)
        assert process_alive(pid), (
            "daemon died despite idle-timeout=0 (should be disabled)"
        )

        # Clean up.
        try:
            os.kill(pid, signal.SIGTERM)
            wait_for_exit(pid, timeout=5)
        except OSError:
            pass


class TestSignalHandling:
    """SIGTERM / SIGHUP cause a clean shutdown (state file removed)."""

    def test_sigterm_cleans_up(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        doc = tmp_path / "sigterm.md"
        doc.write_text("# SIGTERM\n")

        info = spawn_daemon(state_dir, doc, idle_timeout="0")
        pid = info["pid"]

        # Daemon should be alive.
        assert process_alive(pid)

        # Send SIGTERM.
        os.kill(pid, signal.SIGTERM)

        # Should exit promptly.
        assert wait_for_exit(pid, timeout=5), (
            f"daemon pid {pid} did not exit after SIGTERM"
        )

        # State file should be cleaned up.
        time.sleep(0.5)
        assert state_file_path(state_dir) is None, (
            "state file not removed after SIGTERM"
        )

    def test_sighup_cleans_up(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        doc = tmp_path / "sighup.md"
        doc.write_text("# SIGHUP\n")

        info = spawn_daemon(state_dir, doc, idle_timeout="0")
        pid = info["pid"]

        assert process_alive(pid)

        os.kill(pid, signal.SIGHUP)

        assert wait_for_exit(pid, timeout=5), (
            f"daemon pid {pid} did not exit after SIGHUP"
        )

        time.sleep(0.5)
        assert state_file_path(state_dir) is None, "state file not removed after SIGHUP"


class TestReviewCleanFailure:
    """A blocking `review` fails cleanly (no traceback) if the daemon dies
    underneath it."""

    def test_review_fails_on_daemon_death(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        doc = tmp_path / "review.md"
        doc.write_text("# Review\n")

        info = spawn_daemon(state_dir, doc, idle_timeout="0")
        pid = info["pid"]

        # Start a blocking review in a subprocess.
        env = make_env(state_dir, "0")
        proc = subprocess.Popen(
            [
                sys.executable,
                str(MDEDIT),
                "--no-browser",
                "review",
                "--json",
                str(doc),
                "--timeout",
                "0",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        # Give it a moment to enter the poll loop.
        time.sleep(1.0)

        # Kill the daemon.
        os.kill(pid, signal.SIGKILL)

        # The review process should exit with a non-zero code and a clean
        # error message (not a traceback).
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode != 0, "review should have failed"
        err = stderr.decode()
        assert "Traceback" not in err, (
            f"review produced a traceback instead of clean error:\n{err}"
        )
        assert (
            "session ended" in err.lower() or "session ended" in stdout.decode().lower()
        ), (
            f"expected 'session ended' error, got stderr={err!r} stdout={stdout.decode()!r}"
        )
