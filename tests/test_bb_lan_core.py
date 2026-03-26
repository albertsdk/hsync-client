# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for LAN core protocol (election, TOFU pinning, pairing, resilience).

Requires HSYNC_BB_LAN=1 to run.
"""

import os
from pathlib import Path

import pytest

from .lan_helpers import (
    _lan_device_dir,
    _lan_ready_path,
    _lan_start,
    _stderr_text,
    _terminate,
    _wait_for_client_ready,
    _wait_for_file,
    _wait_for_master,
    _wait_for_slave_events,
    _wait_for_text_in_stderr,
)

_LAN_ENABLED = os.environ.get("HSYNC_BB_LAN") == "1"
_LAN_REASON = (
    "LAN e2e needs loopback mDNS + two live client instances; set HSYNC_BB_LAN=1 to enable"
)

pytestmark = pytest.mark.skipif(not _LAN_ENABLED, reason=_LAN_REASON)


class TestLanElection:
    @pytest.mark.flaky(reruns=3, reruns_delay=5)
    def test_force_master_wins_and_slave_syncs(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """A force-master client becomes master; a never-master slave syncs to it."""
        # Given a force-master peer (election needs at least two participants)
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            # Wait for the master to register its mDNS service before
            # starting the slave, so the election can proceed immediately
            # when both peers are discovered.
            assert _wait_for_text_in_stderr(master, "Registered mDNS service", timeout=15), (
                "Master mDNS service did not register"
            )
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # Wait for "MASTER" in either peer's logs (election needs both running)
                became_master = _wait_for_text_in_stderr(
                    master, "MASTER", timeout=60
                ) or _wait_for_text_in_stderr(slave, "MASTER", timeout=10)
                assert became_master, "Neither peer became master"

                # When a file is written on the master side
                content = b"master-origin\n"
                (Path(lan_master_dir) / "shared.txt").write_bytes(content)

                # Then it appears on the slave with the same content
                assert _wait_for_file(str(Path(lan_slave_dir) / "shared.txt"), timeout=30), (
                    "File did not sync from master to slave"
                )

                assert (Path(lan_slave_dir) / "shared.txt").read_bytes() == content

                # When a file is written on the slave side
                slave_content = b"slave-origin\n"
                (Path(lan_slave_dir) / "from_slave.txt").write_bytes(slave_content)

                # Then it appears on the master with the same content
                assert _wait_for_file(str(Path(lan_master_dir) / "from_slave.txt"), timeout=30), (
                    "File did not sync from slave to master"
                )

                assert (Path(lan_master_dir) / "from_slave.txt").read_bytes() == slave_content
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestLanTofuPinning:
    def test_first_sight_trusts_and_syncs(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """On first sight the slave TOFU-pins the master cert and syncs."""
        # Given a serving master
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # Wait for the TOFU pin log line
                assert _wait_for_text_in_stderr(
                    slave, "Pinned LAN master certificate", timeout=30
                ), "Slave did not TOFU-pin the master"

                # Wait for the event channel before writing: a file created
                # in the post-initial-sync blind spot (see _wait_for_slave_events)
                # is only recovered by the periodic resync (RESYNC=300s), far
                # beyond the sync timeout below.
                assert _wait_for_slave_events(slave), (
                    "Slave did not connect to the master event channel"
                )

                # Then the slave TOFU-pins the master and syncs a new file
                content = b"tofu-test\n"
                (Path(lan_master_dir) / "tofu.txt").write_bytes(content)
                assert _wait_for_file(str(Path(lan_slave_dir) / "tofu.txt"), timeout=30), (
                    "File did not sync after TOFU pinning"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)

    def test_changed_fingerprint_is_refused(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """When the master's certificate rotates (same device ID), the slave refuses.

        The device identity lives in the device dir (sibling of the synced dir,
        see ``_lan_device_dir``): deleting only the ``*.pem`` files rotates the
        keypair while the device ID — and thus the slave's ``(device_id, bucket)``
        TOFU pin — survives. The slave must detect the fingerprint change and
        refuse the connection.
        """
        # Given a master whose certificate the slave has TOFU-pinned
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            # First slave: trust the certificate (TOFU)
            slave1 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            assert _wait_for_text_in_stderr(slave1, "Pinned LAN master certificate", timeout=30), (
                "First slave did not TOFU-pin the master"
            )
            _terminate(slave1)

            # When the master's keypair is rotated (device ID preserved)
            _terminate(master)
            device_dir = Path(_lan_device_dir(lan_master_dir))
            for pem in device_dir.glob("*.pem"):
                pem.unlink()

            master2 = _lan_start(lan_master_dir, lan_bucket, force_master=True)
            try:
                _wait_for_master(master2, lan_slave_dir, lan_bucket)

                # Then a new slave refuses the changed fingerprint
                slave2 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
                try:
                    refused = _wait_for_text_in_stderr(slave2, "FINGERPRINT CHANGED", timeout=30)
                    assert refused, "Slave should have refused the changed fingerprint"
                finally:
                    _terminate(slave2)
            finally:
                _terminate(master2)
        except BaseException:
            _terminate(master)
            raise


# =============================================================================
# Additional e2e scenarios (HSYNC_BB_LAN=1 required)
# =============================================================================


class TestLanAutomaticElection:
    def test_automatic_election_and_sync(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Two neutral peers: one becomes master, the other slave, sync works."""
        # Given two neutral peers started simultaneously (election via mDNS)
        peer1 = _lan_start(lan_master_dir, lan_bucket)
        peer2 = _lan_start(lan_slave_dir, lan_bucket)
        try:
            # At least one of them logs "MASTER" (the winner)
            master_ready = _wait_for_text_in_stderr(
                peer1, "MASTER", timeout=30
            ) or _wait_for_text_in_stderr(peer2, "MASTER", timeout=5)
            assert master_ready, "Neither peer became master"

            # Wait for BOTH peers to be ready (slave watcher live + handshake
            # done) before mutating: a file written in the post-initial-sync
            # blind spot is only recovered by the periodic resync (300s), far
            # beyond the sync timeout below.
            assert _wait_for_text_in_stderr(peer1, "[READY]", timeout=30), (
                "peer1 did not reach [READY]"
            )
            assert _wait_for_text_in_stderr(peer2, "[READY]", timeout=30), (
                "peer2 did not reach [READY]"
            )

            # When a file is written on the first peer
            content = b"auto-election\n"
            (Path(lan_master_dir) / "auto.txt").write_bytes(content)

            # Then it appears on the second peer
            assert _wait_for_file(str(Path(lan_slave_dir) / "auto.txt"), timeout=30), (
                "File did not sync after automatic election"
            )
        finally:
            _terminate(peer1)
            _terminate(peer2)


