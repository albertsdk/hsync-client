# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""The watcher's delete-noise guard must consider buffered *file* deletes.

``IN_ATTRIB`` fires on a directory every time one of its children is removed,
so a recursive delete emits a spurious directory ``modified`` per removed
child. The guard that suppresses them used to look only at buffered directory
deletes, missing exactly the moment it was written for: a directory losing its
* files has no pending entry of its own yet, and the resulting broadcast made a
peer resurrect the tree that was being deleted.
"""

from unittest.mock import MagicMock

from hsync_client.p2p_websocket import P2PMasterFileEventHandler


class TestPendingDeleteCoverage:
    def _handler(self) -> P2PMasterFileEventHandler:
        """A master handler: the class that owns the delete-noise guard."""
        return P2PMasterFileEventHandler(MagicMock(), watcher=None)

    def test_a_directory_losing_files_is_being_emptied(self):
        handler = self._handler()
        handler._pending_delete_set = {("/bucket/Music/i_o/1.mp3", False)}
        assert handler._is_covered_by_pending_delete("/bucket/Music/i_o")

    def test_a_directory_whose_subdirectory_is_deleted_is_covered(self):
        handler = self._handler()
        handler._pending_delete_dir_paths = {"/bucket/Music/i_o"}
        assert handler._is_covered_by_pending_delete("/bucket/Music")

    def test_a_directory_below_a_deleted_one_is_covered(self):
        handler = self._handler()
        handler._pending_delete_dir_paths = {"/bucket/Music"}
        assert handler._is_covered_by_pending_delete("/bucket/Music/i_o/1.mp3")

    def test_a_sibling_directory_is_not_covered(self):
        handler = self._handler()
        handler._pending_delete_set = {("/bucket/Music/i_o/1.mp3", False)}
        assert not handler._is_covered_by_pending_delete("/bucket/Music/i_o2")

    def test_a_parent_of_a_pending_file_delete_is_covered(self):
        """The directory those files are leaving is being emptied: noise."""
        handler = self._handler()
        handler._pending_delete_set = {("/bucket/Music/i_o/1.mp3", False)}
        assert handler._is_covered_by_pending_delete("/bucket/Music/i_o")

    def test_a_sibling_prefix_is_not_covered(self):
        """Component-aware: /bucket/Music2 is not inside /bucket/Music."""
        handler = self._handler()
        handler._pending_delete_set = {("/bucket/Music2/1.mp3", False)}
        assert not handler._is_covered_by_pending_delete("/bucket/Music")
