# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box tests for P2P feature flags, group filtering, and edge cases.

Requires HSYNC_BB_P2P=1 to run.
"""

import os
import time
from pathlib import Path

import pytest
from hsync_common.labels import ActionLabel

from .p2p_helpers import (
    _p2p_ready_path,
    _p2p_start,
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
# P2P group filtering
# =============================================================================


class TestP2PGroupFiltering:
    def test_different_p2p_groups_dont_discover(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        """Peers in different P2P groups should not discover each other."""
        # Given a master in group-alpha
        master = _p2p_start(
            p2p_master_dir,
            p2p_bucket,
            prefer_master=True,
            extra_args=("--group", "group-alpha"),
        )
        try:
            # The temp slave must join the master's group or it is invisible.
            _wait_for_master(
                master, p2p_slave_dir, p2p_bucket, slave_extra_args=("--group", "group-alpha")
            )

            # When a slave starts in a different group
            slave = _p2p_start(
                p2p_slave_dir,
                p2p_bucket,
                never_master=True,
                extra_args=("--group", "group-beta"),
            )
            try:
                # Then it never discovers the master (file never syncs)
                (Path(p2p_master_dir) / "group_test.txt").write_bytes(b"group\n")
                time.sleep(5)
                assert _wait_for_text_in_stderr(
                    slave, "No other peers discovered", timeout=10
                ) or _wait_for_text_in_stderr(slave, "not found", timeout=5)
            finally:
                _terminate(slave)
        finally:
            _terminate(master)

    def test_same_p2p_group_discovers_and_syncs(
        self, p2p_master_dir, p2p_slave_dir, p2p_bucket, bb_sample
    ):
        """Peers in the same P2P group discover each other and sync (positive
        counterpart of test_different_p2p_groups_dont_discover)."""
        # Given a master and slave in the same P2P group
        master = _p2p_start(
            p2p_master_dir,
            p2p_bucket,
            prefer_master=True,
            extra_args=("--group", "group-alpha"),
        )
        try:
            _wait_for_master(
                master, p2p_slave_dir, p2p_bucket, slave_extra_args=("--group", "group-alpha")
            )

            slave = _p2p_start(
                p2p_slave_dir,
                p2p_bucket,
                never_master=True,
                extra_args=("--group", "group-alpha"),
            )
            try:
                # Wait for the slave's watcher to be live before creating files
                assert _wait_for_slave_events(slave), "slave did not reach [READY]"

                # When a file is created on the master
                s = bb_sample(prefix="grp", size=0)
                content = b"same-group\n"
                (Path(p2p_master_dir) / s.name).write_bytes(content)

                # Then the slave in the same group receives it
                assert _wait_for_file(str(Path(p2p_slave_dir) / s.name), timeout=30), (
                    f"file did not sync within the same group. {s.ctx}"
                )
                assert (Path(p2p_slave_dir) / s.name).read_bytes() == content
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Compression / delta / autotune feature flags
# =============================================================================


class TestP2PCompression:
    def test_compression_disabled_still_syncs(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        """--no-compression master: sync still works, compression just disabled."""
        # Given a master running with --no-compression
        master = _p2p_start(
            p2p_master_dir,
            p2p_bucket,
            prefer_master=True,
            extra_args=("--no-compression",),
        )
        try:
            _wait_for_master(master, p2p_slave_dir, p2p_bucket)

            slave = _p2p_start(p2p_slave_dir, p2p_bucket, never_master=True)
            try:
                # When a file is created on the master
                content = b"no-compression\n"
                (Path(p2p_master_dir) / "nocmp.txt").write_bytes(content)

                # Then it syncs despite compression being disabled
                assert _wait_for_file(str(Path(p2p_slave_dir) / "nocmp.txt"), timeout=30), (
                    "File did not sync with compression disabled"
                )
                assert (Path(p2p_slave_dir) / "nocmp.txt").read_bytes() == content
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestP2PDelta:
    def test_delta_disabled_still_syncs(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        """--no-delta master: sync still works, delta just disabled."""
        # Given a master running with --no-delta
        master = _p2p_start(
            p2p_master_dir,
            p2p_bucket,
            prefer_master=True,
            extra_args=("--no-delta",),
        )
        try:
            _wait_for_master(master, p2p_slave_dir, p2p_bucket)

            slave = _p2p_start(p2p_slave_dir, p2p_bucket, never_master=True)
            try:
                # When a file is created on the master
                content = b"no-delta\n"
                (Path(p2p_master_dir) / "nodlt.txt").write_bytes(content)

                # Then it syncs despite delta being disabled
                assert _wait_for_file(str(Path(p2p_slave_dir) / "nodlt.txt"), timeout=30), (
                    "File did not sync with delta disabled"
                )
                assert (Path(p2p_slave_dir) / "nodlt.txt").read_bytes() == content
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestP2PPeriodicResync:
    # Two independent timing budgets (60 s to complete the first sync, then a
    # 20 s interval inside a 40 s window) on a suite whose multicast discovery
    # is environment-sensitive; retried like the other election-timing tests.
    @pytest.mark.flaky(reruns=3, reruns_delay=5)
    def test_periodic_resync_fires(self, p2p_master_dir, p2p_slave_dir, p2p_bucket, bb_sample):
        """Slave with --interval triggers a periodic full resync after the
        configured interval, observable in its log output."""
        # Given a serving master and a slave with --interval 20
        master = _p2p_start(p2p_master_dir, p2p_bucket, prefer_master=True)
        try:
            _wait_for_master(master, p2p_slave_dir, p2p_bucket)

            slave = _p2p_start(
                p2p_slave_dir,
                p2p_bucket,
                never_master=True,
                extra_args=("--interval", "20"),
            )
            try:
                # Wait for the slave's initial sync to complete (ready-file)
                assert _wait_for_client_ready(slave, _p2p_ready_path(p2p_slave_dir), timeout=60), (
                    f"slave did not complete initial sync. --- slave log ---\n{_stderr_text(slave)}"
                )

                # When a file is created on the master
                s = bb_sample(prefix="periodic", size=0)
                content = b"periodic\n"
                (Path(p2p_master_dir) / s.name).write_bytes(content)

                # Then the slave receives it (pipeline is warm)
                assert _wait_for_file(str(Path(p2p_slave_dir) / s.name), timeout=30), (
                    f"file did not sync to slave. {s.ctx}"
                )

                # And after the interval elapses the periodic resync fires,
                # observable as the "P2P force resync" log line on the slave
                assert _wait_for_text_in_stderr(slave, "P2P force resync", timeout=40), (
                    "periodic resync did not fire within the interval. "
                    f"{s.ctx}\n--- slave log ---\n{_stderr_text(slave)}"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Edge cases
# =============================================================================


class TestP2PHiddenFiles:
    def test_default_ignored_files_not_synced(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        """Files matched by the built-in default ignore patterns never sync.

        Plain dotfiles (``.env``-style) are synced by design (industry
        standard: Syncthing, rsync, Nextcloud) — only the built-in patterns
        (OS caches, editor/office temp files, ``*.tmp`` …) are filtered. See
        ``hsync_common.constants.DEFAULT_IGNORE_PATTERNS``.
        """
        # Given a master with one file per default-ignored family
        master = _p2p_start(p2p_master_dir, p2p_bucket, prefer_master=True)
        try:
            _wait_for_master(master, p2p_slave_dir, p2p_bucket)

            (Path(p2p_master_dir) / ".DS_Store").write_bytes(b"macos\n")
            (Path(p2p_master_dir) / "Thumbs.db").write_bytes(b"windows\n")
            (Path(p2p_master_dir) / "notes.tmp").write_bytes(b"temp\n")

            slave = _p2p_start(p2p_slave_dir, p2p_bucket, never_master=True)
            try:
                _wait_for_text_in_stderr(slave, "Initial sync complete", timeout=30)
                time.sleep(5)

                # Then none of the default-ignored files reach the slave
                for name in (".DS_Store", "Thumbs.db", "notes.tmp"):
                    assert not (Path(p2p_slave_dir) / name).exists(), (
                        f"Default-ignored file {name} was synced (should be ignored)"
                    )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestP2PKeyless:
    def test_keyless_unattended_write_is_refused(self, p2p_master_dir, p2p_bucket):
        """Keyless + write-capable + non-interactive is refused at startup.

        The write policy (``p2p_write_requires_key``) requires a shared
        ``HSYNC_P2P_KEY`` or the explicit dangerous opt-in whenever a run may
        upload to a master. An unattended P2P run without either must therefore
        exit immediately with the policy error.
        """
        # Given a write-capable keyless master in unattended mode
        master = _p2p_start(
            p2p_master_dir,
            p2p_bucket,
            prefer_master=True,
            extra_env={"HSYNC_P2P_KEY": None},
        )
        try:
            # Then it is refused at startup with the write-policy error. The
            # assertion anchors on the SSOT marker ([AUTH REQUIRED], see
            # hsync_common/docs/log-labels.md) rather than the surrounding
            # prose, which is free to change without breaking the contract.
            refused = _wait_for_text_in_stderr(master, ActionLabel.AUTH_REQUIRED, timeout=15)
            assert refused, "Keyless unattended write-capable run should hit the write policy"
            # The error line can be drained before the process finishes exiting.
            master.wait(timeout=5)
            assert master.returncode == 1, "Policy refusal must exit non-zero"
        finally:
            _terminate(master)

    def test_keyless_download_only_node_is_allowed(self, p2p_slave_dir, p2p_bucket):
        """A keyless download-only node passes the write policy and, with the
        explicit keyless acknowledgement, keeps running.

        The node starts, runs discovery and keeps waiting for peers — the
        point is that the run is *allowed* without HSYNC_P2P_KEY once the
        posture is acknowledged (no write-policy error) and stays alive.
        """
        # Given a keyless download-only node with the explicit acknowledgement
        node = _p2p_start(
            p2p_slave_dir,
            p2p_bucket,
            never_master=True,
            extra_args=("--sync-direction", "download-only", "--allow-unauthenticated-p2p"),
            extra_env={"HSYNC_P2P_KEY": None},
        )
        try:
            # Then it completes discovery (no write-policy error) and stays alive
            alone = _wait_for_text_in_stderr(node, "No other peers discovered", timeout=30)
            assert alone, "Download-only keyless node should complete a discovery round"
            assert ActionLabel.AUTH_REQUIRED not in _stderr_text(node), (
                "Download-only keyless run must not trip the write policy"
            )
            assert node.poll() is None, "The node should still be alive, waiting for peers"
        finally:
            _terminate(node)


# =============================================================================
# Bucket isolation (two buckets on one host must not cross-sync)
# =============================================================================


class TestP2PBucketIsolation:
    def test_peers_in_different_buckets_do_not_cross_sync(
        self, p2p_master_dir, p2p_slave_dir, p2p_bucket, tmp_path, bb_sample
    ):
        """A file on bucket A never reaches a peer that is only on bucket B.

        mDNS announces ``{bucket}-{cid}`` and the handshake filters by bucket, so
        two peers that share a host but not a bucket must not discover or sync
        each other. The positive half (bucket A's own slave receives the file)
        proves the three peers share a reachable transport; the negative half
        then shows the other bucket's peer stayed untouched well past the
        convergence bucket A already observed.
        """
        import uuid

        other_bucket = f"lan-bb-{uuid.uuid4().hex[:10]}"
        other_dir = tmp_path / "other"

        # Given a prefer-master and a never-master peer sharing bucket A
        master = _p2p_start(p2p_master_dir, p2p_bucket, prefer_master=True)
        try:
            _wait_for_master(master, p2p_slave_dir, p2p_bucket)
            slave = _p2p_start(p2p_slave_dir, p2p_bucket, never_master=True)
            try:
                # And an unrelated peer on a different bucket, on the same host
                other_dir.mkdir()
                other = _p2p_start(str(other_dir), other_bucket, never_master=True)
                try:
                    # When a file is created on the bucket-A master
                    sample = bb_sample(prefix="isol")
                    (Path(p2p_master_dir) / sample.name).write_bytes(b"bucket-a only\n")

                    # Then it reaches the bucket-A slave...
                    assert _wait_for_file(str(Path(p2p_slave_dir) / sample.name), timeout=30), (
                        f"bucket-A file did not sync to its own slave. {sample.ctx}"
                    )

                    # ...but never the peer that is only on bucket B
                    assert other.poll() is None, "the other-bucket peer exited early"
                    assert not _wait_for_file(str(other_dir / sample.name), timeout=6), (
                        "buckets are not isolated: the other-bucket peer received a "
                        f"bucket-A file. {sample.ctx}"
                    )
                finally:
                    _terminate(other)
            finally:
                _terminate(slave)
        finally:
            _terminate(master)
