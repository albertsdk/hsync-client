# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for download transfer functionality."""

import threading
import time
from unittest.mock import MagicMock, patch

from requests import RequestException

from hsync_client.events import UiEventName
from hsync_client.models.file import File
from hsync_client.transfer import download_file


class TestDownloadFile:
    """Tests for download_file function."""

    def test_network_error_logs_and_returns(self, tmp_path):
        """Network error during download is logged."""
        file = File(filepath="/path/to/file.txt", sha256sum="abc123", size=100)

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                    # Disable retry/backoff (§11) so a single attempt is asserted.
                    "TRANSFER_RETRY_ATTEMPTS": 1,
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
        file = File(filepath="/path/to/file.txt", sha256sum="abc123", size=100)

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
        file = File(filepath="/path/to/file.txt", sha256sum="abc123", size=100)

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
        file = File(filepath="/path/to/file.txt", sha256sum="abc123", size=100)

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
                    # Disable retry/backoff (§11): a 5xx is raised and surfaced once.
                    "TRANSFER_RETRY_ATTEMPTS": 1,
                },
            ),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            download_file(file)

    def test_successful_download_with_sha256_verification(self, tmp_path):
        """Successful download verifies SHA-256 and moves file."""
        expected_sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        file = File(
            filepath="subdir/file.txt",
            sha256sum=expected_sha,  # SHA-256 of empty file
            size=0,
            mtime=1000,
            ctime=1000,
            permissions="-rw-r--r--",
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.iter_content = MagicMock(return_value=[b""])
        # Real dict so headers.get() returns proper defaults instead of MagicMock
        mock_response.headers = {"X-Checksum-SHA256": expected_sha}

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
                "hsync_client.transfer.compute_sha256",
                return_value=expected_sha,
            ),
            patch("hsync_client.transfer.replace") as mock_replace,
            patch("hsync_client.transfer.utime") as mock_utime,
            patch("hsync_client.transfer.set_permissions") as mock_set_perms,
            patch("hsync_client.transfer.remove"),
        ):
            download_file(file)
            mock_replace.assert_called_once()
            mock_utime.assert_called_once()
            # atime mirrors mtime; ctime is not restored (POSIX ctime is not settable)
            assert mock_utime.call_args.args[1] == (file.mtime, file.mtime)
            mock_set_perms.assert_called_once()

    def test_download_skipped_when_insufficient_disk_space(self, tmp_path):
        """Size-aware guard skips the download when free space is too low."""
        file = File(
            filepath="big.bin",
            sha256sum="abc123",
            size=10 * 1024 * 1024,
            mtime=1000,
            ctime=1000,
        )

        fake_usage = MagicMock()
        fake_usage.free = 1 * 1024 * 1024  # only 1 MB free

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                    "MIN_FREE_DISK_SPACE": 100,  # 100 MB reserve
                },
            ),
            patch("hsync_client.transfer.disk_usage", return_value=fake_usage),
            patch("hsync_client.transfer.get") as mock_get,
            patch("hsync_client.transfer.replace") as mock_replace,
        ):
            download_file(file)
            # Guard fires before any network request or final commit
            mock_get.assert_not_called()
            mock_replace.assert_not_called()

    def test_sha256_mismatch_returns_without_saving(self, tmp_path):
        """SHA-256 mismatch logs error and returns."""
        file = File(filepath="file.txt", sha256sum="expected_sha256", size=100)

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
            patch("hsync_client.transfer.compute_sha256", return_value="different_sha256"),
            patch("hsync_client.transfer.replace") as mock_replace,
            patch("hsync_client.transfer.remove"),
        ):
            download_file(file)
            mock_replace.assert_not_called()


class TestDownloadRetry:
    """Retry/backoff and transfer_failed surfacing for downloads (§11)."""

    def test_transient_error_retries_then_emits_transfer_failed(self, tmp_path):
        """A persistent transient error is retried then surfaced as transfer_failed."""
        file = File(filepath="file.txt", sha256sum="abc123", size=100)

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                    "TRANSFER_RETRY_ATTEMPTS": 3,
                    "TRANSFER_RETRY_MIN_WAIT": 0,
                    "TRANSFER_RETRY_MAX_WAIT": 0,
                },
            ),
            patch("hsync_client.transfer.get") as mock_get,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.emit_event") as mock_emit,
        ):
            mock_get.side_effect = RequestException("flaky network")
            download_file(file)
            # Exhausted all attempts before giving up.
            assert mock_get.call_count == 3
            # Surfaced exactly one transfer_failed event for the UI.
            failed = [
                c for c in mock_emit.call_args_list if c.args[0] == UiEventName.transfer_failed
            ]
            assert len(failed) == 1
            assert failed[0].kwargs["op"] == "download"


class TestDownloadFileResume:
    """Tests for download resume functionality."""

    def test_download_with_existing_partial_file(self, tmp_path):
        """Test download resumes from existing partial file."""
        file = File(
            filepath="file.txt",
            sha256sum="abc123def",
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
            patch("hsync_client.transfer.compute_sha256", return_value="abc123def"),
            patch("hsync_client.transfer.replace"),
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
            sha256sum="abc123def",
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
            patch("hsync_client.transfer.compute_sha256", return_value="abc123def"),
            patch("hsync_client.transfer.replace"),
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
            sha256sum="abc123",
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
            sha256sum="abc123def",
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
            patch("hsync_client.transfer.compute_sha256", return_value="abc123def"),
            patch("hsync_client.transfer.replace"),
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
            sha256sum="abc123def",
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
            patch("hsync_client.transfer.compute_sha256", return_value="abc123def"),
            patch("hsync_client.transfer.replace") as mock_replace,
            patch("hsync_client.transfer.utime"),
            patch("hsync_client.transfer.set_permissions"),
        ):
            download_file(file)
            mock_replace.assert_called()


class TestDownloadPathLocking:
    """Concurrency tests for per-path serialization of downloads (§14)."""

    def _run_concurrent(self, files, tmp_path):
        """Run download_file on *files* from threads, tracking max concurrency."""
        guard = threading.Lock()
        start = threading.Barrier(len(files))
        active = 0
        max_active = 0

        def fake_attempt(_file):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with guard:
                active -= 1

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {"LOCAL_BUCKET": str(tmp_path), "TRANSFER_RETRY_ATTEMPTS": 1},
            ),
            patch("hsync_client.transfer._download_file_attempt", side_effect=fake_attempt),
        ):

            def worker(f):
                start.wait()
                download_file(f)

            threads = [threading.Thread(target=worker, args=(f,)) for f in files]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        return max_active

    def test_same_path_downloads_are_serialized(self, tmp_path):
        """Two downloads of the same path never overlap their critical section."""
        files = [
            File(filepath="dir/file.txt", sha256sum="abc", size=10),
            File(filepath="dir/file.txt", sha256sum="abc", size=10),
        ]
        assert self._run_concurrent(files, tmp_path) == 1

    def test_distinct_path_downloads_run_concurrently(self, tmp_path):
        """Downloads of distinct paths are not serialized by the per-path lock."""
        files = [
            File(filepath="dir/a.txt", sha256sum="abc", size=10),
            File(filepath="dir/b.txt", sha256sum="abc", size=10),
        ]
        assert self._run_concurrent(files, tmp_path) == 2
