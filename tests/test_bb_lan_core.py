# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for LAN core protocol (election, TOFU pinning, pairing, resilience).

Requires HSYNC_BB_LAN=1 to run.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .lan_helpers import (
    _HSYNC_COMMON_DIR,
    _LAN_KEY,
    _lan_start,
    _stderr_text,
    _terminate,
    _wait_for_file,
    _wait_for_text_in_stderr,
)

_LAN_ENABLED = os.environ.get("HSYNC_BB_LAN") == "1"
_LAN_REASON = (
    "LAN e2e needs loopback mDNS + two live client instances; set HSYNC_BB_LAN=1 to enable"
)

pytestmark = pytest.mark.skipif(not _LAN_ENABLED, reason=_LAN_REASON)


class TestLanElection:
    def test_force_master_wins_and_slave_syncs(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """A force-master client becomes master; a never-master slave syncs to it."""
        # Start both peers — election needs at least two participants
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        time.sleep(2)  # let master register mDNS before slave discovers
        try:
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # Wait for "MASTER" in either peer's logs (election needs both running)
                became_master = _wait_for_text_in_stderr(
                    master, "MASTER", timeout=60
                ) or _wait_for_text_in_stderr(slave, "MASTER", timeout=10)
                assert became_master, "Neither peer became master"

                # Create a file on the master side
                content = b"master-origin\n"
                (Path(lan_master_dir) / "shared.txt").write_bytes(content)

                # Wait for it to appear on the slave
                assert _wait_for_file(str(Path(lan_slave_dir) / "shared.txt"), timeout=30), (
                    "File did not sync from master to slave"
                )

                # Verify content
                assert (Path(lan_slave_dir) / "shared.txt").read_bytes() == content

                # Create a file on the slave side
                slave_content = b"slave-origin\n"
                (Path(lan_slave_dir) / "from_slave.txt").write_bytes(slave_content)

                # Wait for it to appear on the master
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
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # Wait for the TOFU pin log line
                assert _wait_for_text_in_stderr(
                    slave, "Pinned LAN master certificate", timeout=30
                ), "Slave did not TOFU-pin the master"

                # File sync as smoke test
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
        """When the master's fingerprint changes, the slave refuses to connect."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # First slave: trust the certificate (TOFU)
            slave1 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            _wait_for_text_in_stderr(slave1, "Pinned LAN master certificate", timeout=30)
            _terminate(slave1)

            # Stop master, recreate with a new cert
            _terminate(master)

            # Remove the master dir and start fresh master (new cert generated)
            import shutil

            shutil.rmtree(lan_master_dir, ignore_errors=True)
            Path(lan_master_dir).mkdir()

            master2 = _lan_start(lan_master_dir, lan_bucket, force_master=True)
            try:
                assert _wait_for_text_in_stderr(master2, "MASTER", timeout=30), (
                    "Second master did not become ready"
                )

                # Second slave: should refuse the changed fingerprint
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


class TestLanHttpsOnly:
    def test_master_without_cert_fingerprint_refused(self):
        """A slave refuses a master that does not advertise a cert fingerprint.

        This is currently enforced unconditionally by the LAN handler:
        there is no plain-HTTP downgrade path.
        """
        # This test is inherently satisfied by the protocol design:
        # _run_as_slave (lan_handler.py:461) checks
        # `not (master.use_https and master.cert_fingerprint)` and
        # refuses.  An e2e verification would require a crafty master
        # stub that advertises via mDNS without a fingerprint, which is
        # not possible with the real client (it always generates one).
        pytest.skip("HTTPS-only enforcement verified by unit tests in test_lan_handler_advanced.py")


# =============================================================================
# Additional e2e scenarios (HSYNC_BB_LAN=1 required)
# =============================================================================


class TestLanAutomaticElection:
    def test_automatic_election_and_sync(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Two neutral peers: one becomes master, the other slave, sync works."""
        # Start both simultaneously — election happens via mDNS
        peer1 = _lan_start(lan_master_dir, lan_bucket)
        peer2 = _lan_start(lan_slave_dir, lan_bucket)
        try:
            # At least one of them logs "MASTER" (the winner)
            master_ready = _wait_for_text_in_stderr(
                peer1, "MASTER", timeout=30
            ) or _wait_for_text_in_stderr(peer2, "MASTER", timeout=5)
            assert master_ready, "Neither peer became master"

            # File written on the first peer should appear on the second
            content = b"auto-election\n"
            (Path(lan_master_dir) / "auto.txt").write_bytes(content)
            assert _wait_for_file(str(Path(lan_slave_dir) / "auto.txt"), timeout=30), (
                "File did not sync after automatic election"
            )
        finally:
            _terminate(peer1)
            _terminate(peer2)


class TestLanResilience:
    def test_slave_reconnects_after_disconnect(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Slave reconnects and catches up after being restarted."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # First slave session
            slave1 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            _wait_for_text_in_stderr(slave1, "Initial sync complete", timeout=30)
            _terminate(slave1)

            # Master creates a new file while slave is gone
            content = b"catch-up\n"
            (Path(lan_master_dir) / "catch_up.txt").write_bytes(content)

            # Restart slave
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


class TestLanNeverMaster:
    def test_never_master_peers_no_election(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Two never-master peers: no election, [NO MASTER] warning appears."""
        p1 = _lan_start(lan_master_dir, lan_bucket, never_master=True)
        p2 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
        try:
            # Neither should become master; after ~60s the [NO MASTER] warning fires
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
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # Start slave with --require-pairing (no prior pin exists)
            env = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY, "PYTHONPATH": _HSYNC_COMMON_DIR}
            slave = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "hsync_client",
                    "--lan-mode",
                    "--never-master",
                    "-b",
                    lan_bucket,
                    "-p",
                    lan_slave_dir,
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
                    "--require-pairing",
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                # Should see pairing_needed / unpinned certificate rejection
                refused = _wait_for_text_in_stderr(slave, "pairing", timeout=30)
                assert refused, (
                    "Slave should have refused the unpinned master with --require-pairing"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)
