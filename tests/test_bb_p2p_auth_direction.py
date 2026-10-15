# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box tests for P2P authentication and sync-direction enforcement.

Requires HSYNC_BB_P2P=1 to run.
"""

import json
import os
import time
from pathlib import Path

import pytest

from .p2p_helpers import (
    _p2p_download,
    _p2p_files,
    _p2p_ready_path,
    _p2p_serving_master,
    _p2p_start,
    _p2p_upload,
    _stderr_text,
    _terminate,
    _wait_for_client_ready,
    _wait_for_file,
    _wait_for_master,
    _wait_for_slave_events,
    _wait_for_text_in_stderr,
)

_P2P_ENABLED = os.environ.get("HSYNC_BB_P2P") == "1"
_P2P_REASON = (
    "P2P e2e needs loopback mDNS + two live client instances; set HSYNC_BB_P2P=1 to enable"
)

pytestmark = pytest.mark.skipif(not _P2P_ENABLED, reason=_P2P_REASON)


# =============================================================================
# P2P key authentication
# =============================================================================


class TestP2PKeyAuth:
    def test_p2p_key_mismatch_rejected(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        """Master with P2P key A, slave with P2P key B → connection refused."""
        # Given a serving master
        master = _p2p_start(p2p_master_dir, p2p_bucket, prefer_master=True)
        try:
            _wait_for_master(master, p2p_slave_dir, p2p_bucket)

            # When the slave starts with a different P2P key
            slave = _p2p_start(
                p2p_slave_dir,
                p2p_bucket,
                never_master=True,
                extra_env={"HSYNC_P2P_KEY": "wrong-key-xxxxxxxx"},
            )
            try:
                # Then the connection is refused (the master answers 401; the
                # observable rejection is the slave's "Invalid API key" abort).
                auth_failed = _wait_for_text_in_stderr(slave, "Invalid API key", timeout=20)
                assert auth_failed, "Slave should have been rejected with wrong P2P key"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Directional sync
# =============================================================================


class TestP2PSyncDirection:
    def test_download_only_prevents_upload(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        """Slave with --sync-direction download-only: its files don't reach master."""
        # Given a serving master and a download-only slave
        master = _p2p_start(p2p_master_dir, p2p_bucket, prefer_master=True)
        try:
            _wait_for_master(master, p2p_slave_dir, p2p_bucket)

            slave = _p2p_start(
                p2p_slave_dir,
                p2p_bucket,
                never_master=True,
                extra_args=("--sync-direction", "download-only"),
            )
            try:
                # When the master creates a file, it reaches the slave (download works)
                (Path(p2p_master_dir) / "from_master.txt").write_bytes(b"master\n")
                assert _wait_for_file(str(Path(p2p_slave_dir) / "from_master.txt"), timeout=30), (
                    "Download did not work"
                )

                # But when the slave creates a file, it does NOT reach the master
                (Path(p2p_slave_dir) / "slave_only.txt").write_bytes(b"slave\n")
                time.sleep(5)  # Give time for any potential upload
                assert not (Path(p2p_master_dir) / "slave_only.txt").exists(), (
                    "Slave file leaked to master despite download-only mode"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)

    def test_upload_only_prevents_download(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        """Slave with --sync-direction upload-only: master files don't reach slave."""
        # Given a serving master and an upload-only slave
        master = _p2p_start(p2p_master_dir, p2p_bucket, prefer_master=True)
        try:
            _wait_for_master(master, p2p_slave_dir, p2p_bucket)

            slave = _p2p_start(
                p2p_slave_dir,
                p2p_bucket,
                never_master=True,
                extra_args=("--sync-direction", "upload-only"),
            )
            try:
                # When the master creates a file, it does NOT reach the slave
                (Path(p2p_master_dir) / "master_only.txt").write_bytes(b"master\n")
                time.sleep(5)  # Give time for any potential download
                assert not (Path(p2p_slave_dir) / "master_only.txt").exists(), (
                    "Master file leaked to slave despite upload-only mode"
                )

                # But when the slave creates a file, it reaches the master (upload works)
                (Path(p2p_slave_dir) / "from_slave.txt").write_bytes(b"slave\n")
                assert _wait_for_file(str(Path(p2p_master_dir) / "from_slave.txt"), timeout=30), (
                    "Upload did not work"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Master restart resilience
# =============================================================================


class TestP2PMasterRestart:
    @pytest.mark.flaky(reruns=3, reruns_delay=5)
    def test_master_restart_slave_recovers(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        """After master crash + restart, slave rediscovers and resumes syncing."""
        # Given a first master session and a slave that has completed initial sync
        master1 = _p2p_start(p2p_master_dir, p2p_bucket, prefer_master=True)
        try:
            _wait_for_master(master1, p2p_slave_dir, p2p_bucket)

            slave = _p2p_start(p2p_slave_dir, p2p_bucket, never_master=True)
            try:
                _wait_for_text_in_stderr(slave, "Initial sync complete", timeout=30)

                # When the master is killed
                _terminate(master1)

                # Then the slave detects the disconnection and re-enters discovery
                assert _wait_for_text_in_stderr(
                    slave, "discovery", timeout=30
                ) or _wait_for_text_in_stderr(slave, "Waiting for peers", timeout=5), (
                    "Slave did not re-enter discovery after master crash"
                )

                # When the master is restarted on the same directory
                master2 = _p2p_start(p2p_master_dir, p2p_bucket, prefer_master=True)
                try:
                    # Then the restarted master becomes ready
                    assert _wait_for_client_ready(
                        master2, _p2p_ready_path(p2p_master_dir), timeout=60
                    ), "Restarted master did not become ready"

                    # And a new file on the restarted master reaches the slave
                    content = b"post-restart\n"
                    (Path(p2p_master_dir) / "post_restart.txt").write_bytes(content)
                    assert _wait_for_file(
                        str(Path(p2p_slave_dir) / "post_restart.txt"), timeout=60
                    ), "Slave did not receive file after master restart"
                    assert (Path(p2p_slave_dir) / "post_restart.txt").read_bytes() == content
                finally:
                    _terminate(master2)
            finally:
                _terminate(slave)
        finally:
            # Safety net: master1 may still be alive if an early failure
            # prevented the explicit _terminate above.
            _terminate(master1)


# =============================================================================
# Slave restart resilience
# =============================================================================


class TestP2PSlaveRestart:
    @pytest.mark.flaky(reruns=3, reruns_delay=5)
    def test_slave_restart_recovers_and_resyncs(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        """After the slave process is terminated and restarted on the same dir,
        it re-discovers the master, reconnects with its persistent device
        identity and TOFU pin, and resumes syncing."""

        # Given a master and a live slave that have completed their initial sync
        master = _p2p_start(p2p_master_dir, p2p_bucket, prefer_master=True)
        try:
            _wait_for_master(master, p2p_slave_dir, p2p_bucket)

            slave1 = _p2p_start(p2p_slave_dir, p2p_bucket, never_master=True)
            try:
                _wait_for_text_in_stderr(slave1, "Initial sync complete", timeout=30)

                # Sanity check: a file written on the master reaches the slave
                # (event channel must be live first — see _wait_for_slave_events)
                assert _wait_for_slave_events(slave1), "Slave is not listening for master events"
                (Path(p2p_master_dir) / "pre_restart.txt").write_bytes(b"pre\n")
                assert _wait_for_file(str(Path(p2p_slave_dir) / "pre_restart.txt"), timeout=30), (
                    "Pre-restart sync did not work"
                )

                # When the slave is terminated and restarted on the same dir
                # (same device identity + same pinned master fingerprint)
                _terminate(slave1)
                slave2 = _p2p_start(p2p_slave_dir, p2p_bucket, never_master=True)
                try:
                    # Then the restarted slave reconnects and resumes syncing:
                    # a new file on the master is pulled after rediscovery
                    content = b"post-restart\n"
                    (Path(p2p_master_dir) / "post_restart.txt").write_bytes(content)
                    assert _wait_for_file(
                        str(Path(p2p_slave_dir) / "post_restart.txt"), timeout=60
                    ), "Restarted slave did not receive file after recovery"
                    assert (Path(p2p_slave_dir) / "post_restart.txt").read_bytes() == content
                    # The process should survive without crashing
                    assert slave2.poll() is None
                finally:
                    _terminate(slave2)
            finally:
                _terminate(slave1)
        except BaseException:
            _terminate(master)
            raise


# =============================================================================
# Conflicting edits of the same file on both peers
# =============================================================================


class TestP2PConflict:
    def test_same_file_conflict_on_both_peers(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        """When both peers diverge on the same path while disconnected, the
        causal tie-break picks the winner deterministically (highest SHA-256)
        and the loser's version is preserved as a Dropbox-style conflicted
        copy — no silent data loss.

        The shared file is synced *before* the slave starts, so the slave's
        initial full sync records the common ancestor (merge base) in its
        persisted sync-state side-car. Diverging both copies while the slave
        is offline makes the next reconciliation a genuine three-way conflict
        (both sides changed since the base): the higher hash wins and the
        losing version is saved as
        ``<stem>_conflicted_copy_<ts>_<cid>.<ext>``.

        The master is a transport coordinator, not an authority (ADR-0022):
        the winner is decided purely by content hash, not by role.
        """

        # Given a shared file both peers already agreed on, synced before the
        # slave session starts
        master = _p2p_start(p2p_master_dir, p2p_bucket, prefer_master=True)
        try:
            (Path(p2p_master_dir) / "shared.txt").write_bytes(b"base\n")
            _wait_for_master(master, p2p_slave_dir, p2p_bucket)

            slave1 = _p2p_start(p2p_slave_dir, p2p_bucket, never_master=True)
            try:
                # The slave's initial full sync must complete (ready file) — the
                # merge-base side-car is persisted only at the end of the cycle,
                # and without it the three-way merge can never flag a conflict.
                assert _wait_for_client_ready(slave1, _p2p_ready_path(p2p_slave_dir), timeout=30), (
                    "Slave did not finish its initial sync"
                )
                assert (Path(p2p_slave_dir) / "shared.txt").read_bytes() == b"base\n"

                # When the slave goes offline and both peers edit the same
                # path with different content. The deterministic tie-break is
                # by highest SHA-256: the winner is decided purely by content,
                # not by role.
                _terminate(slave1)
                # These two payloads are chosen so the *master* wins the
                # deterministic SHA-256 tie-break (sha("master-data-zz") >
                # sha("slave-data-aa")): the slave then preserves its losing
                # content as the conflicted copy below.
                master_bytes = b"master-data-zz\n"
                slave_bytes = b"slave-data-aa\n"
                (Path(p2p_master_dir) / "shared.txt").write_bytes(master_bytes)
                (Path(p2p_slave_dir) / "shared.txt").write_bytes(slave_bytes)
                shared_ts = 1_700_000_000
                os.utime(str(Path(p2p_master_dir) / "shared.txt"), (shared_ts, shared_ts))
                os.utime(str(Path(p2p_slave_dir) / "shared.txt"), (shared_ts, shared_ts))

                # Pre-compute the deterministic winner (highest SHA-256).
                import hashlib

                master_hash = hashlib.sha256(master_bytes).hexdigest()
                slave_hash = hashlib.sha256(slave_bytes).hexdigest()
                winner_bytes = master_bytes if master_hash > slave_hash else slave_bytes
                loser_bytes = slave_bytes if winner_bytes is master_bytes else master_bytes

                # Then, on reconnect, the winner takes the original path on the
                # slave and the loser survives as a conflicted copy
                slave2 = _p2p_start(p2p_slave_dir, p2p_bucket, never_master=True)
                try:
                    assert _wait_for_text_in_stderr(slave2, "Initial sync complete", timeout=30), (
                        "Restarted slave did not complete its initial sync"
                    )

                    deadline = time.time() + 30
                    conflict_path = None
                    while time.time() < deadline:
                        candidates = [
                            p
                            for p in Path(p2p_slave_dir).iterdir()
                            if p.name.startswith("shared_conflicted_copy_")
                        ]
                        if candidates:
                            conflict_path = candidates[0]
                            break
                        time.sleep(0.5)
                    assert conflict_path is not None, (
                        "The loser's divergent version was not preserved as a "
                        "conflicted copy after reconnection.\n"
                        f"--- slave2 log ---\n{_stderr_text(slave2)}"
                    )
                    assert conflict_path.read_bytes() == loser_bytes
                    assert (Path(p2p_slave_dir) / "shared.txt").read_bytes() == winner_bytes, (
                        "The deterministic hash winner did not take the original path "
                        "after reconnect"
                    )
                    assert slave2.poll() is None, "Restarted slave exited unexpectedly"

                    # ── Post-conflict convergence ────────────────────────────
                    # Once the conflict is resolved, a follow-up one-sided edit
                    # must propagate as a plain update: the merged history makes
                    # it causally dominant, so the slave adopts it silently and
                    # the existing conflicted copy is neither duplicated nor
                    # re-flagged. The edit travels on the event channel: gate
                    # on it (see _wait_for_slave_events).
                    assert _wait_for_slave_events(slave2), (
                        "Slave is not listening for master events"
                    )
                    follow_up = b"follow-up v3\n"
                    (Path(p2p_master_dir) / "shared.txt").write_bytes(follow_up)
                    os.utime(str(Path(p2p_master_dir) / "shared.txt"), (shared_ts, shared_ts))

                    deadline = time.time() + 30
                    while time.time() < deadline:
                        if (Path(p2p_slave_dir) / "shared.txt").read_bytes() == follow_up:
                            break
                        time.sleep(0.5)
                    assert (Path(p2p_slave_dir) / "shared.txt").read_bytes() == follow_up, (
                        "The follow-up edit did not reach the slave after the conflict"
                    )
                    copies_after = [
                        p
                        for p in Path(p2p_slave_dir).iterdir()
                        if p.name.startswith("shared_conflicted_copy_")
                    ]
                    assert len(copies_after) == 1, (
                        "Post-conflict convergence must not duplicate the conflicted "
                        f"copy; found {len(copies_after)}: {copies_after}"
                    )
                finally:
                    _terminate(slave2)
            finally:
                _terminate(slave1)
        except BaseException:
            _terminate(master)
            raise

    def test_same_file_conflict_slave_wins_master_preserves_copy(
        self, p2p_master_dir, p2p_slave_dir, p2p_bucket
    ):
        """Mirror of :meth:`test_same_file_conflict_on_both_peers` with the
        tie-break going the other way: the *slave's* content has the higher
        SHA-256, so the master — a transport hub, not an authority — adopts
        the slave's version at the original path and preserves its own losing
        content as a conflicted copy of its own. The slave, being the winner,
        never creates a local copy.

        The merge base is established exactly as in the sibling test: the
        shared file must be synced *before* the divergence (otherwise there is
        no common ancestor and the reconciliation is a plain adoption, not a
        true three-way conflict).
        """
        # Given a shared file synced before the slave session starts
        master = _p2p_start(p2p_master_dir, p2p_bucket, prefer_master=True)
        try:
            (Path(p2p_master_dir) / "shared.txt").write_bytes(b"base\n")
            _wait_for_master(master, p2p_slave_dir, p2p_bucket)

            slave1 = _p2p_start(p2p_slave_dir, p2p_bucket, never_master=True)
            try:
                # The slave's initial full sync must complete so the common
                # ancestor (merge base) is persisted in its sync-state.
                assert _wait_for_client_ready(slave1, _p2p_ready_path(p2p_slave_dir), timeout=30), (
                    "Slave did not finish its initial sync"
                )
                assert (Path(p2p_slave_dir) / "shared.txt").read_bytes() == b"base\n"

                # When both peers diverge while the slave is offline (the
                # payloads make the *slave* win the SHA-256 tie-break).
                _terminate(slave1)
                master_bytes = b"master-loses-v2\n"
                slave_bytes = b"slave-wins-v2\n"
                (Path(p2p_master_dir) / "shared.txt").write_bytes(master_bytes)
                (Path(p2p_slave_dir) / "shared.txt").write_bytes(slave_bytes)
                shared_ts = 1_700_000_000
                os.utime(str(Path(p2p_master_dir) / "shared.txt"), (shared_ts, shared_ts))
                os.utime(str(Path(p2p_slave_dir) / "shared.txt"), (shared_ts, shared_ts))

                # Then, on reconnect, the master adopts the slave's content at
                # the shared path and keeps its own losing version as a
                # conflicted copy.
                slave2 = _p2p_start(p2p_slave_dir, p2p_bucket, never_master=True)
                try:
                    assert _wait_for_text_in_stderr(slave2, "Initial sync complete", timeout=30), (
                        "Restarted slave did not complete its initial sync"
                    )

                    deadline = time.time() + 30
                    while time.time() < deadline:
                        if (Path(p2p_master_dir) / "shared.txt").read_bytes() == slave_bytes:
                            break
                        time.sleep(0.5)
                    assert (Path(p2p_master_dir) / "shared.txt").read_bytes() == slave_bytes, (
                        "The winner (slave) content did not take the original path "
                        f"on the master.\n--- master log ---\n{_stderr_text(master)}"
                    )

                    deadline = time.time() + 30
                    conflict_path = None
                    while time.time() < deadline:
                        candidates = [
                            p
                            for p in Path(p2p_master_dir).iterdir()
                            if p.name.startswith("shared_conflicted_copy_")
                        ]
                        if candidates:
                            conflict_path = candidates[0]
                            break
                        time.sleep(0.5)
                    assert conflict_path is not None, (
                        "The losing (master) version was not preserved as a "
                        "conflicted copy on the master.\n"
                        f"--- master log ---\n{_stderr_text(master)}"
                    )
                    assert conflict_path.read_bytes() == master_bytes

                    # The winning slave keeps its own content at the original
                    # path and never preserves a local copy for its win.
                    assert (Path(p2p_slave_dir) / "shared.txt").read_bytes() == slave_bytes, (
                        "The slave's winning version did not stay on the original path"
                    )
                    slave_copies = [
                        p
                        for p in Path(p2p_slave_dir).iterdir()
                        if p.name.startswith("shared_conflicted_copy_")
                    ]
                    assert len(slave_copies) <= 1, (
                        "The slave wins the tie-break: at most the master's "
                        f"propagated copy may appear, got {len(slave_copies)}: {slave_copies}"
                    )
                    assert slave2.poll() is None, "Restarted slave exited unexpectedly"
                finally:
                    _terminate(slave2)
            finally:
                _terminate(slave1)
        except BaseException:
            _terminate(master)
            raise


# =============================================================================
# Concurrent writes on both peers
# =============================================================================


class TestP2PConcurrency:
    def test_concurrent_file_writes_on_both_peers(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        """Master and slave write different files concurrently → both converge."""
        # Given a serving master and a slave with a live event channel
        master = _p2p_start(p2p_master_dir, p2p_bucket, prefer_master=True)
        try:
            _wait_for_master(master, p2p_slave_dir, p2p_bucket)

            slave = _p2p_start(p2p_slave_dir, p2p_bucket, never_master=True)
            try:
                # Wait for the event channel (implies initial sync done and the
                # slave-side watcher started — both directions need them).
                assert _wait_for_slave_events(slave), "Slave is not listening for master events"

                # When both peers write files at the same time
                (Path(p2p_master_dir) / "from_master.txt").write_bytes(b"master-data\n")
                (Path(p2p_slave_dir) / "from_slave.txt").write_bytes(b"slave-data\n")

                # Then both files converge on the other peer
                assert _wait_for_file(str(Path(p2p_slave_dir) / "from_master.txt"), timeout=30), (
                    "Master file did not reach slave"
                )
                assert _wait_for_file(str(Path(p2p_master_dir) / "from_slave.txt"), timeout=30), (
                    "Slave file did not reach master"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Causal upload arbitration over the public P2P REST surface (ADR-0022)
#
# Parity with the server-side ``TestUploadOverwrite``: the P2P master is a
# transport hub, not an authority. The ``vclock`` form field is the author's
# causal history; the ``superseded`` 409 token is the observable outcome.
# =============================================================================


class TestP2PCausalArbitration:
    def test_dominating_version_accepted_without_copy(
        self, p2p_master_dir, p2p_slave_dir, p2p_bucket
    ):
        """A causally-ahead upload (same author, higher counter) replaces the
        content silently: 201, no conflicted copy."""
        # Given a valid 21-char author cid
        cid = "TestClientA1234567890"
        old = b"version one data"
        new = b"version two - updated content"

        with _p2p_serving_master(p2p_master_dir, p2p_slave_dir, p2p_bucket) as p2p_listen_port:
            base = f"https://127.0.0.1:{p2p_listen_port}"
            # First push v1, then a causally-dominant v2
            assert (
                _p2p_upload(
                    base, p2p_bucket, "arb.bin", old, cid=cid, vclock=json.dumps({cid: 1})
                ).status_code
                == 201
            )
            r = _p2p_upload(base, p2p_bucket, "arb.bin", new, cid=cid, vclock=json.dumps({cid: 2}))

            # Then the dominant version is accepted, the file holds the new content...
            assert r.status_code == 201
            assert _p2p_download(base, p2p_bucket, "arb.bin") == new
            # ...and no conflicted copy is created: this was a clean causal edit
            assert not any("conflicted_copy" in p for p in _p2p_files(base, p2p_bucket)), (
                "a dominant upload must not create a conflicted copy"
            )

    def test_stale_version_superseded_without_copy(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        """A causally-dominated upload (older content, older clock) is rejected
        with 409 ``superseded``: the winner stays and no copy is created."""
        cid = "TestClientA1234567890"
        new = b"version two - updated content"
        old = b"version one data"

        with _p2p_serving_master(p2p_master_dir, p2p_slave_dir, p2p_bucket) as p2p_listen_port:
            base = f"https://127.0.0.1:{p2p_listen_port}"
            # First push the causally-newer version...
            assert (
                _p2p_upload(
                    base, p2p_bucket, "stale.bin", new, cid=cid, vclock=json.dumps({cid: 2})
                ).status_code
                == 201
            )
            # ...then a stale peer re-pushes the older content with the older clock.
            r = _p2p_upload(
                base, p2p_bucket, "stale.bin", old, cid=cid, vclock=json.dumps({cid: 1})
            )

            # Then the stale upload is superseded, the winner remains on disk...
            assert r.status_code == 409
            assert r.json()["error"] == "superseded"
            assert _p2p_download(base, p2p_bucket, "stale.bin") == new
            # ...and no conflicted copy is created: the ancestor is inside the winner's history
            assert not any("conflicted_copy" in p for p in _p2p_files(base, p2p_bucket)), (
                "a superseded upload must not create a conflicted copy"
            )
