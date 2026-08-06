# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for the continuous (watch-mode) sync use case.

Drives the real client as a subprocess in watch mode against the real
in-process server. Verifies observable filesystem convergence — no internals
are imported or mocked.
"""

import subprocess
import time
from pathlib import Path


def _wait_for(predicate, timeout=20.0, interval=0.3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _wait_for_subprocess_ready(proc: subprocess.Popen, timeout=10.0) -> bool:
    """Wait for the watch-mode client to connect to the server.

    Since stdout is discarded (to avoid pipe-buffer deadlocks), readiness is
    a bounded wait that only checks the process is still alive. The initial
    sync of an empty bucket + WS connection typically completes in 1-2s.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False  # process exited unexpectedly
        time.sleep(0.5)
    return proc.poll() is None


def _terminate(proc: subprocess.Popen) -> None:
    """Terminate and reap a watch subprocess, killing if needed."""
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


class TestContinuousSync:
    def test_local_change_uploaded_to_server(
        self, live_server, bb_bucket, local_dir, watch_sync, bb_server_files
    ):
        # Given a client running in watch mode against an empty bucket
        proc = watch_sync(bb_bucket, local_dir)
        try:
            assert _wait_for_subprocess_ready(proc), "watch mode did not become ready"

            # When a new file is created locally
            content = b"watched change\n"
            (Path(local_dir) / "watched.txt").write_bytes(content)

            # Then the file is uploaded to the server within a few seconds
            assert _wait_for(lambda: "watched.txt" in bb_server_files(bb_bucket))
        finally:
            _terminate(proc)

    def test_remote_file_downloaded_to_local(
        self, live_server, bb_bucket, local_dir, watch_sync, bb_server_upload
    ):
        # Given a client running in watch mode against an empty bucket
        proc = watch_sync(bb_bucket, local_dir)
        try:
            assert _wait_for_subprocess_ready(proc), "watch mode did not become ready"

            # When a file is created on the server (broadcasts an "added" event)
            content = b"from server via ws\n"
            bb_server_upload(bb_bucket, "pushed.txt", content)

            # Then the file is downloaded to the local dir via the WS event
            assert _wait_for(lambda: (Path(local_dir) / "pushed.txt").exists())
            assert (Path(local_dir) / "pushed.txt").read_bytes() == content
        finally:
            _terminate(proc)
