# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for LAN advanced topology, filtering, throttling, and file attributes.

Requires HSYNC_BB_LAN=1 to run.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .lan_helpers import (
    _lan_env,
    _lan_start,
    _stderr_text,
    _terminate,
    _wait_for_file,
    _wait_for_file_gone,
    _wait_for_master,
    _wait_for_slave_events,
    _wait_for_text_in_stderr,
)

_LAN_ENABLED = os.environ.get("HSYNC_BB_LAN") == "1"
_LAN_REASON = (
    "LAN e2e needs loopback mDNS + two live client instances; set HSYNC_BB_LAN=1 to enable"
)

pytestmark = pytest.mark.skipif(not _LAN_ENABLED, reason=_LAN_REASON)


# =============================================================================
# Tombstone propagation (offline deletion catch-up)
# =============================================================================


class TestLanTombstone:
    def test_tombstone_propagation_on_reconnect(
        self, lan_master_dir, lan_slave_dir, tmp_path, lan_bucket
    ):
        """File deleted while slave is offline → tombstone cleans it up on reconnect."""
        bridge_dir = str(tmp_path / "bridge")
        Path(bridge_dir).mkdir()

        # Create a file on master before anyone connects
        (Path(lan_master_dir) / "pre_delete.txt").write_bytes(b"to-be-deleted\n")

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            # Phase 1: first slave syncs the file, then disconnects
            slave1 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_file(str(Path(lan_slave_dir) / "pre_delete.txt"), timeout=30), (
                    "File did not sync from master to slave during initial sync"
                )
            finally:
                _terminate(slave1)

            # Phase 2: start a bridge peer so the master becomes the active
            # master and starts its file watcher. Wait for the watcher to be
            # running before deleting.
            bridge = _lan_start(bridge_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_text_in_stderr(master, "File watcher started", timeout=30), (
                    "Master file watcher did not start"
                )
                # Readiness threshold: servers + watcher are up, the master is
                # serving — the observable [READY] line must follow.
                assert _wait_for_text_in_stderr(master, "[READY]", timeout=30), (
                    "Master did not reach the [READY] threshold"
                )
                # Delete the file on master — the watcher detects it and
                # records a tombstone
                (Path(lan_master_dir) / "pre_delete.txt").unlink()
                assert _wait_for_text_in_stderr(master, "[LOCAL DELETE]", timeout=15), (
                    "Master did not detect the deletion and record a tombstone"
                )
            finally:
                _terminate(bridge)

            # Phase 3: reconnect slave — the initial sync reconciliation
            # serves the tombstone and deletes the stale file locally
            slave2 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_file_gone(
                    str(Path(lan_slave_dir) / "pre_delete.txt"), timeout=30
                ), "Deleted file still present on slave after reconnect (tombstone missed)"
            finally:
                _terminate(slave2)
        finally:
            _terminate(master)

    def test_slave_deletion_survives_master_restart(
        self, lan_master_dir, lan_slave_dir, lan_bucket
    ):
        """Slave deletes while the master is down; on reconnect the deletion
        propagates to the master instead of resurrecting the file on the slave.

        Regression for the reported scenario: A/B killed, AAA deleted on the
        still-running (or freshly reconnecting) C, A restarted with the same
        identity — AAA and its file previously reappeared on C. The deletion
        must survive locally (causal tombstone, ADR-0003) and the stale remote
        copy must be deleted, never downloaded back.
        """
        import shutil

        aaa = Path(lan_master_dir) / "AAA"
        aaa.mkdir(parents=True)
        (aaa / "test.txt").write_bytes(b"delete-me\n")

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # Phase 1: the file syncs to the slave (its merge base is recorded).
                assert _wait_for_file(str(Path(lan_slave_dir) / "AAA" / "test.txt"), timeout=60), (
                    "File did not sync from master to slave during initial sync"
                )
                assert _wait_for_text_in_stderr(slave, "Initial sync complete", timeout=60), (
                    "Slave did not complete the initial sync"
                )

                # Phase 2: master dies. Wait until the slave's watcher is fully
                # stopped so the deletion below happens while the slave is
                # offline and the watched tree is cold.
                _terminate(master)
                assert _wait_for_text_in_stderr(slave, "Client file watcher stopped", timeout=30), (
                    "Slave watcher did not stop after the master went down"
                )

                # Phase 3: delete the whole AAA tree on the slave while offline.
                shutil.rmtree(Path(lan_slave_dir) / "AAA")

                # Phase 4: master restarts with the same identity (same device
                # dir), so the slave's TOFU pin stays valid and it reconnects.
                master2 = _lan_start(lan_master_dir, lan_bucket, force_master=True)
                try:
                    # The slave records the offline deletion and the stale
                    # remote copy is deleted instead of being downloaded back.
                    assert _wait_for_text_in_stderr(slave, "[TOMBSTONE]", timeout=90), (
                        "Slave did not reconcile the deletion via tombstone\n"
                        f"--- slave log tail ---\n{_stderr_text(slave)[-3000:]}"
                    )
                    assert _wait_for_file_gone(
                        str(Path(lan_master_dir) / "AAA" / "test.txt"), timeout=60
                    ), "Deleted file still present on master after reconnect (delete_remote missed)"
                    # The slave must not resurrect the deleted file.
                    time.sleep(3)
                    assert not (Path(lan_slave_dir) / "AAA" / "test.txt").exists(), (
                        "Deleted file resurrected on the slave after reconnect"
                    )
                finally:
                    _terminate(master2)
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Multi-slave topology (3 peers)
# =============================================================================


class TestLanMultiSlave:
    def test_multiple_slaves_converge(self, lan_master_dir, lan_slave_dir, tmp_path, lan_bucket):
        """Master + 2 slaves: file from slave1 appears on slave2 via master."""
        slave2_dir = str(tmp_path / "slave2")
        Path(slave2_dir).mkdir()

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            # Start both slaves first so the election runs (master only emits
            # "MASTER" after discovering a peer via mDNS).
            slave1 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            slave2 = _lan_start(slave2_dir, lan_bucket, never_master=True)
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )
            try:
                # Wait for both event channels: slave1's watcher must be live
                # to upload and slave2 must be listening to receive the
                # broadcast (see _wait_for_slave_events).
                assert _wait_for_slave_events(slave1), "Slave1 is not listening for events"
                assert _wait_for_slave_events(slave2), "Slave2 is not listening for events"

                # Slave1 creates a file
                (Path(lan_slave_dir) / "from_slave1.txt").write_bytes(b"via-master\n")

                # It should appear on both master and slave2
                assert _wait_for_file(str(Path(lan_master_dir) / "from_slave1.txt"), timeout=30), (
                    "Slave1 file did not reach master"
                )
                assert _wait_for_file(str(Path(slave2_dir) / "from_slave1.txt"), timeout=30), (
                    "Slave1 file did not reach slave2 via master"
                )

                assert (Path(slave2_dir) / "from_slave1.txt").read_bytes() == b"via-master\n"
            finally:
                _terminate(slave1)
                _terminate(slave2)
        finally:
            _terminate(master)


