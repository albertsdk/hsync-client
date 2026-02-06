# HSYNC Client - HTTP-based file synchronization client
# Copyright (C) 2025 Alberto Basso
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

"""Tests for transfer module."""

import pytest
from unittest.mock import patch, MagicMock, mock_open
from requests import RequestException

from hsync_client.models.file import File
from hsync_client.transfer import download_file, upload_file, nothing_to_do, is_file_stable


class TestDownloadFile:
    """Tests for download_file function."""

    def test_network_error_logs_and_returns(self, tmp_path):
        """Network error during download is logged."""
        file = File(filepath="/path/to/file.txt", md5sum="abc123", size=100)

        with patch("hsync_client.transfer.CONFIG", {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "test",
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "LOCAL_BUCKET": str(tmp_path),
        }), patch("hsync_client.transfer.get") as mock_get, \
             patch("hsync_client.transfer.get_ssl_verify", return_value=False):
            mock_get.side_effect = RequestException("Connection error")
            download_file(file)
            mock_get.assert_called_once()

    def test_auth_error_401_returns(self, tmp_path):
        """401 response returns without downloading."""
        file = File(filepath="/path/to/file.txt", md5sum="abc123", size=100)

        mock_response = MagicMock()
        mock_response.status_code = 401

        with patch("hsync_client.transfer.CONFIG", {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "test",
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "LOCAL_BUCKET": str(tmp_path),
        }), patch("hsync_client.transfer.get", return_value=mock_response), \
             patch("hsync_client.transfer.get_ssl_verify", return_value=False):
            download_file(file)

    def test_not_found_404_returns(self, tmp_path):
        """404 response returns without downloading."""
        file = File(filepath="/path/to/file.txt", md5sum="abc123", size=100)

        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch("hsync_client.transfer.CONFIG", {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "test",
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "LOCAL_BUCKET": str(tmp_path),
        }), patch("hsync_client.transfer.get", return_value=mock_response), \
             patch("hsync_client.transfer.get_ssl_verify", return_value=False):
            download_file(file)

    def test_server_error_500_returns(self, tmp_path):
        """500 response returns without downloading."""
        file = File(filepath="/path/to/file.txt", md5sum="abc123", size=100)

        mock_response = MagicMock()
        mock_response.status_code = 500

        with patch("hsync_client.transfer.CONFIG", {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "test",
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "LOCAL_BUCKET": str(tmp_path),
        }), patch("hsync_client.transfer.get", return_value=mock_response), \
             patch("hsync_client.transfer.get_ssl_verify", return_value=False):
            download_file(file)

    def test_successful_download_with_md5_verification(self, tmp_path):
        """Successful download verifies MD5 and moves file."""
        file = File(
            filepath="subdir/file.txt",
            md5sum="d41d8cd98f00b204e9800998ecf8427e",  # MD5 of empty file
            size=0,
            mtime=1000,
            ctime=1000,
            permissions="-rw-r--r--",
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.iter_content = MagicMock(return_value=[b""])

        with patch("hsync_client.transfer.CONFIG", {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "test",
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "LOCAL_BUCKET": str(tmp_path),
        }), patch("hsync_client.transfer.get", return_value=mock_response), \
             patch("hsync_client.transfer.get_ssl_verify", return_value=False), \
             patch("hsync_client.transfer.compute_md5", return_value="d41d8cd98f00b204e9800998ecf8427e"), \
             patch("hsync_client.transfer.move") as mock_move, \
             patch("hsync_client.transfer.utime") as mock_utime, \
             patch("hsync_client.transfer.set_permissions") as mock_set_perms, \
             patch("hsync_client.transfer.remove"):
            download_file(file)
            mock_move.assert_called_once()
            mock_utime.assert_called_once()
            mock_set_perms.assert_called_once()

    def test_md5_mismatch_returns_without_saving(self, tmp_path):
        """MD5 mismatch logs error and returns."""
        file = File(filepath="file.txt", md5sum="expected_md5", size=100)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.iter_content = MagicMock(return_value=[b"test content"])

        with patch("hsync_client.transfer.CONFIG", {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "test",
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "LOCAL_BUCKET": str(tmp_path),
        }), patch("hsync_client.transfer.get", return_value=mock_response), \
             patch("hsync_client.transfer.get_ssl_verify", return_value=False), \
             patch("hsync_client.transfer.compute_md5", return_value="different_md5"), \
             patch("hsync_client.transfer.move") as mock_move, \
             patch("hsync_client.transfer.remove"):
            download_file(file)
            mock_move.assert_not_called()


class TestUploadFile:
    """Tests for upload_file function."""

    def test_missing_filepath_returns_early(self):
        """File with no filepath returns without uploading."""
        file = File(filepath="", md5sum="abc123", size=100)

        with patch("hsync_client.transfer.post") as mock_post:
            upload_file(file, check_size=False)
            mock_post.assert_not_called()

    def test_successful_upload_201(self, tmp_path):
        """Successful upload returns 201."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")

        file = File(
            filepath=str(test_file),
            md5sum="abc123",
            size=12,
            mtime=1000,
            ctime=1000,
            permissions="-rw-r--r--",
        )

        mock_response = MagicMock()
        mock_response.status_code = 201

        with patch("hsync_client.transfer.CONFIG", {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "test",
            "LOCAL_BUCKET": str(tmp_path),
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "CID": "test-client",
        }), patch("hsync_client.transfer.post", return_value=mock_response), \
             patch("hsync_client.transfer.get_ssl_verify", return_value=False):
            upload_file(file, check_size=False)

    def test_auth_error_401(self, tmp_path):
        """401 response logs authentication error."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")

        file = File(filepath=str(test_file), md5sum="abc123", size=12)

        mock_response = MagicMock()
        mock_response.status_code = 401

        with patch("hsync_client.transfer.CONFIG", {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "test",
            "LOCAL_BUCKET": str(tmp_path),
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "CID": "test-client",
        }), patch("hsync_client.transfer.post", return_value=mock_response), \
             patch("hsync_client.transfer.get_ssl_verify", return_value=False):
            upload_file(file, check_size=False)

    def test_access_denied_403(self, tmp_path):
        """403 response logs access denied."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")

        file = File(filepath=str(test_file), md5sum="abc123", size=12)

        mock_response = MagicMock()
        mock_response.status_code = 403

        with patch("hsync_client.transfer.CONFIG", {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "test",
            "LOCAL_BUCKET": str(tmp_path),
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "CID": "test-client",
        }), patch("hsync_client.transfer.post", return_value=mock_response), \
             patch("hsync_client.transfer.get_ssl_verify", return_value=False):
            upload_file(file, check_size=False)

    def test_conflict_409_already_exists(self, tmp_path):
        """409 response means file already exists."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")

        file = File(filepath=str(test_file), md5sum="abc123", size=12)

        mock_response = MagicMock()
        mock_response.status_code = 409

        with patch("hsync_client.transfer.CONFIG", {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "test",
            "LOCAL_BUCKET": str(tmp_path),
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "CID": "test-client",
        }), patch("hsync_client.transfer.post", return_value=mock_response), \
             patch("hsync_client.transfer.get_ssl_verify", return_value=False):
            upload_file(file, check_size=False)

    def test_server_error_500(self, tmp_path):
        """500 response logs server error."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")

        file = File(filepath=str(test_file), md5sum="abc123", size=12)

        mock_response = MagicMock()
        mock_response.status_code = 500

        with patch("hsync_client.transfer.CONFIG", {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "test",
            "LOCAL_BUCKET": str(tmp_path),
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "CID": "test-client",
        }), patch("hsync_client.transfer.post", return_value=mock_response), \
             patch("hsync_client.transfer.get_ssl_verify", return_value=False):
            upload_file(file, check_size=False)

    def test_file_not_found(self, tmp_path):
        """FileNotFoundError is caught and logged."""
        file = File(filepath="/nonexistent/file.txt", md5sum="abc123", size=12)

        with patch("hsync_client.transfer.CONFIG", {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "test",
            "LOCAL_BUCKET": str(tmp_path),
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "CID": "test-client",
        }), patch("hsync_client.transfer.get_ssl_verify", return_value=False):
            upload_file(file, check_size=False)  # Should not raise

    def test_network_error(self, tmp_path):
        """Network error is caught and logged."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")

        file = File(filepath=str(test_file), md5sum="abc123", size=12)

        with patch("hsync_client.transfer.CONFIG", {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "test",
            "LOCAL_BUCKET": str(tmp_path),
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "CID": "test-client",
        }), patch("hsync_client.transfer.post") as mock_post, \
             patch("hsync_client.transfer.get_ssl_verify", return_value=False):
            mock_post.side_effect = RequestException("Connection error")
            upload_file(file, check_size=False)


