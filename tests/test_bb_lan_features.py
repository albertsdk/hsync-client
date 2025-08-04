# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for LAN feature flags, group filtering, and edge cases.

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
    _terminate,
    _wait_for_file,
    _wait_for_text_in_stderr,
)

_LAN_ENABLED = os.environ.get("HSYNC_BB_LAN") == "1"
_LAN_REASON = (
    "LAN e2e needs loopback mDNS + two live client instances; set HSYNC_BB_LAN=1 to enable"
)

pytestmark = pytest.mark.skipif(not _LAN_ENABLED, reason=_LAN_REASON)


# =============================================================================
# LAN group filtering
# =============================================================================


class TestLanGroupFiltering:
    def test_different_lan_groups_dont_discover(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Peers in different LAN groups do not discover each other."""
        # Master in group "red"
        env_red = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY, "PYTHONPATH": _HSYNC_COMMON_DIR}
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
                "--lan-group",
                "red",
            ],
            env=env_red,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # Slave in group "blue" — should NOT discover the red master
            env_blue = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY, "PYTHONPATH": _HSYNC_COMMON_DIR}
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
                    "--lan-group",
                    "blue",
                ],
                env=env_blue,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                # Slave should eventually warn that no peer was found (group mismatch)
                no_peer = _wait_for_text_in_stderr(
                    slave, "No master elected", timeout=30
                ) or _wait_for_text_in_stderr(slave, "alone", timeout=5)
                assert no_peer, "Slave should not have discovered the master in a different group"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Feature flags
# =============================================================================


class TestLanFeatureFlags:
    def test_compression_disabled_still_syncs(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """--no-compression on both peers still allows sync."""
        env = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY, "PYTHONPATH": _HSYNC_COMMON_DIR}
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
                "--no-compression",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

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
                    "--no-compression",
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                (Path(lan_master_dir) / "nocomp.txt").write_bytes(b"no compression\n")
                assert _wait_for_file(str(Path(lan_slave_dir) / "nocomp.txt"), timeout=30), (
                    "File did not sync with --no-compression"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)

    def test_delta_disabled_still_syncs(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """--no-delta on both peers still allows sync."""
        env = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY, "PYTHONPATH": _HSYNC_COMMON_DIR}
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
                "--no-delta",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

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
                    "--no-delta",
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                (Path(lan_master_dir) / "nodelta.txt").write_bytes(b"no delta\n")
                assert _wait_for_file(str(Path(lan_slave_dir) / "nodelta.txt"), timeout=30), (
                    "File did not sync with --no-delta"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)

    def test_periodic_resync_fires(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """--interval 5 triggers a periodic resync logged in stderr."""
        env = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY, "PYTHONPATH": _HSYNC_COMMON_DIR}
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
                "--interval",
                "5",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # The master with --interval should log "Force sync" within ~10s
            resync_fired = _wait_for_text_in_stderr(master, "Force sync", timeout=15)
            assert resync_fired, "Periodic resync did not fire (expected 'Force sync' in stderr)"
        finally:
            _terminate(master)


# =============================================================================
# Edge cases & robustness
# =============================================================================


class TestLanEdgeCases:
    def test_hidden_file_not_synced(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Hidden files (e.g. .hidden) are not synced to the slave."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # Create a hidden file on master
                (Path(lan_master_dir) / ".secret").write_bytes(b"hidden\n")

                # Wait a few seconds — it should NOT appear on slave
                time.sleep(5)
                assert not (Path(lan_slave_dir) / ".secret").exists(), (
                    "Hidden file was synced to slave"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)

    def test_lan_without_key_still_works(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """LAN mode without HSYNC_LAN_KEY still syncs (no auth)."""
        # Neither master nor slave set HSYNC_LAN_KEY
        env_no_key = {**os.environ, "PYTHONPATH": _HSYNC_COMMON_DIR}
        env_no_key.pop("HSYNC_LAN_KEY", None)

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
            env=env_no_key,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

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
                env=env_no_key,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                (Path(lan_master_dir) / "nokey.txt").write_bytes(b"no auth key\n")
                assert _wait_for_file(str(Path(lan_slave_dir) / "nokey.txt"), timeout=30), (
                    "File did not sync without LAN key"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)
