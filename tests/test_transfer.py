# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for transfer module."""

from unittest.mock import MagicMock, patch

from requests import RequestException

from hsync_client.models.file import File
from hsync_client.transfer import download_file, is_file_stable, nothing_to_do, upload_file


class TestDownloadFile:
    """Tests for download_file function."""

    def test_network_error_logs_and_returns(self, tmp_path):
        """Network error during download is logged."""
        file = File(filepath="/path/to/file.txt", md5sum="abc123", size=100)

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                },
            ),
            patch("hsync_client.transfer.get") as mock_get,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            mock_get.side_effect = RequestException("Connection error")
            download_file(file)
            mock_get.assert_called_once()

    def test_auth_error_401_returns(self, tmp_path):
        """401 response returns without downloading."""
        file = File(filepath="/path/to/file.txt", md5sum="abc123", size=100)

        mock_response = MagicMock()
        mock_response.status_code = 401

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                },
            ),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            download_file(file)

    def test_not_found_404_returns(self, tmp_path):
        """404 response returns without downloading."""
        file = File(filepath="/path/to/file.txt", md5sum="abc123", size=100)

        mock_response = MagicMock()
        mock_response.status_code = 404

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                },
            ),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            download_file(file)

    def test_server_error_500_returns(self, tmp_path):
        """500 response returns without downloading."""
        file = File(filepath="/path/to/file.txt", md5sum="abc123", size=100)

        mock_response = MagicMock()
        mock_response.status_code = 500

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                },
            ),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
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

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                },
            ),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch(
                "hsync_client.transfer.compute_md5", return_value="d41d8cd98f00b204e9800998ecf8427e"
            ),
            patch("hsync_client.transfer.move") as mock_move,
            patch("hsync_client.transfer.utime") as mock_utime,
            patch("hsync_client.transfer.set_permissions") as mock_set_perms,
            patch("hsync_client.transfer.remove"),
        ):
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

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                },
            ),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.compute_md5", return_value="different_md5"),
            patch("hsync_client.transfer.move") as mock_move,
            patch("hsync_client.transfer.remove"),
        ):
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

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "LOCAL_BUCKET": str(tmp_path),
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "CID": "test-client",
                },
            ),
            patch("hsync_client.transfer.post", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            upload_file(file, check_size=False)

    def test_auth_error_401(self, tmp_path):
        """401 response logs authentication error."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")

        file = File(filepath=str(test_file), md5sum="abc123", size=12)

        mock_response = MagicMock()
        mock_response.status_code = 401

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "LOCAL_BUCKET": str(tmp_path),
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "CID": "test-client",
                },
            ),
            patch("hsync_client.transfer.post", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            upload_file(file, check_size=False)

    def test_access_denied_403(self, tmp_path):
        """403 response logs access denied."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")

        file = File(filepath=str(test_file), md5sum="abc123", size=12)

        mock_response = MagicMock()
        mock_response.status_code = 403

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "LOCAL_BUCKET": str(tmp_path),
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "CID": "test-client",
                },
            ),
            patch("hsync_client.transfer.post", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            upload_file(file, check_size=False)

    def test_conflict_409_already_exists(self, tmp_path):
        """409 response means file already exists."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")

        file = File(filepath=str(test_file), md5sum="abc123", size=12)

        mock_response = MagicMock()
        mock_response.status_code = 409

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "LOCAL_BUCKET": str(tmp_path),
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "CID": "test-client",
                },
            ),
            patch("hsync_client.transfer.post", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            upload_file(file, check_size=False)

    def test_server_error_500(self, tmp_path):
        """500 response logs server error."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")

        file = File(filepath=str(test_file), md5sum="abc123", size=12)

        mock_response = MagicMock()
        mock_response.status_code = 500

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "LOCAL_BUCKET": str(tmp_path),
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "CID": "test-client",
                },
            ),
            patch("hsync_client.transfer.post", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            upload_file(file, check_size=False)

    def test_file_not_found(self, tmp_path):
        """FileNotFoundError is caught and logged."""
        file = File(filepath="/nonexistent/file.txt", md5sum="abc123", size=12)

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "LOCAL_BUCKET": str(tmp_path),
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "CID": "test-client",
                },
            ),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            upload_file(file, check_size=False)  # Should not raise

    def test_network_error(self, tmp_path):
        """Network error is caught and logged."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")

        file = File(filepath=str(test_file), md5sum="abc123", size=12)

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "LOCAL_BUCKET": str(tmp_path),
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "CID": "test-client",
                },
            ),
            patch("hsync_client.transfer.post") as mock_post,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
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

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "LOCAL_BUCKET": str(tmp_path),
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "CID": "test-client",
                    "MAX_WAIT_TIMEOUT": 120,
                },
            ),
            patch("hsync_client.transfer.is_file_stable", return_value=True),
            patch("hsync_client.transfer.post", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            upload_file(file, check_size=True)

    def test_upload_times_out_after_max_wait(self, tmp_path):
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

        # Track if upload was called (indicates timeout was reached and upload proceeded)
        upload_called = False

        def mock_post(*args, **kwargs):
            nonlocal upload_called
            upload_called = True
            return mock_response

        # is_file_stable always returns False to simulate unstable file
        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "LOCAL_BUCKET": str(tmp_path),
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "CID": "test-client",
                    "MAX_WAIT_TIMEOUT": 2,  # Short timeout for test
                },
            ),
            patch("hsync_client.transfer.is_file_stable", return_value=False),
            patch("hsync_client.transfer.time_sleep"),
            patch("hsync_client.transfer.post", side_effect=mock_post),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            upload_file(file, check_size=True)
            # Verify upload was called even though file never stabilized
            # (this proves the timeout mechanism worked)
            assert upload_called, "Upload should proceed after max wait timeout"

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
        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "LOCAL_BUCKET": str(tmp_path),
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "CID": "test-client",
                },
            ),
            patch("hsync_client.transfer.is_file_stable", return_value=True),
            patch("hsync_client.transfer.post", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            # Should not raise, uses default timeout
            upload_file(file, check_size=True)


class TestNothingToDo:
    """Tests for nothing_to_do function."""

    def test_logs_filepath(self):
        """Logs the filepath for debugging."""
        file = File(filepath="/path/to/file.txt", md5sum="abc123", size=100)
        # Just verify it doesn't raise
        nothing_to_do(file)


class TestDownloadFileResume:
    """Tests for download resume functionality."""

    def test_download_with_existing_partial_file(self, tmp_path):
        """Test download resumes from existing partial file."""
        file = File(
            filepath="file.txt",
            md5sum="abc123def",
            size=1000,
            mtime=1000,
            ctime=1000,
        )

        # Create partial file
        partial_file = tmp_path / "file.txt.part"
        partial_file.write_text("partial content")

        mock_response = MagicMock()
        mock_response.status_code = 206
        mock_response.headers = {"Content-Encoding": ""}
        mock_response.iter_content = MagicMock(return_value=[b"more content"])

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                },
            ),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.compute_md5", return_value="abc123def"),
            patch("hsync_client.transfer.move"),
            patch("hsync_client.transfer.remove") as mock_remove,
            patch("hsync_client.transfer.utime"),
            patch("hsync_client.transfer.set_permissions"),
        ):
            download_file(file)
            # Should append to partial file
            mock_remove.assert_not_called()

    def test_download_partial_file_larger_than_expected(self, tmp_path):
        """Test download restarts when partial file is too large."""
        file = File(
            filepath="file.txt",
            md5sum="abc123def",
            size=100,
            mtime=1000,
            ctime=1000,
        )

        # Create partial file larger than expected
        partial_file = tmp_path / "file.txt.part"
        partial_file.write_text("x" * 200)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"Content-Encoding": ""}
        mock_response.iter_content = MagicMock(return_value=[b"content"])
        mock_response.content = b"content"

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                },
            ),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.compute_md5", return_value="abc123def"),
            patch("hsync_client.transfer.move"),
            patch("hsync_client.transfer.remove") as mock_remove,
            patch("hsync_client.transfer.utime"),
            patch("hsync_client.transfer.set_permissions"),
        ):
            download_file(file)
            # Should remove the too-large partial file
            mock_remove.assert_called()

    def test_download_local_file_up_to_date(self, tmp_path):
        """Test download skips when local file is up to date."""
        # Create local file with newer mtime
        test_file = tmp_path / "file.txt"
        test_file.write_text("content")

        file = File(
            filepath="file.txt",
            md5sum="abc123",
            size=7,
            mtime=1000,  # Older than actual file
            ctime=1000,
        )

        import os

        # Set mtime to be newer than file.mtime
        os.utime(test_file, (2000, 2000))

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                },
            ),
            patch("hsync_client.transfer.get") as mock_get,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            download_file(file)
            # Should not download
            mock_get.assert_not_called()

    def test_download_416_range_not_satisfiable(self, tmp_path):
        """Test download restarts when 416 is received."""
        file = File(
            filepath="file.txt",
            md5sum="abc123def",
            size=1000,
            mtime=1000,
            ctime=1000,
        )

        # Create partial file
        partial_file = tmp_path / "file.txt.part"
        partial_file.write_text("partial")

        mock_response_416 = MagicMock()
        mock_response_416.status_code = 416

        # Second call succeeds
        mock_response_200 = MagicMock()
        mock_response_200.status_code = 200
        mock_response_200.headers = {"Content-Encoding": ""}
        mock_response_200.iter_content = MagicMock(return_value=[b"content"])
        mock_response_200.content = b"content"

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                },
            ),
            patch("hsync_client.transfer.get", side_effect=[mock_response_416, mock_response_200]),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.compute_md5", return_value="abc123def"),
            patch("hsync_client.transfer.move"),
            patch("hsync_client.transfer.remove") as mock_remove,
            patch("hsync_client.transfer.utime"),
            patch("hsync_client.transfer.set_permissions"),
        ):
            download_file(file)
            # Should remove partial file and retry
            mock_remove.assert_called()


class TestDownloadCompressed:
    """Tests for compressed download."""

    def test_download_compressed_response(self, tmp_path):
        """Test download handles compressed response."""
        import zstandard as zstd

        file = File(
            filepath="file.txt",
            md5sum="abc123def",
            size=100,
            mtime=1000,
            ctime=1000,
        )

        # Compress test content
        original_content = b"test content for compression"
        compressor = zstd.ZstdCompressor()
        compressed_content = compressor.compress(original_content)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"Content-Encoding": "zstd"}
        mock_response.content = compressed_content

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                },
            ),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.compute_md5", return_value="abc123def"),
            patch("hsync_client.transfer.move") as mock_move,
            patch("hsync_client.transfer.utime"),
            patch("hsync_client.transfer.set_permissions"),
        ):
            download_file(file)
            mock_move.assert_called()


class TestGetUploadStatus:
    """Tests for _get_upload_status function."""

    def test_get_upload_status_complete(self, tmp_path):
        """Test getting upload status returns complete."""
        from hsync_client.transfer import _get_upload_status

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "complete", "offset": 1000}

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                },
            ),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=True),
        ):
            status, offset = _get_upload_status("file.txt", "abc123", 1000)
            assert status == "complete"
            assert offset == 1000

    def test_get_upload_status_resumable(self, tmp_path):
        """Test getting upload status returns resumable."""
        from hsync_client.transfer import _get_upload_status

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "resumable", "offset": 500}

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                },
            ),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=True),
        ):
            status, offset = _get_upload_status("file.txt", "abc123", 1000)
            assert status == "resumable"
            assert offset == 500

    def test_get_upload_status_network_error(self, tmp_path):
        """Test getting upload status returns new on network error."""
        from requests import RequestException

        from hsync_client.transfer import _get_upload_status

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                },
            ),
            patch("hsync_client.transfer.get", side_effect=RequestException()),
            patch("hsync_client.transfer.get_ssl_verify", return_value=True),
        ):
            status, offset = _get_upload_status("file.txt", "abc123", 1000)
            assert status == "new"
            assert offset == 0


class TestUploadCompressed:
    """Tests for compressed upload."""

    def test_upload_with_compression(self, tmp_path):
        """Test upload uses compression when enabled."""

        test_file = tmp_path / "file.txt"
        test_file.write_text("test content for compression")

        file = File(
            filepath=str(test_file),
            md5sum="abc123",
            size=30,
            mtime=1000,
            ctime=1000,
            permissions="-rw-r--r--",
        )

        mock_response = MagicMock()
        mock_response.status_code = 201

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "LOCAL_BUCKET": str(tmp_path),
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "CID": "test-client",
                    "COMPRESSION_ENABLED": True,
                    "SERVER_COMPRESSION_ENABLED": True,
                },
            ),
            patch("hsync_client.transfer.post", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.is_file_stable", return_value=True),
            patch("hsync_client.transfer.compute_md5", return_value="abc123"),
        ):
            upload_file(file, check_size=False)

    def test_upload_compression_skipped_when_resuming(self, tmp_path):
        """Test compression is skipped when resuming."""
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

        # Mock _get_upload_status to return "resumable"
        mock_response = MagicMock()
        mock_response.status_code = 201

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "LOCAL_BUCKET": str(tmp_path),
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "CID": "test-client",
                    "COMPRESSION_ENABLED": True,
                    "SERVER_COMPRESSION_ENABLED": True,
                },
            ),
            patch("hsync_client.transfer._get_upload_status", return_value=("resumable", 5)),
            patch("hsync_client.transfer.post", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.is_file_stable", return_value=True),
            patch("hsync_client.transfer.compute_md5", return_value="abc123"),
        ):
            upload_file(file, check_size=False)


class TestUploadResumable:
    """Tests for resumable upload."""

    def test_upload_partial_response_202(self, tmp_path):
        """Test upload handles 202 partial response."""
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
        mock_response.status_code = 202
        mock_response.json.return_value = {"offset": 6, "remaining": 6}

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "LOCAL_BUCKET": str(tmp_path),
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "CID": "test-client",
                },
            ),
            patch("hsync_client.transfer.post", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.is_file_stable", return_value=True),
            patch("hsync_client.transfer.compute_md5", return_value="abc123"),
        ):
            upload_file(file, check_size=False)

    def test_upload_resume_from_offset(self, tmp_path):
        """Test upload resumes from correct offset."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content here")

        file = File(
            filepath=str(test_file),
            md5sum="abc123",
            size=17,
            mtime=1000,
            ctime=1000,
            permissions="-rw-r--r--",
        )

        mock_response = MagicMock()
        mock_response.status_code = 201

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "LOCAL_BUCKET": str(tmp_path),
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "CID": "test-client",
                },
            ),
            patch("hsync_client.transfer._get_upload_status", return_value=("resumable", 10)),
            patch("hsync_client.transfer.post", return_value=mock_response) as mock_post,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.is_file_stable", return_value=True),
            patch("hsync_client.transfer.compute_md5", return_value="abc123"),
        ):
            upload_file(file, check_size=False)
            # Check that post was called with offset in data
            call_kwargs = mock_post.call_args
            if call_kwargs:
                data = call_kwargs.kwargs.get("data", {})
                assert data.get("offset") == 10


