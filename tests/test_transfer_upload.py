# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for upload transfer functionality."""

from unittest.mock import MagicMock, patch

import pytest
from requests import RequestException

from hsync_client.events import UiEventName
from hsync_client.models.file import File
from hsync_client.transfer import upload_file

_BASE_CONFIG = {
    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
    "REMOTE_BUCKET": "test",
    "REQUEST_HEADERS": {"X-API-Key": "test"},
    "CID": "test-client",
    # Disable retry/backoff (§11) so error-status paths resolve in one attempt.
    "TRANSFER_RETRY_ATTEMPTS": 1,
}


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
                    # Disable retry/backoff (§11): a 5xx is raised and surfaced once.
                    "TRANSFER_RETRY_ATTEMPTS": 1,
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
                    # Disable retry/backoff (§11) so a single attempt is asserted.
                    "TRANSFER_RETRY_ATTEMPTS": 1,
                },
            ),
            patch("hsync_client.transfer.post") as mock_post,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            mock_post.side_effect = RequestException("Connection error")
            upload_file(file, check_size=False)
            # Retry disabled (§11): the single failing attempt is asserted.
            mock_post.assert_called_once()


class TestUploadRetry:
    """Retry/backoff and transfer_failed surfacing for uploads (§11)."""

    def test_transient_error_retries_then_emits_transfer_failed(self, tmp_path):
        """A persistent 5xx is retried then surfaced as transfer_failed."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")

        file = File(filepath=str(test_file), sha256sum="abc123", size=12)

        mock_response = MagicMock()
        mock_response.status_code = 500

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    **_BASE_CONFIG,
                    "LOCAL_BUCKET": str(tmp_path),
                    "TRANSFER_RETRY_ATTEMPTS": 3,
                    "TRANSFER_RETRY_MIN_WAIT": 0,
                    "TRANSFER_RETRY_MAX_WAIT": 0,
                },
            ),
            patch("hsync_client.transfer._get_upload_status", return_value=("new", 0)),
            patch("hsync_client.transfer.post", return_value=mock_response) as mock_post,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.emit_event") as mock_emit,
        ):
            upload_file(file, check_size=False)
            # Exhausted all attempts before giving up.
            assert mock_post.call_count == 3
            # Surfaced exactly one transfer_failed event for the UI.
            failed = [
                c for c in mock_emit.call_args_list if c.args[0] == UiEventName.transfer_failed
            ]
            assert len(failed) == 1
            assert failed[0].kwargs["op"] == "upload"


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

    def test_upload_reschedules_after_max_wait(self, tmp_path):
        """An unstable file is rescheduled (not force-uploaded) after timeout (§15)."""
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

        # is_file_stable always returns False to simulate an unstable file
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
            patch("hsync_client.transfer.post") as mock_post,
            patch("hsync_client.transfer._reschedule_upload") as mock_reschedule,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            upload_file(file, check_size=True)
            # The file never stabilized, so the upload must be deferred, not forced.
            mock_post.assert_not_called()
            mock_reschedule.assert_called_once_with(file, None)

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


class TestRescheduleUpload:
    """Tests for _reschedule_upload deferred-upload logic (§15)."""

    def _make_file(self, tmp_path):
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")
        return File(
            filepath=str(test_file),
            sha256sum="abc123",
            size=12,
            mtime=1000,
            ctime=1000,
            permissions="-rw-r--r--",
        )

    def test_schedules_timer_and_decrements(self, tmp_path):
        """A deferred upload arms a daemon Timer with one fewer attempt left."""
        from hsync_client.transfer import _reschedule_upload

        file = self._make_file(tmp_path)

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {"UPLOAD_RESCHEDULE_DELAY": 7},
            ),
            patch("hsync_client.transfer.threading.Timer") as mock_timer,
        ):
            _reschedule_upload(file, reschedules_left=3)

            args, kwargs = mock_timer.call_args
            # delay, target callable
            assert args[0] == 7
            assert args[1] is upload_file
            # re-arms upload_file(file, True, 2)
            assert kwargs["args"] == (file, True, 2)
            assert mock_timer.return_value.daemon is True
            mock_timer.return_value.start.assert_called_once()

    def test_resolves_max_from_config_when_none(self, tmp_path):
        """A first deferral (None) resolves the bound from CONFIG."""
        from hsync_client.transfer import _reschedule_upload

        file = self._make_file(tmp_path)

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {"UPLOAD_MAX_RESCHEDULES": 4, "UPLOAD_RESCHEDULE_DELAY": 1},
            ),
            patch("hsync_client.transfer.threading.Timer") as mock_timer,
        ):
            _reschedule_upload(file, reschedules_left=None)

            # Resolved 4 from CONFIG, then decremented to 3 for the next attempt.
            assert mock_timer.call_args.kwargs["args"] == (file, True, 3)

    def test_exhausted_does_not_schedule(self, tmp_path):
        """Once reschedules are exhausted the file is skipped, not deferred."""
        from hsync_client.transfer import _reschedule_upload

        file = self._make_file(tmp_path)

        with (
            patch("hsync_client.transfer.CONFIG", {}),
            patch("hsync_client.transfer.threading.Timer") as mock_timer,
        ):
            _reschedule_upload(file, reschedules_left=0)
            mock_timer.assert_not_called()


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

    def test_compressed_branch_sends_zstd_header(self, tmp_path):
        """When should_compress() is True the body is zstd-compressed (201 path)."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("compress me " * 50)

        file = File(
            filepath=str(test_file),
            sha256sum="abc123",
            size=test_file.stat().st_size,
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
            patch("hsync_client.transfer._get_upload_status", return_value=("new", 0)),
            patch("hsync_client.transfer.should_compress", return_value=True),
            patch("hsync_client.transfer.post", return_value=mock_response) as mock_post,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.is_file_stable", return_value=True),
        ):
            upload_file(file, check_size=False)
            kwargs = mock_post.call_args.kwargs
            assert kwargs["headers"].get("Content-Encoding") == "zstd"
            assert kwargs["data"]["compressed"] == "true"
            assert kwargs["data"]["compressed_size"] > 0

    def test_compressed_branch_wraps_throttled_reader(self, tmp_path):
        """An upload bandwidth limit wraps the compressed body in a throttled reader."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("compress me " * 50)

        file = File(
            filepath=str(test_file),
            sha256sum="abc123",
            size=test_file.stat().st_size,
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
                    "MAX_BANDWIDTH_UPLOAD": 1024,
                },
            ),
            patch("hsync_client.transfer._get_upload_status", return_value=("new", 0)),
            patch("hsync_client.transfer.should_compress", return_value=True),
            patch("hsync_client.transfer.post", return_value=mock_response) as mock_post,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.is_file_stable", return_value=True),
        ):
            from hsync_client.transfer import _ThrottledFileReader

            upload_file(file, check_size=False)
            files = mock_post.call_args.kwargs["files"]
            assert isinstance(files["file"][1], _ThrottledFileReader)

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


class TestThrottledFileReader:
    """Tests for the _ThrottledFileReader wrapper."""

    def test_proxies_unknown_attributes(self):
        """Attributes not defined on the wrapper proxy to the inner file."""
        from io import BytesIO

        from hsync_client.transfer import _ThrottledFileReader

        inner = BytesIO(b"payload")
        reader = _ThrottledFileReader(inner, bandwidth_limit=None)
        # ``name`` only exists on the inner object → proxied via __getattr__
        assert reader.seekable() is True
        reader.seek(0)
        assert reader.tell() == 0

    def test_read_without_limit_skips_throttle(self):
        """No bandwidth limit means no delay is computed."""
        from io import BytesIO

        from hsync_client.transfer import _ThrottledFileReader

        reader = _ThrottledFileReader(BytesIO(b"abcdef"), bandwidth_limit=None)
        with patch("hsync_client.transfer.get_throttle_delay") as mock_delay:
            assert reader.read() == b"abcdef"
            mock_delay.assert_not_called()

    def test_read_with_limit_applies_delay(self):
        """A positive delay triggers a sleep after the read."""
        from io import BytesIO

        from hsync_client.transfer import _ThrottledFileReader

        reader = _ThrottledFileReader(
            BytesIO(b"abcdef"), bandwidth_limit=1024, transfer_key="upload"
        )
        with (
            patch("hsync_client.transfer.get_throttle_delay", return_value=0.01) as mock_delay,
            patch("hsync_client.transfer.time_sleep") as mock_sleep,
        ):
            assert reader.read() == b"abcdef"
            mock_delay.assert_called_once()
            mock_sleep.assert_called_once_with(0.01)

    def test_read_with_limit_zero_delay_no_sleep(self):
        """A zero delay does not call sleep even with a limit set."""
        from io import BytesIO

        from hsync_client.transfer import _ThrottledFileReader

        reader = _ThrottledFileReader(BytesIO(b"abcdef"), bandwidth_limit=1024)
        with (
            patch("hsync_client.transfer.get_throttle_delay", return_value=0.0),
            patch("hsync_client.transfer.time_sleep") as mock_sleep,
        ):
            assert reader.read() == b"abcdef"
            mock_sleep.assert_not_called()

    def test_read_empty_data_skips_throttle(self):
        """Reading past EOF returns b'' and skips throttling."""
        from io import BytesIO

        from hsync_client.transfer import _ThrottledFileReader

        inner = BytesIO(b"")
        reader = _ThrottledFileReader(inner, bandwidth_limit=1024)
        with patch("hsync_client.transfer.get_throttle_delay") as mock_delay:
            assert reader.read() == b""
            mock_delay.assert_not_called()


class TestUploadCompressedResponses:
    """Error-status handling within the compressed-upload branch."""

    @pytest.mark.parametrize("status_code", [401, 403, 409, 500])
    def test_compressed_branch_handles_error_statuses(self, tmp_path, status_code):
        """Non-201 responses on the compressed path are handled without raising."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("compress me " * 50)

        file = File(
            filepath=str(test_file),
            sha256sum="abc123",
            size=test_file.stat().st_size,
            mtime=1000,
            ctime=1000,
            permissions="-rw-r--r--",
        )

        mock_response = MagicMock()
        mock_response.status_code = status_code

        config = {
            **_BASE_CONFIG,
            "LOCAL_BUCKET": str(tmp_path),
            "COMPRESSION_ENABLED": True,
            "SERVER_COMPRESSION_ENABLED": True,
        }

        with (
            patch("hsync_client.transfer.CONFIG", config),
            patch("hsync_client.transfer._get_upload_status", return_value=("new", 0)),
            patch("hsync_client.transfer.should_compress", return_value=True),
            patch("hsync_client.transfer.post", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.is_file_stable", return_value=True),
        ):
            upload_file(file, check_size=False)


class TestUploadStatusBranches:
    """Cover the early-status branches of upload_file."""

    def test_fingerprint_failure_aborts(self, tmp_path):
        """A failed master-fingerprint check aborts before posting."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("content")

        file = File(filepath=str(test_file), sha256sum="abc123", size=7)
        config = {**_BASE_CONFIG, "LOCAL_BUCKET": str(tmp_path)}

        with (
            patch("hsync_client.transfer.CONFIG", config),
            patch("hsync_client.transfer.verify_master_fingerprint", return_value=False),
            patch("hsync_client.transfer.post") as mock_post,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.is_file_stable", return_value=True),
        ):
            upload_file(file, check_size=False)
            mock_post.assert_not_called()

    def test_sha256sum_none_treated_as_new(self, tmp_path):
        """A file with no sha256sum skips the status check and uploads as new."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("content")

        file = File(filepath=str(test_file), sha256sum=None, size=7, mtime=1000, ctime=1000)
        config = {**_BASE_CONFIG, "LOCAL_BUCKET": str(tmp_path)}

        mock_response = MagicMock()
        mock_response.status_code = 201

        with (
            patch("hsync_client.transfer.CONFIG", config),
            patch("hsync_client.transfer._get_upload_status") as mock_status,
            patch("hsync_client.transfer.post", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.is_file_stable", return_value=True),
        ):
            upload_file(file, check_size=False)
            # sha256sum is None → status check is skipped entirely.
            mock_status.assert_not_called()
