# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for _handle_rename_event function in websocket_handler module."""

from unittest.mock import patch

from hsync_client.websocket_handler import _handle_rename_event


class TestHandleRenameEvent:
    """Tests for _handle_rename_event function."""

    def test_rename_directory_success(self, tmp_path):
        """Successfully renames directory when source exists."""
        src_dir = tmp_path / "source"
        src_dir.mkdir()
        dst_dir = tmp_path / "dest"

        # Use relative paths - functions add LOCAL_BUCKET prefix
        src = {"path": "source", "permissions": "drwxr-xr-x"}
        dst = {"path": "dest", "permissions": "drwxr-xr-x"}

        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.websocket_handler.CONFIG", config):
            _handle_rename_event(src, dst)

        assert dst_dir.exists()
        assert not src_dir.exists()

    def test_rename_directory_source_not_found(self, tmp_path):
        """Handles missing source directory by creating destination."""
        # Use relative paths
        src = {"path": "missing", "permissions": "drwxr-xr-x"}
        dst = {"path": "dest", "permissions": "drwxr-xr-x"}

        config = {"LOCAL_BUCKET": str(tmp_path)}

        with (
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.update_dir") as mock_update,
        ):
            _handle_rename_event(src, dst)
            mock_update.assert_called_once_with(dst)

    def test_rename_file_success(self, tmp_path):
        """Successfully renames file when source exists."""
        src_file = tmp_path / "source.txt"
        src_file.write_text("content")
        dst_file = tmp_path / "dest.txt"

        # Use relative paths
        src = {
            "filepath": "source.txt",
            "md5sum": "abc123",
            "mtime": 1000,
            "ctime": 1000,
            "size": 7,
            "permissions": "-rw-r--r--",
        }
        dst = {
            "filepath": "dest.txt",
            "md5sum": "abc123",
            "mtime": 1000,
            "ctime": 1000,
            "size": 7,
            "permissions": "-rw-r--r--",
        }

        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.websocket_handler.CONFIG", config):
            _handle_rename_event(src, dst)

        assert dst_file.exists()
        assert not src_file.exists()

    def test_rename_file_source_not_found(self, tmp_path):
        """Downloads destination file when source doesn't exist."""
        src = {
            "filepath": "missing.txt",
            "md5sum": "abc123",
            "mtime": 1000,
            "ctime": 1000,
            "size": 7,
            "permissions": "-rw-r--r--",
        }
        dst = {
            "filepath": "dest.txt",
            "md5sum": "abc123",
            "mtime": 1000,
            "ctime": 1000,
            "size": 7,
            "permissions": "-rw-r--r--",
        }

        config = {"LOCAL_BUCKET": str(tmp_path)}

        with (
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.download_file") as mock_dl,
        ):
            _handle_rename_event(src, dst)
            mock_dl.assert_called_once()
