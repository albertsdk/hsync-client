# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for LAN authentication and sync-direction enforcement.

Requires HSYNC_BB_LAN=1 to run.
"""

import os
import time
from pathlib import Path

import pytest

from .lan_helpers import (
    _lan_ready_path,
    _lan_start,
    _stderr_text,
    _terminate,
    _wait_for_client_ready,
    _wait_for_file,
    _wait_for_master,
    _wait_for_text_in_stderr,
)

_LAN_ENABLED = os.environ.get("HSYNC_BB_LAN") == "1"
_LAN_REASON = (
    "LAN e2e needs loopback mDNS + two live client instances; set HSYNC_BB_LAN=1 to enable"
)

pytestmark = pytest.mark.skipif(not _LAN_ENABLED, reason=_LAN_REASON)


# =============================================================================
# LAN key authentication
# =============================================================================


class TestLanKeyAuth:
    def test_lan_key_mismatch_rejected(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Master with LAN key A, slave with LAN key B → connection refused."""
        # Start master with LAN_KEY
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            # Start slave with a DIFFERENT LAN key
            slave = _lan_start(
                lan_slave_dir,
                lan_bucket,
                never_master=True,
                extra_env={"HSYNC_LAN_KEY": "wrong-key-xxxxxxxx"},
            )
            try:
                # The master answers 401 (response body only, never logged);
                # the observable rejection is on the slave: the initial sync
                # aborts with AuthenticationError ("Sync failed: Invalid API
                # key") and the slave exits. The helper drains final output
                # even after process exit.
                auth_failed = _wait_for_text_in_stderr(slave, "Invalid API key", timeout=20)
                assert auth_failed, "Slave should have been rejected with wrong LAN key"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Directional sync
# =============================================================================


