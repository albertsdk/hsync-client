# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for LAN file, directory, and symlink operations.

Requires HSYNC_BB_LAN=1 to run.
"""

import os
import time
from pathlib import Path

import pytest

from .lan_helpers import (
    _lan_start,
    _stderr_text,
    _terminate,
    _wait_for_file,
    _wait_for_file_gone,
    _wait_for_master,
    _wait_for_slave_events,
)

_LAN_ENABLED = os.environ.get("HSYNC_BB_LAN") == "1"
_LAN_REASON = (
    "LAN e2e needs loopback mDNS + two live client instances; set HSYNC_BB_LAN=1 to enable"
)

pytestmark = pytest.mark.skipif(not _LAN_ENABLED, reason=_LAN_REASON)


# =============================================================================
# File operations
# =============================================================================


class TestLanPeerDownload:
    @pytest.mark.parametrize("peer_enabled", (True, False), ids=("peer", "master-only"))
    def test_one_shot_requester_download_path(
        self,
        peer_enabled,
        lan_master_dir,
        lan_slave_dir,
        lan_bucket,
        tmp_path,
    ):
        """Distinct same-address CIDs seed exact bodies; opt-out remains master-only."""
        content = (b"peer-assisted-download\n" * 65536)[: 2 * 1024 * 1024]
        source_path = Path(lan_master_dir) / "peer-source.bin"
        source_path.write_bytes(content)
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)
            seeder = _lan_start(
                lan_slave_dir,
                lan_bucket,
                never_master=True,
                extra_args=("-v",),
            )
            try:
                seeded_path = Path(lan_slave_dir) / source_path.name
                assert _wait_for_file(str(seeded_path), timeout=45), _stderr_text(seeder)
                assert _wait_for_slave_events(seeder, timeout=30), _stderr_text(seeder)
                time.sleep(2)

                requester_dir = tmp_path / "requester"
                requester_dir.mkdir()
                requester = _lan_start(
                    str(requester_dir),
                    lan_bucket,
                    never_master=True,
                    extra_args=("--once", "-v") + (() if peer_enabled else ("--no-peer-download",)),
                )
                try:
                    requester.wait(timeout=60)
                    requester_output = _stderr_text(requester)
                    assert requester.returncode == 0, requester_output
                    assert (requester_dir / source_path.name).read_bytes() == content
                    seeder_output = _stderr_text(seeder)
                    if peer_enabled:
                        assert '/files/sources HTTP/1.1" 200' in requester_output
                        assert f"filename={source_path.name}&sha256=" in seeder_output, (
                            f"requester:\n{requester_output}\nseeder:\n{seeder_output}"
                        )
                        assert "source_cid=" in requester_output, requester_output
                    else:
                        assert '/files/sources HTTP/1.1" 200' not in requester_output
                        assert f"filename={source_path.name}&sha256=" not in seeder_output
                        assert "source_cid=" not in requester_output
                finally:
                    _terminate(requester)
            finally:
                _terminate(seeder)
        finally:
            _terminate(master)


class TestLanFileDeletion:
    def test_file_deletion_propagates(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """File deleted on master → deleted on slave."""
        # Given a serving master with a synced file
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            (Path(lan_master_dir) / "to_delete.txt").write_bytes(b"delete-me\n")
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_file(str(Path(lan_slave_dir) / "to_delete.txt"), timeout=30), (
                    "File did not sync before deletion"
                )
                # The deletion propagates over the event channel: wait for it
                # or the unlink falls into the post-initial-sync blind spot.
                assert _wait_for_slave_events(slave), "Slave is not listening for master events"

                # When the file is deleted on the master
                (Path(lan_master_dir) / "to_delete.txt").unlink()

                # Then it disappears on the slave
                assert _wait_for_file_gone(
                    str(Path(lan_slave_dir) / "to_delete.txt"), timeout=30
                ), "Deleted file still present on slave"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestLanIgnoreDelete:
    def test_ignore_delete_flag_preserves_file(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """--ignore-delete keeps the file on the slave when deleted on the master."""
        # Given a serving master with a synced file
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            (Path(lan_master_dir) / "keep_me.txt").write_bytes(b"keep\n")
            slave = _lan_start(
                lan_slave_dir,
                lan_bucket,
                never_master=True,
                extra_args=("--ignore-delete",),
            )
            try:
                assert _wait_for_file(str(Path(lan_slave_dir) / "keep_me.txt"), timeout=30)
                # Gate on the event channel: without it a missed broadcast
                # would make the "file preserved" assertion pass vacuously.
                assert _wait_for_slave_events(slave), "Slave is not listening for master events"

                # When the file is deleted on the master with --ignore-delete
                (Path(lan_master_dir) / "keep_me.txt").unlink()
                time.sleep(5)

                # Then the slave copy survives
                assert (Path(lan_slave_dir) / "keep_me.txt").exists(), (
                    "File was deleted on slave despite deletion protection"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestLanFileRename:
    def test_file_rename_propagates(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """File renamed on master → renamed on slave."""
        # Given a serving master with a synced file
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            (Path(lan_master_dir) / "old_name.txt").write_bytes(b"renamed\n")
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_file(str(Path(lan_slave_dir) / "old_name.txt"), timeout=30)
                # The rename propagates over the event channel: wait for it
                # or the rename falls into the post-initial-sync blind spot.
                assert _wait_for_slave_events(slave), "Slave is not listening for master events"

                # When the file is renamed on the master
                (Path(lan_master_dir) / "old_name.txt").rename(
                    Path(lan_master_dir) / "new_name.txt"
                )

                # Then the new name appears and the old one disappears on the slave
                assert _wait_for_file(str(Path(lan_slave_dir) / "new_name.txt"), timeout=30), (
                    "Renamed file did not appear on slave"
                )
                assert _wait_for_file_gone(str(Path(lan_slave_dir) / "old_name.txt"), timeout=30), (
                    "Old file still present on slave after rename"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestLanLargeFile:
    def test_large_file_sync(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """A 1 MB file syncs correctly."""
        # Given a serving master with a 1 MB file
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            content = b"X" * (1024 * 1024)
            (Path(lan_master_dir) / "big.bin").write_bytes(content)
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # When the slave starts and syncs
                # Then the large file is replicated byte-for-byte
                assert _wait_for_file(str(Path(lan_slave_dir) / "big.bin"), timeout=60), (
                    "Large file did not sync"
                )
                assert (Path(lan_slave_dir) / "big.bin").read_bytes() == content
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestLanEmptyFile:
    def test_empty_file_sync(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """An empty file syncs correctly."""
        # Given a serving master with an empty file
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            (Path(lan_master_dir) / "empty.txt").write_bytes(b"")
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # When the slave starts and syncs
                # Then the empty file is replicated (0 bytes)
                assert _wait_for_file(str(Path(lan_slave_dir) / "empty.txt"), timeout=30), (
                    "Empty file did not sync"
                )
                assert (Path(lan_slave_dir) / "empty.txt").read_bytes() == b""
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestLanUnicodeFilename:
    def test_unicode_filename_sync(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """File with Unicode characters in name syncs correctly."""
        # Given a serving master with a Unicode-named file
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            (Path(lan_master_dir) / "café_日本語_файл.txt").write_bytes(b"unicode\n")
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # When the slave starts and syncs
                # Then the Unicode-named file is replicated
                assert _wait_for_file(
                    str(Path(lan_slave_dir) / "café_日本語_файл.txt"), timeout=30
                ), "Unicode filename did not sync"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestLanNestedDirectory:
    def test_nested_directory_sync(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Deeply nested directory structure syncs correctly."""
        # Given a serving master with a deeply nested directory
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            deep = Path(lan_master_dir) / "a" / "b" / "c" / "d"
            deep.mkdir(parents=True)
            (deep / "deep.txt").write_bytes(b"nested\n")
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # When the slave starts and syncs
                # Then the nested directory tree is replicated
                assert _wait_for_file(
                    str(Path(lan_slave_dir) / "a" / "b" / "c" / "d" / "deep.txt"), timeout=30
                ), "Nested directory did not sync"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestLanSymlink:
    def test_symlink_sync(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Symlinked file on master → regular file on slave (symlink resolved)."""
        # Given a serving master with a target file and an in-bucket symlink
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            target = Path(lan_master_dir) / "target.txt"
            target.write_bytes(b"symlink-target\n")
            link = Path(lan_master_dir) / "link.txt"
            link.symlink_to("target.txt")
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # When the slave starts and syncs
                # Then the link is replicated with the target's content
                assert _wait_for_file(str(Path(lan_slave_dir) / "link.txt"), timeout=30), (
                    "Symlinked file did not sync"
                )
                assert (Path(lan_slave_dir) / "link.txt").read_bytes() == b"symlink-target\n"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)

    def test_symlink_delete_keeps_target(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Deleting a synced link removes the LINK on every peer, never the
        target file it points to.

        Regression: the master's DELETE endpoint resolved the incoming path
        with full dereference, so deleting a link deleted the TARGET on the
        master and the event broadcast propagated the data loss to every
        peer.
        """
        # Given a synced link and its target on both peers
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            target = Path(lan_master_dir) / "target.txt"
            target.write_bytes(b"symlink-target\n")
            link = Path(lan_master_dir) / "link.txt"
            link.symlink_to("target.txt")
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_file(str(Path(lan_slave_dir) / "link.txt"), timeout=30), (
                    "Symlink did not sync before deletion"
                )
                assert _wait_for_slave_events(slave), "Slave is not listening for master events"

                # When the link is deleted on the master
                link.unlink()

                # Then the LINK is removed on the slave…
                assert _wait_for_file_gone(str(Path(lan_slave_dir) / "link.txt"), timeout=30), (
                    "Deleted link still present on slave"
                )
                # …and the target file survives on BOTH peers.
                assert target.exists(), "Target file was deleted on the master"
                assert (Path(lan_slave_dir) / "target.txt").exists(), (
                    "Target file was deleted on the slave"
                )
                assert (Path(lan_slave_dir) / "target.txt").read_bytes() == b"symlink-target\n"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)

    def test_symlink_converges_without_oscillation(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """A synced link converges exactly once: no SYMLINK/DELETE oscillation.

        Regression: every re-emitted echo used to unlink+recreate the link on
        the master, whose watcher rebroadcast a delete+create pair for each
        application — a never-ending ping-pong between peers (visible as an
        unbounded stream of ``[REMOTE SYMLINK]``/``[REMOTE DELETE]`` lines).
        """
        # Given a synced link on both peers
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            target = Path(lan_master_dir) / "target.txt"
            target.write_bytes(b"symlink-target\n")
            link = Path(lan_master_dir) / "link.txt"
            link.symlink_to("target.txt")
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_file(str(Path(lan_slave_dir) / "link.txt"), timeout=30), (
                    "Symlink did not sync"
                )
                assert _wait_for_slave_events(slave), "Slave is not listening for master events"

                # When any residual echoes settle over a quiet window
                _ = _stderr_text(slave)
                count_before = _stderr_text(slave).count("Creating symlink")
                time.sleep(5)
                count_after = _stderr_text(slave).count("Creating symlink")

                # Then the link survives and no further symlink application occurs
                assert (Path(lan_slave_dir) / "link.txt").is_symlink(), (
                    "Slave link disappeared during convergence"
                )
                assert count_after == count_before, (
                    f"Symlink oscillation detected: {count_before} -> {count_after} "
                    "inbound applications without any local change"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestLanFileModification:
    def test_file_modification_propagates(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """File modified on master → updated on slave."""
        # Given a serving master with a synced file at version v1
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            (Path(lan_master_dir) / "mod.txt").write_bytes(b"v1\n")
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_file(str(Path(lan_slave_dir) / "mod.txt"), timeout=30)
                assert (Path(lan_slave_dir) / "mod.txt").read_bytes() == b"v1\n"
                # The edit propagates over the event channel: wait for it
                # or the write falls into the post-initial-sync blind spot.
                assert _wait_for_slave_events(slave), "Slave is not listening for master events"

                # When the file is modified on the master
                (Path(lan_master_dir) / "mod.txt").write_bytes(b"v2-updated\n")
                time.sleep(3)

                # Then the update reaches the slave within the watch window
                deadline = time.time() + 30
                while time.time() < deadline:
                    if (Path(lan_slave_dir) / "mod.txt").read_bytes() == b"v2-updated\n":
                        break
                    time.sleep(0.5)
                else:
                    pytest.fail("Modified file did not update on slave")
            finally:
                _terminate(slave)
        finally:
            _terminate(master)
