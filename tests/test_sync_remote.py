# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for remote file operations in sync module."""

from unittest.mock import MagicMock, patch

import pytest
from hsync_common.models.file import File


class TestGetFilesRemote:
    """Tests for get_files_remote function."""

    def test_get_files_remote_success(self):
        """get_files_remote returns file list on success."""
        from hsync_client.sync import get_files_remote

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [{"filepath": "file.txt"}]

        config = {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "testbucket",
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "NO_VERIFY_SSL": True,
        }

        with (
            patch("hsync_client.sync.CONFIG", config),
            patch("hsync_client.sync.get", return_value=mock_response),
            patch("hsync_client.sync.get_ssl_verify", return_value=False),
        ):
            result = get_files_remote()
            assert result == [{"filepath": "file.txt"}]

    def test_get_files_remote_with_filter_path(self):
        """get_files_remote includes filter path in request."""
        from hsync_client.sync import get_files_remote

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []

        config = {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "testbucket",
            "FILTER_PATH": "subdir",
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "NO_VERIFY_SSL": True,
        }

        with (
            patch("hsync_client.sync.CONFIG", config),
            patch("hsync_client.sync.get", return_value=mock_response) as mock_get,
            patch("hsync_client.sync.get_ssl_verify", return_value=False),
        ):
            get_files_remote()
            # Check that path parameter was included
            call_kwargs = mock_get.call_args[1]
            assert call_kwargs["params"]["path"] == "subdir"

    def test_get_files_remote_401_raises_auth_error(self):
        """get_files_remote raises AuthenticationError on 401."""
        from hsync_client.exceptions import AuthenticationError
        from hsync_client.sync import get_files_remote

        mock_response = MagicMock()
        mock_response.status_code = 401

        config = {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "testbucket",
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "NO_VERIFY_SSL": True,
        }

        with (
            patch("hsync_client.sync.CONFIG", config),
            patch("hsync_client.sync.get", return_value=mock_response),
            patch("hsync_client.sync.get_ssl_verify", return_value=False),
        ):
            with pytest.raises(AuthenticationError, match="Invalid API key"):
                get_files_remote()

    def test_get_files_remote_403_raises_auth_error(self):
        """get_files_remote raises AuthenticationError on 403."""
        from hsync_client.exceptions import AuthenticationError
        from hsync_client.sync import get_files_remote

        mock_response = MagicMock()
        mock_response.status_code = 403

        config = {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "testbucket",
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "NO_VERIFY_SSL": True,
        }

        with (
            patch("hsync_client.sync.CONFIG", config),
            patch("hsync_client.sync.get", return_value=mock_response),
            patch("hsync_client.sync.get_ssl_verify", return_value=False),
        ):
            with pytest.raises(AuthenticationError, match="API key required"):
                get_files_remote()

    def test_get_files_remote_500_raises_server_error(self):
        """get_files_remote raises ServerError on 500."""
        from hsync_client.exceptions import ServerError
        from hsync_client.sync import get_files_remote

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        config = {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "testbucket",
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "NO_VERIFY_SSL": True,
        }

        with (
            patch("hsync_client.sync.CONFIG", config),
            patch("hsync_client.sync.get", return_value=mock_response),
            patch("hsync_client.sync.get_ssl_verify", return_value=False),
            pytest.raises(ServerError, match="Server returned status 500"),
        ):
            get_files_remote()

    def test_get_files_remote_network_error(self):
        """get_files_remote raises NetworkError on connection failure."""
        from requests.exceptions import RequestException

        from hsync_client.exceptions import NetworkError
        from hsync_client.sync import get_files_remote

        config = {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "testbucket",
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "NO_VERIFY_SSL": True,
        }

        with (
            patch("hsync_client.sync.CONFIG", config),
            patch("hsync_client.sync.get", side_effect=RequestException("Connection failed")),
            patch("hsync_client.sync.get_ssl_verify", return_value=False),
        ):
            with pytest.raises(NetworkError, match="Error downloading files list"):
                get_files_remote()