class TestLanResilience:
    def test_slave_reconnects_after_disconnect(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Slave reconnects and catches up after being restarted."""
        # Given a serving master
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            # And a slave that has completed its initial sync, then stopped
            slave1 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            _wait_for_text_in_stderr(slave1, "Initial sync complete", timeout=30)
            _terminate(slave1)

            # When a new file is created on the master while the slave is gone
            content = b"catch-up\n"
            (Path(lan_master_dir) / "catch_up.txt").write_bytes(content)

            # Then the restarted slave catches up on reconnection
            slave2 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_file(str(Path(lan_slave_dir) / "catch_up.txt"), timeout=30), (
                    "Slave did not catch up after reconnect"
                )
                assert (Path(lan_slave_dir) / "catch_up.txt").read_bytes() == content
            finally:
                _terminate(slave2)
        finally:
            _terminate(master)

    def test_require_pairing_accepts_pinned_master(
        self, lan_master_dir, lan_slave_dir, lan_bucket, bb_sample
    ):
        """--require-pairing accepts a master whose cert was TOFU-pinned in a
        prior connection (the positive counterpart of the rejection test)."""
        # Given a serving master
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            # And a slave that has already TOFU-pinned the master (no --require-pairing)
            slave1 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_text_in_stderr(
                    slave1, "Pinned LAN master certificate", timeout=30
                ), "First slave did not TOFU-pin the master"
            finally:
                _terminate(slave1)

            # When the slave is restarted with --require-pairing
            slave2 = _lan_start(
                lan_slave_dir,
                lan_bucket,
                never_master=True,
                extra_args=("--require-pairing",),
            )
            try:
                # Then the pinned master is accepted (no pairing rejection)
                assert _wait_for_client_ready(slave2, _lan_ready_path(lan_slave_dir), timeout=60), (
                    "Slave with --require-pairing did not accept the pinned master"
                )

                # Wait for the slave's watcher to be live before creating files
                assert _wait_for_slave_events(slave2), "slave2 did not reach [READY]"

                # And a new file on the master syncs to the slave
                s = bb_sample(prefix="pair", size=0)
                content = b"paired-sync\n"
                (Path(lan_master_dir) / s.name).write_bytes(content)
                assert _wait_for_file(str(Path(lan_slave_dir) / s.name), timeout=30), (
                    f"file did not sync after pairing acceptance. {s.ctx}"
                )
                assert (Path(lan_slave_dir) / s.name).read_bytes() == content
            finally:
                _terminate(slave2)
        finally:
            _terminate(master)


class TestLanNeverMaster:
    def test_never_master_peers_no_election(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Two never-master peers: no election, [NO MASTER] warning appears."""
        # Given two never-master peers
        p1 = _lan_start(lan_master_dir, lan_bucket, never_master=True)
        p2 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
        try:
            # Then neither becomes master and the [NO MASTER] warning fires
            no_master = _wait_for_text_in_stderr(
                p1, "NO MASTER", timeout=90
            ) or _wait_for_text_in_stderr(p2, "NO MASTER", timeout=5)
            assert no_master, "Expected [NO MASTER] warning did not appear"

            # Neither process should log "MASTER" (they're both never-master)
            stderr1 = _stderr_text(p1)
            stderr2 = _stderr_text(p2)
            assert "This client is the MASTER" not in stderr1, "Peer 1 became master"
            assert "This client is the MASTER" not in stderr2, "Peer 2 became master"
        finally:
            _terminate(p1)
            _terminate(p2)


class TestLanStrictPairing:
    def test_require_pairing_rejects_unpinned_master(
        self, lan_master_dir, lan_slave_dir, lan_bucket
    ):
        """--require-pairing without a pre-existing pin rejects the master."""
        # Given a serving master the slave has not yet pinned
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            # When the slave starts with --require-pairing (no prior pin exists)
            slave = _lan_start(
                lan_slave_dir,
                lan_bucket,
                never_master=True,
                extra_args=("--require-pairing",),
            )
            try:
                # Then it refuses the unpinned master with a pairing rejection
                refused = _wait_for_text_in_stderr(slave, "pairing", timeout=30)
                assert refused, (
                    "Slave should have refused the unpinned master with --require-pairing"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)
