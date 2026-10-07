# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for the retry behaviour of transfer operations.

The existing upload/download tests in ``test_transfer_upload_base.py`` set
``TRANSFER_RETRY_ATTEMPTS=1``, which disables the retry mechanism entirely.
This module verifies that transient errors are retried with exponential
backoff and that permanent errors are never retried.
"""

from unittest.mock import MagicMock, patch

from hsync_common.models.file import File
from requests import RequestException

from hsync_client.transfer import download_file, upload_file

# Config with retry enabled (3 attempts) so the backoff path is exercised.
_RETRY_CONFIG = {
    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
    "REMOTE_BUCKET": "test",
    "REQUEST_HEADERS": {"X-API-Key": "test"},
    "CID": "test-client",
    "LOCAL_BUCKET": "/tmp/test-bucket",
    "TRANSFER_RETRY_ATTEMPTS": 3,
    "TRANSFER_RETRY_MIN_WAIT": 0.01,
    "TRANSFER_RETRY_MAX_WAIT": 0.05,
    "MIN_FREE_DISK_SPACE": 0,
    "MAX_INBOUND_SIZE": 0,
    "HTTP_TIMEOUT": 30,
}


def _mock_200_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"X-Checksum-SHA256": "abc123"}
    resp.iter_content.return_value = [b"ok"]
    resp.raw = MagicMock()
    return resp


def _mock_500_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 500
    resp.headers = {}
    return resp


def _mock_400_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 400
    resp.headers = {}
    return resp


class TestUploadRetryBehavior:
    """Tests that verify the actual retry mechanism for uploads."""

    def test_retries_on_transient_connection_error(self, tmp_path):
        """Upload is retried on connection-level errors, succeeding eventually."""
        local = tmp_path / "local"
        local.mkdir()
        test_file = local / "f.txt"
        test_file.write_bytes(b"hello upload retry")

        file = File(
            filepath=str(test_file),
            sha256sum="abc123",
            size=len(b"hello upload retry"),
        )

        call_count = 0

        def _transient_then_success(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RequestException("connection reset")
            return _mock_200_response()

        with (
            patch("hsync_client.transfer.CONFIG", _RETRY_CONFIG),
            patch("hsync_client.transfer.post", side_effect=_transient_then_success),
            patch("hsync_client.transfer.compute_sha256", return_value="abc123"),
            patch("hsync_client.transfer._wait_for_stable_file", return_value=True),
            patch("hsync_client.transfer._record_synced_base"),
            patch("hsync_client.transfer.prepare_delta_upload", return_value=None),
        ):
            upload_file(file, check_size=False)

        assert call_count == 3  # 2 failures + 1 success

    def test_stops_retry_on_permanent_4xx(self, tmp_path):
        """Upload is NOT retried on 4xx (permanent) errors."""
        local = tmp_path / "local"
        local.mkdir()
        test_file = local / "f.txt"
        test_file.write_bytes(b"no retry on 4xx")

        file = File(
            filepath=str(test_file),
            sha256sum="abc123",
            size=len(b"no retry on 4xx"),
        )

        call_count = 0

        def _always_400(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            resp = _mock_400_response()
            resp.status_code = 400
            return resp

        with (
            patch("hsync_client.transfer.CONFIG", _RETRY_CONFIG),
            patch("hsync_client.transfer.post", side_effect=_always_400),
            patch("hsync_client.transfer.compute_sha256", return_value="abc123"),
            patch("hsync_client.transfer._wait_for_stable_file", return_value=True),
            patch("hsync_client.transfer._record_synced_base"),
            patch("hsync_client.transfer.prepare_delta_upload", return_value=None),
            patch("hsync_client.transfer._emit_transfer_failed"),
        ):
            upload_file(file, check_size=False)

        # 4xx should not be retried — one attempt only
        assert call_count == 1

    def test_retries_on_server_5xx(self, tmp_path):
        """Upload is retried on server-side 5xx errors."""
        local = tmp_path / "local"
        local.mkdir()
        test_file = local / "f.txt"
        test_file.write_bytes(b"retry on 5xx")

        file = File(
            filepath=str(test_file),
            sha256sum="abc123",
            size=len(b"retry on 5xx"),
        )

        call_count = 0

        def _five_hundred_then_success(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                resp = _mock_500_response()
                resp.status_code = 503
                return resp
            return _mock_200_response()

        with (
            patch("hsync_client.transfer.CONFIG", _RETRY_CONFIG),
            patch("hsync_client.transfer.post", side_effect=_five_hundred_then_success),
            patch("hsync_client.transfer.compute_sha256", return_value="abc123"),
            patch("hsync_client.transfer._wait_for_stable_file", return_value=True),
            patch("hsync_client.transfer._record_synced_base"),
            patch("hsync_client.transfer.prepare_delta_upload", return_value=None),
        ):
            upload_file(file, check_size=False)

        assert call_count == 2  # 1 failure + 1 success

    def test_emits_transfer_failed_after_all_retries_exhausted(self, tmp_path):
        """After max retries, the failure is surfaced via transfer_failed event."""
        local = tmp_path / "local"
        local.mkdir()
        test_file = local / "f.txt"
        test_file.write_bytes(b"exhausted")

        file = File(
            filepath=str(test_file),
            sha256sum="abc123",
            size=len(b"exhausted"),
        )

        call_count = 0

        def _always_transient(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise RequestException("always fails")

        with (
            patch("hsync_client.transfer.CONFIG", _RETRY_CONFIG),
            patch("hsync_client.transfer.post", side_effect=_always_transient),
            patch("hsync_client.transfer.compute_sha256", return_value="abc123"),
            patch("hsync_client.transfer._wait_for_stable_file", return_value=True),
            patch("hsync_client.transfer._record_synced_base"),
            patch("hsync_client.transfer.prepare_delta_upload", return_value=None),
            patch("hsync_client.transfer._emit_transfer_failed") as mock_emit,
        ):
            upload_file(file, check_size=False)

        assert call_count == 3  # all 3 attempts exhausted
        mock_emit.assert_called_once()
        assert mock_emit.call_args[0][0] == "upload"


class TestDownloadRetryBehavior:
    """Tests that verify the actual retry mechanism for downloads."""

    def test_retries_on_transient_connection_error(self, tmp_path):
        """Download is retried on connection-level errors, succeeding eventually."""
        local = tmp_path / "local"
        local.mkdir()

        file = File(filepath="remote/f.txt", sha256sum="abc123", size=12)

        call_count = 0

        def _transient_then_success(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise RequestException("connection reset")
            return _mock_200_response()

        with (
            patch("hsync_client.transfer.CONFIG", _RETRY_CONFIG),
            patch("hsync_client.transfer.get", side_effect=_transient_then_success),
            patch("hsync_client.transfer.compute_sha256", return_value="abc123"),
            patch("hsync_client.transfer.is_sha256_hex", return_value=True),
            patch("hsync_client.transfer.replace"),
            patch("hsync_client.transfer._record_synced_base"),
            patch("hsync_client.transfer.prepare_delta_download", return_value=None),
        ):
            download_file(file)

        assert call_count == 2  # 1 failure + 1 success

    def test_stops_retry_on_permanent_4xx(self, tmp_path):
        """Download is NOT retried on 4xx (permanent) errors."""
        local = tmp_path / "local"
        local.mkdir()

        file = File(filepath="remote/f.txt", sha256sum="abc123", size=12)

        call_count = 0

        def _always_404(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.status_code = 404
            resp.headers = {}
            return resp

        with (
            patch("hsync_client.transfer.CONFIG", _RETRY_CONFIG),
            patch("hsync_client.transfer.get", side_effect=_always_404),
            patch("hsync_client.transfer._emit_transfer_failed"),
            patch("hsync_client.transfer.prepare_delta_download", return_value=None),
        ):
            download_file(file)

        # 404 should not be retried — one attempt only
        assert call_count == 1
