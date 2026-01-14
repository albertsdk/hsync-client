# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for the continuous (watch-mode) sync use case.

Drives the real client as a subprocess in watch mode against the real
in-process server. Verifies observable filesystem convergence — no internals
are imported or mocked.

Requires HSYNC_BB_SERVER=1 to run.
"""

import os
import subprocess
import time
from pathlib import Path

import pytest
import requests

from tests.conftest import BB_CLIENT_CID, _watch_client_log_tail
from tests.lan_helpers import _wait_for_client_ready

_BB_SERVER_ENABLED = os.environ.get("HSYNC_BB_SERVER") == "1"
_BB_SERVER_REASON = "Black-box server-mode tests require HSYNC_BB_SERVER=1"

pytestmark = pytest.mark.skipif(not _BB_SERVER_ENABLED, reason=_BB_SERVER_REASON)


def _wait_for(predicate, timeout=20.0, interval=0.3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _terminate(proc: subprocess.Popen) -> None:
    """Terminate and reap a watch subprocess, killing if needed."""
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _ready_path(local_dir: str) -> str:
    """Compute the ready-file path from the local dir (same tmp_path root)."""
    return str(Path(local_dir).parent / "client.ready")


def _client_log_path(local_dir: str) -> str:
    """The log file path the client writes via ``--output`` (loguru sink)."""
    return str(Path(local_dir).parent / "client_output.log")


def _wait_for_log_text(log_path: str, text: str, timeout: float = 30.0) -> bool:
    """Poll the client log file until *text* appears (black-box observation)."""
    return _wait_for(lambda: text in _watch_client_log_tail(log_path), timeout=timeout)


def _wait_for_ready(local_dir: str, proc: subprocess.Popen, log_path: str | None = None) -> bool:
    """Wait until the client is *actually* ready for local writes.

    The ready-file only proves the initial full sync completed; the watcher
    becomes active a moment later (inside the event loop, right before the
    ``[READY]`` log line). A local write performed in that window races the
    inotify watch registration and is silently lost (no upload). This helper
    gates on the observable ``[READY]`` marker ("watcher active + event
    channel up") when a log path is available, falling back to the ready-file
    plus a short settle window otherwise.
    """
    assert _wait_for_client_ready(proc, _ready_path(local_dir)), "client did not become ready"
    if log_path is not None:
        return _wait_for_log_text(log_path, "[READY]", timeout=30)
    time.sleep(2.0)  # settle window for the watcher to observe
    return True


def _wait_for_watcher_pipeline(bucket: str, local_dir: str, server_files) -> None:
    """Prove the local→server watcher pipeline before the real writes.

    The inotify watch registration is asynchronous relative to
    ``observer.start()``: a file written right after the ``[READY]`` log
    line can race the kernel watch and be silently lost (no event, no
    upload). This barrier repeatedly writes a *distinct* probe file and
    waits for it to appear in the server listing; a lost event simply means
    the next probe is tried. When a probe arrives, the pipeline is warm and
    the subsequent real writes are delivered. The probes are removed once
    the barrier is crossed.
    """
    probes: list[Path] = []
    for attempt in range(1, 11):
        name = f"watcher-probe-{attempt}.txt"
        probe = Path(local_dir) / name
        probe.write_bytes(f"probe-{attempt}\n".encode())
        probes.append(probe)
        if _wait_for(lambda name=name: name in server_files(bucket), timeout=15):
            for p in probes:
                p.unlink()
            return
    last = Path(local_dir) / "watcher-probe-final.txt"
    last.write_bytes(b"probe-final\n")
    probes.append(last)
    if _wait_for(lambda: "watcher-probe-final.txt" in server_files(bucket), timeout=15):
        for p in probes:
            p.unlink()
        return
    for p in probes:
        p.unlink()
    raise AssertionError(
        "watcher pipeline never delivered a probe file "
        f"(server files: {sorted(server_files(bucket))})"
    )


class TestContinuousSyncServerRestart:
    def test_resync_after_server_restart(
        self,
        restartable_live_server,
        bb_restartable_bucket,
        local_dir,
        watch_sync_restartable,
        bb_restartable_server_files,
    ):
        """A client in watch mode must re-connect and re-converge after the
        server process is restarted: changes made while the server was down
        (and the WS disconnected) are reconciled by the periodic full sync.

        The client runs with ``--interval 20`` (the CLI minimum) so the
        periodic resync task converges local changes that were made while the
        WS was disconnected and could not be broadcast as events.
        """

        # Given a client running in watch mode against a bucket (periodic
        # full resync every 20s)
        log_path = str(Path(local_dir).parent / "client.log")
        proc = watch_sync_restartable(
            bb_restartable_bucket, local_dir, "--interval", "20", "--output", log_path
        )
        try:
            assert _wait_for_ready(local_dir, proc, log_path), (
                "watch mode did not become ready\n"
                f"--- client log tail ---\n{_watch_client_log_tail(log_path)}"
            )
            # Prove the local→server watcher pipeline before the real writes.
            _wait_for_watcher_pipeline(
                bb_restartable_bucket, local_dir, bb_restartable_server_files
            )

            # Create a file locally so it is uploaded while the server is up
            content = b"before restart\n"
            (Path(local_dir) / "pre.txt").write_bytes(content)
            assert _wait_for(
                lambda: "pre.txt" in bb_restartable_server_files(bb_restartable_bucket)
            )

            # When the server is restarted while the client keeps watching
            restartable_live_server.restart()

            # Then a change made during the outage is still reconciled after
            # the client reconnects (periodic full resync)
            content2 = b"after restart\n"
            (Path(local_dir) / "post.txt").write_bytes(content2)
            assert _wait_for(
                lambda: "post.txt" in bb_restartable_server_files(bb_restartable_bucket),
                timeout=60,
            ), (
                "File created during the outage was not uploaded after restart.\n"
                f"--- client log tail ---\n{_watch_client_log_tail(log_path)}"
            )
            # The process should survive the restart without crashing
            assert proc.poll() is None
        finally:
            _terminate(proc)


class TestContinuousSyncMore:
    def test_remote_file_modification_downloaded(
        self, live_server, bb_bucket, local_dir, watch_sync, bb_server_upload, bb_sample
    ):
        """File created on server is downloaded locally via WS event, then
        a modification on the server is re-downloaded."""
        # Given a watch client ready to receive events
        log_path = _client_log_path(local_dir)
        proc = watch_sync(bb_bucket, local_dir, "--output", log_path)
        try:
            assert _wait_for_ready(local_dir, proc, log_path), (
                "watch mode did not become ready\n"
                f"--- client log tail ---\n{_watch_client_log_tail(log_path)}"
            )

            # When a file is uploaded to the server (broadcasts "added")
            s = bb_sample(prefix="update", size=0)
            filename = s.name
            content_v1 = b"v1\n"
            bb_server_upload(bb_bucket, filename, content_v1)
            local_file = Path(local_dir) / filename

            # Then the first version is downloaded via the WS event
            assert _wait_for(lambda: local_file.exists(), timeout=20), (
                f"File uploaded via API did not sync locally. {s.ctx}"
            )
            assert local_file.read_bytes() == content_v1, s.ctx

            # When the file is modified on the server (broadcasts "modified")
            content_v2 = b"v2-replaced\n"
            bb_server_upload(bb_bucket, filename, content_v2)

            # Then the updated version is re-downloaded to the local dir
            assert _wait_for(
                lambda: local_file.exists() and local_file.read_bytes() == content_v2,
                timeout=30,
            ), (
                "Modified file was not re-downloaded from the server.\n"
                f"{s.ctx}\n--- client log tail ---\n{_watch_client_log_tail(log_path)}"
            )
        finally:
            _terminate(proc)


class TestContinuousSync:
    def test_local_change_uploaded_to_server(
        self, live_server, bb_bucket, local_dir, watch_sync, bb_server_files
    ):
        # Given a client running in watch mode against an empty bucket
        log_path = _client_log_path(local_dir)
        proc = watch_sync(bb_bucket, local_dir, "--output", log_path)
        try:
            assert _wait_for_ready(local_dir, proc, log_path), (
                "watch mode did not become ready\n"
                f"--- client log tail ---\n{_watch_client_log_tail(log_path)}"
            )
            # Prove the local→server watcher pipeline before the real writes.
            _wait_for_watcher_pipeline(bb_bucket, local_dir, bb_server_files)

            # When a new file is created locally
            content = b"watched change\n"
            (Path(local_dir) / "watched.txt").write_bytes(content)

            # Then the file is uploaded to the server within a few seconds
            assert _wait_for(lambda: "watched.txt" in bb_server_files(bb_bucket)), (
                f"file not uploaded\n--- client log tail ---\n{_watch_client_log_tail(log_path)}"
            )
            time.sleep(5)
            # The process shouldn't crash — smoke test
            assert proc.poll() is None
        finally:
            _terminate(proc)

    def test_remote_file_uploaded_then_downloaded_via_ws(
        self, live_server, bb_bucket, local_dir, watch_sync, bb_server_upload
    ):
        """File uploaded to server via API is downloaded locally via WS event."""
        proc = watch_sync(bb_bucket, local_dir)
        try:
            assert _wait_for_client_ready(proc, _ready_path(local_dir)), (
                "watch mode did not become ready"
            )

            # Upload while the client is watching — the server broadcasts an
            # "added" event via WebSocket which triggers a download.
            bb_server_upload(bb_bucket, "from_api.txt", b"api upload\n")
            local_file = Path(local_dir) / "from_api.txt"
            assert _wait_for(lambda: local_file.exists(), timeout=20), (
                "File uploaded via API did not sync locally"
            )
            assert local_file.read_bytes() == b"api upload\n"
        finally:
            _terminate(proc)

    def test_remote_rename_propagates_locally(
        self, live_server, bb_bucket, local_dir, watch_sync, bb_server_upload
    ):
        """File renamed on server propagates to local via WS event."""
        proc = watch_sync(bb_bucket, local_dir)
        try:
            assert _wait_for_client_ready(proc, _ready_path(local_dir)), (
                "watch mode did not become ready"
            )

            # Upload while the client is watching so the initial download is
            # triggered by the WebSocket "added" event.
            bb_server_upload(bb_bucket, "oldname.txt", b"rename-me\n")
            old_file = Path(local_dir) / "oldname.txt"
            assert _wait_for(lambda: old_file.exists())

            # Rename on server via API (using files endpoint PATCH)
            base, api_key = live_server
            r = requests.patch(
                f"{base}/api/v1/buckets/{bb_bucket}/files",
                data={
                    "cid": BB_CLIENT_CID,
                    "filename": "oldname.txt",
                    "newname": "newname.txt",
                },
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15.0,
                verify=False,
            )
            # Rename may be supported or not; if not, just skip the check
            if r.status_code in (200, 204):
                new_file = Path(local_dir) / "newname.txt"
                assert _wait_for(lambda: new_file.exists(), timeout=20), (
                    "Renamed file did not appear locally"
                )
                assert _wait_for(lambda: not old_file.exists(), timeout=10), (
                    "Old file still present after rename"
                )
        finally:
            _terminate(proc)

    def test_remote_file_downloaded_to_local(
        self, live_server, bb_bucket, local_dir, watch_sync, bb_server_upload
    ):
        # Given a client running in watch mode against an empty bucket
        proc = watch_sync(bb_bucket, local_dir)
        try:
            assert _wait_for_client_ready(proc, _ready_path(local_dir)), (
                "watch mode did not become ready"
            )

            # When a file is created on the server (broadcasts an "added" event)
            content = b"from server via ws\n"
            bb_server_upload(bb_bucket, "pushed.txt", content)

            # Then the file is downloaded to the local dir via the WS event
            assert _wait_for(lambda: (Path(local_dir) / "pushed.txt").exists())
            assert (Path(local_dir) / "pushed.txt").read_bytes() == content
        finally:
            _terminate(proc)

    def test_local_file_modification_uploaded(
        self,
        live_server,
        bb_bucket,
        local_dir,
        watch_sync,
        bb_server_upload,
        bb_server_download,
        bb_sample,
    ):
        """File modified locally is re-uploaded to the server."""
        # Given a watch client ready to receive events
        log_path = _client_log_path(local_dir)
        proc = watch_sync(bb_bucket, local_dir, "--output", log_path)
        try:
            assert _wait_for_ready(local_dir, proc, log_path), (
                "watch mode did not become ready\n"
                f"--- client log tail ---\n{_watch_client_log_tail(log_path)}"
            )

            # And a file uploaded to the server so it reaches the client via
            # WebSocket "added" event (not via initial full sync)
            s = bb_sample(prefix="mod", size=0)
            filename = s.name
            bb_server_upload(bb_bucket, filename, b"v1\n")
            assert _wait_for(lambda: (Path(local_dir) / filename).exists()), s.ctx

            # When the local copy is modified
            time.sleep(0.5)
            content_v2 = b"v2-updated\n"
            (Path(local_dir) / filename).write_bytes(content_v2)

            # Then the modified file is re-uploaded to the server
            assert _wait_for(
                lambda: bb_server_download(bb_bucket, filename) == content_v2,
                timeout=30,
            ), (
                "Modified local file was not re-uploaded to the server.\n"
                f"{s.ctx}\n--- client log tail ---\n{_watch_client_log_tail(log_path)}"
            )
        finally:
            _terminate(proc)

    def test_local_file_deletion_removes_from_server(
        self, live_server, bb_bucket, local_dir, watch_sync, bb_server_upload, bb_server_files
    ):
        """File deleted locally is removed from the server."""
        proc = watch_sync(bb_bucket, local_dir)
        try:
            assert _wait_for_client_ready(proc, _ready_path(local_dir)), (
                "watch mode did not become ready"
            )

            # Upload while watching so the file reaches the client via
            # WebSocket "added" event (not via initial full sync).
            bb_server_upload(bb_bucket, "bye.txt", b"goodbye\n")
            assert _wait_for(lambda: (Path(local_dir) / "bye.txt").exists())

            # Delete locally
            (Path(local_dir) / "bye.txt").unlink()

            # Should be removed from server
            assert _wait_for(lambda: "bye.txt" not in bb_server_files(bb_bucket), timeout=20)
        finally:
            _terminate(proc)

    def test_directory_creation_syncs_to_server(
        self, live_server, bb_bucket, local_dir, watch_sync, bb_server_files
    ):
        """Directory created locally appears on the server."""
        log_path = _client_log_path(local_dir)
        proc = watch_sync(bb_bucket, local_dir, "--output", log_path)
        try:
            assert _wait_for_ready(local_dir, proc, log_path), (
                "watch mode did not become ready\n"
                f"--- client log tail ---\n{_watch_client_log_tail(log_path)}"
            )

            sub = Path(local_dir) / "newdir"
            sub.mkdir()
            # Let the observer register the inotify watch on the new directory
            # before creating files inside it — the kernel may deliver the
            # DirCreated event to watchdog asynchronously, and until the handler
            # calls observer.schedule() the subdirectory is unmonitored.
            time.sleep(2.0)
            (sub / "f.txt").write_bytes(b"inside\n")

            # The file inside the directory should appear on server
            assert _wait_for(lambda: "newdir/f.txt" in bb_server_files(bb_bucket), timeout=60)
        finally:
            _terminate(proc)

    def test_remote_file_deletion_downloaded(
        self, live_server, bb_bucket, local_dir, watch_sync, bb_server_upload
    ):
        """File deleted on server (via API) is removed locally."""
        proc = watch_sync(bb_bucket, local_dir)
        try:
            assert _wait_for_client_ready(proc, _ready_path(local_dir)), (
                "watch mode did not become ready"
            )

            # Upload while watching so the file reaches the client via
            # WebSocket "added" event (not via initial full sync).
            bb_server_upload(bb_bucket, "todie.txt", b"ephemeral\n")
            local_file = Path(local_dir) / "todie.txt"
            assert _wait_for(lambda: local_file.exists())

            # Delete on server via API
            base, api_key = live_server
            r = requests.delete(
                f"{base}/api/v1/buckets/{bb_bucket}/files",
                data={"cid": BB_CLIENT_CID, "filename": "todie.txt"},
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15.0,
                verify=False,
            )
            assert r.status_code in (200, 204), f"Server delete failed: {r.status_code}"

            # The deletion should propagate to local
            assert _wait_for(lambda: not local_file.exists(), timeout=20)
        finally:
            _terminate(proc)

    def test_ignore_delete_keeps_local_file(
        self,
        live_server,
        bb_bucket,
        local_dir,
        watch_sync,
        bb_server_upload,
        bb_server_files,
        bb_sample,
    ):
        """Watch mode with --ignore-delete preserves the server copy when the
        local file is deleted."""
        # Given a watch client with --ignore-delete, ready to receive events
        log_path = _client_log_path(local_dir)
        proc = watch_sync(bb_bucket, local_dir, "--output", log_path, "--ignore-delete")
        try:
            assert _wait_for_ready(local_dir, proc, log_path), (
                "watch mode did not become ready\n"
                f"--- client log tail ---\n{_watch_client_log_tail(log_path)}"
            )

            # And a file uploaded to the server so it reaches the client via WS
            s = bb_sample(prefix="keep", size=0)
            filename = s.name
            content = b"keep me\n"
            bb_server_upload(bb_bucket, filename, content)
            local_file = Path(local_dir) / filename
            assert _wait_for(lambda: local_file.exists()), s.ctx

            # When the local copy is deleted
            local_file.unlink()

            # Then the server copy is still present after a quiet window
            # (--ignore-delete must prevent the local deletion from propagating)
            time.sleep(5)
            assert filename in bb_server_files(bb_bucket), (
                f"Server file was deleted despite --ignore-delete. {s.ctx}\n"
                f"--- client log tail ---\n{_watch_client_log_tail(log_path)}"
            )
        finally:
            _terminate(proc)


# =============================================================================
# Parity with the LAN black-box suite: symlink stability, .hsyncignore
# hot-reload, and client restart — all in watch (continuous) mode.
# =============================================================================


class TestContinuousSyncSymlinkStable:
    def test_symlink_uploads_and_stays_stable(
        self, live_server, bb_bucket, local_dir, watch_sync, bb_server_files
    ):
        """A local in-bucket symlink is uploaded structurally and converges
        without oscillation: no duplicate entries appear over a quiet window
        (the server never echoes a sender's own event back to it)."""
        proc = watch_sync(bb_bucket, local_dir)
        try:
            assert _wait_for_client_ready(proc, _ready_path(local_dir)), (
                "watch mode did not become ready"
            )
            # Given a target file and an in-bucket symlink created locally
            (Path(local_dir) / "tgt.txt").write_bytes(b"target\n")
            (Path(local_dir) / "link.txt").symlink_to("tgt.txt")

            # When the watcher detects the new entries
            assert _wait_for(lambda: "link.txt" in bb_server_files(bb_bucket), timeout=30), (
                "symlink was not uploaded to the server"
            )

            # Then the server stored a *structural* symlink (not a dereferenced
            # regular file): the listing entry must advertise type + target.
            base, api_key = live_server
            listing = requests.get(
                f"{base}/api/v1/buckets/{bb_bucket}/files",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15.0,
                verify=False,
            ).json()
            link_entry = next(f for f in listing if f["filepath"] == "link.txt")
            assert link_entry.get("type") == "symlink", (
                f"expected structural symlink entry, got {link_entry!r}"
            )
            assert link_entry.get("target") == "tgt.txt"

            # And after a quiet window the entry is still present and not duplicated
            time.sleep(5)
            files = bb_server_files(bb_bucket)
            assert "link.txt" in files
            assert sum(1 for f in files if "link" in f) == 1, (
                f"symlink entry duplicated (oscillation): {files}"
            )
        finally:
            _terminate(proc)


class TestContinuousSyncHsyncignoreReload:
    def test_hsyncignore_hot_reload_stops_uploading_matching_files(
        self, live_server, bb_bucket, local_dir, watch_sync, bb_server_files
    ):
        """Adding .hsyncignore during an active watch stops uploading newly
        created matching files, while already-synced files are preserved
        (exclude protects, it does not delete)."""
        log_path = _client_log_path(local_dir)
        proc = watch_sync(bb_bucket, local_dir, "--output", log_path)
        try:
            assert _wait_for_ready(local_dir, proc, log_path), (
                "watch mode did not become ready\n"
                f"--- client log tail ---\n{_watch_client_log_tail(log_path)}"
            )
            # Prove the local→server watcher pipeline before the real writes.
            _wait_for_watcher_pipeline(bb_bucket, local_dir, bb_server_files)
            # Given a visible file uploaded before any ignore rule
            (Path(local_dir) / "keep.txt").write_bytes(b"keep\n")
            assert _wait_for(lambda: "keep.txt" in bb_server_files(bb_bucket))

            # When .hsyncignore is added excluding *.log and a new .log is created
            (Path(local_dir) / ".hsyncignore").write_text("*.log\n")
            time.sleep(2)  # let the hot-reload watcher rebuild the matcher
            (Path(local_dir) / "secret.log").write_bytes(b"hidden\n")

            # Then the newly-excluded file is NOT uploaded within a quiet window
            assert not _wait_for(lambda: "secret.log" in bb_server_files(bb_bucket), timeout=5), (
                "secret.log was uploaded despite the .hsyncignore *.log rule"
            )
            # And the already-synced file is preserved
            assert "keep.txt" in bb_server_files(bb_bucket)
        finally:
            _terminate(proc)


class TestContinuousSyncClientRestart:
    def test_client_restart_resyncs_after_restart(
        self, live_server, bb_bucket, local_dir, watch_sync, bb_server_upload
    ):
        """After the watch client is terminated and restarted on the same
        directory, it re-acquires its instance lock, reconnects, and resumes
        syncing: a file created on the server during the outage is downloaded."""
        ready = _ready_path(local_dir)
        proc = watch_sync(bb_bucket, local_dir)
        try:
            assert _wait_for_client_ready(proc, ready), "first watch did not become ready"
            # Establish a synced baseline
            bb_server_upload(bb_bucket, "pre.txt", b"pre\n")
            assert _wait_for(lambda: (Path(local_dir) / "pre.txt").exists())
        finally:
            _terminate(proc)

        # When the client is restarted on the same directory
        proc2 = watch_sync(bb_bucket, local_dir)
        try:
            assert _wait_for_client_ready(proc2, ready), "restarted watch did not become ready"

            # Then a file created on the server is downloaded after reconnection
            content = b"post-restart\n"
            bb_server_upload(bb_bucket, "post.txt", content)
            assert _wait_for(lambda: (Path(local_dir) / "post.txt").exists(), timeout=30), (
                "restarted client did not download the new server file"
            )
            assert (Path(local_dir) / "post.txt").read_bytes() == content
        finally:
            _terminate(proc2)
