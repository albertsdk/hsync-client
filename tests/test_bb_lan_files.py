# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for LAN file, directory, and symlink operations.

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
    _wait_for_file_gone,
    _wait_for_text_in_stderr,
)

_LAN_ENABLED = os.environ.get("HSYNC_BB_LAN") == "1"
_LAN_REASON = (
    "LAN e2e needs loopback mDNS + two live client instances; set HSYNC_BB_LAN=1 to enable"
)

pytestmark = pytest.mark.skipif(not _LAN_ENABLED, reason=_LAN_REASON)


class TestLanFileDeletion:
    def test_file_deletion_propagates(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """A file deleted on the master is removed from the slave."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # Create a file on master
                file_path = Path(lan_master_dir) / "to_delete.txt"
                file_path.write_bytes(b"delete-me\n")

                # Wait for sync to slave
                slave_file = Path(lan_slave_dir) / "to_delete.txt"
                assert _wait_for_file(str(slave_file), timeout=30), "File did not appear on slave"

                # Delete on master
                file_path.unlink()

                # Slave should delete it too
                assert _wait_for_file_gone(str(slave_file), timeout=30), (
                    "Deleted file still present on slave"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)

    def test_ignore_delete_flag_preserves_file(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Slave with --ignore-delete keeps files deleted by the master."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # Start slave with --ignore-delete
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
                    "--ignore-delete",
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                # Create a file on master
                file_path = Path(lan_master_dir) / "keep_me.txt"
                file_path.write_bytes(b"keep-this\n")

                # Wait for sync to slave
                slave_file = Path(lan_slave_dir) / "keep_me.txt"
                assert _wait_for_file(str(slave_file), timeout=30), "File did not appear on slave"

                # Delete on master
                file_path.unlink()

                # Wait a bit and verify slave still has it
                time.sleep(5)
                assert slave_file.exists(), "File was deleted on slave despite --ignore-delete"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Directory & symlink propagation
# =============================================================================


class TestLanDirectorySync:
    def test_directory_creation_propagates(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """mkdir on master → directory (with .hsynckeep) appears on slave."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # Create directory on master
                master_subdir = Path(lan_master_dir) / "subdir"
                master_subdir.mkdir()
                (master_subdir / "file.txt").write_bytes(b"inside\n")

                # Wait for the directory to appear on slave
                slave_subdir = Path(lan_slave_dir) / "subdir"
                assert _wait_for_file(str(slave_subdir), timeout=30), (
                    "Directory did not appear on slave"
                )
                assert slave_subdir.is_dir(), "Expected a directory on slave"
                assert (slave_subdir / "file.txt").read_bytes() == b"inside\n"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)

    def test_directory_deletion_propagates(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """rmdir on master → directory disappears from slave."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # Pre-create directory on master before slave connects
            master_subdir = Path(lan_master_dir) / "todel_dir"
            master_subdir.mkdir()
            (master_subdir / "f.txt").write_bytes(b"x\n")

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                slave_subdir = Path(lan_slave_dir) / "todel_dir"
                assert _wait_for_file(str(slave_subdir), timeout=30), (
                    "Directory did not appear on slave"
                )

                # Delete on master
                import shutil

                shutil.rmtree(str(master_subdir))

                # Slave should remove it too
                assert _wait_for_file_gone(str(slave_subdir), timeout=30), (
                    "Deleted directory still present on slave"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)

    def test_symlink_propagates(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Symlink created on master → appears on slave (in-bucket target)."""
        import platform

        if platform.system() == "Windows":
            pytest.skip("Symlink test requires Unix")

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # Create target file inside the bucket
            target = Path(lan_master_dir) / "real_file.txt"
            target.write_bytes(b"symlink target\n")

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # Create symlink on master pointing to in-bucket target
                link_path = Path(lan_master_dir) / "link.txt"
                link_path.symlink_to(target)

                # Wait for symlink to appear on slave
                slave_link = Path(lan_slave_dir) / "link.txt"
                assert _wait_for_file(str(slave_link), timeout=30), (
                    "Symlink did not appear on slave"
                )
                assert slave_link.is_symlink(), "Expected a symlink on slave"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Advanced sync operations
# =============================================================================


class TestLanFileModification:
    def test_file_modification_propagates(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Modifying a file on the master updates it on the slave."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # Pre-create a file before slave connects
            (Path(lan_master_dir) / "modify_me.txt").write_bytes(b"v1\n")

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                slave_file = Path(lan_slave_dir) / "modify_me.txt"
                assert _wait_for_file(str(slave_file), timeout=30), (
                    "Initial file did not appear on slave"
                )
                assert slave_file.read_bytes() == b"v1\n"

                # Modify the file on master
                time.sleep(0.5)  # let mtime settle
                (Path(lan_master_dir) / "modify_me.txt").write_bytes(b"v2-updated\n")

                # Wait for the update to propagate
                deadline = time.time() + 30
                updated = False
                while time.time() < deadline:
                    if slave_file.read_bytes() == b"v2-updated\n":
                        updated = True
                        break
                    time.sleep(0.3)
                assert updated, "Modified content did not reach slave"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestLanFileRename:
    def test_file_rename_propagates(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Renaming a file on the master propagates to the slave."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # Pre-create file
            (Path(lan_master_dir) / "old_name.txt").write_bytes(b"rename-me\n")

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                old_slave = Path(lan_slave_dir) / "old_name.txt"
                assert _wait_for_file(str(old_slave), timeout=30), (
                    "File did not appear on slave before rename"
                )

                # Rename on master
                (Path(lan_master_dir) / "old_name.txt").rename(
                    Path(lan_master_dir) / "new_name.txt"
                )

                # Old path should vanish, new path should appear on slave
                new_slave = Path(lan_slave_dir) / "new_name.txt"
                assert _wait_for_file(str(new_slave), timeout=30), (
                    "Renamed file did not appear on slave"
                )
                assert _wait_for_file_gone(str(old_slave), timeout=10), (
                    "Old file name still present on slave after rename"
                )
                assert new_slave.read_bytes() == b"rename-me\n"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestLanBulkSync:
    def test_multiple_files_sync(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """10+ files created on master → all appear on slave with correct content."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # Create 10 files on master before slave connects
            files: dict[str, bytes] = {}
            for i in range(10):
                name = f"bulk_{i:02d}.txt"
                content = f"file-{i:02d}\n".encode()
                (Path(lan_master_dir) / name).write_bytes(content)
                files[name] = content

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                for name, expected in files.items():
                    slave_path = Path(lan_slave_dir) / name
                    assert _wait_for_file(str(slave_path), timeout=30), (
                        f"File {name} did not appear on slave"
                    )
                    assert slave_path.read_bytes() == expected, f"Wrong content for {name}"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)

    def test_empty_directory_propagates(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Empty directory (with .hsynckeep) appears on slave."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # Create an empty directory on master
                empty_dir = Path(lan_master_dir) / "emptydir"
                empty_dir.mkdir()

                # The directory should appear on slave (with .hsynckeep sentinel)
                slave_empty = Path(lan_slave_dir) / "emptydir"
                assert _wait_for_file(str(slave_empty), timeout=30), (
                    "Empty directory did not appear on slave"
                )
                assert slave_empty.is_dir(), "Expected a directory"
                # Sentinel should be present
                sentinel = slave_empty / ".hsynckeep"
                assert sentinel.exists(), "Missing .hsynckeep sentinel in empty dir"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)