class TestIsFileStable:
    """Tests for is_file_stable function."""

    def test_empty_filepath_returns_true(self):
        """Empty filepath returns True (stable)."""
        file = File(filepath="", md5sum="abc123", size=100)
        assert is_file_stable(file) is True

    def test_stable_file_returns_true(self, tmp_path):
        """File with unchanged size returns True."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")

        file = File(filepath=str(test_file), md5sum="abc123", size=12)

        with patch("hsync_client.transfer.time_sleep"):  # Skip waiting
            result = is_file_stable(file, interval=0)
            assert result is True


class TestUploadFileMaxWaitTimeout:
    """Tests for max wait timeout in upload_file function."""

    def test_upload_waits_for_file_stability(self, tmp_path):
        """Upload waits for file to stabilize before uploading."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")

        file = File(
            filepath=str(test_file),
            md5sum="abc123",
            size=12,
            mtime=1000,
            ctime=1000,
            permissions="-rw-r--r--",
        )

        mock_response = MagicMock()
        mock_response.status_code = 201

        with patch("hsync_client.transfer.CONFIG", {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "test",
            "LOCAL_BUCKET": str(tmp_path),
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "CID": "test-client",
            "MAX_WAIT_TIMEOUT": 120,
        }), patch("hsync_client.transfer.is_file_stable", return_value=True), \
             patch("hsync_client.transfer.post", return_value=mock_response), \
             patch("hsync_client.transfer.get_ssl_verify", return_value=False):
            upload_file(file, check_size=True)

    def test_upload_times_out_after_max_wait(self, tmp_path, caplog):
        """Upload proceeds after max wait timeout even if file not stable."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")

        file = File(
            filepath=str(test_file),
            md5sum="abc123",
            size=12,
            mtime=1000,
            ctime=1000,
            permissions="-rw-r--r--",
        )

        mock_response = MagicMock()
        mock_response.status_code = 201

        # is_file_stable always returns False to simulate unstable file
        with patch("hsync_client.transfer.CONFIG", {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "test",
            "LOCAL_BUCKET": str(tmp_path),
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "CID": "test-client",
            "MAX_WAIT_TIMEOUT": 2,  # Short timeout for test
        }), patch("hsync_client.transfer.is_file_stable", return_value=False), \
             patch("hsync_client.transfer.time_sleep"), \
             patch("hsync_client.transfer.post", return_value=mock_response), \
             patch("hsync_client.transfer.get_ssl_verify", return_value=False):
            upload_file(file, check_size=True)
            assert "did not stabilize within 2s" in caplog.text

    def test_upload_uses_default_max_wait_timeout(self, tmp_path):
        """Upload uses default timeout when not configured."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")

        file = File(
            filepath=str(test_file),
            md5sum="abc123",
            size=12,
            mtime=1000,
            ctime=1000,
            permissions="-rw-r--r--",
        )

        mock_response = MagicMock()
        mock_response.status_code = 201

        # CONFIG without MAX_WAIT_TIMEOUT should use default (120)
        with patch("hsync_client.transfer.CONFIG", {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "test",
            "LOCAL_BUCKET": str(tmp_path),
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "CID": "test-client",
        }), patch("hsync_client.transfer.is_file_stable", return_value=True), \
             patch("hsync_client.transfer.post", return_value=mock_response), \
             patch("hsync_client.transfer.get_ssl_verify", return_value=False):
            # Should not raise, uses default timeout
            upload_file(file, check_size=True)


class TestNothingToDo:
    """Tests for nothing_to_do function."""

    def test_logs_filepath(self):
        """Logs the filepath for debugging."""
        file = File(filepath="/path/to/file.txt", md5sum="abc123", size=100)
        # Just verify it doesn't raise
        nothing_to_do(file)