# =============================================================================
# Persistent mode control
# =============================================================================


class TestLanPersistent:
    def test_no_lan_persistent_exits_when_alone(self, lan_master_dir, lan_bucket):
        """--no-lan-persistent exits instead of waiting when no peers are found."""
        env = _lan_env(lan_master_dir)
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "hsync_client",
                "--lan-mode",
                "-b",
                lan_bucket,
                "-p",
                lan_master_dir,
                "--yes",
                "--min-free-disk-space",
                "0",
                "--no-autotune",
                "--discovery-timeout",
                "5",
                "--retry-delay",
                "1",
                "--stability-delay",
                "1",
                "--no-lan-persistent",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            # Should exit (not hang) within ~15s when no peers found
            try:
                exit_code = proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                pytest.fail("--no-lan-persistent did not exit when no peers were found")
            # Exit code 0 or 5 is acceptable (0 = clean exit, 5 = no peers found;
            # see hsync_client.exitcodes.ExitCode.LAN_NO_PEER)
            assert exit_code in (0, 5), f"Unexpected exit code: {exit_code}"
        finally:
            _terminate(proc)


# =============================================================================
# Port override
# =============================================================================


class TestLanPortOverride:
    def test_lan_port_override(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """--lan-port on master: slave connects on the specified port."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True, lan_port=12345)
        try:
            # Start a slave so the master discovers it and runs the election
            # (master only emits "MASTER" after add_peer triggers _elect_master).
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                    "Master did not become ready"
                )
                # The master log should mention port 12345
                stderr = _stderr_text(master)
                assert "12345" in stderr, "Master did not use the specified port"

                (Path(lan_master_dir) / "port_test.txt").write_bytes(b"custom port\n")
                assert _wait_for_file(str(Path(lan_slave_dir) / "port_test.txt"), timeout=30), (
                    "File did not sync with custom master port"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Filter path (subdirectory sync)
# =============================================================================


class TestLanFilterPath:
    def test_filter_path_restricts_sync(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """--filter-path subdir/ restricts sync to that subdirectory only."""
        # Pre-create two subdirectories on master
        sub_a = Path(lan_master_dir) / "sub_a"
        sub_a.mkdir()
        (sub_a / "a.txt").write_bytes(b"in-a\n")

        sub_b = Path(lan_master_dir) / "sub_b"
        sub_b.mkdir()
        (sub_b / "b.txt").write_bytes(b"in-b\n")

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            # Start a slave so the master discovers it and runs the election.
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            # Start slave with --filter-path sub_a
            slave = _lan_start(
                lan_slave_dir,
                lan_bucket,
                never_master=True,
                extra_args=("--filter-path", "sub_a"),
            )
            try:
                # File in sub_a should appear on slave
                assert _wait_for_file(str(Path(lan_slave_dir) / "sub_a" / "a.txt"), timeout=30), (
                    "File in filtered subdirectory did not sync"
                )
                # File in sub_b should NOT appear on slave
                time.sleep(5)
                assert not (Path(lan_slave_dir) / "sub_b").exists(), (
                    "File outside filter path was synced"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Bandwidth throttle
# =============================================================================


class TestLanBandwidthThrottle:
    def test_bandwidth_throttle_allows_sync(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """--max-bandwidth-download with a generous limit still allows sync."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            # Start a slave so the master discovers it and runs the election.
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            # Start slave with bandwidth limit (10 MB/s — generous enough)
            slave = _lan_start(
                lan_slave_dir,
                lan_bucket,
                never_master=True,
                extra_args=(
                    "--max-bandwidth-download",
                    "10MB",
                    "--max-bandwidth-upload",
                    "10MB",
                ),
            )
            try:
                # Create a moderately sized file on master
                content = b"x" * 50000  # 50 KB
                (Path(lan_master_dir) / "throttled.txt").write_bytes(content)

                slave_file = Path(lan_slave_dir) / "throttled.txt"
                assert _wait_for_file(str(slave_file), timeout=30), (
                    "File did not sync with bandwidth throttle"
                )
                assert slave_file.read_bytes() == content
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# hsyncignore hot-reload
# =============================================================================


class TestLanHsyncignore:
    @pytest.mark.flaky(reruns=3, reruns_delay=5)
    def test_hsyncignore_reload_respected(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Adding .hsyncignore stops syncing matching files but keeps existing copies.

        This encodes the industry-standard "exclude protects, it does not
        delete" contract (rsync ``--exclude`` without ``--delete-excluded``,
        Syncthing ``.stignore``): a newly added ignore pattern must stop
        propagating matching files going forward, but must NOT retroactively
        remove copies already present on a peer. ``.hsyncignore`` is
        engine-owned and never itself synced, so the master applies the rule
        per-node and drops matching files from its listing after the hot-reload.
        """
        # Pre-create one visible file and one that will later be excluded.
        (Path(lan_master_dir) / "include_me.txt").write_bytes(b"visible\n")
        (Path(lan_master_dir) / "exclude_me.log").write_bytes(b"hidden\n")

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            # Start a slave so the master discovers it and runs the election.
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            # First slave session: both files sync (no .hsyncignore yet).
            slave1 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            assert _wait_for_file(str(Path(lan_slave_dir) / "include_me.txt"), timeout=30), (
                "include_me.txt did not sync"
            )
            assert _wait_for_file(str(Path(lan_slave_dir) / "exclude_me.log"), timeout=30), (
                "exclude_me.log did not sync"
            )
            _terminate(slave1)

            # Add .hsyncignore on the master excluding *.log, then create fresh
            # files: one matching the pattern, one not. The master hot-reloads
            # the matcher and must exclude the new *.log from its listing.
            (Path(lan_master_dir) / ".hsyncignore").write_text("*.log\n")
            (Path(lan_master_dir) / "new_exclude.log").write_bytes(b"nope\n")
            (Path(lan_master_dir) / "new_include.txt").write_bytes(b"yes\n")

            # Slave reconnects.
            slave2 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # A new non-matching file syncs normally: proves the reconcile
                # ran and sync is healthy after the .hsyncignore hot-reload.
                assert _wait_for_file(str(Path(lan_slave_dir) / "new_include.txt"), timeout=30), (
                    "new_include.txt did not sync after .hsyncignore reload"
                )

                # The newly excluded file is NOT propagated: the master drops it
                # from its listing after the hot-reload (reload respected).
                assert not _wait_for_file(
                    str(Path(lan_slave_dir) / "new_exclude.log"), timeout=5
                ), "new_exclude.log was synced despite the .hsyncignore *.log rule"

                # Existing copies are preserved, never retroactively deleted:
                # adding an ignore pattern protects matching files, it does not
                # remove copies already present on the peer.
                assert (Path(lan_slave_dir) / "exclude_me.log").exists(), (
                    "exclude_me.log was deleted; an ignore rule must not remove "
                    "already-synced copies on a peer"
                )
                assert (Path(lan_slave_dir) / "include_me.txt").exists(), (
                    "include_me.txt disappeared"
                )
            finally:
                _terminate(slave2)
        finally:
            _terminate(master)


# =============================================================================
# File permissions
# =============================================================================


class TestLanPermissions:
    def test_file_permissions_synced(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """File with 600 perms on master → slave receives 600."""
        import stat

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            # Start a slave so the master discovers it and runs the election.
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            # Create file with specific permissions on master
            f = Path(lan_master_dir) / "perms.txt"
            f.write_bytes(b"permissions-test\n")
            f.chmod(0o600)

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                slave_f = Path(lan_slave_dir) / "perms.txt"
                assert _wait_for_file(str(slave_f), timeout=30), (
                    "File with custom permissions did not sync"
                )
                # Check that permissions were preserved (at least not world-readable)
                mode = slave_f.stat().st_mode
                assert not (mode & stat.S_IROTH), "File should not be world-readable"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Max inbound size guard
# =============================================================================
