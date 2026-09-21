# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for upload file, retry, and symlink safety."""

from unittest.mock import MagicMock, patch

from hsync_common.models.file import File
from requests import RequestException

from hsync_client.events import UiEventName
from hsync_client.transfer import upload_file

_BASE_CONFIG = {
    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
    "REMOTE_BUCKET": "test",
    "REQUEST_HEADERS": {"X-API-Key": "test"},
    "CID": "test-client",
    # Disable retry/backoff so error-status paths resolve in one attempt.
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
                    # Disable retry/backoff: a 5xx is raised and surfaced once.
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
                    # Disable retry/backoff so a single attempt is asserted.
                    "TRANSFER_RETRY_ATTEMPTS": 1,
                },
            ),
            patch("hsync_client.transfer.post") as mock_post,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            mock_post.side_effect = RequestException("Connection error")
            upload_file(file, check_size=False)
            # Retry disabled: the single failing attempt is asserted.
            mock_post.assert_called_once()


class TestUploadRetry:
    """Retry/backoff and transfer_failed surfacing for uploads."""

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


class TestUploadFileSymlinkSafety:
    """upload_file must never dereference a symlink whose target escapes the bucket.

    This is the defence-in-depth guard for the realtime (watch/P2P slave) upload
    path, which builds the ``File`` directly from the watchdog event without the
    ``get_files_local`` scan guard.
    """

    def _config(self, tmp_path):
        return {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "test",
            "LOCAL_BUCKET": str(tmp_path),
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "CID": "test-client",
            "TRANSFER_RETRY_ATTEMPTS": 1,
        }

    def test_escaping_symlink_not_uploaded(self, tmp_path):
        """A symlink whose target escapes the bucket is refused, not dereferenced."""
        outside = tmp_path.parent / f"hsync_ulsecret_{tmp_path.name}.txt"
        outside.write_text("top-secret-exfil")
        try:
            link = tmp_path / "secret"
            link.symlink_to(outside)
            file = File(filepath=str(link))

            with (
                patch("hsync_client.transfer.CONFIG", self._config(tmp_path)),
                patch("hsync_client.transfer.post") as mock_post,
            ):
                upload_file(file, check_size=False)
                mock_post.assert_not_called()
        finally:
            if outside.exists():
                outside.unlink()

    def test_escaping_relative_symlink_not_uploaded(self, tmp_path):
        """A relative symlink that resolves outside the bucket is refused too."""
        outside = tmp_path.parent / f"hsync_ulrel_{tmp_path.name}.txt"
        outside.write_text("top-secret-rel")
        try:
            sub = tmp_path / "sub"
            sub.mkdir()
            link = sub / "escape"
            link.symlink_to("../../" + outside.name)
            file = File(filepath=str(link))

            with (
                patch("hsync_client.transfer.CONFIG", self._config(tmp_path)),
                patch("hsync_client.transfer.post") as mock_post,
            ):
                upload_file(file, check_size=False)
                mock_post.assert_not_called()
        finally:
            if outside.exists():
                outside.unlink()

    def test_escaping_symlink_emits_security_rejected(self, tmp_path):
        """The refusal surfaces a security_rejected UI event."""
        outside = tmp_path.parent / f"hsync_ulemit_{tmp_path.name}.txt"
        outside.write_text("top-secret")
        try:
            link = tmp_path / "secret"
            link.symlink_to(outside)
            file = File(filepath=str(link))

            with (
                patch("hsync_client.transfer.CONFIG", self._config(tmp_path)),
                patch("hsync_client.transfer.emit_event") as mock_emit,
            ):
                upload_file(file, check_size=False)
            from hsync_client.events import UiEventName

            names = [c.args[0] for c in mock_emit.call_args_list]
            assert UiEventName.security_rejected in names
        finally:
            if outside.exists():
                outside.unlink()

    def test_in_bucket_symlink_still_uploaded(self, tmp_path):
        """A symlink whose target stays inside the bucket is uploaded as usual."""
        real = tmp_path / "real.txt"
        real.write_text("inside")
        link = tmp_path / "alias"
        link.symlink_to(real)
        file = File(filepath=str(link))

        mock_response = MagicMock()
        mock_response.status_code = 201

        with (
            patch("hsync_client.transfer.CONFIG", self._config(tmp_path)),
            patch("hsync_client.transfer._get_upload_status", return_value=("new", 0)),
            patch("hsync_client.transfer.post", return_value=mock_response) as mock_post,
        ):
            upload_file(file, check_size=False)
            mock_post.assert_called_once()