class TestLanSyncDirection:
    def test_download_only_prevents_upload(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Slave with --sync-direction download-only: its files don't reach master."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            slave = _lan_start(
                lan_slave_dir,
                lan_bucket,
                never_master=True,
                extra_args=("--sync-direction", "download-only"),
            )
            try:
                # Master creates a file → appears on slave (download works)
                (Path(lan_master_dir) / "from_master.txt").write_bytes(b"master\n")
                assert _wait_for_file(str(Path(lan_slave_dir) / "from_master.txt"), timeout=30), (
                    "Download did not work"
                )

                # Slave creates a file → must NOT appear on master (upload blocked)
                (Path(lan_slave_dir) / "slave_only.txt").write_bytes(b"slave\n")
                time.sleep(5)  # Give time for any potential upload
                assert not (Path(lan_master_dir) / "slave_only.txt").exists(), (
                    "Slave file leaked to master despite download-only mode"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)

    def test_upload_only_prevents_download(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Slave with --sync-direction upload-only: master files don't reach slave."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            slave = _lan_start(
                lan_slave_dir,
                lan_bucket,
                never_master=True,
                extra_args=("--sync-direction", "upload-only"),
            )
            try:
                # Master creates a file → must NOT appear on slave (download blocked)
                (Path(lan_master_dir) / "master_only.txt").write_bytes(b"master\n")
                time.sleep(5)  # Give time for any potential download
                assert not (Path(lan_slave_dir) / "master_only.txt").exists(), (
                    "Master file leaked to slave despite upload-only mode"
                )

                # Slave creates a file → appears on master (upload works)
                (Path(lan_slave_dir) / "from_slave.txt").write_bytes(b"slave\n")
                assert _wait_for_file(str(Path(lan_master_dir) / "from_slave.txt"), timeout=30), (
                    "Upload did not work"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Master restart resilience
# =============================================================================


class TestLanMasterRestart:
    @pytest.mark.flaky(reruns=3, reruns_delay=5)
    def test_master_restart_slave_recovers(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """After master crash + restart, slave rediscovers and resumes syncing."""
        # First master session
        master1 = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master1, lan_slave_dir, lan_bucket)

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            _wait_for_text_in_stderr(slave, "Initial sync complete", timeout=30)

            # Kill master
            _terminate(master1)

            # Wait for slave to detect disconnection and re-enter discovery
            assert _wait_for_text_in_stderr(
                slave, "discovery", timeout=30
            ) or _wait_for_text_in_stderr(slave, "Waiting for peers", timeout=5), (
                "Slave did not re-enter discovery after master crash"
            )

            # Restart master with same dir (same bucket, inherits existing files)
            master2 = _lan_start(lan_master_dir, lan_bucket, force_master=True)
            try:
                # The already-running slave triggers the election; wait for the
                # restarted master to finish startup (ready file carries its PID).
                assert _wait_for_client_ready(
                    master2, _lan_ready_path(lan_master_dir), timeout=60
                ), "Restarted master did not become ready"

                # Create a new file on restarted master
                content = b"post-restart\n"
                (Path(lan_master_dir) / "post_restart.txt").write_bytes(content)

                # Slave should pick it up after rediscovery
                assert _wait_for_file(str(Path(lan_slave_dir) / "post_restart.txt"), timeout=60), (
                    "Slave did not receive file after master restart"
                )
                assert (Path(lan_slave_dir) / "post_restart.txt").read_bytes() == content
            finally:
                _terminate(master2)
                _terminate(slave)
        except BaseException:
            _terminate(master1)
            raise


# =============================================================================
# Slave restart resilience
# =============================================================================


class TestLanSlaveRestart:
    @pytest.mark.flaky(reruns=3, reruns_delay=5)
    def test_slave_restart_recovers_and_resyncs(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """After the slave process is terminated and restarted on the same dir,
        it re-discovers the master, reconnects with its persistent device
        identity and TOFU pin, and resumes syncing."""

        # Given a master and a live slave that have completed their initial sync
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            slave1 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                _wait_for_text_in_stderr(slave1, "Initial sync complete", timeout=30)

                # Sanity check: a file written on the master reaches the slave
                (Path(lan_master_dir) / "pre_restart.txt").write_bytes(b"pre\n")
                assert _wait_for_file(str(Path(lan_slave_dir) / "pre_restart.txt"), timeout=30), (
                    "Pre-restart sync did not work"
                )

                # When the slave is terminated and restarted on the same dir
                # (same device identity + same pinned master fingerprint)
                _terminate(slave1)
                slave2 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
                try:
                    # Then the restarted slave reconnects and resumes syncing:
                    # a new file on the master is pulled after rediscovery
                    content = b"post-restart\n"
                    (Path(lan_master_dir) / "post_restart.txt").write_bytes(content)
                    assert _wait_for_file(
                        str(Path(lan_slave_dir) / "post_restart.txt"), timeout=60
                    ), "Restarted slave did not receive file after recovery"
                    assert (Path(lan_slave_dir) / "post_restart.txt").read_bytes() == content
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


class TestLanConflict:
    def test_same_file_conflict_on_both_peers(self, lan_master_dir, lan_slave_dir, lan_bucket):
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
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            (Path(lan_master_dir) / "shared.txt").write_bytes(b"base\n")
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            slave1 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # The slave's initial full sync must complete (ready file) — the
                # merge-base side-car is persisted only at the end of the cycle,
                # and without it the three-way merge can never flag a conflict.
                assert _wait_for_client_ready(slave1, _lan_ready_path(lan_slave_dir), timeout=30), (
                    "Slave did not finish its initial sync"
                )
                assert (Path(lan_slave_dir) / "shared.txt").read_bytes() == b"base\n"

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
                (Path(lan_master_dir) / "shared.txt").write_bytes(master_bytes)
                (Path(lan_slave_dir) / "shared.txt").write_bytes(slave_bytes)
                shared_ts = 1_700_000_000
                os.utime(str(Path(lan_master_dir) / "shared.txt"), (shared_ts, shared_ts))
                os.utime(str(Path(lan_slave_dir) / "shared.txt"), (shared_ts, shared_ts))

                # Pre-compute the deterministic winner (highest SHA-256).
                import hashlib

                master_hash = hashlib.sha256(master_bytes).hexdigest()
                slave_hash = hashlib.sha256(slave_bytes).hexdigest()
                winner_bytes = master_bytes if master_hash > slave_hash else slave_bytes
                loser_bytes = slave_bytes if winner_bytes is master_bytes else master_bytes

                # Then, on reconnect, the winner takes the original path on the
                # slave and the loser survives as a conflicted copy
                slave2 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
                try:
                    assert _wait_for_text_in_stderr(slave2, "Initial sync complete", timeout=30), (
                        "Restarted slave did not complete its initial sync"
                    )

                    deadline = time.time() + 30
                    conflict_path = None
                    while time.time() < deadline:
                        candidates = [
                            p
                            for p in Path(lan_slave_dir).iterdir()
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
                    assert (Path(lan_slave_dir) / "shared.txt").read_bytes() == winner_bytes, (
                        "The deterministic hash winner did not take the original path "
                        "after reconnect"
                    )
                    assert slave2.poll() is None, "Restarted slave exited unexpectedly"

                    # ── Post-conflict convergence ────────────────────────────
                    # Once the conflict is resolved, a follow-up one-sided edit
                    # must propagate as a plain update: the merged history makes
                    # it causally dominant, so the slave adopts it silently and
                    # the existing conflicted copy is neither duplicated nor
                    # re-flagged.
                    follow_up = b"follow-up v3\n"
                    (Path(lan_master_dir) / "shared.txt").write_bytes(follow_up)
                    os.utime(str(Path(lan_master_dir) / "shared.txt"), (shared_ts, shared_ts))

                    deadline = time.time() + 30
                    while time.time() < deadline:
                        if (Path(lan_slave_dir) / "shared.txt").read_bytes() == follow_up:
                            break
                        time.sleep(0.5)
                    assert (Path(lan_slave_dir) / "shared.txt").read_bytes() == follow_up, (
                        "The follow-up edit did not reach the slave after the conflict"
                    )
                    copies_after = [
                        p
                        for p in Path(lan_slave_dir).iterdir()
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
        self, lan_master_dir, lan_slave_dir, lan_bucket
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
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            (Path(lan_master_dir) / "shared.txt").write_bytes(b"base\n")
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            slave1 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # The slave's initial full sync must complete so the common
                # ancestor (merge base) is persisted in its sync-state.
                assert _wait_for_client_ready(slave1, _lan_ready_path(lan_slave_dir), timeout=30), (
                    "Slave did not finish its initial sync"
                )
                assert (Path(lan_slave_dir) / "shared.txt").read_bytes() == b"base\n"

                # Both peers diverge while the slave is offline. The payloads
                # are chosen so the *slave* wins the deterministic SHA-256
                # tie-break (sha("slave-wins-v2") > sha("master-loses-v2")).
                _terminate(slave1)
                master_bytes = b"master-loses-v2\n"
                slave_bytes = b"slave-wins-v2\n"
                (Path(lan_master_dir) / "shared.txt").write_bytes(master_bytes)
                (Path(lan_slave_dir) / "shared.txt").write_bytes(slave_bytes)
                shared_ts = 1_700_000_000
                os.utime(str(Path(lan_master_dir) / "shared.txt"), (shared_ts, shared_ts))
                os.utime(str(Path(lan_slave_dir) / "shared.txt"), (shared_ts, shared_ts))

                # On reconnect, the slave reconciles its divergence: the master
                # must adopt the slave's content at the shared path and keep
                # its own losing version beside it as a conflicted copy.
                slave2 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
                try:
                    assert _wait_for_text_in_stderr(slave2, "Initial sync complete", timeout=30), (
                        "Restarted slave did not complete its initial sync"
                    )

                    deadline = time.time() + 30
                    while time.time() < deadline:
                        if (Path(lan_master_dir) / "shared.txt").read_bytes() == slave_bytes:
                            break
                        time.sleep(0.5)
                    assert (Path(lan_master_dir) / "shared.txt").read_bytes() == slave_bytes, (
                        "The winner (slave) content did not take the original path "
                        f"on the master.\n--- master log ---\n{_stderr_text(master)}"
                    )

                    deadline = time.time() + 30
                    conflict_path = None
                    while time.time() < deadline:
                        candidates = [
                            p
                            for p in Path(lan_master_dir).iterdir()
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
                    assert (Path(lan_slave_dir) / "shared.txt").read_bytes() == slave_bytes, (
                        "The slave's winning version did not stay on the original path"
                    )
                    slave_copies = [
                        p
                        for p in Path(lan_slave_dir).iterdir()
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


class TestLanConcurrency:
    def test_concurrent_file_writes_on_both_peers(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Master and slave write different files concurrently → both converge."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # Wait for initial sync
                _wait_for_text_in_stderr(slave, "Initial sync complete", timeout=30)

                # Both peers write files at the same time
                (Path(lan_master_dir) / "from_master.txt").write_bytes(b"master-data\n")
                (Path(lan_slave_dir) / "from_slave.txt").write_bytes(b"slave-data\n")

                # Master file should appear on slave
                assert _wait_for_file(str(Path(lan_slave_dir) / "from_master.txt"), timeout=30), (
                    "Master file did not reach slave"
                )
                # Slave file should appear on master
                assert _wait_for_file(str(Path(lan_master_dir) / "from_slave.txt"), timeout=30), (
                    "Slave file did not reach master"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)