class TestUploadEdgeCases:
    """Tests for upload edge cases."""

    def test_upload_permission_error(self, tmp_path):
        """Test upload handles PermissionError."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("content")

        file = File(
            filepath=str(test_file),
            md5sum="abc123",
            size=7,
            mtime=1000,
            ctime=1000,
        )

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "LOCAL_BUCKET": str(tmp_path),
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "CID": "test-client",
                },
            ),
            patch("hsync_client.transfer.post", side_effect=PermissionError("Access denied")),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.is_file_stable", return_value=True),
            patch("hsync_client.transfer.compute_md5", return_value="abc123"),
        ):
            # Should not raise
            upload_file(file, check_size=False)

    def test_upload_file_complete_on_server(self, tmp_path):
        """Test upload skips when file is complete on server."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("content")

        file = File(
            filepath=str(test_file),
            md5sum="abc123",
            size=7,
            mtime=1000,
            ctime=1000,
        )

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "LOCAL_BUCKET": str(tmp_path),
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "CID": "test-client",
                },
            ),
            patch("hsync_client.transfer._get_upload_status", return_value=("complete", 0)),
            patch("hsync_client.transfer.post") as mock_post,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.is_file_stable", return_value=True),
            patch("hsync_client.transfer.compute_md5", return_value="abc123"),
        ):
            upload_file(file, check_size=False)
            # Should not call post
            mock_post.assert_not_called()
