# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for _handle_symlink_event function in websocket_handler module."""

from unittest.mock import patch

from hsync_client.websocket_handler import _handle_symlink_event


class TestHandleSymlinkEvent:
    """Tests for _handle_symlink_event function."""

    def test_symlink_creation_success(self, tmp_path):
        """Successfully creates symlink."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link_path = "link.txt"

        src = {
            "type": "symlink",
            "filepath": link_path,
            "target": str(target),
        }

        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.websocket_handler.CONFIG", config):
            _handle_symlink_event(src)

        assert (tmp_path / link_path).is_symlink()

    def test_symlink_ignores_non_symlink_events(self):
        """Ignores events that are not symlinks."""
        src = {"type": "file", "filepath": "file.txt"}

        with patch("hsync_client.websocket_handler.os_symlink") as mock_symlink:
            _handle_symlink_event(src)
            mock_symlink.assert_not_called()

    def test_symlink_ignores_none_src(self):
        """Ignores None source."""
        with patch("hsync_client.websocket_handler.os_symlink") as mock_symlink:
            _handle_symlink_event(None)
            mock_symlink.assert_not_called()