class TestGetFilesRemoteRetry:
    """Retry policy: transient failures are retried, permanent auth failures are not.

    Regression for the retry-policy narrowing: ``retry_on_network_error`` must
    retry connection failures and 5xx responses, but never ``AuthenticationError``
    (401/403), which is permanent and would not be resolved by retrying.
    """

    _CONFIG = {
        "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
        "REMOTE_BUCKET": "testbucket",
        "REQUEST_HEADERS": {"X-API-Key": "test"},
        "NO_VERIFY_SSL": True,
        "TRANSFER_RETRY_ATTEMPTS": 3,
        "TRANSFER_RETRY_MIN_WAIT": 0.01,
        "TRANSFER_RETRY_MAX_WAIT": 0.05,
    }

    def test_does_not_retry_on_401(self):
        """A permanent auth failure (401) must be attempted exactly once."""
        from hsync_client.exceptions import AuthenticationError
        from hsync_client.sync import get_files_remote

        mock_response = MagicMock()
        mock_response.status_code = 401

        with (
            patch("hsync_client.sync.CONFIG", self._CONFIG),
            patch("hsync_client.sync.get", return_value=mock_response) as mock_get,
            patch("hsync_client.sync.get_ssl_verify", return_value=False),
        ):
            with pytest.raises(AuthenticationError):
                get_files_remote()

        assert mock_get.call_count == 1

    def test_retries_transient_connection_error(self):
        """A transient connection failure is retried until attempts are exhausted."""
        from requests.exceptions import RequestException

        from hsync_client.exceptions import NetworkError
        from hsync_client.sync import get_files_remote

        with (
            patch("hsync_client.sync.CONFIG", self._CONFIG),
            patch(
                "hsync_client.sync.get", side_effect=RequestException("Connection failed")
            ) as mock_get,
            patch("hsync_client.sync.get_ssl_verify", return_value=False),
        ):
            with pytest.raises(NetworkError):
                get_files_remote()

        assert mock_get.call_count == 3

    def test_retries_transient_server_error(self):
        """A transient 5xx (ServerError) is still retried."""
        from hsync_client.exceptions import ServerError
        from hsync_client.sync import get_files_remote

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        with (
            patch("hsync_client.sync.CONFIG", self._CONFIG),
            patch("hsync_client.sync.get", return_value=mock_response) as mock_get,
            patch("hsync_client.sync.get_ssl_verify", return_value=False),
        ):
            with pytest.raises(ServerError):
                get_files_remote()

        assert mock_get.call_count == 3


class TestExecuteParallelTransfers:
    """Tests for _execute_parallel_transfers function."""

    @pytest.mark.asyncio
    async def test_executes_downloads_in_parallel(self):
        """Downloads are executed in parallel."""
        from hsync_client.sync import _execute_parallel_transfers

        files = [
            File(filepath="/path/file1.txt"),
            File(filepath="/path/file2.txt"),
            File(filepath="/path/file3.txt"),
        ]

        download_calls = []

        def mock_download(file):
            download_calls.append(file.filepath)

        # Patch in transfer module (local import in _execute_parallel_transfers)
        with patch("hsync_client.transfer.download_file", mock_download):
            await _execute_parallel_transfers(files, [])

        assert len(download_calls) == 3
        assert "/path/file1.txt" in download_calls
        assert "/path/file2.txt" in download_calls
        assert "/path/file3.txt" in download_calls

    @pytest.mark.asyncio
    async def test_executes_uploads_in_parallel(self):
        """Uploads are executed in parallel."""
        from hsync_client.sync import _execute_parallel_transfers

        files = [
            File(filepath="/path/file1.txt"),
            File(filepath="/path/file2.txt"),
        ]

        upload_calls = []

        def mock_upload(file):
            upload_calls.append(file.filepath)

        # Patch in transfer module (local import in _execute_parallel_transfers)
        with patch("hsync_client.transfer.upload_file", mock_upload):
            await _execute_parallel_transfers([], files)

        assert len(upload_calls) == 2
        assert "/path/file1.txt" in upload_calls
        assert "/path/file2.txt" in upload_calls

    @pytest.mark.asyncio
    async def test_executes_mixed_transfers(self):
        """Downloads and uploads are executed together."""
        from hsync_client.sync import _execute_parallel_transfers

        downloads = [
            File(filepath="/path/download.txt"),
        ]
        uploads = [
            File(filepath="/path/upload.txt"),
        ]

        download_calls = []
        upload_calls = []

        def mock_download(file):
            download_calls.append(file.filepath)

        def mock_upload(file):
            upload_calls.append(file.filepath)

        # Patch in transfer module (local import in _execute_parallel_transfers)
        with (
            patch("hsync_client.transfer.download_file", mock_download),
            patch("hsync_client.transfer.upload_file", mock_upload),
        ):
            await _execute_parallel_transfers(downloads, uploads)

        assert len(download_calls) == 1
        assert len(upload_calls) == 1

    @pytest.mark.asyncio
    async def test_handles_transfer_errors_gracefully(self):
        """Transfer errors are logged but don't stop other transfers."""
        from hsync_client.sync import _execute_parallel_transfers

        files = [
            File(filepath="/path/file1.txt"),
            File(filepath="/path/file2.txt"),
        ]

        call_count = 0

        def mock_download(file):
            nonlocal call_count
            call_count += 1
            if file.filepath == "/path/file1.txt":
                raise Exception("Download failed")

        # Patch in transfer module (local import in _execute_parallel_transfers)
        with patch("hsync_client.transfer.download_file", mock_download):
            # Should not raise exception
            await _execute_parallel_transfers(files, [])

        # Both files should have been attempted
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_empty_lists_do_nothing(self):
        """Empty download and upload lists complete without error."""
        from hsync_client.sync import _execute_parallel_transfers

        # Should complete without error
        await _execute_parallel_transfers([], [])


