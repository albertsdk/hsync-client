# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for the delete_file_remote action (remote-mode + P2P-mode wire shapes)."""

from unittest.mock import MagicMock, patch

import pytest
from hsync_common.models.file import File

from hsync_client.exceptions import ServerError
from hsync_client.transfer import delete_file_remote


def _config(tmp_path, *, p2p_mode: bool):
    return {
        "API": {"buckets": "https://peer/api/v1/buckets"},
        "REMOTE_BUCKET": "test",
        "REQUEST_HEADERS": {"Authorization": "Bearer key"},
        "LOCAL_BUCKET": str(tmp_path),
        "CID": "ClientDeleter1234567",
        "HTTP_TIMEOUT": 10,
        "P2P_MODE": p2p_mode,
    }


class TestDeleteFileRemote:
    """The same reconcile action must speak each transport's DELETE contract:

    - central server: form fields ``filename`` + ``cid`` (fastapi ``Form``);
    - P2P master: query params ``path`` + ``is_dir`` + ``cid``
      (``p2p_server._handle_delete`` reads only the query string).

    Sending the wrong shape makes the other side answer 400 and the stale
    remote copy survives (the resurrection bug). This is the regression test
    for that wire-shape gap.
    """

    def test_server_mode_sends_form_fields(self, tmp_path):
        """Server mode sends ``data=`` form fields, no query params."""
        file = File.from_dict({"filepath": "gone.txt"}, str(tmp_path), False)

        mock_response = MagicMock()
        mock_response.status_code = 200

        with (
            patch("hsync_client.transfer.CONFIG", _config(tmp_path, p2p_mode=False)),
            patch("hsync_client.transfer.get_ssl_verify", return_value=True),
            patch("hsync_client.utils.http_delete", return_value=mock_response) as mock_delete,
        ):
            delete_file_remote(file)

        args, kwargs = mock_delete.call_args
        assert args[0] == "https://peer/api/v1/buckets/test/files"
        assert kwargs["params"] is None
        assert kwargs["data"] == {
            "cid": "ClientDeleter1234567",
            "filename": "gone.txt",
        }

    def test_p2p_mode_sends_query_params(self, tmp_path):
        """P2P mode sends ``path``/``is_dir``/``cid`` query params, no form body."""
        file = File.from_dict({"filepath": "gone.txt"}, str(tmp_path), False)

        mock_response = MagicMock()
        mock_response.status_code = 204

        with (
            patch("hsync_client.transfer.CONFIG", _config(tmp_path, p2p_mode=True)),
            patch("hsync_client.transfer.get_ssl_verify", return_value=True),
            patch("hsync_client.utils.http_delete", return_value=mock_response) as mock_delete,
        ):
            delete_file_remote(file)

        args, kwargs = mock_delete.call_args
        assert args[0] == "https://peer/api/v1/buckets/test/files"
        assert kwargs["params"] == {
            "path": "gone.txt",
            "is_dir": "false",
            "cid": "ClientDeleter1234567",
        }
        assert kwargs["data"] is None

    def test_404_is_the_desired_end_state(self, tmp_path):
        """A 404 means the remote copy is already gone: silent success."""
        file = File.from_dict({"filepath": "gone.txt"}, str(tmp_path), False)

        mock_response = MagicMock()
        mock_response.status_code = 404

        with (
            patch("hsync_client.transfer.CONFIG", _config(tmp_path, p2p_mode=True)),
            patch("hsync_client.transfer.get_ssl_verify", return_value=True),
            patch("hsync_client.utils.http_delete", return_value=mock_response),
        ):
            # Must not raise.
            delete_file_remote(file)

    def test_server_error_raises(self, tmp_path):
        """A non-404 error response raises ServerError so the next cycle retries."""
        file = File.from_dict({"filepath": "gone.txt"}, str(tmp_path), False)

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "boom"

        with (
            patch("hsync_client.transfer.CONFIG", _config(tmp_path, p2p_mode=True)),
            patch("hsync_client.transfer.get_ssl_verify", return_value=True),
            patch("hsync_client.utils.http_delete", return_value=mock_response),
        ):
            with pytest.raises(ServerError):
                delete_file_remote(file)
