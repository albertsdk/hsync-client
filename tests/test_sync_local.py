# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for local file operations in sync module."""

import os
from unittest.mock import patch

import pytest

from hsync_client.sync import get_files_local, get_sync_actions
from hsync_client.models.file import File
from hsync_client.models.sync_action import SyncActionName


class TestGetFilesLocal:
    """Tests for get_files_local function."""

    def test_returns_empty_list_for_empty_directory(self, tmp_path):
        """Empty directory returns empty list."""
        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "SYNC_ORDER": "mtime"}):
            result = get_files_local()
            assert result == []

    def test_returns_files_in_directory(self, tmp_path):
        """Files in directory are returned."""
        # Create test files
        (tmp_path / "file1.txt").write_text("content1")
        (tmp_path / "file2.txt").write_text("content2")

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "SYNC_ORDER": "mtime"}):
            result = get_files_local()
            assert len(result) == 2
            filepaths = [f["filepath"] for f in result]
            assert any("file1.txt" in fp for fp in filepaths)
            assert any("file2.txt" in fp for fp in filepaths)

    def test_skips_hidden_files(self, tmp_path):
        """Hidden files are skipped."""
        (tmp_path / "visible.txt").write_text("visible")
        (tmp_path / ".hidden").write_text("hidden")

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "SYNC_ORDER": "mtime"}):
            result = get_files_local()
            assert len(result) == 1
            assert "visible.txt" in result[0]["filepath"]

    def test_skips_hidden_directories(self, tmp_path):
        """Hidden directories are skipped."""
        visible_dir = tmp_path / "visible"
        visible_dir.mkdir()
        (visible_dir / "file.txt").write_text("visible")

        hidden_dir = tmp_path / ".hidden"
        hidden_dir.mkdir()
        (hidden_dir / "file.txt").write_text("hidden")

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "SYNC_ORDER": "mtime"}):
            result = get_files_local()
            assert len(result) == 1
            assert "visible" in result[0]["filepath"]

    def test_handles_nested_directories(self, tmp_path):
        """Files in nested directories are returned."""
        nested = tmp_path / "level1" / "level2"
        nested.mkdir(parents=True)
        (nested / "deep_file.txt").write_text("deep content")

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "SYNC_ORDER": "mtime"}):
            result = get_files_local()
            assert len(result) == 1
            assert "deep_file.txt" in result[0]["filepath"]


class TestGetSyncActions:
    """Tests for get_sync_actions function."""

    def test_returns_empty_list_for_no_files(self):
        """No files returns empty list."""
        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": "/tmp", "SYNC_ORDER": "mtime"}):
            result = get_sync_actions([], [])
            assert result == []

    def test_returns_empty_list_for_none_inputs(self):
        """None inputs returns empty list."""
        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": "/tmp", "SYNC_ORDER": "mtime"}):
            result = get_sync_actions(None, None)
            assert result == []

    def test_download_action_for_remote_only_file(self, tmp_path):
        """File on remote only triggers download."""
        remote_files = [{
            "filepath": str(tmp_path / "remote_file.txt"),
            "name": "remote_file.txt",
            "mtime": 1000,
            "ctime": 1000,
            "size": 100,
            "md5sum": "abc123",
            "permissions": "-rw-r--r--",
        }]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "SYNC_ORDER": "mtime"}):
            result = get_sync_actions(remote_files, [])
            assert len(result) == 1
            assert result[0].name == SyncActionName.download

    def test_upload_action_for_local_only_file(self, tmp_path):
        """File on local only triggers upload."""
        # Create local file
        local_file = tmp_path / "local_file.txt"
        local_file.write_text("local content")

        file = File.get(str(local_file))
        assert file is not None
        local_files = [file.to_dict()]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "SYNC_ORDER": "mtime"}):
            result = get_sync_actions([], local_files)
            assert len(result) == 1
            assert result[0].name == SyncActionName.upload

    def test_nothing_action_for_same_mtime(self, tmp_path):
        """Same mtime triggers no action."""
        local_file = tmp_path / "same_file.txt"
        local_file.write_text("content")
        mtime = int(os.path.getmtime(str(local_file)))

        local_file_obj = File.get(str(local_file))
        assert local_file_obj is not None
        local_file_dict = local_file_obj.to_dict()
        # Use relative filepath - the function will join with LOCAL_BUCKET
        remote_files = [{
            "filepath": "same_file.txt",
            "name": "same_file.txt",
            "mtime": mtime,
            "ctime": mtime,
            "size": 7,
            "md5sum": local_file_dict["md5sum"],
            "permissions": "-rw-r--r--",
        }]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "SYNC_ORDER": "mtime"}):
            result = get_sync_actions(remote_files, [local_file_dict])
            assert len(result) == 1
            assert result[0].name == SyncActionName.nothing

    def test_download_action_for_newer_remote(self, tmp_path):
        """Newer remote file triggers download."""
        local_file = tmp_path / "file.txt"
        local_file.write_text("content")
        local_mtime = int(os.path.getmtime(str(local_file)))

        local_file_obj = File.get(str(local_file))
        assert local_file_obj is not None
        local_file_dict = local_file_obj.to_dict()
        remote_files = [{
            "filepath": "file.txt",
            "name": "file.txt",
            "mtime": local_mtime + 1000,  # Remote is newer
            "ctime": local_mtime + 1000,
            "size": 7,
            "md5sum": "different_md5",
            "permissions": "-rw-r--r--",
        }]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "SYNC_ORDER": "mtime"}):
            result = get_sync_actions(remote_files, [local_file_dict])
            assert len(result) == 1
            assert result[0].name == SyncActionName.download

    def test_upload_action_for_newer_local(self, tmp_path):
        """Newer local file triggers upload."""
        local_file = tmp_path / "file.txt"
        local_file.write_text("content")
        local_mtime = int(os.path.getmtime(str(local_file)))

        local_file_obj = File.get(str(local_file))
        assert local_file_obj is not None
        local_file_dict = local_file_obj.to_dict()
        remote_files = [{
            "filepath": "file.txt",
            "name": "file.txt",
            "mtime": local_mtime - 1000,  # Remote is older
            "ctime": local_mtime - 1000,
            "size": 7,
            "md5sum": "different_md5",
            "permissions": "-rw-r--r--",
        }]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "SYNC_ORDER": "mtime"}):
            result = get_sync_actions(remote_files, [local_file_dict])
            assert len(result) == 1
            assert result[0].name == SyncActionName.upload

