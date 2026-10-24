# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for transfer status functionality."""

from unittest.mock import MagicMock, patch

from hsync_common.models.file import File
from requests import RequestException

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

    def test_get_upload_status_invalid_offsets_fall_back_to_new(self, tmp_path):
        """Hostile/garbage resumable offsets are clamped back to a fresh upload."""
        from hsync_client.transfer import _get_upload_status

        config = {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "test",
            "REQUEST_HEADERS": {"X-API-Key": "test"},
        }
        for bad_offset in (-1, "500", 1001, None, True):
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"status": "resumable", "offset": bad_offset}

            with (
                patch("hsync_client.transfer.CONFIG", config),
                patch("hsync_client.transfer.get", return_value=mock_response),
                patch("hsync_client.transfer.get_ssl_verify", return_value=True),
            ):
                status, offset = _get_upload_status("file.txt", "abc123", 1000)
                assert (status, offset) == ("new", 0), f"offset {bad_offset!r}"

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


class TestUploadStatusPreflightShortCircuit:
    """The resumable-upload probe is skipped for small non-permission uploads.

    Regression guard for the per-file ``GET /files/upload-status`` round trip:
    files below ``UPLOAD_STATUS_PREFLIGHT_MIN_SIZE`` start at offset 0 without
    probing, while larger files — and every permission-only upload, whose fast
    path depends on probing — still issue the request.
    """

    @staticmethod
    def _file(size: int):
        file = MagicMock()
        file.filepath = "/bucket/blob.bin"
        file.name = "blob.bin"
        file.size = size
        file.sha256sum = "a" * 64
        file.ctime = 1000
        file.mtime = 1000.0
        file.permissions = "600"
        return file

    @staticmethod
    def _run(transfer_mod, file, probe, permissions_only: bool = False) -> None:
        response = MagicMock()
        response.status_code = 201
        with (
            patch.object(transfer_mod, "_get_upload_status", probe),
            patch.object(transfer_mod, "prepare_delta_upload", return_value=None),
            patch.object(transfer_mod, "resolve_upload_version_fields", return_value=("{}", "", 0)),
            patch.object(transfer_mod, "should_compress", return_value=False),
            patch.object(transfer_mod, "_build_multipart_streaming") as mock_build,
            patch.object(transfer_mod, "post", return_value=response),
            patch.object(transfer_mod, "path_lock"),
            patch.object(transfer_mod, "emit_event"),
            patch.object(transfer_mod, "_record_201_upload"),
            patch.object(
                transfer_mod,
                "CONFIG",
                {
                    "API": {"buckets": "http://srv/api/v1/buckets"},
                    "REMOTE_BUCKET": "demo",
                    "LOCAL_BUCKET": "/bucket",
                    "REQUEST_HEADERS": {},
                    "CID": "TestClientX1234567890",
                    "HTTP_TIMEOUT": 30,
                    "COMPRESSION_ENABLED": False,
                    "SERVER_COMPRESSION_ENABLED": False,
                },
            ),
        ):
            mock_build.return_value = (b"body", "multipart/form-data; boundary=x")
            transfer_mod._upload_file_attempt(file, permissions_only=permissions_only)

    def test_small_file_skips_probe(self):
        from hsync_client import transfer as transfer_mod

        probe = MagicMock(return_value=("new", 0))
        self._run(transfer_mod, self._file(1024), probe)
        probe.assert_not_called()

    def test_file_at_threshold_still_probes(self):
        from hsync_client import transfer as transfer_mod

        probe = MagicMock(return_value=("new", 0))
        self._run(
            transfer_mod,
            self._file(transfer_mod.UPLOAD_STATUS_PREFLIGHT_MIN_SIZE),
            probe,
        )
        probe.assert_called_once()

    def test_permission_only_probes_even_when_small(self):
        from hsync_client import transfer as transfer_mod

        probe = MagicMock(return_value=("complete", 0))
        with patch.object(transfer_mod, "_patch_permissions_only", return_value=True):
            self._run(transfer_mod, self._file(10), probe, permissions_only=True)
        probe.assert_called_once()


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