# ---------------------------------------------------------------------------
# Realistic server-response tests — mock responses that mirror the actual
# server contract to catch regressions in client-side error handling and
# header parsing.
# ---------------------------------------------------------------------------


class TestGetFilesRemoteErrorPropagation:
    """Tests that verify server errors are correctly propagated to the caller.

    The real server returns well-defined HTTP status codes for specific error
    conditions (401 invalid key, 404 missing bucket, 426 protocol mismatch,
    409 file conflict). The client must raise the appropriate exception.
    """

    def test_401_raises_authentication_error(self):
        """401 Unauthorized raises AuthenticationError (real server contract)."""
        from hsync_client.exceptions import AuthenticationError
        from hsync_client.sync import get_files_remote

        mock_response = MagicMock()
        mock_response.status_code = 401

        config = {
            "API": {"buckets": "http://localhost/api/v1/buckets"},
            "REMOTE_BUCKET": "test",
            "REQUEST_HEADERS": {},
            "NO_VERIFY_SSL": True,
        }

        with (
            patch("hsync_client.sync.CONFIG", config),
            patch("hsync_client.sync.get", return_value=mock_response),
            patch("hsync_client.sync.get_ssl_verify", return_value=False),
            pytest.raises(AuthenticationError, match="Invalid API key"),
        ):
            get_files_remote()

    def test_404_raises_server_error(self):
        """404 Not Found raises ServerError (real server contract)."""
        from hsync_client.exceptions import ServerError
        from hsync_client.sync import get_files_remote

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Bucket not found"

        config = {
            "API": {"buckets": "http://localhost/api/v1/buckets"},
            "REMOTE_BUCKET": "nonexistent",
            "REQUEST_HEADERS": {},
            "NO_VERIFY_SSL": True,
        }

        with (
            patch("hsync_client.sync.CONFIG", config),
            patch("hsync_client.sync.get", return_value=mock_response),
            patch("hsync_client.sync.get_ssl_verify", return_value=False),
            pytest.raises(ServerError, match="Server returned status 404"),
        ):
            get_files_remote()

    def test_non_200_status_raises_server_error(self):
        """Any non-200 status (that isn't 401/403) raises ServerError."""
        from hsync_client.exceptions import ServerError
        from hsync_client.sync import get_files_remote

        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.text = "Too Many Requests"

        config = {
            "API": {"buckets": "http://localhost/api/v1/buckets"},
            "REMOTE_BUCKET": "test",
            "REQUEST_HEADERS": {},
            "NO_VERIFY_SSL": True,
        }

        with (
            patch("hsync_client.sync.CONFIG", config),
            patch("hsync_client.sync.get", return_value=mock_response),
            patch("hsync_client.sync.get_ssl_verify", return_value=False),
            pytest.raises(ServerError, match="Server returned status 429"),
        ):
            get_files_remote()


class TestSyncActionsWithRealisticServerData:
    """Tests that sync actions work correctly with realistic server payloads."""

    def test_server_listing_with_checksum_is_processed(self):
        """Files from server with sha256sum produce valid sync actions."""
        from hsync_client.sync import get_sync_actions

        remote = [
            {
                "filepath": "docs/report.txt",
                "name": "report.txt",
                "sha256sum": "abc123",
                "size": 100,
                "mtime": 1000,
                "permissions": "644",
            }
        ]
        local: list[dict] = []

        config = {"LOCAL_BUCKET": "/tmp/test-local"}

        with patch("hsync_client.sync.CONFIG", config):
            actions = get_sync_actions(remote, local)

        # File exists remotely but not locally: exactly one action, a download.
        assert len(actions) == 1
        download_actions = [a for a in actions if a.name == "download"]
        assert len(download_actions) == 1

    def test_server_listing_with_zstd_content_encoding_is_processed(self):
        """Files from compressed server responses are processed correctly."""
        from hsync_client.sync import get_files_remote

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "filepath": "large.dat",
                "name": "large.dat",
                "sha256sum": "def456",
                "size": 50000,
                "mtime": 2000,
                "permissions": "644",
            }
        ]
        # Server advertises compression capability
        mock_response.headers = {"Content-Encoding": "zstd"}

        config = {
            "API": {"buckets": "http://localhost/api/v1/buckets"},
            "REMOTE_BUCKET": "test",
            "REQUEST_HEADERS": {},
            "NO_VERIFY_SSL": True,
        }

        with (
            patch("hsync_client.sync.CONFIG", config),
            patch("hsync_client.sync.get", return_value=mock_response),
            patch("hsync_client.sync.get_ssl_verify", return_value=False),
        ):
            result = get_files_remote()

        # The client correctly returns the listing even when compression is
        # advertised (it does not need to decompress the listing itself).
        assert len(result) == 1
        assert result[0]["sha256sum"] == "def456"
