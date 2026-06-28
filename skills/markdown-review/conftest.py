"""Shared harness for mdedit's daemon + browser tests.

Centralizes the daemon spawn/stop/state helpers and the standalone-share-page UI
helpers that were previously copy-pasted across ``test_idle_shutdown.py``,
``test_comment_highlights.py``, ``test_comment_edit_reply.py`` and
``test_share.py``. Test modules import the names they need::

    from conftest import spawn_daemon, stop_daemon, make_env

These are plain functions (not fixtures) so existing call sites need only swap
their local definitions for the import — no per-test signature changes, identical
runtime behaviour. ``spawn_daemon`` mirrors the previous ``open``-via-subprocess
flow; callers stop the daemon themselves with ``stop_daemon`` (as before).
"""

from __future__ import annotations

import http.client
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent
MDEDIT = SKILL_DIR / "mdedit.py"

_STATE_FILE_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Daemon env + state-file helpers
# ---------------------------------------------------------------------------


def make_env(state_dir: Path, idle_timeout: str = "0") -> dict[str, str]:
    """Build the environment for a daemon subprocess (isolated state dir)."""
    env = dict(os.environ)
    env["MDEDIT_STATE_DIR"] = str(state_dir)
    env["MDEDIT_IDLE_TIMEOUT"] = idle_timeout
    return env


def state_file_path(state_dir: Path) -> Path | None:
    """Return the daemon's state file (current or legacy flat), or None."""
    files = list(state_dir.glob("*/daemon.json"))
    if not files:
        files = list(state_dir.glob("*.json"))
    return files[0] if files else None


def read_state(state_dir: Path) -> dict | None:
    """Read+parse the daemon state file, or None if absent/unreadable."""
    sf = state_file_path(state_dir)
    if sf is None:
        return None
    try:
        return json.loads(sf.read_text())
    except (ValueError, OSError):
        return None


# ---------------------------------------------------------------------------
# Daemon lifecycle
# ---------------------------------------------------------------------------


def spawn_daemon(
    state_dir: Path, doc: Path, idle_timeout: str = "0", timeout: float = 15
) -> dict:
    """Open ``doc`` via the CLI (no browser) and wait for the daemon state file.

    Returns the parsed state dict (``{port, pid, …}``). Mirrors the previous
    per-module ``_spawn`` / ``_spawn_daemon`` helpers.
    """
    env = make_env(state_dir, idle_timeout)
    subprocess.run(
        [sys.executable, str(MDEDIT), "--no-browser", "open", str(doc)],
        check=True,
        capture_output=True,
        env=env,
        timeout=timeout,
    )
    deadline = time.time() + _STATE_FILE_TIMEOUT
    while time.time() < deadline:
        info = read_state(state_dir)
        if info:
            return info
        time.sleep(0.1)
    raise RuntimeError("daemon state file never appeared")


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def wait_for_exit(pid: int, timeout: float = 10.0) -> bool:
    """Return True if the process exits within ``timeout`` seconds."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.2)
    return False


def stop_daemon(pid: int) -> None:
    """SIGTERM the daemon and wait for it to exit (no-op if already dead)."""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + 5
    while time.time() < deadline:
        if not process_alive(pid):
            return
        time.sleep(0.1)


# ---------------------------------------------------------------------------
# Daemon HTTP helpers
# ---------------------------------------------------------------------------


def http_get(port: int, path: str, timeout: float = 3.0) -> dict:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request("GET", path)
    resp = conn.getresponse()
    data = json.loads(resp.read().decode())
    conn.close()
    return data


def post_comment(port: int, timeout: float = 3.0, **fields: Any) -> dict:
    body = json.dumps(fields)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request(
        "POST",
        "/api/comment",
        body=body,
        headers={"Content-Type": "application/json"},
    )
    resp = conn.getresponse()
    data = json.loads(resp.read().decode())
    conn.close()
    return data


def edit_cli(doc: Path, state_dir: Path, old: str, new: str) -> None:
    """Apply a search/replace edit via the CLI so the diff view gets content."""
    env = make_env(state_dir)
    subprocess.run(
        [sys.executable, str(MDEDIT), "edit", str(doc), "--old", old, "--new", new],
        check=True,
        capture_output=True,
        env=env,
        timeout=15,
    )


# ---------------------------------------------------------------------------
# Standalone share-page helpers (playwright Page typed as Any so importing this
# module never requires playwright; the browser tests importorskip it themselves)
# ---------------------------------------------------------------------------


def make_snapshot(
    name: str = "share-doc.md",
    text: str = "# Shared Plan\n\nShip feature X in two weeks.\n",
) -> dict:
    return {
        "name": name,
        "version": 1,
        "submitted": False,
        "current_round": 1,
        "original_text": text,
        "current_text": text,
        "edits": [],
        "comments": [],
    }


def write_share_html(tmp_path: Path, snapshot: dict | None = None) -> Path:
    """Render a standalone share HTML file under ``tmp_path`` for browser tests."""
    if str(SKILL_DIR) not in sys.path:
        sys.path.insert(0, str(SKILL_DIR))
    import frontend  # noqa: PLC0415 — lazy so daemon-only sessions don't need it

    html = frontend.build_share_html(snapshot or make_snapshot())
    out = tmp_path / "share.share.html"
    out.write_text(html, encoding="utf-8")
    return out


def set_author(page: Any, name: str = "Alice") -> None:
    """Dismiss the share-page author prompt with the given name."""
    page.fill("#author-input", name)
    page.click("#author-save")
    page.wait_for_selector("#author-prompt.hidden", state="hidden")


def add_general_comment(page: Any, body: str = "original note") -> None:
    """Add a general comment via the sidebar box and wait for its card."""
    page.fill("#general-input", body)
    page.click("#general-add")
    page.wait_for_selector(".comment-card")
