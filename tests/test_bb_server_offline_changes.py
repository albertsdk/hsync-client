# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for offline bucket modifications (server mode).

Verifies the two scenarios from the resilience analysis:

* **S1** — the client loses its WebSocket connection to the server (server
  stopped) while the local bucket is modified (create/modify/delete files and
  directories). On reconnect a full sync must reconcile every change.
* **S2** — the ``hsync`` client process is killed (SIGKILL) while the local
  bucket is modified. On restart a full sync must reconcile every change,
  **including deletions** — the critical case: a file uploaded by the live
  watcher and then deleted while the process was dead must not be resurrected
  from the server.

Drives the real client/server through their public surfaces only (REST + the
documented continuous-mode CLI, the default). No internal module is imported.

Requires ``HSYNC_BB_SERVER=1`` to run.
"""

import os
import subprocess
from pathlib import Path

import pytest

from tests.conftest import (
    BB_CLIENT_CID,
    _ready_path,
    _wait_for,
    _wait_for_watcher_pipeline,
)
from tests.p2p_helpers import (
    _terminate,
    _wait_for_client_ready,
    _wait_for_file_gone,
)

_BB_SERVER_ENABLED = os.environ.get("HSYNC_BB_SERVER") == "1"
_BB_SERVER_REASON = "Black-box server-mode tests require HSYNC_BB_SERVER=1"

pytestmark = pytest.mark.skipif(not _BB_SERVER_ENABLED, reason=_BB_SERVER_REASON)


def _client_log_path(local_dir: str) -> str:
    return str(Path(local_dir).parent / "client.log")


def _full_log(local_dir: str, limit: int = 60000) -> str:
    """Return the full client log (large limit) for failure reports."""
    try:
        with open(_client_log_path(local_dir), "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - limit))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return "<client log unavailable>"


def _wait_for_log_text(local_dir: str, text: str, timeout: float = 30.0) -> bool:
    """Poll the client log until *text* appears.

    The ready-file only signals that the initial full sync completed; the
    filesystem watcher becomes active a moment later (inside the remote/lan
    event loop). The ``[READY]`` log marker is the observable readiness
    threshold for local writes: "watcher active + event channel up". Waiting
    for it here eliminates the race where a test writes a local file while
    the inotify watch is not yet registered (the event is then lost and the
    file never reaches the server).
    """
    return _wait_for(lambda: text in _full_log(local_dir), timeout=timeout)


def _kill(proc: subprocess.Popen) -> None:
    """SIGKILL the client subprocess and reap it (simulates a hard crash)."""
    proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    if proc.stdout:
        proc.stdout.close()


# =============================================================================
# S1 — client disconnected (process alive), bucket modified locally
# =============================================================================


class TestOfflineChangesServerDisconnect:
    def test_local_changes_during_server_outage_reconcile(
        self,
        restartable_live_server,
        bb_restartable_bucket,
        local_dir,
        watch_sync_restartable,
        bb_restartable_server_files,
    ):
        """While the server is down (WS disconnected, watcher stopped) the user
        creates, modifies and deletes files and a directory locally. After the
        server restarts the client reconnects and a full sync reconciles every
        change: the new file appears, the modified file's new content is on the
        server, the deleted file is gone, and the new directory's file appears.
        """
        # ``--interval 20`` keeps the periodic full resync as a safety net: the
        # reconnect sync can race with the local modifications (its
        # ``get_files_local`` may snapshot the pre-modify tree while
        # ``get_files_remote`` retries bridge the server restart), so a created
        # file may only be uploaded by the next periodic cycle. The
        # offline-deletion minting is race-free (it checks the live
        # ``existing_keys`` snapshot, not the stale listing). ``--log-file``
        # redirects loguru to a file for failure reports.
        log_path = _client_log_path(local_dir)
        proc = watch_sync_restartable(
            bb_restartable_bucket, local_dir, "--interval", "20", "--log-file", log_path
        )
        try:
            assert _wait_for_client_ready(proc, _ready_path(local_dir)), (
                f"watch mode did not become ready\n--- client log tail ---\n{_full_log(local_dir)}"
            )
            # Watcher readiness (see _wait_for_log_text above): local writes
            # must not race the inotify watch registration.
            assert _wait_for_log_text(local_dir, "[READY]"), (
                f"watcher did not become ready\n--- client log tail ---\n{_full_log(local_dir)}"
            )
            _wait_for_watcher_pipeline(
                bb_restartable_bucket,
                local_dir,
                bb_restartable_server_files,
                log_tail=_full_log(local_dir),
            )

            # Baseline: a synced file the client uploaded (merge base recorded).
            (Path(local_dir) / "keep.txt").write_bytes(b"keep\n")
            (Path(local_dir) / "delete_me.txt").write_bytes(b"delete\n")
            (Path(local_dir) / "modify_me.txt").write_bytes(b"v1\n")
            assert _wait_for(
                lambda: {
                    "keep.txt",
                    "delete_me.txt",
                    "modify_me.txt",
                }.issubset(bb_restartable_server_files(bb_restartable_bucket)),
                timeout=40,
            ), "baseline files did not reach the server"

            # --- outage: stop the server (client WS drops, watcher stops) ---
            restartable_live_server.stop()

            # Local modifications while disconnected (watcher is stopped).
            (Path(local_dir) / "created.txt").write_bytes(b"new\n")
            (Path(local_dir) / "modify_me.txt").write_bytes(b"v2\n")
            (Path(local_dir) / "delete_me.txt").unlink()
            new_dir = Path(local_dir) / "newdir"
            new_dir.mkdir()
            (new_dir / "inside.txt").write_bytes(b"inside\n")

            # --- restore the server; the client reconnects and reconciles ---
            restartable_live_server.restart()

            files = bb_restartable_server_files
            assert _wait_for(lambda: "created.txt" in files(bb_restartable_bucket), timeout=60), (
                "file created during the outage was not uploaded after reconnect\n"
                f"--- client log tail ---\n{_full_log(local_dir)}"
            )
            assert _wait_for(
                lambda: "newdir/inside.txt" in files(bb_restartable_bucket), timeout=60
            ), "directory created during the outage was not uploaded after reconnect"
            assert _wait_for(
                lambda: "delete_me.txt" not in files(bb_restartable_bucket), timeout=60
            ), (
                "file deleted during the outage was resurrected after reconnect\n"
                f"--- client log tail ---\n{_full_log(local_dir)}"
            )
            # The modified file must carry the new content.

            base = restartable_live_server.base_url
            headers = {"Authorization": f"Bearer {restartable_live_server.api_key}"}
            assert _wait_for(
                lambda: (
                    _server_file_bytes(base, bb_restartable_bucket, "modify_me.txt", headers)
                    == b"v2\n"
                ),
                timeout=60,
            ), "file modified during the outage did not converge to v2"
            assert proc.poll() is None, "client crashed during the outage/reconnect"
        finally:
            _terminate(proc)


# =============================================================================
# S2 — client process killed, bucket modified locally
# =============================================================================


class TestOfflineChangesClientKill:
    def test_create_and_modify_during_kill_reconcile(
        self,
        live_server,
        bb_bucket,
        local_dir,
        watch_sync,
        bb_server_files,
    ):
        """After the client is SIGKILLed, files created and modified locally are
        reconciled on restart: a brand-new file is uploaded and a modified file
        converges to its new content."""
        # interval=3600 disables the periodic resync during the test so the
        # only reconcile is the initial full sync on restart (deterministic).
        log_path = _client_log_path(local_dir)
        proc = watch_sync(bb_bucket, local_dir, "--interval", "3600", "--log-file", log_path)
        try:
            assert _wait_for_client_ready(proc, _ready_path(local_dir)), (
                f"watch mode did not become ready\n--- client log tail ---\n{_full_log(local_dir)}"
            )
            assert _wait_for_log_text(local_dir, "[READY]"), (
                f"watcher did not become ready\n--- client log tail ---\n{_full_log(local_dir)}"
            )
            _wait_for_watcher_pipeline(
                bb_bucket, local_dir, bb_server_files, log_tail=_full_log(local_dir)
            )
            (Path(local_dir) / "modify_me.txt").write_bytes(b"v1\n")
            assert _wait_for(lambda: "modify_me.txt" in bb_server_files(bb_bucket), timeout=40), (
                "baseline file did not reach the server"
            )
        finally:
            _kill(proc)

        # --- process dead: modify + create locally ---
        (Path(local_dir) / "modify_me.txt").write_bytes(b"v2-killed\n")
        (Path(local_dir) / "created_while_dead.txt").write_bytes(b"born offline\n")

        # --- restart the client on the same directory ---
        proc2 = watch_sync(bb_bucket, local_dir, "--interval", "3600")
        try:
            assert _wait_for_client_ready(proc2, _ready_path(local_dir)), (
                "restarted client did not become ready\n"
                f"--- client log tail ---\n{_full_log(local_dir)}"
            )
            assert _wait_for(
                lambda: "created_while_dead.txt" in bb_server_files(bb_bucket), timeout=60
            ), (
                "file created while the client was dead was not uploaded on restart\n"
                f"--- client log tail ---\n{_full_log(local_dir)}"
            )
            base, api_key = live_server
            headers = {"Authorization": f"Bearer {api_key}"}
            assert _wait_for(
                lambda: (
                    _server_file_bytes(base, bb_bucket, "modify_me.txt", headers) == b"v2-killed\n"
                ),
                timeout=60,
            ), (
                "file modified while the client was dead did not converge\n"
                f"actual={_server_file_bytes(base, bb_bucket, 'modify_me.txt', headers)!r}\n"
                f"--- client log tail ---\n{_full_log(local_dir)}"
            )
        finally:
            _terminate(proc2)

    @pytest.mark.flaky(reruns=3, reruns_delay=5)
    def test_delete_during_kill_is_not_resurrected(
        self,
        live_server,
        bb_bucket,
        local_dir,
        watch_sync,
        bb_server_files,
    ):
        """Regression for the delete-while-dead resurrection bug (G3).

        A file uploaded by the live watcher has its merge base recorded only in
        memory (persisted at the end of a sync cycle). If the process is killed
        before that flush and the file is then deleted locally, on restart the
        merge base is gone, no offline-deletion tombstone is minted, and the
        server copy is downloaded back — the deletion is silently undone.

        The fix persists the merge base durably on real-time transfers, so the
        offline-deletion minting on restart detects the deletion and removes the
        server copy (``delete_remote``) instead of resurrecting it.
        """
        # interval=3600 guarantees no periodic flush between the watcher upload
        # and the kill, making the resurrection deterministic before the fix.
        # --verbose enables the DEBUG-level durable-persistence marker
        # ("Merge base persisted for ...") that this test waits for before the
        # SIGKILL: the file can appear in the server listing a moment before the
        # client finishes flushing the merge base, and killing in that window
        # loses the base and makes the deletion resurrect on restart.
        log_path = _client_log_path(local_dir)
        kill_me_path = str(Path(local_dir) / "kill_me.txt")
        proc = watch_sync(
            bb_bucket, local_dir, "--interval", "3600", "--log-file", log_path, "--verbose"
        )
        try:
            assert _wait_for_client_ready(proc, _ready_path(local_dir)), (
                f"watch mode did not become ready\n--- client log tail ---\n{_full_log(local_dir)}"
            )
            assert _wait_for_log_text(local_dir, "[READY]"), (
                f"watcher did not become ready\n--- client log tail ---\n{_full_log(local_dir)}"
            )
            _wait_for_watcher_pipeline(
                bb_bucket, local_dir, bb_server_files, log_tail=_full_log(local_dir)
            )
            # File created locally and uploaded by the watcher (merge base only
            # in memory until a sync cycle flushes it).
            (Path(local_dir) / "kill_me.txt").write_bytes(b"kill\n")
            assert _wait_for(lambda: "kill_me.txt" in bb_server_files(bb_bucket), timeout=40), (
                "watcher-uploaded file did not reach the server"
            )
            # Wait for the merge base to be durable before the SIGKILL, so the
            # offline-deletion minting on restart has a base to mint from.
            assert _wait_for_log_text(local_dir, f'Merge base persisted for "{kill_me_path}"'), (
                "merge base was not durably persisted before the kill\n"
                f"--- client log tail ---\n{_full_log(local_dir)}"
            )
        finally:
            _kill(proc)

        # --- process dead: delete the file locally ---
        (Path(local_dir) / "kill_me.txt").unlink()

        # --- restart the client on the same directory ---
        proc2 = watch_sync(bb_bucket, local_dir, "--interval", "3600")
        try:
            assert _wait_for_client_ready(proc2, _ready_path(local_dir)), (
                "restarted client did not become ready\n"
                f"--- client log tail ---\n{_full_log(local_dir)}"
            )
            # The deletion must propagate to the server (no resurrection).
            assert _wait_for(lambda: "kill_me.txt" not in bb_server_files(bb_bucket), timeout=60), (
                "file deleted while the client was dead was resurrected on restart\n"
                f"--- client log tail ---\n{_full_log(local_dir)}"
            )
            assert _wait_for_file_gone(str(Path(local_dir) / "kill_me.txt"), timeout=10)
        finally:
            _terminate(proc2)


# =============================================================================
# shared helpers
# =============================================================================


def _server_file_bytes(base_url: str, bucket: str, filename: str, headers: dict) -> bytes | None:
    """Download a file's current bytes from the server (black-box observation)."""
    import requests

    try:
        r = requests.get(
            f"{base_url}/api/v1/buckets/{bucket}/files/download",
            params={"filename": filename, "cid": BB_CLIENT_CID},
            headers=headers,
            timeout=15.0,
            verify=False,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    return r.content
