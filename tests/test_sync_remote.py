# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for remote file operations in sync module."""

from unittest.mock import MagicMock, patch

import pytest

from hsync_client.models.file import File


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
            with pytest.raises(AuthenticationError):
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
            with pytest.raises(AuthenticationError):
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
            pytest.raises(ServerError),
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
            with pytest.raises(NetworkError):
                get_files_remote()


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
