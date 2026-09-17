# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests: files moved into/out of the synced tree over LAN.

Regression for the lost move-in: a file atomically renamed into the watched
tree from outside (drag & drop, ``mv``, extraction tools, atomic publishers)
produces only an unmatched ``IN_MOVED_TO``. The standard watchdog emitter
collapsed that into ``FileCreatedEvent``, which the handler drops (on inotify
content propagates via ``on_closed``, which a move-in never fires), so the
file was never broadcast until the next periodic resync. The full-event
emitter plus the ``on_moved`` mapping now propagate a move-in as a create and
a move-out as a delete.

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
    _wait_for_slave_events,
    _wait_for_text_in_stderr,
)

_LAN_ENABLED = os.environ.get("HSYNC_BB_LAN") == "1"
_LAN_REASON = (
    "LAN e2e needs loopback mDNS + two live client instances; set HSYNC_BB_LAN=1 to enable"
)

pytestmark = pytest.mark.skipif(not _LAN_ENABLED, reason=_LAN_REASON)


def _staging_dir(master_dir: Path) -> Path:
    """Sibling staging dir of *master_dir* (same filesystem, outside the tree)."""
    staging = Path(str(master_dir) + "_staging")
    staging.mkdir(parents=True, exist_ok=True)
    return staging


def _deposit(master_dir: Path, name: str, rel: str, content: bytes) -> None:
    """Atomically move *name* into ``master_dir/<rel>`` from a sibling staging dir."""
    staging = _staging_dir(master_dir)
    src = staging / f"{len(rel)}-{name}"
    src.write_bytes(content)
    dest = Path(master_dir) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dest)


def _wait_for_file_removed(path: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not Path(path).exists():
            return True
        time.sleep(0.2)
    return False


class TestLanMoveEvents:
    """Move-in from outside and move-out to outside propagate to the slave."""

    @pytest.mark.flaky(reruns=3, reruns_delay=5)
    def test_move_in_from_outside_syncs(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """A file moved into the tree root (regression) or a subdir reaches the slave."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "Registered mDNS service", timeout=15), (
                "Master mDNS service did not register"
            )
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_slave_events(slave, timeout=90), (
                    "Slave did not become ready (watcher live + handshake done)"
                )

                root_content = b"moved into the root\n"
                sub_content = b"moved into a subdir\n"
                # The root-level deposit is the regression case: its event is
                # an unmatched IN_MOVED_TO on the root watch.
                _deposit(Path(lan_master_dir), "root-file.bin", "root-file.bin", root_content)
                _deposit(Path(lan_master_dir), "sub-file.bin", "sub/sub-file.bin", sub_content)

                root_slave = str(Path(lan_slave_dir) / "root-file.bin")
                sub_slave = str(Path(lan_slave_dir) / "sub" / "sub-file.bin")
                assert _wait_for_file(root_slave, timeout=30), (
                    "File moved into the tree root never reached the slave"
                )
                assert _wait_for_file(sub_slave, timeout=30), (
                    "File moved into a subdir never reached the slave"
                )
                assert Path(root_slave).read_bytes() == root_content
                assert Path(sub_slave).read_bytes() == sub_content
            finally:
                _terminate(slave)
        finally:
            _terminate(master)

    @pytest.mark.flaky(reruns=3, reruns_delay=5)
    def test_move_out_of_tree_deletes_on_slave(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """A file moved out of the tree is deleted on the slave."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "Registered mDNS service", timeout=15), (
                "Master mDNS service did not register"
            )
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_slave_events(slave, timeout=90), (
                    "Slave did not become ready (watcher live + handshake done)"
                )

                # Establish the file first (ordinary in-place write → close).
                (Path(lan_master_dir) / "leaving.txt").write_bytes(b"leaving\n")
                slave_copy = str(Path(lan_slave_dir) / "leaving.txt")
                assert _wait_for_file(slave_copy, timeout=30), (
                    "Baseline file never reached the slave"
                )

                # Move it out of the tree: an unmatched IN_MOVED_FROM.
                staging = _staging_dir(Path(lan_master_dir))
                os.replace(Path(lan_master_dir) / "leaving.txt", staging / "leaving.txt")

                assert _wait_for_file_removed(slave_copy, timeout=30), (
                    "File moved out of the tree was not deleted on the slave"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)
