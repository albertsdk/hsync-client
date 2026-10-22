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

    def test_compressed_branch_signals_via_form_field_not_header(self, tmp_path):
        """The compressed branch must NOT set ``Content-Encoding``.

        Only the multipart *file part* is zstd-compressed, not the request body,
        and both servers key decompression off the ``compressed`` form field. A
        ``Content-Encoding`` on the multipart envelope makes aiohttp (the p2p
        master) try to decode the whole body and fail with 400.
        """
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

        captured: dict = {}

        def fake_build(_path, fields, **_kwargs):
            captured.update(fields)
            return iter([b""]), "multipart/form-data; boundary=x"

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
            patch("hsync_client.transfer._build_multipart_streaming", side_effect=fake_build),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.is_file_stable", return_value=True),
        ):
            upload_file(file, check_size=False)
            kwargs = mock_post.call_args.kwargs
            assert "Content-Encoding" not in kwargs["headers"]
            assert "files" not in kwargs  # streamed via the sized builder
            assert captured["compressed"] == "true"
            assert captured["compressed_size"] > 0

    def test_compressed_branch_streams_a_sized_multipart_body(self, tmp_path):
        """The compressed branch uses the streaming builder, not requests' files=.

        ``requests`` reads a ``files=`` file object in one ``read()`` to compute
        the body length, which truncates the upload to one chunk; the sized
        streaming body avoids that and keeps the throttled chunking.
        """
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
            patch("hsync_client.transfer._build_multipart_streaming") as mock_build,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.is_file_stable", return_value=True),
        ):
            mock_build.return_value = (iter([b""]), "multipart/form-data; boundary=x")

            upload_file(file, check_size=False)

            kwargs = mock_post.call_args.kwargs
            assert "files" not in kwargs  # no requests file-object encoding
            args, build_kwargs = mock_build.call_args
            assert args[0].endswith(".zst")  # the compressed temp file
            assert build_kwargs["filename"] == "file.txt"  # original destination name
            assert args[1]["compressed"] == "true"
            assert args[1]["compressed_size"] > 0

    def test_compression_not_beneficial_falls_back_to_original(self, tmp_path):
        """A frame that is not smaller than the source is discarded.

        Incompressible input must not be sent in a larger body (nor routed
        through the whole-file reader): the upload falls back to the standard
        streaming path and the temporary frame is removed.
        """
        from hsync_client import transfer as transfer_mod

        test_file = tmp_path / "file.bin"
        test_file.write_bytes(b"x" * 100)
        bigger = tmp_path / "frame.zst"
        bigger.write_bytes(b"y" * 200)

        file = File(
            filepath=str(test_file),
            sha256sum="abc123",
            size=100,
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
            patch("hsync_client.transfer._compress_to_temp_file", return_value=str(bigger)),
            patch("hsync_client.transfer.post", return_value=mock_response) as mock_post,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.is_file_stable", return_value=True),
        ):
            transfer_mod.upload_file(file, check_size=False)

            kwargs = mock_post.call_args.kwargs
            assert "files" not in kwargs  # standard streaming path
            assert kwargs["data"] is not None
            assert not bigger.exists()  # temporary frame cleaned up

    def test_compressed_partial_falls_back_to_standard_upload(self, tmp_path):
        """A 202 on the compressed path is not fatal.

        Compressed uploads are not resumable, so a partial response must not be
        reported as an error: the client retries the standard resumable upload
        instead (which also truncates the leftover partial on the peer).
        """
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

        partial = MagicMock()
        partial.status_code = 202
        partial.json.return_value = {
            "info": "Partial upload received",
            "offset": 100,
            "remaining": 50,
        }
        final = MagicMock()
        final.status_code = 201

        captured_calls: list[dict] = []

        def fake_build(_path, fields, **_kwargs):
            captured_calls.append(dict(fields))
            return iter([b""]), "multipart/form-data; boundary=x"

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
            patch("hsync_client.transfer.post", side_effect=[partial, final]) as mock_post,
            patch("hsync_client.transfer._build_multipart_streaming", side_effect=fake_build),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.is_file_stable", return_value=True),
        ):
            upload_file(file, check_size=False)

            assert mock_post.call_count == 2
            assert all("files" not in call.kwargs for call in mock_post.call_args_list)
            # First body is the compressed frame; the fallback body is the
            # original and must not carry the compressed form fields, or the
            # peer tries to zstd-decompress raw bytes (HTTP 500).
            assert "compressed" in captured_calls[0]
            assert "compressed" not in captured_calls[-1]
            assert "compressed_size" not in captured_calls[-1]

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
