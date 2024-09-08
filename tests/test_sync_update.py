# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for update operations in sync module."""

import os
from unittest.mock import MagicMock, patch

import pytest
from hsync_common.models.file import File

from hsync_client.sync import update_dir, update_file


class TestUpdateDir:
    """Tests for update_dir function."""

    def test_creates_new_directory(self, tmp_path):
        """New directory is created."""
        data = {
            "path": "new_dir",
            "permissions": "755",
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
            "permissions": "755",
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
            "permissions": "755",
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
            "sha256sum": "abc123",
            "permissions": "644",
        }

        mock_download = MagicMock()
        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.transfer.download_file", mock_download),
        ):
            update_file(data)
            mock_download.assert_called_once()

    def test_downloads_file_with_different_sha256(self, tmp_path):
        """File with different SHA-256 (older locally) triggers download, no conflict copy."""
        local_file = tmp_path / "file.txt"
        local_file.write_text("old content")
        # Local file is clearly older than the incoming event - normal update.
        os.utime(local_file, (1000, 1000))

        data = {
            "filepath": "file.txt",
            "name": "file.txt",
            "path": "",
            "mtime": 2000,
            "ctime": 2000,
            "size": 11,
            "sha256sum": "different_sha256_hash",
            "permissions": "644",
        }

        mock_download = MagicMock()
        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.transfer.download_file", mock_download),
        ):
            update_file(data)
            mock_download.assert_called_once()
            # Local copy was older, so no conflict copy should be created.
            assert list(tmp_path.glob("file.txt.conflict-*")) == []

    def test_saves_conflict_copy_when_local_not_older(self, tmp_path):
        """A conflicting inbound event preserves the local copy before overwriting."""
        local_file = tmp_path / "file.txt"
        local_file.write_text("local change")
        # Local mtime ties the incoming event mtime but content differs.
        os.utime(local_file, (2000, 2000))

        data = {
            "filepath": "file.txt",
            "name": "file.txt",
            "path": "",
            "mtime": 2000,
            "ctime": 2000,
            "size": 12,
            "sha256sum": "different_sha256_hash",
            "permissions": "644",
        }

        mock_download = MagicMock()
        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.transfer.download_file", mock_download),
        ):
            update_file(data)
            mock_download.assert_called_once()
            conflict_copies = list(tmp_path.glob("file.txt.conflict-*"))
            assert len(conflict_copies) == 1
            assert conflict_copies[0].read_text() == "local change"

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
            "sha256sum": file_obj.sha256sum,
            "permissions": "777",  # Different permissions
        }

        mock_set_perms = MagicMock()
        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.sync.set_permissions", mock_set_perms),
        ):
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
            "sha256sum": file_obj.sha256sum,
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
                "sha256sum": "abc123",
                "permissions": "644",
            }
        ]

        config = {
            "LOCAL_BUCKET": str(tmp_path),
        }

        with (
            patch("hsync_client.sync.CONFIG", config),
            patch("hsync_client.sync.get_files_remote", return_value=remote_files),
            patch("hsync_client.sync.get_tombstones_remote", return_value={}),
            patch("hsync_client.sync.ensure_hsynckeep_files"),
            patch("hsync_client.transfer.download_file") as mock_download,
            patch("hsync_client.transfer.upload_file") as mock_upload,
        ):
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
        }

        with (
            patch("hsync_client.sync.CONFIG", config),
            patch("hsync_client.sync.get_files_remote", return_value=[]),
            patch("hsync_client.sync.get_tombstones_remote", return_value={}),
            patch("hsync_client.transfer.download_file") as mock_download,
            patch("hsync_client.transfer.upload_file") as mock_upload,
        ):
            await sync()
            mock_download.assert_not_called()
            mock_upload.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_handles_auth_error(self, tmp_path):
        """sync handles authentication errors gracefully."""
        from hsync_client.exceptions import AuthenticationError
        from hsync_client.sync import sync

        config = {
            "LOCAL_BUCKET": str(tmp_path),
        }

        with (
            patch("hsync_client.sync.CONFIG", config),
            patch(
                "hsync_client.sync.get_files_remote",
                side_effect=AuthenticationError("Invalid API key"),
            ),
            patch("hsync_client.sync.is_debug", return_value=False),
        ):
            # Should not raise exception
            await sync()

    @pytest.mark.asyncio
    async def test_sync_handles_network_error(self, tmp_path):
        """sync handles network errors gracefully."""
        from hsync_client.exceptions import NetworkError
        from hsync_client.sync import sync

        config = {
            "LOCAL_BUCKET": str(tmp_path),
        }

        with (
            patch("hsync_client.sync.CONFIG", config),
            patch(
                "hsync_client.sync.get_files_remote", side_effect=NetworkError("Connection failed")
            ),
            patch("hsync_client.sync.is_debug", return_value=False),
        ):
            # Should not raise exception
            await sync()

    @pytest.mark.asyncio
    async def test_sync_handles_unexpected_error(self, tmp_path):
        """sync handles unexpected errors gracefully."""
        from hsync_client.sync import sync

        config = {
            "LOCAL_BUCKET": str(tmp_path),
        }

        with (
            patch("hsync_client.sync.CONFIG", config),
            patch("hsync_client.sync.get_files_remote", side_effect=Exception("Unexpected")),
            patch("hsync_client.sync.is_debug", return_value=False),
        ):
            # Should not raise exception
            await sync()
