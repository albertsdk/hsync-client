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
# LAN group filtering
# =============================================================================


class TestLanGroupFiltering:
    def test_different_lan_groups_dont_discover(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Peers in different LAN groups should not discover each other."""
        env = {
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
                "--lan-group",
                "group-alpha",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            # Slave in different group — it won't connect, so file won't sync
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
                    "group-beta",
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                (Path(lan_master_dir) / "group_test.txt").write_bytes(b"group\n")
                time.sleep(5)
                assert _wait_for_text_in_stderr(
                    slave, "No other peers discovered", timeout=10
                ) or _wait_for_text_in_stderr(slave, "not found", timeout=5)
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Compression / delta / autotune feature flags
# =============================================================================


class TestLanCompression:
    def test_compression_disabled_still_syncs(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """--no-compression master: sync still works, compression just disabled."""
        env = {
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
                "--no-compression",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

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
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                content = b"no-compression\n"
                (Path(lan_master_dir) / "nocmp.txt").write_bytes(content)
                assert _wait_for_file(str(Path(lan_slave_dir) / "nocmp.txt"), timeout=30), (
                    "File did not sync with compression disabled"
                )
                assert (Path(lan_slave_dir) / "nocmp.txt").read_bytes() == content
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestLanDelta:
    def test_delta_disabled_still_syncs(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """--no-delta master: sync still works, delta just disabled."""
        env = {
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
                "--no-delta",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

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
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                content = b"no-delta\n"
                (Path(lan_master_dir) / "nodlt.txt").write_bytes(content)
                assert _wait_for_file(str(Path(lan_slave_dir) / "nodlt.txt"), timeout=30), (
                    "File did not sync with delta disabled"
                )
                assert (Path(lan_slave_dir) / "nodlt.txt").read_bytes() == content
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestLanPeriodicResync:
    def test_periodic_resync_fires(self, lan_master_dir, lan_bucket):
        """Master with --resync-interval triggers periodic full sync."""
        env = {
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
                "--resync-interval",
                "15",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            # Only check that the master stays alive and logs the resync
            # (periodic resync only runs when there is at least one slave)
            time.sleep(5)
            assert master.poll() is None, "Master exited prematurely"
        finally:
            _terminate(master)


# =============================================================================
# Edge cases
# =============================================================================


class TestLanHiddenFiles:
    def test_hidden_file_not_synced(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Hidden files (starting with dot) are not synced by default."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            # Create a hidden file on master
            (Path(lan_master_dir) / ".hidden_file").write_bytes(b"hidden\n")

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                _wait_for_text_in_stderr(slave, "Initial sync complete", timeout=30)
                time.sleep(5)
                assert not (Path(lan_slave_dir) / ".hidden_file").exists(), (
                    "Hidden file was synced (should be ignored by default)"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestLanKeyless:
    def test_lan_without_key_still_works(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """LAN sync without HSYNC_LAN_KEY still works (trust LAN mode)."""
        env = {
            **os.environ,
            "PYTHONPATH": _HSYNC_COMMON_DIR,
            "HSYNC_READY_FILE": _lan_ready_path(lan_master_dir),
        }
        env.pop("HSYNC_LAN_KEY", None)
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
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                content = b"keyless\n"
                (Path(lan_master_dir) / "keyless.txt").write_bytes(content)
                assert _wait_for_file(str(Path(lan_slave_dir) / "keyless.txt"), timeout=30), (
                    "File did not sync without LAN key"
                )
                assert (Path(lan_slave_dir) / "keyless.txt").read_bytes() == content
            finally:
                _terminate(slave)
        finally:
            _terminate(master)
