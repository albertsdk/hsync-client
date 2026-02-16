# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for local file operations in sync module."""

import os
from unittest.mock import patch

from hsync_client.models.file import File
from hsync_client.models.sync_action import SyncActionName
from hsync_client.sync import get_files_local, get_sync_actions


class TestGetFilesLocal:
    """Tests for get_files_local function."""

    def test_returns_empty_list_for_empty_directory(self, tmp_path):
        """Empty directory returns empty list."""
        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_files_local()
            assert result == []

    def test_returns_files_in_directory(self, tmp_path):
        """Files in directory are returned."""
        # Create test files
        (tmp_path / "file1.txt").write_text("content1")
        (tmp_path / "file2.txt").write_text("content2")

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_files_local()
            assert len(result) == 2
            filepaths = [f["filepath"] for f in result]
            assert any("file1.txt" in fp for fp in filepaths)
            assert any("file2.txt" in fp for fp in filepaths)

    def test_skips_hidden_files(self, tmp_path):
        """Hidden files are skipped."""
        (tmp_path / "visible.txt").write_text("visible")
        (tmp_path / ".hidden").write_text("hidden")

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
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

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_files_local()
            assert len(result) == 1
            assert "visible" in result[0]["filepath"]

    def test_handles_nested_directories(self, tmp_path):
        """Files in nested directories are returned."""
        nested = tmp_path / "level1" / "level2"
        nested.mkdir(parents=True)
        (nested / "deep_file.txt").write_text("deep content")

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_files_local()
            assert len(result) == 1
            assert "deep_file.txt" in result[0]["filepath"]


class TestGetSyncActions:
    """Tests for get_sync_actions function."""

    def test_returns_empty_list_for_no_files(self):
        """No files returns empty list."""
        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": "/tmp"}):
            result = get_sync_actions([], [])
            assert result == []

    def test_returns_empty_list_for_none_inputs(self):
        """None inputs returns empty list."""
        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": "/tmp"}):
            result = get_sync_actions(None, None)
            assert result == []

    def test_download_action_for_remote_only_file(self, tmp_path):
        """File on remote only triggers download."""
        remote_files = [
            {
                "filepath": str(tmp_path / "remote_file.txt"),
                "name": "remote_file.txt",
                "mtime": 1000,
                "ctime": 1000,
                "size": 100,
                "md5sum": "abc123",
                "permissions": "-rw-r--r--",
            }
        ]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
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

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
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
        remote_files = [
            {
                "filepath": "same_file.txt",
                "name": "same_file.txt",
                "mtime": mtime,
                "ctime": mtime,
                "size": 7,
                "md5sum": local_file_dict["md5sum"],
                "permissions": "-rw-r--r--",
            }
        ]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
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
        remote_files = [
            {
                "filepath": "file.txt",
                "name": "file.txt",
                "mtime": local_mtime + 1000,  # Remote is newer
                "ctime": local_mtime + 1000,
                "size": 7,
                "md5sum": "different_md5",
                "permissions": "-rw-r--r--",
            }
        ]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
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
        remote_files = [
            {
                "filepath": "file.txt",
                "name": "file.txt",
                "mtime": local_mtime - 1000,  # Remote is older
                "ctime": local_mtime - 1000,
                "size": 7,
                "md5sum": "different_md5",
                "permissions": "-rw-r--r--",
            }
        ]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_sync_actions(remote_files, [local_file_dict])
            assert len(result) == 1
            assert result[0].name == SyncActionName.upload


class TestGetSyncActionsLanMode:
    """Tests for get_sync_actions function in LAN mode."""

    def test_delete_action_for_local_only_file_in_lan_mode(self, tmp_path):
        """Local-only file triggers delete action in LAN mode (master is source of truth)."""
        local_file = tmp_path / "local_only.txt"
        local_file.write_text("local content")

        file = File.get(str(local_file))
        assert file is not None
        local_files = [file.to_dict()]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_sync_actions([], local_files, lan_mode=True)
            assert len(result) == 1
            assert result[0].name == SyncActionName.delete

    def test_upload_action_for_local_only_file_in_server_mode(self, tmp_path):
        """Local-only file triggers upload action in server mode (default)."""
        local_file = tmp_path / "local_only.txt"
        local_file.write_text("local content")

        file = File.get(str(local_file))
        assert file is not None
        local_files = [file.to_dict()]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_sync_actions([], local_files, lan_mode=False)
            assert len(result) == 1
            assert result[0].name == SyncActionName.upload

    def test_download_action_for_remote_only_file_in_lan_mode(self, tmp_path):
        """Remote-only file triggers download action in LAN mode."""
        remote_files = [
            {
                "filepath": "remote_only.txt",
                "name": "remote_only.txt",
                "mtime": 1000000,
                "ctime": 1000000,
                "size": 10,
                "md5sum": "abc123",
                "permissions": "-rw-r--r--",
            }
        ]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_sync_actions(remote_files, [], lan_mode=True)
            assert len(result) == 1
            assert result[0].name == SyncActionName.download

    def test_lan_mode_does_not_affect_existing_files(self, tmp_path):
        """LAN mode doesn't change behavior for files that exist on both sides."""
        local_file = tmp_path / "both_sides.txt"
        local_file.write_text("content")
        mtime = int(os.path.getmtime(str(local_file)))

        local_file_obj = File.get(str(local_file))
        assert local_file_obj is not None
        local_file_dict = local_file_obj.to_dict()

        # Remote file with same mtime
        remote_files = [
            {
                "filepath": "both_sides.txt",
                "name": "both_sides.txt",
                "mtime": mtime,
                "ctime": mtime,
                "size": 7,
                "md5sum": local_file_dict["md5sum"],
                "permissions": "-rw-r--r--",
            }
        ]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_sync_actions(remote_files, [local_file_dict], lan_mode=True)
            assert len(result) == 1
            assert result[0].name == SyncActionName.nothing

    def test_lan_mode_download_newer_remote(self, tmp_path):
        """LAN mode downloads newer remote files."""
        local_file = tmp_path / "file.txt"
        local_file.write_text("old content")
        local_mtime = int(os.path.getmtime(str(local_file)))

        local_file_obj = File.get(str(local_file))
        assert local_file_obj is not None
        local_file_dict = local_file_obj.to_dict()

        # Remote file is newer
        remote_files = [
            {
                "filepath": "file.txt",
                "name": "file.txt",
                "mtime": local_mtime + 100,
                "ctime": local_mtime + 100,
                "size": 11,
                "md5sum": "different",
                "permissions": "-rw-r--r--",
            }
        ]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_sync_actions(remote_files, [local_file_dict], lan_mode=True)
            assert len(result) == 1
            assert result[0].name == SyncActionName.download
