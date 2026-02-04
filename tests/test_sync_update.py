# HSYNC Client - HTTP-based file synchronization client
# Copyright (C) 2025 Alberto Basso <alberto.sdk [at] gmail [dot] com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Tests for update operations in sync module."""

import os
from unittest.mock import patch, MagicMock

import pytest

from hsync_client.sync import update_dir, update_file
from hsync_client.models.file import File


class TestUpdateDir:
    """Tests for update_dir function."""

    def test_creates_new_directory(self, tmp_path):
        """New directory is created."""
        data = {
            "path": "new_dir",
            "permissions": "drwxr-xr-x",
        }

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = update_dir(data)
            assert result is not None
            assert os.path.exists(tmp_path / "new_dir")
            assert os.path.isdir(tmp_path / "new_dir")

    def test_creates_nested_directory(self, tmp_path):
        """Nested directory is created."""
        data = {
            "path": "level1/level2/level3",
            "permissions": "drwxr-xr-x",
        }

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = update_dir(data)
            assert result is not None
            assert os.path.exists(tmp_path / "level1" / "level2" / "level3")

    def test_updates_permissions_on_existing_directory(self, tmp_path):
        """Permissions are updated on existing directory."""
        existing_dir = tmp_path / "existing"
        existing_dir.mkdir()
        os.chmod(existing_dir, 0o777)

        data = {
            "path": "existing",
            "permissions": "drwxr-xr-x",
        }

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = update_dir(data)
            assert result is not None
            # Check permissions were updated
            assert (os.stat(existing_dir).st_mode & 0o777) == 0o755


class TestUpdateFile:
    """Tests for update_file function."""

    def test_downloads_new_file(self, tmp_path):
        """New file triggers download."""
        data = {
            "filepath": "new_file.txt",
            "name": "new_file.txt",
            "path": "",
            "mtime": 1000,
            "ctime": 1000,
            "size": 100,
            "md5sum": "abc123",
            "permissions": "-rw-r--r--",
        }

        mock_download = MagicMock()
        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}), \
             patch("hsync_client.transfer.download_file", mock_download):
            update_file(data)
            mock_download.assert_called_once()

    def test_downloads_file_with_different_md5(self, tmp_path):
        """File with different MD5 triggers download."""
        local_file = tmp_path / "file.txt"
        local_file.write_text("old content")

        data = {
            "filepath": "file.txt",
            "name": "file.txt",
            "path": "",
            "mtime": 2000,
            "ctime": 2000,
            "size": 11,
            "md5sum": "different_md5_hash",
            "permissions": "-rw-r--r--",
        }

        mock_download = MagicMock()
        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}), \
             patch("hsync_client.transfer.download_file", mock_download):
            update_file(data)
            mock_download.assert_called_once()

    def test_updates_permissions_when_only_permissions_differ(self, tmp_path):
        """Only permissions differ - updates without download."""
        local_file = tmp_path / "file.txt"
        local_file.write_text("content")
        file_obj = File.get(str(local_file))
        assert file_obj is not None

        data = {
            "filepath": "file.txt",
            "name": "file.txt",
            "path": "",
            "mtime": file_obj.mtime,
            "ctime": file_obj.ctime,
            "size": file_obj.size,
            "md5sum": file_obj.md5sum,
            "permissions": "-rwxrwxrwx",  # Different permissions
        }

        mock_set_perms = MagicMock()
        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}), \
             patch("hsync_client.sync.set_permissions", mock_set_perms):
            update_file(data)
            mock_set_perms.assert_called_once()

    def test_skips_file_with_no_changes(self, tmp_path):
        """File with no changes is skipped."""
        local_file = tmp_path / "file.txt"
        local_file.write_text("content")
        file_obj = File.get(str(local_file))
        assert file_obj is not None

        data = {
            "filepath": "file.txt",
            "name": "file.txt",
            "path": "",
            "mtime": file_obj.mtime,
            "ctime": file_obj.ctime,
            "size": file_obj.size,
            "md5sum": file_obj.md5sum,
            "permissions": file_obj.permissions,
        }

        # No download should occur - just check no exception is raised
        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            update_file(data)  # Should complete without error


class TestSync:
    """Tests for sync function."""

    @pytest.mark.asyncio
    async def test_sync_downloads_remote_files(self, tmp_path):
        """sync downloads files from remote."""
        from hsync_client.sync import sync

        remote_files = [
            {
                "filepath": "remote.txt",
                "name": "remote.txt",
                "path": "",
                "mtime": 1000,
                "ctime": 1000,
                "size": 10,
                "md5sum": "abc123",
                "permissions": "-rw-r--r--",
            }
        ]

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_ORDER": "mtime",
        }

        with patch("hsync_client.sync.CONFIG", config), \
             patch("hsync_client.sync.get_files_remote", return_value=remote_files), \
             patch("hsync_client.transfer.download_file") as mock_download, \
             patch("hsync_client.transfer.upload_file") as mock_upload:
            await sync()
            mock_download.assert_called_once()
            mock_upload.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_uploads_local_files(self, tmp_path):
        """sync uploads local files not on remote."""
        from hsync_client.sync import sync

        # Create local file
        local_file = tmp_path / "local.txt"
        local_file.write_text("content")

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_ORDER": "mtime",
        }

        with patch("hsync_client.sync.CONFIG", config), \
             patch("hsync_client.sync.get_files_remote", return_value=[]), \
             patch("hsync_client.transfer.download_file") as mock_download, \
             patch("hsync_client.transfer.upload_file") as mock_upload:
            await sync()
            mock_download.assert_not_called()
            mock_upload.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_handles_auth_error(self, tmp_path):
        """sync handles authentication errors gracefully."""
        from hsync_client.sync import sync
        from hsync_client.exceptions import AuthenticationError

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_ORDER": "mtime",
        }

        with patch("hsync_client.sync.CONFIG", config), \
             patch("hsync_client.sync.get_files_remote", side_effect=AuthenticationError("Invalid API key")), \
             patch("hsync_client.sync.is_debug", return_value=False):
            # Should not raise exception
            await sync()

    @pytest.mark.asyncio
    async def test_sync_handles_network_error(self, tmp_path):
        """sync handles network errors gracefully."""
        from hsync_client.sync import sync
        from hsync_client.exceptions import NetworkError

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_ORDER": "mtime",
        }

        with patch("hsync_client.sync.CONFIG", config), \
             patch("hsync_client.sync.get_files_remote", side_effect=NetworkError("Connection failed")), \
             patch("hsync_client.sync.is_debug", return_value=False):
            # Should not raise exception
            await sync()

    @pytest.mark.asyncio
    async def test_sync_handles_unexpected_error(self, tmp_path):
        """sync handles unexpected errors gracefully."""
        from hsync_client.sync import sync

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_ORDER": "mtime",
        }

        with patch("hsync_client.sync.CONFIG", config), \
             patch("hsync_client.sync.get_files_remote", side_effect=Exception("Unexpected")), \
             patch("hsync_client.sync.is_debug", return_value=False):
            # Should not raise exception
            await sync()

