# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box: deleting a whole tree converges on both peers.

A folder the user removes on one peer must stay removed on both: neither side
may keep it, re-create it, or announce it back. Both ways of removing it are
covered because they emit completely different events: a rename out of the
bucket (same filesystem, one atomic move — what ``gio trash`` does) and a
recursive removal (``rm -rf``, or a trash on another filesystem), which fires
one event per entry.

The wire-level contract for the stale directory event itself lives in
``tests/test_event_dispatch_dir_modified.py``: this suite proves the observable
outcome.

Requires HSYNC_BB_P2P=1 to run.
"""

import os
import shutil
import time
from pathlib import Path

import pytest

from .p2p_helpers import (
    _p2p_start,
    _stderr_text,
    _terminate,
    _wait_for_file,
    _wait_for_file_gone,
    _wait_for_master,
    _wait_for_slave_events,
)

_P2P_ENABLED = os.environ.get("HSYNC_BB_P2P") == "1"
_P2P_REASON = (
    "P2P e2e needs loopback mDNS + two live client instances; set HSYNC_BB_P2P=1 to enable"
)

pytestmark = pytest.mark.skipif(not _P2P_ENABLED, reason=_P2P_REASON)

# The tree the user deleted at once, nested exactly like the reported folder.
TREE_FILES = ("i_o/1-01 Panopticom.mp3", "i_o/1-02 The Court.mp3", "notes.txt")


def _plant_tree(root: Path) -> None:
    for rel in TREE_FILES:
        target = root / "Peter Gabriel" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 1024)


class TestTreeDeleteConverges:
    @pytest.mark.parametrize("variant", ("trash-rename", "recursive-remove"))
    def test_a_deleted_tree_stays_deleted_on_both_peers(
        self, variant, p2p_master_dir, p2p_slave_dir, p2p_bucket, tmp_path
    ):
        tree = Path(p2p_slave_dir) / "Peter Gabriel"
        _plant_tree(Path(p2p_slave_dir))

        master = _p2p_start(p2p_master_dir, p2p_bucket, prefer_master=True)
        trash_dir = tmp_path / "trash"
        trash_dir.mkdir()
        try:
            _wait_for_master(master, p2p_slave_dir, p2p_bucket)
            slave = _p2p_start(p2p_slave_dir, p2p_bucket, never_master=True)
            try:
                synced = Path(p2p_master_dir) / "Peter Gabriel" / "i_o" / "1-01 Panopticom.mp3"
                assert _wait_for_file(str(synced), timeout=60), _stderr_text(master)
                # A local change is only propagated by the watcher, which the
                # slave starts once it reaches its event loop (after the
                # initial sync). The master holding the file does not prove
                # that; waiting for the slave's ready marker does.
                assert _wait_for_slave_events(slave), _stderr_text(slave)

                # Everything from here on happened after the delete.
                before = _stderr_text(slave)

                # When the user removes the whole folder the way the desktop
                # does (rename into the trash) or with a recursive delete
                if variant == "trash-rename":
                    os.rename(tree, trash_dir / tree.name)
                else:
                    shutil.rmtree(tree)

                # Then the master loses it too, and the slave's DELETE succeeds
                assert _wait_for_file_gone(
                    str(Path(p2p_master_dir) / "Peter Gabriel"), timeout=45
                ), f"The delete never reached the master.\n{_stderr_text(master)}"
                tail = _stderr_text(slave)[len(before) :]
                assert "Delete request failed" not in tail, tail

                # Nothing resurrects the folder, and the slave does not announce
                # the removed directories back to the master as local creates.
                time.sleep(3)
                assert not (Path(p2p_slave_dir) / "Peter Gabriel").exists(), (
                    "The deleted folder came back on the slave.\n"
                    f"slave:\n{_stderr_text(slave)}\nmaster:\n{_stderr_text(master)}"
                )
                assert not (Path(p2p_master_dir) / "Peter Gabriel").exists(), (
                    f"The deleted folder came back on the master.\nmaster:\n{_stderr_text(master)}"
                )
                tail = _stderr_text(slave)[len(before) :]
                assert "[LOCAL CREATE]" not in tail, tail
            finally:
                _terminate(slave)
        finally:
            _terminate(master)
