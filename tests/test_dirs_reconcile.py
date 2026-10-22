# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Startup directory reconcile: create-only, both directions.

Directories never appear in the /files listing and have no tombstones, so the
startup pass closes the gap left by the realtime channel connecting only after
the initial sync. It must create missing entries on either side, respect the
sync direction, and never push in remote-server mode.
"""

from unittest.mock import MagicMock, patch

from requests import RequestException

from hsync_client import sync as sync_mod
from hsync_client.sync import get_dirs_remote, reconcile_directories


def _config(tmp_path, direction: str = "bidirectional") -> dict:
    return {
        "LOCAL_BUCKET": str(tmp_path),
        "REMOTE_BUCKET": "share",
        "SYNC_DIRECTION": direction,
        "API": {"buckets": "https://peer/api/v1/buckets"},
        "REQUEST_HEADERS": {"X-API-Key": "k"},
        "CID": "cid",
        "HTTP_TIMEOUT": 30,
    }


class TestReconcileDirectories:
    def test_remote_directories_are_created_locally(self, tmp_path):
        with patch.object(sync_mod, "CONFIG", _config(tmp_path)):
            reconcile_directories(
                [
                    {"path": "empty", "permissions": "755"},
                    {"path": "empty/sub", "permissions": "755"},
                ],
                p2p_mode=False,
            )

        assert (tmp_path / "empty").is_dir()
        assert (tmp_path / "empty" / "sub").is_dir()

    def test_local_directories_missing_remotely_are_pushed(self, tmp_path):
        (tmp_path / "localempty").mkdir()
        posted: list[dict] = []

        def fake_post(_uri, **kwargs):
            posted.append(kwargs)
            return MagicMock(status_code=201)

        with (
            patch.object(sync_mod, "CONFIG", _config(tmp_path)),
            patch.object(sync_mod, "get_ssl_verify", return_value=False),
            patch.object(sync_mod, "post", side_effect=fake_post),
        ):
            reconcile_directories([], p2p_mode=True)

        assert len(posted) == 1
        params = posted[0]["params"]
        assert params["path"] == "localempty"
        assert params["cid"] == "cid"
        assert "permissions" in params

    def test_download_only_never_pushes_local_directories(self, tmp_path):
        (tmp_path / "localempty").mkdir()
        with (
            patch.object(sync_mod, "CONFIG", _config(tmp_path, "download-only")),
            patch.object(sync_mod, "post") as mock_post,
        ):
            reconcile_directories([], p2p_mode=True)

        mock_post.assert_not_called()

    def test_upload_only_does_not_create_remote_directories_locally(self, tmp_path):
        with (
            patch.object(sync_mod, "CONFIG", _config(tmp_path, "upload-only")),
            patch.object(sync_mod, "post") as mock_post,
        ):
            reconcile_directories([{"path": "remoteonly", "permissions": "755"}], p2p_mode=True)

        assert not (tmp_path / "remoteonly").exists()
        mock_post.assert_not_called()

    def test_remote_server_mode_does_not_push(self, tmp_path):
        (tmp_path / "localempty").mkdir()
        with (
            patch.object(sync_mod, "CONFIG", _config(tmp_path)),
            patch.object(sync_mod, "post") as mock_post,
        ):
            reconcile_directories([], p2p_mode=False)

        mock_post.assert_not_called()


class TestGetDirsRemoteContract:
    @staticmethod
    def _config() -> dict:
        return {
            "REMOTE_BUCKET": "share",
            "API": {"buckets": "https://peer/api/v1/buckets"},
            "REQUEST_HEADERS": {},
            "HTTP_TIMEOUT": 30,
        }

    def test_empty_listing_is_an_empty_list(self):
        response = MagicMock(status_code=200)
        response.json.return_value = []
        with (
            patch.object(sync_mod, "CONFIG", self._config()),
            patch.object(sync_mod, "get", return_value=response),
        ):
            assert get_dirs_remote() == []

    def test_error_status_is_unavailable(self):
        response = MagicMock(status_code=500)
        with (
            patch.object(sync_mod, "CONFIG", self._config()),
            patch.object(sync_mod, "get", return_value=response),
        ):
            assert get_dirs_remote() is None

    def test_request_exception_is_unavailable(self):
        with (
            patch.object(sync_mod, "CONFIG", self._config()),
            patch.object(sync_mod, "get", side_effect=RequestException("boom")),
        ):
            assert get_dirs_remote() is None
