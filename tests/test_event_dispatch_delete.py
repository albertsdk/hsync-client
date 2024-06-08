# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for delete handling in the unified ``event_dispatch`` module."""

from unittest.mock import patch

from hsync_client.event_dispatch import apply_inbound_event
from hsync_client.remote_websocket import REMOTE_DISPATCH_CTX


class TestApplyInboundDeleteEvent:
    """Tests for the ``deleted`` branch of :func:`apply_inbound_event`."""

    async def test_delete_directory_success(self, tmp_path):
        """Successfully deletes existing directory."""
        dir_to_delete = tmp_path / "to_delete"
        dir_to_delete.mkdir()
        (dir_to_delete / "file.txt").write_text("content")

        event = {
            "name": "deleted",
            "src": {"path": "to_delete", "permissions": "drwxr-xr-x"},
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert not dir_to_delete.exists()

    async def test_delete_directory_not_found(self, tmp_path):
        """Handles missing directory gracefully."""
        event = {
            "name": "deleted",
            "src": {"path": "missing", "permissions": "drwxr-xr-x"},
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

    async def test_delete_file_success(self, tmp_path):
        """Successfully deletes existing file."""
        file_to_delete = tmp_path / "to_delete.txt"
        file_to_delete.write_text("content")

        event = {
            "name": "deleted",
            "src": {
                "filepath": "to_delete.txt",
                "sha256sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 7,
                "permissions": "-rw-r--r--",
            },
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert not file_to_delete.exists()

    async def test_delete_file_not_found(self, tmp_path):
        """Handles missing file gracefully."""
        event = {
            "name": "deleted",
            "src": {
                "filepath": "missing.txt",
                "sha256sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 7,
                "permissions": "-rw-r--r--",
            },
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

    async def test_delete_ignore_delete_mode(self, tmp_path):
        """Skips deletion when ``IGNORE_DELETE`` is true."""
        file_to_keep = tmp_path / "keep_me.txt"
        file_to_keep.write_text("content")

        event = {
            "name": "deleted",
            "src": {
                "filepath": "keep_me.txt",
                "sha256sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 7,
                "permissions": "-rw-r--r--",
            },
        }
        config = {"LOCAL_BUCKET": str(tmp_path), "IGNORE_DELETE": True}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert file_to_keep.exists()

    async def test_delete_hsynckeep_cleans_empty_parent(self, tmp_path):
        """Deleting ``.hsynckeep`` removes the now-empty parent directory."""
        subdir = tmp_path / "emptydir"
        subdir.mkdir()
        sentinel = subdir / ".hsynckeep"
        sentinel.write_text("")

        event = {
            "name": "deleted",
            "src": {
                "filepath": "emptydir/.hsynckeep",
                "sha256sum": "bb25b1c2b2a7427401c823a90904a759e2bd7425205b0702bca5e27e937e7cff",
                "mtime": 1000,
                "ctime": 1000,
                "size": 0,
                "permissions": "-rw-r--r--",
            },
        }
        config = {"LOCAL_BUCKET": str(tmp_path), "IGNORE_DELETE": False}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert not sentinel.exists()
        assert not subdir.exists()
