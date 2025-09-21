# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for LAN authentication and sync-direction enforcement.

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
    _lan_ready_path,
    _lan_start,
    _terminate,
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
        env_master = {
            **os.environ,
            "HSYNC_LAN_KEY": _LAN_KEY,
            "PYTHONPATH": _HSYNC_COMMON_DIR,
            "HSYNC_READY_FILE": _lan_ready_path(lan_master_dir),
        }
        master = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "hsync_client",
                "--lan-mode",
                "--force-master",
                "-b",
                lan_bucket,
                "-p",
                lan_master_dir,
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
            ],
            env=env_master,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            # Start slave with a DIFFERENT LAN key
            env_slave = {
                **os.environ,
                "HSYNC_LAN_KEY": "wrong-key-xxxxxxxx",
                "PYTHONPATH": _HSYNC_COMMON_DIR,
                "HSYNC_READY_FILE": _lan_ready_path(lan_slave_dir),
            }
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
                ],
                env=env_slave,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                # Slave should be rejected (auth error in master or disconnect)
                auth_failed = _wait_for_text_in_stderr(
                    master, "Unauthorized", timeout=15
                ) or _wait_for_text_in_stderr(slave, "Unauthorized", timeout=5)
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

            env = {
                **os.environ,
                "HSYNC_LAN_KEY": _LAN_KEY,
                "PYTHONPATH": _HSYNC_COMMON_DIR,
                "HSYNC_READY_FILE": _lan_ready_path(lan_slave_dir),
            }
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
                    "--sync-direction",
                    "download-only",
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
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

            env = {
                **os.environ,
                "HSYNC_LAN_KEY": _LAN_KEY,
                "PYTHONPATH": _HSYNC_COMMON_DIR,
                "HSYNC_READY_FILE": _lan_ready_path(lan_slave_dir),
            }
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
                    "--sync-direction",
                    "upload-only",
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
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
                _wait_for_master(master2, lan_slave_dir, lan_bucket)

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
