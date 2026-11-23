# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box tests for the peer-mesh role switches (``--peer-role`` / ``--no-*``).

Each device participates in the mesh in three independent ways (serve blocks,
fetch from peers, publish inventory). None of them may change whether the
master↔slave replication itself works: a role switch gates *mesh* participation,
never the shared bucket sync. These tests drive the real client CLI and observe
only files on disk.

Requires ``HSYNC_BB_P2P=1`` to run.
"""

import os
from pathlib import Path

import pytest

from .p2p_helpers import (
    _p2p_start,
    _terminate,
    _wait_for_file,
    _wait_for_master,
    _wait_for_slave_events,
    _wait_for_text_in_stderr,
)

_BB_P2P_ENABLED = os.environ.get("HSYNC_BB_P2P") == "1"
_BB_P2P_REASON = (
    "P2P e2e needs loopback mDNS + two live client instances; set HSYNC_BB_P2P=1 to enable"
)

pytestmark = pytest.mark.skipif(not _BB_P2P_ENABLED, reason=_BB_P2P_REASON)


def _master_then_role_slave(p2p_master_dir, p2p_slave_dir, p2p_bucket, *role_args: str):
    """Start a serving master and a role-restricted slave; return both.

    The slave is ``--never-master`` so the election is deterministic, and the
    caller terminates both (the serving master first, the slave before it).
    """
    master = _p2p_start(p2p_master_dir, p2p_bucket, prefer_master=True)
    _wait_for_master(master, p2p_slave_dir, p2p_bucket)
    slave = _p2p_start(
        p2p_slave_dir,
        p2p_bucket,
        never_master=True,
        extra_args=role_args,
    )
    return master, slave


def _wait_initial_sync_and_events(slave) -> None:
    assert _wait_for_text_in_stderr(slave, "Initial sync complete", timeout=30), (
        "slave never finished its initial sync"
    )
    # A change broadcast between the initial listing and the event subscription
    # is in the harness blind spot (see _wait_for_slave_events).
    assert _wait_for_slave_events(slave), "slave is not listening for events yet"


class TestPeerRolesDoNotBreakReplication:
    def test_peer_role_off_still_syncs_from_the_master(
        self, p2p_master_dir, p2p_slave_dir, p2p_bucket
    ):
        """``--peer-role off`` disables mesh participation, not the shared sync.

        The device serves nothing, fetches nothing from peers and publishes no
        inventory, yet the master's copy of the bucket still replicates to it:
        the serving device is the sync source, not a "peer".
        """
        # Given a serving master and a slave with every mesh capability off
        master, slave = _master_then_role_slave(
            p2p_master_dir, p2p_slave_dir, p2p_bucket, "--peer-role", "off"
        )
        try:
            _wait_initial_sync_and_events(slave)

            # When a file is written on the master
            content = b"role-off still replicates\n"
            (Path(p2p_master_dir) / "role_off.txt").write_bytes(content)

            # Then it still reaches the role-off device
            target = str(Path(p2p_slave_dir) / "role_off.txt")
            assert _wait_for_file(target, timeout=30), "role=off blocked the master sync"
            assert Path(target).read_bytes() == content
        finally:
            _terminate(slave)
            _terminate(master)

    def test_peer_role_leech_still_syncs(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        """A leech (fetch only, no serve, no inventory) still receives the bucket."""
        # Given a serving master and a leech slave
        master, slave = _master_then_role_slave(
            p2p_master_dir, p2p_slave_dir, p2p_bucket, "--peer-role", "leech"
        )
        try:
            _wait_initial_sync_and_events(slave)

            # When a file is written on the master
            content = b"leech receives\n"
            (Path(p2p_master_dir) / "leech.txt").write_bytes(content)

            # Then the leech downloads it
            target = str(Path(p2p_slave_dir) / "leech.txt")
            assert _wait_for_file(target, timeout=30), "leech did not receive the file"
            assert Path(target).read_bytes() == content
        finally:
            _terminate(slave)
            _terminate(master)

    def test_peer_role_seed_still_syncs(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        """A seed (serve + inventory, no peer fetch) still receives the bucket.

        The master's full download is not a "peer fetch", so disabling peer
        fetching must not turn a seed into a read-only island.
        """
        # Given a serving master and a seed slave
        master, slave = _master_then_role_slave(
            p2p_master_dir, p2p_slave_dir, p2p_bucket, "--peer-role", "seed"
        )
        try:
            _wait_initial_sync_and_events(slave)

            # When a file is written on the master
            content = b"seed receives from the master\n"
            (Path(p2p_master_dir) / "seed.txt").write_bytes(content)

            # Then the seed still downloads it
            target = str(Path(p2p_slave_dir) / "seed.txt")
            assert _wait_for_file(target, timeout=30), "seed did not receive the file"
            assert Path(target).read_bytes() == content
        finally:
            _terminate(slave)
            _terminate(master)

    def test_a_non_serving_master_still_replicates_to_a_slave(
        self, p2p_master_dir, p2p_slave_dir, p2p_bucket
    ):
        """``--no-serve-peer-blocks`` on the master keeps replication working.

        Serving blocks is egress for the *mesh*; the slave still syncs the
        master's bucket, it just cannot use it as a block source for peer
        downloads.
        """
        # Given a serving master that does not serve peer blocks
        master = _p2p_start(
            p2p_master_dir,
            p2p_bucket,
            prefer_master=True,
            extra_args=("--no-serve-peer-blocks",),
        )
        try:
            _wait_for_master(master, p2p_slave_dir, p2p_bucket)
            slave = _p2p_start(p2p_slave_dir, p2p_bucket, never_master=True)
            try:
                _wait_initial_sync_and_events(slave)

                # When a file is written on the master
                content = b"no-serve still replicates\n"
                (Path(p2p_master_dir) / "no_serve.txt").write_bytes(content)

                # Then the slave still receives it
                target = str(Path(p2p_slave_dir) / "no_serve.txt")
                assert _wait_for_file(target, timeout=30), "a non-serving master broke sync"
                assert Path(target).read_bytes() == content
            finally:
                _terminate(slave)
        finally:
            _terminate(master)
