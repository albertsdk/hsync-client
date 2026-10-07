# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for compressed uploads, resumable uploads, edge cases, and multipart."""

from unittest.mock import MagicMock, patch

import pytest
from hsync_common.models.file import File

from hsync_client.events import UiEventName
from hsync_client.transfer import UPLOAD_STATUS_PREFLIGHT_MIN_SIZE, upload_file

_BASE_CONFIG = {
    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
    "REMOTE_BUCKET": "test",
    "REQUEST_HEADERS": {"X-API-Key": "test"},
    "CID": "test-client",
    # Disable retry/backoff so error-status paths resolve in one attempt.
    "TRANSFER_RETRY_ATTEMPTS": 1,
}


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
            permissions="644",
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
            permissions="644",
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
            permissions="644",
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
            permissions="644",
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
            permissions="644",
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
        """Test upload resumes from correct offset.

        Uses a file at the preflight threshold: below it the resumable-upload
        probe is intentionally skipped (UPLOAD_STATUS_PREFLIGHT_MIN_SIZE).
        """
        test_file = tmp_path / "file.txt"
        test_file.write_bytes(b"x" * UPLOAD_STATUS_PREFLIGHT_MIN_SIZE)

        file = File(
            filepath=str(test_file),
            sha256sum="abc123",
            size=UPLOAD_STATUS_PREFLIGHT_MIN_SIZE,
            mtime=1000,
            ctime=1000,
            permissions="644",
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
        """Test upload skips when file is complete on server.

        Uses a file at the preflight threshold: below it the resumable-upload
        probe is intentionally skipped (UPLOAD_STATUS_PREFLIGHT_MIN_SIZE).
        """
        test_file = tmp_path / "file.txt"
        test_file.write_bytes(b"x" * UPLOAD_STATUS_PREFLIGHT_MIN_SIZE)

        file = File(
            filepath=str(test_file),
            sha256sum="abc123",
            size=UPLOAD_STATUS_PREFLIGHT_MIN_SIZE,
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
            permissions="644",
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


class TestBuildMultipartStreaming:
    """Tests for the streaming multipart body and its length advertisement."""

    def test_returns_sized_body_with_exact_length(self, tmp_path):
        """Body advertises a length equal to the bytes it actually streams."""
        from hsync_client.transfer import _build_multipart_streaming

        test_file = tmp_path / "payload.bin"
        test_file.write_bytes(b"x" * 1234)

        body, content_type = _build_multipart_streaming(
            str(test_file), {"sha256sum": "abc"}, boundary="boundary123"
        )

        assert content_type == "multipart/form-data; boundary=boundary123"
        streamed = b"".join(body)
        assert len(body) == len(streamed)

    def test_requests_frames_with_content_length_not_chunked(self, tmp_path):
        """requests must use Content-Length, never Transfer-Encoding: chunked.

        Sending both framing headers yields a request strict HTTP/1.1 parsers
        (h11/uvicorn) reject as malformed; the sized body prevents that.
        """
        from requests import Request, Session

        from hsync_client.transfer import _build_multipart_streaming

        test_file = tmp_path / "payload.bin"
        test_file.write_bytes(b"y" * 4096)

        body, content_type = _build_multipart_streaming(
            str(test_file), {"sha256sum": "abc"}, boundary="boundary123"
        )

        prepared = Session().prepare_request(
            Request(
                "POST",
                "https://localhost:9753/x",
                headers={"Content-Type": content_type},
                data=body,
            )
        )

        assert "Transfer-Encoding" not in prepared.headers
        assert prepared.headers["Content-Length"] == str(len(body))

    def test_400_logs_body_and_raises_permanent_upload_error(self, tmp_path):
        """A 4xx (not 401/403/409) logs the server body and raises
        PermanentUploadError (non-retryable) instead of HTTPError.

        Regression: previously ``raise_for_status()`` discarded the response
        body, making the 400 undiagnosable, and the retry decorator retried
        the permanent error 3 times.
        """
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")

        file = File(filepath=str(test_file), sha256sum="abc123", size=12)

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "Checksum mismatch"

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "LOCAL_BUCKET": str(tmp_path),
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "CID": "test-client",
                    "TRANSFER_RETRY_ATTEMPTS": 1,
                },
            ),
            patch("hsync_client.transfer.post", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer._get_upload_status", return_value=("new", 0)),
            patch("hsync_client.transfer.emit_event") as mock_emit,
        ):
            upload_file(file, check_size=False)

        # PermanentUploadError is swallowed by upload_file and surfaced as a
        # transfer_failed UI event: assert the event AND the server body (the
        # whole point of the fix — a bare `raise_for_status` discarded it).
        from hsync_client.events import UiEventName

        assert mock_emit.called
        fail_calls = [
            c
            for c in mock_emit.call_args_list
            if c.args and c.args[0] == UiEventName.transfer_failed
        ]
        assert fail_calls, "expected a transfer_failed event"
        kwargs = fail_calls[0].kwargs
        assert kwargs.get("op") == "upload"
        assert str(kwargs.get("file")) == str(test_file)
        assert "Checksum mismatch" in str(kwargs.get("message"))

    def test_400_not_retried(self, tmp_path):
        """A 4xx must not be retried: post is called exactly once.

        Regression: HTTPError is a RequestException, so the retry decorator
        retried the permanent 400 three times. PermanentUploadError is not a
        RequestException, so the decorator does not retry it.
        """
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")

        file = File(filepath=str(test_file), sha256sum="abc123", size=12)

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "Checksum mismatch"

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "LOCAL_BUCKET": str(tmp_path),
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "CID": "test-client",
                    # Allow 3 retries to prove they do NOT happen.
                    "TRANSFER_RETRY_ATTEMPTS": 3,
                },
            ),
            patch("hsync_client.transfer.post", return_value=mock_response) as mock_post,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer._get_upload_status", return_value=("new", 0)),
        ):
            upload_file(file, check_size=False)

            # Must be called exactly once, not 3 times.
            assert mock_post.call_count == 1

    def test_400_emits_transfer_failed_with_body(self, tmp_path):
        """A 4xx emits a transfer_failed event whose message includes the body."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")

        file = File(filepath=str(test_file), sha256sum="abc123", size=12)

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "Checksum mismatch"

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "LOCAL_BUCKET": str(tmp_path),
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "CID": "test-client",
                    "TRANSFER_RETRY_ATTEMPTS": 1,
                },
            ),
            patch("hsync_client.transfer.post", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer._get_upload_status", return_value=("new", 0)),
            patch("hsync_client.transfer.emit_event") as mock_emit,
        ):
            upload_file(file, check_size=False)

            # Verify transfer_failed was emitted with the body in the message.
            transfer_calls = [
                c
                for c in mock_emit.call_args_list
                if c.args and c.args[0] == UiEventName.transfer_failed
            ]
            assert len(transfer_calls) == 1
            msg = transfer_calls[0].kwargs.get("message", "")
            assert "Checksum mismatch" in msg
