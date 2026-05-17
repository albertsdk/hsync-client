# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for LAN feature flags, group filtering, and edge cases.

Requires HSYNC_BB_LAN=1 to run.
"""

import os
import time
from pathlib import Path

import pytest
from hsync_common.labels import ActionLabel

from .lan_helpers import (
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


# =============================================================================
# LAN group filtering
# =============================================================================


class TestLanGroupFiltering:
    def test_different_lan_groups_dont_discover(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Peers in different LAN groups should not discover each other."""
        # Given a master in group-alpha
        master = _lan_start(
            lan_master_dir,
            lan_bucket,
            force_master=True,
            extra_args=("--group", "group-alpha"),
        )
        try:
            # The temp slave must join the master's group or it is invisible.
            _wait_for_master(
                master, lan_slave_dir, lan_bucket, slave_extra_args=("--group", "group-alpha")
            )

            # When a slave starts in a different group
            slave = _lan_start(
                lan_slave_dir,
                lan_bucket,
                never_master=True,
                extra_args=("--group", "group-beta"),
            )
            try:
                # Then it never discovers the master (file never syncs)
                (Path(lan_master_dir) / "group_test.txt").write_bytes(b"group\n")
                time.sleep(5)
                assert _wait_for_text_in_stderr(
                    slave, "No other peers discovered", timeout=10
                ) or _wait_for_text_in_stderr(slave, "not found", timeout=5)
            finally:
                _terminate(slave)
        finally:
            _terminate(master)

    def test_same_lan_group_discovers_and_syncs(
        self, lan_master_dir, lan_slave_dir, lan_bucket, bb_sample
    ):
        """Peers in the same LAN group discover each other and sync (positive
        counterpart of test_different_lan_groups_dont_discover)."""
        # Given a master and slave in the same LAN group
        master = _lan_start(
            lan_master_dir,
            lan_bucket,
            force_master=True,
            extra_args=("--group", "group-alpha"),
        )
        try:
            _wait_for_master(
                master, lan_slave_dir, lan_bucket, slave_extra_args=("--group", "group-alpha")
            )

            slave = _lan_start(
                lan_slave_dir,
                lan_bucket,
                never_master=True,
                extra_args=("--group", "group-alpha"),
            )
            try:
                # Wait for the slave's watcher to be live before creating files
                assert _wait_for_slave_events(slave), "slave did not reach [READY]"

                # When a file is created on the master
                s = bb_sample(prefix="grp", size=0)
                content = b"same-group\n"
                (Path(lan_master_dir) / s.name).write_bytes(content)

                # Then the slave in the same group receives it
                assert _wait_for_file(str(Path(lan_slave_dir) / s.name), timeout=30), (
                    f"file did not sync within the same group. {s.ctx}"
                )
                assert (Path(lan_slave_dir) / s.name).read_bytes() == content
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
        # Given a master running with --no-compression
        master = _lan_start(
            lan_master_dir,
            lan_bucket,
            force_master=True,
            extra_args=("--no-compression",),
        )
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # When a file is created on the master
                content = b"no-compression\n"
                (Path(lan_master_dir) / "nocmp.txt").write_bytes(content)

                # Then it syncs despite compression being disabled
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
        # Given a master running with --no-delta
        master = _lan_start(
            lan_master_dir,
            lan_bucket,
            force_master=True,
            extra_args=("--no-delta",),
        )
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # When a file is created on the master
                content = b"no-delta\n"
                (Path(lan_master_dir) / "nodlt.txt").write_bytes(content)

                # Then it syncs despite delta being disabled
                assert _wait_for_file(str(Path(lan_slave_dir) / "nodlt.txt"), timeout=30), (
                    "File did not sync with delta disabled"
                )
                assert (Path(lan_slave_dir) / "nodlt.txt").read_bytes() == content
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestLanPeriodicResync:
    def test_periodic_resync_fires(self, lan_master_dir, lan_slave_dir, lan_bucket, bb_sample):
        """Slave with --interval triggers a periodic full resync after the
        configured interval, observable in its log output."""
        # Given a serving master and a slave with --interval 20
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            slave = _lan_start(
                lan_slave_dir,
                lan_bucket,
                never_master=True,
                extra_args=("--interval", "20"),
            )
            try:
                # Wait for the slave's initial sync to complete (ready-file)
                assert _wait_for_client_ready(slave, _lan_ready_path(lan_slave_dir), timeout=60), (
                    "slave did not complete initial sync"
                )

                # When a file is created on the master
                s = bb_sample(prefix="periodic", size=0)
                content = b"periodic\n"
                (Path(lan_master_dir) / s.name).write_bytes(content)

                # Then the slave receives it (pipeline is warm)
                assert _wait_for_file(str(Path(lan_slave_dir) / s.name), timeout=30), (
                    f"file did not sync to slave. {s.ctx}"
                )

                # And after the interval elapses the periodic resync fires,
                # observable as the "LAN force resync" log line on the slave
                assert _wait_for_text_in_stderr(slave, "LAN force resync", timeout=40), (
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


class TestLanHiddenFiles:
    def test_default_ignored_files_not_synced(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Files matched by the built-in default ignore patterns never sync.

        Plain dotfiles (``.env``-style) are synced by design (industry
        standard: Syncthing, rsync, Nextcloud) — only the built-in patterns
        (OS caches, editor/office temp files, ``*.tmp`` …) are filtered. See
        ``hsync_common.constants.DEFAULT_IGNORE_PATTERNS``.
        """
        # Given a master with one file per default-ignored family
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            (Path(lan_master_dir) / ".DS_Store").write_bytes(b"macos\n")
            (Path(lan_master_dir) / "Thumbs.db").write_bytes(b"windows\n")
            (Path(lan_master_dir) / "notes.tmp").write_bytes(b"temp\n")

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                _wait_for_text_in_stderr(slave, "Initial sync complete", timeout=30)
                time.sleep(5)

                # Then none of the default-ignored files reach the slave
                for name in (".DS_Store", "Thumbs.db", "notes.tmp"):
                    assert not (Path(lan_slave_dir) / name).exists(), (
                        f"Default-ignored file {name} was synced (should be ignored)"
                    )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestLanKeyless:
    def test_keyless_unattended_write_is_refused(self, lan_master_dir, lan_bucket):
        """Keyless + write-capable + non-interactive is refused at startup.

        The write policy (``lan_write_requires_key``) requires a shared
        ``HSYNC_LAN_KEY`` or the explicit dangerous opt-in whenever a run may
        upload to a master. A ``--yes`` LAN run without either must therefore
        exit immediately with the policy error.
        """
        # Given a write-capable keyless master in unattended (--yes) mode
        master = _lan_start(
            lan_master_dir,
            lan_bucket,
            force_master=True,
            extra_env={"HSYNC_LAN_KEY": None},
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

    def test_keyless_download_only_node_is_allowed(self, lan_slave_dir, lan_bucket):
        """A keyless download-only node passes the write policy (nothing to upload).

        The node starts, runs discovery and keeps waiting for peers — the
        point is that the run is *allowed* without HSYNC_LAN_KEY (no
        write-policy error) and stays alive instead of being refused.
        """
        # Given a keyless download-only node
        node = _lan_start(
            lan_slave_dir,
            lan_bucket,
            never_master=True,
            extra_args=("--sync-direction", "download-only"),
            extra_env={"HSYNC_LAN_KEY": None},
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
