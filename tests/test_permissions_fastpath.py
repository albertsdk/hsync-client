# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for the metadata-only permission fast path.

``permissions_only`` uploads must not re-send file bytes: the
``PATCH …/files/permissions`` endpoint is tried first, and the legacy
full-upload same-SHA branch is only used as a fallback when the peer does
not expose the endpoint.
"""

from unittest.mock import MagicMock, patch

from hsync_client.transfer import patch_permissions_remote


def _upload_file(**overrides):
    file = MagicMock()
    file.filepath = "/bucket/doc.txt"
    file.name = "doc.txt"
    file.size = 4
    file.sha256sum = "a" * 64
    file.ctime = 1000
    file.mtime = 1000.0
    file.permissions = "600"
    for k, v in overrides.items():
        setattr(file, k, v)
    return file


class TestPatchPermissionsRemote:
    def test_patch_returns_true_on_200(self):
        from hsync_client import transfer as transfer_mod

        file = _upload_file()
        response = MagicMock()
        response.status_code = 200
        with (
            patch.object(transfer_mod, "patch", return_value=response) as mock_patch,
            patch.object(
                transfer_mod,
                "CONFIG",
                {
                    "API": {"buckets": "http://srv/api/v1/buckets"},
                    "REMOTE_BUCKET": "demo",
                    "REQUEST_HEADERS": {},
                    "CID": "TestClientX1234567890",
                    "HTTP_TIMEOUT": 30,
                },
            ),
        ):
            ok = patch_permissions_remote("doc.txt", file.permissions)

        assert ok is True
        mock_patch.assert_called_once()

    def test_patch_returns_true_even_without_permissions_field(self):
        # The caller reads file.permissions; patch_permissions_remote takes the
        # value explicitly. Missing perms upstream is the caller's gate.
        from hsync_client import transfer as transfer_mod

        response = MagicMock()
        response.status_code = 200
        with (
            patch.object(transfer_mod, "patch", return_value=response),
            patch.object(
                transfer_mod,
                "CONFIG",
                {
                    "API": {"buckets": "http://srv/api/v1/buckets"},
                    "REMOTE_BUCKET": "demo",
                    "REQUEST_HEADERS": {},
                    "CID": "TestClientX1234567890",
                    "HTTP_TIMEOUT": 30,
                },
            ),
        ):
            ok = patch_permissions_remote("doc.txt", "600")

        assert ok is True

    def test_patch_returns_false_on_404_fallback(self):
        from hsync_client import transfer as transfer_mod

        response = MagicMock()
        response.status_code = 404
        with (
            patch.object(transfer_mod, "patch", return_value=response),
            patch.object(
                transfer_mod,
                "CONFIG",
                {
                    "API": {"buckets": "http://srv/api/v1/buckets"},
                    "REMOTE_BUCKET": "demo",
                    "REQUEST_HEADERS": {},
                    "CID": "TestClientX1234567890",
                    "HTTP_TIMEOUT": 30,
                },
            ),
        ):
            ok = patch_permissions_remote("doc.txt", "600")

        assert ok is False

    def test_patch_exception_returns_false(self):
        from requests import RequestException

        from hsync_client import transfer as transfer_mod

        def _boom(*args, **kwargs):
            raise RequestException("connection refused")

        with (
            patch.object(transfer_mod, "patch", side_effect=_boom),
            patch.object(
                transfer_mod,
                "CONFIG",
                {
                    "API": {"buckets": "http://srv/api/v1/buckets"},
                    "REMOTE_BUCKET": "demo",
                    "REQUEST_HEADERS": {},
                    "CID": "TestClientX1234567890",
                    "HTTP_TIMEOUT": 30,
                },
            ),
        ):
            ok = patch_permissions_remote("doc.txt", "600")

        assert ok is False


class TestUploadAttemptFastPath:
    def test_complete_permission_only_uses_patch_and_returns(self):
        """A permission-only upload with status 'complete' must call the
        metadata endpoint and STOP — no file payload is sent."""
        from hsync_client import transfer as transfer_mod

        file = _upload_file()
        with (
            patch.object(transfer_mod, "_get_upload_status", return_value=("complete", 0)),
            patch.object(transfer_mod, "_patch_permissions_only", return_value=True) as mock_patch,
            patch.object(transfer_mod, "post") as mock_post,
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
                },
            ),
        ):
            transfer_mod._upload_file_attempt(file, permissions_only=True)

        mock_patch.assert_called_once_with(file, "doc.txt")
        mock_post.assert_not_called()

    def test_complete_permission_only_falls_back_to_full_upload_when_patch_fails(self):
        """When the peer lacks the endpoint, the legacy same-SHA full-upload
        branch is used (bytes re-sent, server dedups them)."""
        from hsync_client import transfer as transfer_mod

        file = _upload_file()
        response = MagicMock()
        response.status_code = 200
        with (
            patch.object(transfer_mod, "_get_upload_status", return_value=("complete", 0)),
            patch.object(transfer_mod, "_patch_permissions_only", return_value=False),
            patch.object(transfer_mod, "prepare_delta_upload", return_value=None),
            patch.object(transfer_mod, "_upload_version_fields", return_value=("{}", "", 0)),
            patch.object(transfer_mod, "should_compress", return_value=False),
            patch.object(transfer_mod, "_build_multipart_streaming") as mock_build,
            patch.object(transfer_mod, "post", return_value=response),
            patch.object(transfer_mod, "path_lock"),
            patch.object(transfer_mod, "_record_synced_base"),
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
            # Prepare a real streaming body so the POST call works.
            mock_build.return_value = (b"body", "multipart/form-data; boundary=x")

            transfer_mod._upload_file_attempt(file, permissions_only=True)

        assert mock_build.called
