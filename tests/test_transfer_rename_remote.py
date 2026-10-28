# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for the rename_file_remote action (zero-byte remote move).

The endpoint is the reason a rename performed while offline no longer has to
re-upload the file: the caller pairs the stale remote path with the new local
one by content hash and asks the peer to move its own copy.
"""

from unittest.mock import MagicMock, patch

import pytest
from hsync_common.models.file import File

from hsync_client.exceptions import ServerError
from hsync_client.transfer import rename_file_remote, wire_relpath


def _config(tmp_path):
    return {
        "API": {"buckets": "https://peer/api/v1/buckets"},
        "REMOTE_BUCKET": "test",
        "REQUEST_HEADERS": {"Authorization": "Bearer key"},
        "LOCAL_BUCKET": str(tmp_path),
        "CID": "ClientRenamer1234567",
        "HTTP_TIMEOUT": 10,
        "P2P_MODE": False,
    }


def _response(status_code: int, payload: dict | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.text = ""
    if payload is None:
        response.json.side_effect = ValueError("no body")
    else:
        response.json.return_value = payload
    return response


class TestWireRelpath:
    """The peer only ever receives a bucket-relative path."""

    def test_absolute_local_path_is_relativised(self, tmp_path):
        file = File(filepath=str(tmp_path / "sub" / "a.bin"), size=1, sha256sum="a" * 64)
        with patch("hsync_client.transfer.CONFIG", _config(tmp_path)):
            assert wire_relpath(file) == "sub/a.bin"

    def test_relative_path_is_kept(self, tmp_path):
        file = File(filepath="sub/a.bin", size=1, sha256sum="a" * 64)
        with patch("hsync_client.transfer.CONFIG", _config(tmp_path)):
            assert wire_relpath(file) == "sub/a.bin"


class TestRenameFileRemote:
    """One PATCH carries the whole operation: paths, guards and metadata."""

    def test_sends_the_move_with_the_expected_content_guard(self, tmp_path):
        digest = "b" * 64
        old = File.from_dict({"filepath": "old.bin", "sha256sum": digest}, str(tmp_path), False)
        new = File(
            filepath=str(tmp_path / "new.bin"),
            size=42,
            sha256sum=digest,
            mtime=1_700_000_000,
            permissions="600",
        )
        payload = {"file": {"filepath": "new.bin"}, "version": {"vclock": {"X" * 21: 1}}}

        with (
            patch("hsync_client.transfer.CONFIG", _config(tmp_path)),
            patch("hsync_client.transfer.get_ssl_verify", return_value=True),
            patch("hsync_client.transfer.patch", return_value=_response(200, payload)) as move,
        ):
            result = rename_file_remote(old, new)

        assert result == payload
        args, kwargs = move.call_args
        assert args[0] == "https://peer/api/v1/buckets/test/files"
        assert kwargs["params"] == {
            "src": "old.bin",
            "dst": "new.bin",
            "cid": "ClientRenamer1234567",
            "expected_sha256": digest,
            "mtime": "1700000000",
            "permissions": "600",
        }

    def test_a_conflict_reports_none_so_the_caller_uploads(self, tmp_path):
        old = File.from_dict({"filepath": "old.bin", "sha256sum": "c" * 64}, str(tmp_path), False)
        new = File(filepath=str(tmp_path / "new.bin"), size=1, sha256sum="c" * 64)

        with (
            patch("hsync_client.transfer.CONFIG", _config(tmp_path)),
            patch("hsync_client.transfer.get_ssl_verify", return_value=True),
            patch("hsync_client.transfer.patch", return_value=_response(409)),
        ):
            assert rename_file_remote(old, new) is None

    def test_an_unsupported_endpoint_reports_none(self, tmp_path):
        old = File.from_dict({"filepath": "old.bin", "sha256sum": "d" * 64}, str(tmp_path), False)
        new = File(filepath=str(tmp_path / "new.bin"), size=1, sha256sum="d" * 64)

        with (
            patch("hsync_client.transfer.CONFIG", _config(tmp_path)),
            patch("hsync_client.transfer.get_ssl_verify", return_value=True),
            patch("hsync_client.transfer.patch", return_value=_response(405)),
        ):
            assert rename_file_remote(old, new) is None

    def test_a_server_error_propagates(self, tmp_path):
        old = File.from_dict({"filepath": "old.bin", "sha256sum": "e" * 64}, str(tmp_path), False)
        new = File(filepath=str(tmp_path / "new.bin"), size=1, sha256sum="e" * 64)

        with (
            patch("hsync_client.transfer.CONFIG", _config(tmp_path)),
            patch("hsync_client.transfer.get_ssl_verify", return_value=True),
            patch("hsync_client.transfer.patch", return_value=_response(500)),
            pytest.raises(ServerError),
        ):
            rename_file_remote(old, new)

    def test_a_bodyless_success_still_reports_the_move(self, tmp_path):
        old = File.from_dict({"filepath": "old.bin", "sha256sum": "f" * 64}, str(tmp_path), False)
        new = File(filepath=str(tmp_path / "new.bin"), size=1, sha256sum="f" * 64)

        with (
            patch("hsync_client.transfer.CONFIG", _config(tmp_path)),
            patch("hsync_client.transfer.get_ssl_verify", return_value=True),
            patch("hsync_client.transfer.patch", return_value=_response(200)),
        ):
            assert rename_file_remote(old, new) == {}
