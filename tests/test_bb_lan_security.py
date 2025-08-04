# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for LAN security guards, dry-run, data integrity, and logging.

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
# Max inbound size guard
# =============================================================================


class TestLanMaxInbound:
    def test_max_inbound_size_blocks_large_file(self):
        """--max-inbound-size e2e requires multi-MB file transfers.

        Functionality verified by unit tests in test_security_inbound.py.
        """
        pytest.skip(
            "max-inbound-size e2e requires multi-MB file; functionality verified by unit tests"
        )


# =============================================================================
# Dry run
# =============================================================================


class TestLanDryRun:
    def test_dry_run_no_files_transferred(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """--dry-run on slave: preview shown, no files transferred."""
        # Pre-create a file on master
        (Path(lan_master_dir) / "should_not_sync.txt").write_bytes(b"dry-run\n")

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

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
                    "--dry-run",
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                # The preview should mention the file in stderr
                preview_shown = _wait_for_text_in_stderr(slave, "should_not_sync.txt", timeout=30)
                assert preview_shown, "Preview did not show the pending file"

                # But the file should NOT actually appear on disk
                time.sleep(3)
                assert not (Path(lan_slave_dir) / "should_not_sync.txt").exists(), (
                    "File was transferred despite --dry-run"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Pre-existing data
# =============================================================================


class TestLanPreExistingData:
    def test_pre_existing_data_syncs(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Directory already populated before client starts → everything syncs."""
        # Create files BEFORE starting any client
        (Path(lan_master_dir) / "pre_existing_a.txt").write_bytes(b"alpha\n")
        (Path(lan_master_dir) / "pre_existing_b.txt").write_bytes(b"beta\n")

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_file(
                    str(Path(lan_slave_dir) / "pre_existing_a.txt"), timeout=30
                ), "Pre-existing file A did not sync"
                assert _wait_for_file(
                    str(Path(lan_slave_dir) / "pre_existing_b.txt"), timeout=10
                ), "Pre-existing file B did not sync"
                assert (Path(lan_slave_dir) / "pre_existing_a.txt").read_bytes() == b"alpha\n"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Unicode filenames
# =============================================================================


class TestLanUnicode:
    def test_unicode_filename_syncs(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """File with non-ASCII name (e.g. caffè.txt) syncs correctly."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                unicode_name = "caffè_日本語.txt"
                content = "unicode content 🎉\n".encode()
                (Path(lan_master_dir) / unicode_name).write_bytes(content)

                slave_path = Path(lan_slave_dir) / unicode_name
                assert _wait_for_file(str(slave_path), timeout=30), (
                    f"Unicode file '{unicode_name}' did not sync"
                )
                assert slave_path.read_bytes() == content
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Nested deep directories
# =============================================================================


class TestLanDeepDirs:
    def test_nested_deep_directory_syncs(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Deeply nested directory (6 levels) syncs correctly."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # Create deep directory structure
            deep = Path(lan_master_dir) / "a" / "b" / "c" / "d" / "e" / "f"
            deep.mkdir(parents=True)
            (deep / "deep_file.txt").write_bytes(b"deep content\n")

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                slave_deep_file = (
                    Path(lan_slave_dir) / "a" / "b" / "c" / "d" / "e" / "f" / "deep_file.txt"
                )
                assert _wait_for_file(str(slave_deep_file), timeout=30), (
                    "Deeply nested file did not sync"
                )
                assert slave_deep_file.read_bytes() == b"deep content\n"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Mass-deletion guard
# =============================================================================


class TestLanMassDeleteGuard:
    def test_mass_delete_guard_trips(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Many files deleted on master -> slave guard blocks the deletion."""
        # Create 10 files on master before anyone connects
        for i in range(10):
            (Path(lan_master_dir) / f"bulk_{i:02d}.txt").write_bytes(f"file{i:02d}\n".encode())

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # Start slave with low mass-delete threshold
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
                    "--mass-delete-threshold",
                    "5",
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                # Wait for initial sync to bring all files to slave
                for i in range(10):
                    assert _wait_for_file(
                        str(Path(lan_slave_dir) / f"bulk_{i:02d}.txt"), timeout=30
                    ), f"bulk_{i:02d}.txt did not appear on slave"

                # Now delete all files on master
                for i in range(10):
                    (Path(lan_master_dir) / f"bulk_{i:02d}.txt").unlink()

                # The guard should trip and log a warning
                guard_tripped = _wait_for_text_in_stderr(
                    slave, "GUARD", timeout=30
                ) or _wait_for_text_in_stderr(slave, "mass", timeout=5)
                assert guard_tripped, "Mass-deletion guard did not trip"

                # Files should still be on slave (guard blocked deletion)
                for i in range(10):
                    slave_file = Path(lan_slave_dir) / f"bulk_{i:02d}.txt"
                    assert slave_file.exists(), (
                        f"bulk_{i:02d}.txt was deleted despite mass-delete guard"
                    )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Escaping symlink (security)
# =============================================================================


class TestLanEscapingSymlink:
    def test_escaping_symlink_not_synced(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Symlink whose target escapes the bucket is NOT propagated."""
        import platform

        if platform.system() == "Windows":
            pytest.skip("Symlink test requires Unix")

        # Create a file OUTSIDE the bucket
        outside = Path(lan_master_dir).parent / "secret.txt"
        outside.write_bytes(b"top-secret\n")
        try:
            master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
            try:
                assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                    "Master did not become ready"
                )

                slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
                try:
                    # Create symlink on master that escapes the bucket
                    link_path = Path(lan_master_dir) / "escape_link.txt"
                    link_path.symlink_to(outside)

                    # Wait — the escaping symlink should NOT appear on slave
                    time.sleep(5)
                    slave_link = Path(lan_slave_dir) / "escape_link.txt"
                    assert not slave_link.exists(), (
                        "Escaping symlink was synced to slave (security breach)"
                    )
                finally:
                    _terminate(slave)
            finally:
                _terminate(master)
        finally:
            if outside.exists():
                outside.unlink()


# =============================================================================
# Ignore patterns flag
# =============================================================================


class TestLanIgnorePatterns:
    def test_ignore_patterns_flag_respected(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """--ignore-patterns '*.tmp,*.bak' excludes matching files."""
        # Pre-create files that should and should not sync
        (Path(lan_master_dir) / "visible.txt").write_bytes(b"sync-me\n")
        (Path(lan_master_dir) / "ignore_me.tmp").write_bytes(b"skip-me\n")
        (Path(lan_master_dir) / "ignore_me.bak").write_bytes(b"skip-me-too\n")

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

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
                    "--ignore-patterns",
                    "*.tmp,*.bak",
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                # Visible file should appear
                assert _wait_for_file(str(Path(lan_slave_dir) / "visible.txt"), timeout=30), (
                    "Visible file did not sync"
                )
                # Ignored files should NOT appear
                time.sleep(5)
                assert not (Path(lan_slave_dir) / "ignore_me.tmp").exists(), (
                    "Ignored .tmp file was synced"
                )
                assert not (Path(lan_slave_dir) / "ignore_me.bak").exists(), (
                    "Ignored .bak file was synced"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Logfile output
# =============================================================================


class TestLanLogfile:
    def test_logfile_writes_output(self, lan_master_dir, lan_slave_dir, lan_bucket, tmp_path):
        """--output writes log to the specified file."""
        log_path = tmp_path / "hsync_test.log"

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

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
                    "--output",
                    str(log_path),
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                # Create a file and wait for sync
                (Path(lan_master_dir) / "logged.txt").write_bytes(b"log-test\n")
                assert _wait_for_file(str(Path(lan_slave_dir) / "logged.txt"), timeout=30), (
                    "File did not sync"
                )

                # The log file should exist and contain output
                assert _wait_for_file(str(log_path), timeout=10), "Log file was not created"
                log_content = log_path.read_text()
                assert len(log_content) > 0, "Log file is empty"
                assert "HSYNC" in log_content, "Log file does not contain expected content"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)
