# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for H8 — permanent download failure skip-set (retry amplification).

A download that fails permanently (e.g. declared size exceeds the configured
cap) must not be retried on every resync cycle within the same sync run.
"""

from unittest.mock import MagicMock, patch

import pytest
from hsync_common.models.file import File

from hsync_client.events import UiEventName
from hsync_client.exceptions import PermanentDownloadError
from hsync_client.sync import (
    _is_permanently_failed,
    _permanent_skip_key,
    _reset_permanent_failures,
    update_file,
)


@pytest.fixture(autouse=True)
def _clean_skip_set():
    """Ensure a clean skip-set before and after each test."""
    _reset_permanent_failures()
    yield
    _reset_permanent_failures()


class TestPermanentSkipSet:
    """Unit tests for the per-cycle skip-set helpers."""

    def test_skip_key_is_path_plus_hash(self):
        f = File(filepath="dir/file.txt", sha256sum="abc123", size=10)
        assert _permanent_skip_key(f) == "dir/file.txt|abc123"

    def test_is_permanently_failed_false_by_default(self):
        f = File(filepath="x.txt", sha256sum="h", size=1)
        assert not _is_permanently_failed(f)

    def test_reset_clears_failures(self):
        from hsync_client.sync import _permanent_download_failures

        f = File(filepath="x.txt", sha256sum="h", size=1)
        _permanent_download_failures.add(_permanent_skip_key(f))
        assert _is_permanently_failed(f)
        _reset_permanent_failures()
        assert not _is_permanently_failed(f)


class TestUpdateFilePermanentFailure:
    """Regression: update_file must not re-download a permanently-failed file
    and must record it in the skip-set + emit transfer_failed."""

    def test_new_file_permanent_failure_is_skipped_and_recorded(self, tmp_path):
        """A permanent download failure on a new file records the skip key."""
        data = {
            "filepath": "big.dat",
            "name": "big.dat",
            "path": "",
            "mtime": 1000,
            "ctime": 1000,
            "size": 100,
            "sha256sum": "deadbeef",
            "permissions": "644",
        }

        def _boom(_file, **kwargs):
            raise PermanentDownloadError("exceeds max download size")

        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.transfer.download_file", side_effect=_boom),
        ):
            update_file(data)

        # The file must now be in the skip-set for this cycle. Reconstruct
        # the File exactly as update_file does in the "new file" branch:
        # File.from_dict(data, bucket, strip_dir_prefix=True) -> relpath.
        f = File.from_dict(data, str(tmp_path))
        assert _is_permanently_failed(f)

    def test_the_failure_event_carries_direction_and_message(self, tmp_path):
        """The desktop builds its error row from op + message (reason is sync_paused only)."""
        data = {
            "filepath": "big.dat",
            "name": "big.dat",
            "path": "",
            "mtime": 1000,
            "ctime": 1000,
            "size": 100,
            "sha256sum": "deadbeef",
            "permissions": "644",
        }

        def _boom(_file, **kwargs):
            raise PermanentDownloadError("exceeds max download size")

        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.transfer.download_file", side_effect=_boom),
            patch("hsync_client.sync.emit_event") as emit,
        ):
            update_file(data)

        failures = [
            call
            for call in emit.call_args_list
            if call.args and call.args[0] == UiEventName.transfer_failed
        ]
        assert failures
        payload = failures[-1].kwargs
        assert payload["op"] == "download"
        assert payload["message"] == "exceeds max download size"
        assert "reason" not in payload

    def test_already_failed_file_is_not_redownloaded(self, tmp_path):
        """A second update_file for the same path+hash does not call download."""
        data = {
            "filepath": "big.dat",
            "name": "big.dat",
            "path": "",
            "mtime": 1000,
            "ctime": 1000,
            "size": 100,
            "sha256sum": "deadbeef",
            "permissions": "644",
        }

        mock_download = MagicMock()
        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.transfer.download_file", mock_download),
        ):
            # Pre-seed the skip-set so the download is skipped. The "new file"
            # branch builds the File with strip_dir_prefix=True (relpath), so
            # the skip key must match that construction.
            f = File.from_dict(data, str(tmp_path))
            from hsync_client.sync import _permanent_download_failures

            _permanent_download_failures.add(_permanent_skip_key(f))

            update_file(data)

            mock_download.assert_not_called()
