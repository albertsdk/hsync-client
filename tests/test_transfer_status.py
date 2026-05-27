# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for transfer status functionality."""

from unittest.mock import MagicMock, patch

from requests import RequestException

from hsync_client.models.file import File
from hsync_client.transfer import is_file_stable


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


class TestIsFileStable:
    """Tests for is_file_stable function."""

    def test_empty_filepath_returns_true(self):
        """Empty filepath returns True (stable)."""
        file = File(filepath="", sha256sum="abc123", size=100)
        assert is_file_stable(file) is True

    def test_stable_file_returns_true(self, tmp_path):
        """File with unchanged size returns True."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")

        file = File(filepath=str(test_file), sha256sum="abc123", size=12)

        with patch("hsync_client.transfer.time_sleep"):  # Skip waiting
            result = is_file_stable(file, interval=0)
            assert result is True
