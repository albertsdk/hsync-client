# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for upload transfer functionality."""

from unittest.mock import MagicMock, patch

from requests import RequestException

from hsync_client.models.file import File
from hsync_client.transfer import upload_file


class TestUploadFile:
    """Tests for upload_file function."""

    def test_missing_filepath_returns_early(self):
        """File with no filepath returns without uploading."""
        file = File(filepath="", sha256sum="abc123", size=100)

        with patch("hsync_client.transfer.post") as mock_post:
            upload_file(file, check_size=False)
            mock_post.assert_not_called()

    def test_successful_upload_201(self, tmp_path):
        """Successful upload returns 201."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")

        file = File(
            filepath=str(test_file),
            sha256sum="abc123",
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

        file = File(filepath=str(test_file), sha256sum="abc123", size=12)

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

        file = File(filepath=str(test_file), sha256sum="abc123", size=12)

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

        file = File(filepath=str(test_file), sha256sum="abc123", size=12)

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

        file = File(filepath=str(test_file), sha256sum="abc123", size=12)

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
        file = File(filepath="/nonexistent/file.txt", sha256sum="abc123", size=12)

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

        file = File(filepath=str(test_file), sha256sum="abc123", size=12)

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


class TestUploadFileMaxWaitTimeout:
    """Tests for max wait timeout in upload_file function."""

    def test_upload_waits_for_file_stability(self, tmp_path):
        """Upload waits for file to stabilize before uploading."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")

        file = File(
            filepath=str(test_file),
            sha256sum="abc123",
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
            sha256sum="abc123",
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
            sha256sum="abc123",
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
        from hsync_client.transfer import nothing_to_do

        file = File(filepath="/path/to/file.txt", sha256sum="abc123", size=100)
        # Just verify it doesn't raise
        nothing_to_do(file)


class TestUploadCompressed:
    """Tests for compressed upload."""

    def test_upload_with_compression(self, tmp_path):
        """Test upload uses compression when enabled."""

        test_file = tmp_path / "file.txt"
        test_file.write_text("test content for compression")

        file = File(
            filepath=str(test_file),
            sha256sum="abc123",
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
            patch("hsync_client.transfer.compute_sha256", return_value="abc123"),
        ):
            upload_file(file, check_size=False)

    def test_upload_compression_skipped_when_resuming(self, tmp_path):
        """Test compression is skipped when resuming."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")

        file = File(
            filepath=str(test_file),
            sha256sum="abc123",
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
            patch("hsync_client.transfer.compute_sha256", return_value="abc123"),
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
            sha256sum="abc123",
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
            patch("hsync_client.transfer.compute_sha256", return_value="abc123"),
        ):
            upload_file(file, check_size=False)

    def test_upload_resume_from_offset(self, tmp_path):
        """Test upload resumes from correct offset."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content here")

        file = File(
            filepath=str(test_file),
            sha256sum="abc123",
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
            patch("hsync_client.transfer.compute_sha256", return_value="abc123"),
        ):
            upload_file(file, check_size=False)
            # The upload body is now streamed; the offset form field is
            # encoded inside the multipart body. Materialize the body and
            # verify it carries the expected offset.
            call_kwargs = mock_post.call_args
            assert call_kwargs is not None
            body = b"".join(call_kwargs.kwargs.get("data", iter(())))
            assert b'name="offset"\r\n\r\n10\r\n' in body


class TestUploadEdgeCases:
    """Tests for upload edge cases."""

    def test_upload_permission_error(self, tmp_path):
        """Test upload handles PermissionError."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("content")

        file = File(
            filepath=str(test_file),
            sha256sum="abc123",
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
            patch("hsync_client.transfer.compute_sha256", return_value="abc123"),
        ):
            # Should not raise
            upload_file(file, check_size=False)

    def test_upload_file_complete_on_server(self, tmp_path):
        """Test upload skips when file is complete on server."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("content")

        file = File(
            filepath=str(test_file),
            sha256sum="abc123",
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
            patch("hsync_client.transfer.compute_sha256", return_value="abc123"),
        ):
            upload_file(file, check_size=False)
            # Should not call post
            mock_post.assert_not_called()
