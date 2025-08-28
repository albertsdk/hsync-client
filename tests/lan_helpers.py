# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Shared helpers, fixtures and constants for LAN black-box tests.

These are imported by the individual test_bb_lan_*.py modules.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_LAN_ENABLED = os.environ.get("HSYNC_BB_LAN") == "1"
_LAN_REASON = (
    "LAN e2e needs loopback mDNS + two live client instances; set HSYNC_BB_LAN=1 to enable"
)

BB_API_KEY = "test-bb-api-key-0123456789abcdef"
_LAN_KEY = "test-lan-shared-secret"
_HSYNC_COMMON_DIR = str(Path(__file__).resolve().parent.parent.parent / "hsync-common")


def _free_port() -> int:
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _lan_start(
    local_path: str,
    bucket: str,
    *,
    force_master: bool = False,
    never_master: bool = False,
) -> subprocess.Popen:
    """Start a LAN-mode client subprocess."""
    env = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY, "PYTHONPATH": _HSYNC_COMMON_DIR}
    args = [
        sys.executable,
        "-m",
        "hsync_client",
        "--lan-mode",
        "-b",
        bucket,
        "-p",
        local_path,
        "--yes",
        "--min-free-disk-space",
        "0",
        "--no-autotune",
        "--discovery-timeout",
        "10",
        "--retry-delay",
        "1",
        "--stability-delay",
        "1",
        "--timeout",
        "10",
    ]
    if force_master:
        args.append("--force-master")
    if never_master:
        args.append("--never-master")
    return subprocess.Popen(
        args,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _terminate(proc: subprocess.Popen) -> None:
    """Terminate and reap a subprocess."""
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    # Drain pipes to avoid ResourceWarning
    if proc.stdout:
        proc.stdout.close()
    # stderr is merged into stdout (PIPE → STDOUT), so no separate pipe to drain.


def _stderr_text(proc: subprocess.Popen) -> str:
    """Return captured output text (non-blocking, best-effort).

    Reads from stdout since stderr is merged into it (loguru logs to stdout).
    """
    if proc.stdout is None:
        return ""
    try:
        import os as _os

        fd = proc.stdout.fileno()
        _os.set_blocking(fd, False)
        data = proc.stdout.read()
        return data.decode("utf-8", errors="replace") if data else ""
    except (OSError, ValueError, AttributeError):
        return ""


def _wait_for_file(path: str, timeout: float = 30.0) -> bool:
    """Poll until *path* exists, or return False on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if Path(path).exists():
            return True
        time.sleep(0.3)
    return False


def _wait_for_text_in_stderr(proc: subprocess.Popen, text: str, timeout: float = 30.0) -> bool:
    """Poll until *text* appears in the process's stderr, or return False."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False  # process exited
        if text in _stderr_text(proc):
            return True
        time.sleep(0.3)
    return False


def _wait_for_file_gone(path: str, timeout: float = 30.0) -> bool:
    """Poll until *path* no longer exists, or return False on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not Path(path).exists():
            return True
        time.sleep(0.3)
    return False


def _wait_for_master(
    master: subprocess.Popen, slave_dir: str, bucket: str, timeout: float = 30
) -> None:
    """Start a temporary slave so the master discovers it and runs the election.

    The master only emits ``"MASTER"`` after ``add_peer`` triggers
    ``_elect_master``, so we must have at least one slave running before
    waiting for the log line.

    Returns once the master logs ``"MASTER"``, or raises ``AssertionError``.
    The temporary slave is terminated before returning.
    """
    slave = _lan_start(slave_dir, bucket, never_master=True)
    try:
        assert _wait_for_text_in_stderr(master, "MASTER", timeout=timeout), (
            "Master did not become ready"
        )
    finally:
        _terminate(slave)


@pytest.fixture
def lan_master_dir(tmp_path):
    """Temp directory for the LAN master."""
    d = tmp_path / "master"
    d.mkdir()
    return str(d)


@pytest.fixture
def lan_slave_dir(tmp_path):
    """Temp directory for the LAN slave."""
    d = tmp_path / "slave"
    d.mkdir()
    return str(d)


@pytest.fixture
def lan_bucket():
    """Unique bucket name for a LAN test."""
    import uuid

    return f"lan-bb-{uuid.uuid4().hex[:10]}"
