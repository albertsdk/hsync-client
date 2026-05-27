# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for _handle_delete_event function in websocket_handler module."""

from unittest.mock import patch

from hsync_client.websocket_handler import _handle_delete_event


class TestHandleDeleteEvent:
    """Tests for _handle_delete_event function."""

    def test_delete_directory_success(self, tmp_path):
        """Successfully deletes existing directory."""
        dir_to_delete = tmp_path / "to_delete"
        dir_to_delete.mkdir()
        (dir_to_delete / "file.txt").write_text("content")

        # Use relative paths - functions add LOCAL_BUCKET prefix
        src = {"path": "to_delete", "permissions": "drwxr-xr-x"}

        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.websocket_handler.CONFIG", config):
            _handle_delete_event(src)

        assert not dir_to_delete.exists()

    def test_delete_directory_not_found(self, tmp_path):
        """Handles missing directory gracefully."""
        # Use relative paths
        src = {"path": "missing", "permissions": "drwxr-xr-x"}

        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.websocket_handler.CONFIG", config):
            # Should not raise exception
            _handle_delete_event(src)

    def test_delete_file_success(self, tmp_path):
        """Successfully deletes existing file."""
        file_to_delete = tmp_path / "to_delete.txt"
        file_to_delete.write_text("content")

        # Use relative paths
        src = {
            "filepath": "to_delete.txt",
            "sha256sum": "abc123",
            "mtime": 1000,
            "ctime": 1000,
            "size": 7,
            "permissions": "-rw-r--r--",
        }

        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.websocket_handler.CONFIG", config):
            _handle_delete_event(src)

        assert not file_to_delete.exists()

    def test_delete_file_not_found(self, tmp_path):
        """Handles missing file gracefully."""
        # Use relative paths
        src = {
            "filepath": "missing.txt",
            "sha256sum": "abc123",
            "mtime": 1000,
            "ctime": 1000,
            "size": 7,
            "permissions": "-rw-r--r--",
        }

        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.websocket_handler.CONFIG", config):
            # Should not raise exception
            _handle_delete_event(src)

    def test_delete_ignore_delete_mode(self, tmp_path):
        """Skips deletion when IGNORE_DELETE is True."""
        file_to_keep = tmp_path / "keep_me.txt"
        file_to_keep.write_text("content")

        src = {
            "filepath": "keep_me.txt",
            "sha256sum": "abc123",
            "mtime": 1000,
            "ctime": 1000,
            "size": 7,
            "permissions": "-rw-r--r--",
        }

        config = {"LOCAL_BUCKET": str(tmp_path), "IGNORE_DELETE": True}

        with patch("hsync_client.websocket_handler.CONFIG", config):
            _handle_delete_event(src)

        # File should still exist
        assert file_to_keep.exists()

    def test_delete_hsynckeep_cleans_empty_parent(self, tmp_path):
        """Deleting .hsynckeep removes empty parent directory."""
        subdir = tmp_path / "emptydir"
        subdir.mkdir()
        sentinel = subdir / ".hsynckeep"
        sentinel.write_text("")

        src = {
            "filepath": "emptydir/.hsynckeep",
            "sha256sum": "bb25b1c2b2a7427401c823a90904a759e2bd7425205b0702bca5e27e937e7cff",
            "mtime": 1000,
            "ctime": 1000,
            "size": 0,
            "permissions": "-rw-r--r--",
        }

        config = {"LOCAL_BUCKET": str(tmp_path), "IGNORE_DELETE": False}

        with patch("hsync_client.websocket_handler.CONFIG", config):
            _handle_delete_event(src)

        assert not sentinel.exists()
        assert not subdir.exists()
