# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for rename handling in the unified ``event_dispatch`` module."""

from unittest.mock import patch

from hsync_client.event_dispatch import apply_inbound_event
from hsync_client.remote_websocket import REMOTE_DISPATCH_CTX


class TestApplyInboundRenameEvent:
    """Tests for the ``renamed`` branch of :func:`apply_inbound_event`."""

    async def test_rename_directory_success(self, tmp_path):
        """Successfully renames directory when source exists."""
        src_dir = tmp_path / "source"
        src_dir.mkdir()
        dst_dir = tmp_path / "dest"

        event = {
            "name": "renamed",
            "src": {"path": "source", "permissions": "drwxr-xr-x"},
            "dst": {"path": "dest", "permissions": "drwxr-xr-x"},
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert dst_dir.exists()
        assert not src_dir.exists()

    async def test_rename_directory_source_not_found(self, tmp_path):
        """Recovers a missing source directory by re-fetching the destination."""
        event = {
            "name": "renamed",
            "src": {"path": "missing", "permissions": "drwxr-xr-x"},
            "dst": {"path": "dest", "permissions": "drwxr-xr-x"},
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.update_dir") as mock_update,
        ):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)
            mock_update.assert_called_once_with(event["dst"])

    async def test_rename_file_success(self, tmp_path):
        """Successfully renames file when source exists."""
        src_file = tmp_path / "source.txt"
        src_file.write_text("content")
        dst_file = tmp_path / "dest.txt"

        file_attrs = {
            "sha256sum": "abc123",
            "mtime": 1000,
            "ctime": 1000,
            "size": 7,
            "permissions": "-rw-r--r--",
        }
        event = {
            "name": "renamed",
            "src": {"filepath": "source.txt", **file_attrs},
            "dst": {"filepath": "dest.txt", **file_attrs},
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert dst_file.exists()
        assert not src_file.exists()

    async def test_rename_file_source_not_found(self, tmp_path):
        """Recovers a missing source file by downloading the destination."""
        file_attrs = {
            "sha256sum": "abc123",
            "mtime": 1000,
            "ctime": 1000,
            "size": 7,
            "permissions": "-rw-r--r--",
        }
        event = {
            "name": "renamed",
            "src": {"filepath": "missing.txt", **file_attrs},
            "dst": {"filepath": "dest.txt", **file_attrs},
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.download_file") as mock_dl,
        ):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)
            mock_dl.assert_called_once()
