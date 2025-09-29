# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for LAN security: TOFU pinning, certificate validation, dry-run, and edge cases.

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
# Dry-run mode
# =============================================================================


class TestLanDryRun:
    def test_dry_run_no_files_transferred(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """--dry-run prevents any file transfer."""
        (Path(lan_master_dir) / "dry_test.txt").write_bytes(b"dry\n")

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            slave = _lan_start(
                lan_slave_dir,
                lan_bucket,
                never_master=True,
                extra_args=("--dry-run",),
            )
            try:
                time.sleep(5)
                assert not (Path(lan_slave_dir) / "dry_test.txt").exists(), (
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
        """Files already present on master before slave starts are synced."""
        (Path(lan_master_dir) / "pre_existing.txt").write_bytes(b"pre-existing\n")

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_file(str(Path(lan_slave_dir) / "pre_existing.txt"), timeout=30), (
                    "Pre-existing file did not sync"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Unicode and deep paths
# =============================================================================


class TestLanUnicodePath:
    def test_unicode_filename_syncs(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Unicode filename syncs correctly."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            (Path(lan_master_dir) / "δοκιμή.txt").write_bytes(b"unicode\n")
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_file(str(Path(lan_slave_dir) / "δοκιμή.txt"), timeout=30), (
                    "Unicode filename did not sync"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestLanDeepPath:
    def test_nested_deep_directory_syncs(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Deeply nested directory syncs correctly."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            deep = Path(lan_master_dir) / "level1" / "level2" / "level3" / "level4" / "level5"
            deep.mkdir(parents=True)
            (deep / "deep_file.txt").write_bytes(b"deep\n")
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                target = (
                    Path(lan_slave_dir)
                    / "level1"
                    / "level2"
                    / "level3"
                    / "level4"
                    / "level5"
                    / "deep_file.txt"
                )
                assert _wait_for_file(str(target), timeout=60), "Deeply nested file did not sync"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Mass-delete guard
# =============================================================================


class TestLanMassDelete:
    def test_mass_delete_guard_trips(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Mass deletion reconciled by a full sync trips the slave's guard.

        Real-time WS ``deleted`` events are applied one by one by design; the
        guard lives in the full-sync reconciliation. The scenario therefore
        takes the slave offline (master stop), deletes the files, and restarts
        the master: the slave rediscovers it and reconciles — 20 deletions
        reach the threshold of 2 and the guard trips.

        The slave runs ``--sync-direction download-only`` so it *mirrors* the
        master: a bidirectional slave would instead re-upload local-only files
        to an emptied master and no deletion would ever be computed.
        """
        for i in range(20):
            (Path(lan_master_dir) / f"md{i:02d}.txt").write_bytes(b"mass-delete\n")

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            slave = _lan_start(
                lan_slave_dir,
                lan_bucket,
                never_master=True,
                extra_args=("--mass-delete-threshold", "2", "--sync-direction", "download-only"),
            )
            try:
                assert _wait_for_text_in_stderr(slave, "Initial sync complete", timeout=60), (
                    "Slave did not complete the initial sync"
                )

                # Slave goes "offline": master stops; the deletions become
                # visible only at the next full reconciliation.
                _terminate(master)
                for i in range(20):
                    (Path(lan_master_dir) / f"md{i:02d}.txt").unlink()

                # Master restarts with the same identity (same device dir), so
                # the slave's TOFU pin stays valid and it reconnects.
                master2 = _lan_start(lan_master_dir, lan_bucket, force_master=True)
                try:
                    assert _wait_for_text_in_stderr(
                        slave, "Mass-deletion guard tripped", timeout=90
                    ), (
                        "Mass-delete guard did not trip on reconcile after reconnect"
                        f"\n--- slave log tail ---\n{_stderr_text(slave)[-3000:]}"
                    )
                finally:
                    _terminate(master2)
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Symlink escape
# =============================================================================


class TestLanSymlinkEscape:
    def test_escaping_symlink_not_synced(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Symlink pointing outside the sync dir is not followed."""
        outside = Path("/tmp") / f"hsync-esc-{lan_bucket}"
        outside.write_bytes(b"outside\n")

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            link = Path(lan_master_dir) / "escape.txt"
            link.symlink_to(str(outside))
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                _wait_for_text_in_stderr(slave, "Initial sync complete", timeout=30)
                time.sleep(3)
                # The escaping symlink should NOT result in a regular file on slave
                if (Path(lan_slave_dir) / "escape.txt").exists():
                    content = (Path(lan_slave_dir) / "escape.txt").read_bytes()
                    assert content != b"outside\n", "Escaping symlink content was followed"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)
            outside.unlink(missing_ok=True)


# =============================================================================
# Ignore patterns
# =============================================================================


class TestLanIgnorePatterns:
    def test_ignore_patterns_flag_respected(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """--ignore '*.tmp' prevents .tmp files from syncing."""
        (Path(lan_master_dir) / "keep.me").write_bytes(b"keep\n")
        (Path(lan_master_dir) / "drop.tmp").write_bytes(b"drop\n")

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            slave = _lan_start(
                lan_slave_dir,
                lan_bucket,
                never_master=True,
                extra_args=("--ignore-patterns", "*.tmp"),
            )
            try:
                assert _wait_for_file(str(Path(lan_slave_dir) / "keep.me"), timeout=30), (
                    "keep.me should have synced"
                )
                time.sleep(5)
                assert not (Path(lan_slave_dir) / "drop.tmp").exists(), (
                    "drop.tmp was synced despite --ignore *.tmp"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Logfile output
# =============================================================================


class TestLanLogfile:
    def test_logfile_writes_output(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """--output writes the session log to a file.

        With an output file the log sink moves off stdout, so readiness cannot
        be parsed from the process output: wait on the master's ready file
        instead (a temporary slave still triggers the election).
        """
        logfile = Path(lan_master_dir) / "session.log"

        master = _lan_start(
            lan_master_dir,
            lan_bucket,
            force_master=True,
            extra_args=("--output", str(logfile)),
        )
        try:
            temp_slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_client_ready(
                    master, _lan_ready_path(lan_master_dir), timeout=60
                ), "Master did not become ready"
            finally:
                _terminate(temp_slave)

            assert logfile.exists(), "Logfile was not created"
            log_content = logfile.read_text()
            assert "HSYNC Client" in log_content
        finally:
            _terminate(master)
