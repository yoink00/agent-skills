"""Tests for idle auto-shutdown and signal handling of the mdedit daemon.

These are integration tests: they spawn the real daemon as a subprocess,
interact with it over HTTP, and assert that it exits when expected.
"""

import http.client
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

MDEDIT = Path(__file__).resolve().parent / "mdedit.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env(state_dir: Path, idle_timeout: str) -> dict[str, str]:
    """Build an environment for a daemon subprocess."""
    env = dict(os.environ)
    env["MDEDIT_STATE_DIR"] = str(state_dir)
    env["MDEDIT_IDLE_TIMEOUT"] = idle_timeout
    return env


def _state_file(state_dir: Path) -> Path | None:
    files = list(state_dir.glob("*.json"))
    return files[0] if files else None


def _read_state(state_dir: Path) -> dict | None:
    sf = _state_file(state_dir)
    if sf is None:
        return None
    try:
        return json.loads(sf.read_text())
    except (ValueError, OSError):
        return None


def _spawn(state_dir: Path, doc: Path, idle_timeout: str = "2") -> dict:
    """Open a doc; return the state dict {port, pid, ...}."""
    env = _env(state_dir, idle_timeout)
    subprocess.run(
        [sys.executable, str(MDEDIT), "--no-browser", "open", str(doc)],
        check=True,
        capture_output=True,
        env=env,
        timeout=15,
    )
    # Wait for state file to appear.
    for _ in range(50):
        info = _read_state(state_dir)
        if info:
            return info
        time.sleep(0.1)
    raise RuntimeError("daemon state file never appeared")


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _wait_exit(pid: int, timeout: float = 10.0) -> bool:
    """Return True if the process exits within `timeout` seconds."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.2)
    return False


def _http_get(port: int, path: str) -> dict:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3.0)
    conn.request("GET", path)
    resp = conn.getresponse()
    data = json.loads(resp.read().decode())
    conn.close()
    return data


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

        info = _spawn(state_dir, doc, idle_timeout="2")
        pid = info["pid"]

        # Do nothing; daemon should exit within a few seconds.
        assert _wait_exit(pid, timeout=10), (
            f"daemon pid {pid} did not exit after idle timeout"
        )

        # State file should be cleaned up.
        time.sleep(0.5)
        assert _state_file(state_dir) is None, "state file not removed after exit"

    def test_keepalive_extends_life(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        doc = tmp_path / "keep.md"
        doc.write_text("# Keep\n")

        info = _spawn(state_dir, doc, idle_timeout="2")
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
                _http_get(port, "/api/comments")
            except OSError:
                break
            time.sleep(1.0)

        # Daemon should still be alive.
        assert _process_alive(pid), (
            "daemon died despite active polling (keepalive not working)"
        )

        # Clean up.
        try:
            os.kill(pid, signal.SIGTERM)
            _wait_exit(pid, timeout=5)
        except OSError:
            pass

    def test_disabled_timeout_zero(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        doc = tmp_path / "disabled.md"
        doc.write_text("# Disabled\n")

        info = _spawn(state_dir, doc, idle_timeout="0")
        pid = info["pid"]

        # Wait longer than the default timeout; daemon should still be alive.
        time.sleep(5)
        assert _process_alive(pid), (
            "daemon died despite idle-timeout=0 (should be disabled)"
        )

        # Clean up.
        try:
            os.kill(pid, signal.SIGTERM)
            _wait_exit(pid, timeout=5)
        except OSError:
            pass


class TestSignalHandling:
    """SIGTERM / SIGHUP cause a clean shutdown (state file removed)."""

    def test_sigterm_cleans_up(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        doc = tmp_path / "sigterm.md"
        doc.write_text("# SIGTERM\n")

        info = _spawn(state_dir, doc, idle_timeout="0")
        pid = info["pid"]

        # Daemon should be alive.
        assert _process_alive(pid)

        # Send SIGTERM.
        os.kill(pid, signal.SIGTERM)

        # Should exit promptly.
        assert _wait_exit(pid, timeout=5), (
            f"daemon pid {pid} did not exit after SIGTERM"
        )

        # State file should be cleaned up.
        time.sleep(0.5)
        assert _state_file(state_dir) is None, "state file not removed after SIGTERM"

    def test_sighup_cleans_up(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        doc = tmp_path / "sighup.md"
        doc.write_text("# SIGHUP\n")

        info = _spawn(state_dir, doc, idle_timeout="0")
        pid = info["pid"]

        assert _process_alive(pid)

        os.kill(pid, signal.SIGHUP)

        assert _wait_exit(pid, timeout=5), f"daemon pid {pid} did not exit after SIGHUP"

        time.sleep(0.5)
        assert _state_file(state_dir) is None, "state file not removed after SIGHUP"


class TestReviewCleanFailure:
    """A blocking `review` fails cleanly (no traceback) if the daemon dies
    underneath it."""

    def test_review_fails_on_daemon_death(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        doc = tmp_path / "review.md"
        doc.write_text("# Review\n")

        info = _spawn(state_dir, doc, idle_timeout="0")
        pid = info["pid"]

        # Start a blocking review in a subprocess.
        env = _env(state_dir, "0")
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
