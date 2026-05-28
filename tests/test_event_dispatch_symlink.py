# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for symlink handling in the unified ``event_dispatch`` module."""

from unittest.mock import patch

from hsync_client.event_dispatch import apply_inbound_event
from hsync_client.remote_websocket import REMOTE_DISPATCH_CTX


class TestApplyInboundSymlinkEvent:
    """Tests for the ``symlink`` branch of :func:`apply_inbound_event`."""

    async def test_symlink_creation_success(self, tmp_path):
        """Successfully creates symlink at the requested path."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link_path = "link.txt"

        event = {
            "name": "symlink",
            "src": {
                "type": "symlink",
                "filepath": link_path,
                "target": str(target),
            },
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert (tmp_path / link_path).is_symlink()

    async def test_symlink_ignores_non_symlink_events(self, tmp_path):
        """Ignores symlink events whose payload is not a symlink."""
        event = {
            "name": "symlink",
            "src": {"type": "file", "filepath": "file.txt"},
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.os_symlink") as mock_symlink,
        ):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)
            mock_symlink.assert_not_called()
