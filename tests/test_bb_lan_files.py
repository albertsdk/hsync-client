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


class TestLanFileDeletion:
    def test_file_deletion_propagates(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """File deleted on master → deleted on slave."""
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
                (Path(lan_master_dir) / "to_delete.txt").unlink()
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
                (Path(lan_master_dir) / "keep_me.txt").unlink()
                time.sleep(5)
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
                (Path(lan_master_dir) / "old_name.txt").rename(
                    Path(lan_master_dir) / "new_name.txt"
                )
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
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            content = b"X" * (1024 * 1024)
            (Path(lan_master_dir) / "big.bin").write_bytes(content)
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
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
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            (Path(lan_master_dir) / "empty.txt").write_bytes(b"")
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
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
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            (Path(lan_master_dir) / "café_日本語_файл.txt").write_bytes(b"unicode\n")
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
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
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            deep = Path(lan_master_dir) / "a" / "b" / "c" / "d"
            deep.mkdir(parents=True)
            (deep / "deep.txt").write_bytes(b"nested\n")
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
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
                    "Symlinked file did not sync"
                )
                assert (Path(lan_slave_dir) / "link.txt").read_bytes() == b"symlink-target\n"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestLanFileModification:
    def test_file_modification_propagates(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """File modified on master → updated on slave."""
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
                (Path(lan_master_dir) / "mod.txt").write_bytes(b"v2-updated\n")
                time.sleep(3)
                # Re-read: modification should sync within the watch window
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
