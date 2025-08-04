# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for upload max-wait timeout, reschedule, stable-file, and nothing-to-do."""

from unittest.mock import MagicMock, patch

from hsync_common.models.file import File

from hsync_client.transfer import upload_file

_BASE_CONFIG = {
    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
    "REMOTE_BUCKET": "test",
    "REQUEST_HEADERS": {"X-API-Key": "test"},
    "CID": "test-client",
    # Disable retry/backoff so error-status paths resolve in one attempt.
    "TRANSFER_RETRY_ATTEMPTS": 1,
}


class TestUploadFileMaxWaitTimeout:
    """Tests for max wait timeout in upload_file function."""

    def test_upload_waits_for_file_stability(self, tmp_path):
        """Upload waits for file to stabilize before uploading."""
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
                    "MAX_WAIT_TIMEOUT": 120,
                },
            ),
            patch("hsync_client.transfer.is_file_stable", return_value=True),
            patch("hsync_client.transfer.post", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            upload_file(file, check_size=True)

    def test_upload_reschedules_after_max_wait(self, tmp_path):
        """An unstable file is rescheduled (not force-uploaded) after timeout."""
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

        # is_file_stable always returns False to simulate an unstable file
        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "LOCAL_BUCKET": str(tmp_path),
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "CID": "test-client",
                    "MAX_WAIT_TIMEOUT": 2,  # Short timeout for test
                },
            ),
            patch("hsync_client.transfer.is_file_stable", return_value=False),
            patch("hsync_client.transfer.time_sleep"),
            patch("hsync_client.transfer.post") as mock_post,
            patch("hsync_client.transfer._reschedule_upload") as mock_reschedule,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            upload_file(file, check_size=True)
            # The file never stabilized, so the upload must be deferred, not forced.
            mock_post.assert_not_called()
            mock_reschedule.assert_called_once_with(file, None)

    def test_upload_uses_default_max_wait_timeout(self, tmp_path):
        """Upload uses default timeout when not configured."""
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

        # CONFIG without MAX_WAIT_TIMEOUT should use default (120)
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
            patch("hsync_client.transfer.is_file_stable", return_value=True),
            patch("hsync_client.transfer.post", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            # Should not raise, uses default timeout
            upload_file(file, check_size=True)


class TestRescheduleUpload:
    """Tests for _reschedule_upload deferred-upload logic."""

    def _make_file(self, tmp_path):
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")
        return File(
            filepath=str(test_file),
            sha256sum="abc123",
            size=12,
            mtime=1000,
            ctime=1000,
            permissions="644",
        )

    def test_schedules_timer_and_decrements(self, tmp_path):
        """A deferred upload arms a daemon Timer with one fewer attempt left."""
        from hsync_client.transfer import _reschedule_upload

        file = self._make_file(tmp_path)

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {"UPLOAD_RESCHEDULE_DELAY": 7},
            ),
            patch("hsync_client.transfer.threading.Timer") as mock_timer,
        ):
            _reschedule_upload(file, reschedules_left=3)

            args, kwargs = mock_timer.call_args
            # delay, target callable
            assert args[0] == 7
            assert args[1] is upload_file
            # re-arms upload_file(file, _reschedules_left=2)
            assert kwargs["args"] == (file,)
            assert kwargs["kwargs"] == {"_reschedules_left": 2}
            assert mock_timer.return_value.daemon is True
            mock_timer.return_value.start.assert_called_once()

    def test_resolves_max_from_config_when_none(self, tmp_path):
        """A first deferral (None) resolves the bound from CONFIG."""
        from hsync_client.transfer import _reschedule_upload

        file = self._make_file(tmp_path)

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {"UPLOAD_MAX_RESCHEDULES": 4, "UPLOAD_RESCHEDULE_DELAY": 1},
            ),
            patch("hsync_client.transfer.threading.Timer") as mock_timer,
        ):
            _reschedule_upload(file, reschedules_left=None)

            # Resolved 4 from CONFIG, then decremented to 3 for the next attempt.
            assert mock_timer.call_args.kwargs["args"] == (file,)
            assert mock_timer.call_args.kwargs["kwargs"] == {"_reschedules_left": 3}

    def test_exhausted_does_not_schedule(self, tmp_path):
        """Once reschedules are exhausted the file is skipped, not deferred."""
        from hsync_client.transfer import _reschedule_upload

        file = self._make_file(tmp_path)

        with (
            patch("hsync_client.transfer.CONFIG", {}),
            patch("hsync_client.transfer.threading.Timer") as mock_timer,
        ):
            _reschedule_upload(file, reschedules_left=0)
            mock_timer.assert_not_called()


class TestWaitForStableFile:
    """Tests for the hybrid on_closed fast-path stability guard."""

    def _make_file(self, tmp_path):
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content")
        return File(
            filepath=str(test_file),
            sha256sum="abc123",
            size=12,
            mtime=1000,
            ctime=1000,
            permissions="644",
        )

    def test_from_closed_uses_short_recheck(self, tmp_path):
        """An on_closed trigger trusts the close after one short re-check."""
        from hsync_client.constants import STABILITY_DELAY_ON_CLOSED
        from hsync_client.transfer import _wait_for_stable_file

        file = self._make_file(tmp_path)

        with (
            patch("hsync_client.transfer.is_file_stable", return_value=True) as mock_stable,
            patch("hsync_client.transfer.time_sleep") as mock_sleep,
        ):
            assert _wait_for_stable_file(file, from_closed=True) is True
            # Single re-check with the fast interval, no conservative polling.
            mock_stable.assert_called_once_with(file, STABILITY_DELAY_ON_CLOSED)
            mock_sleep.assert_not_called()

    def test_from_closed_falls_back_when_still_changing(self, tmp_path):
        """If the short re-check still sees changes, the full guard takes over."""
        from hsync_client.transfer import _wait_for_stable_file

        file = self._make_file(tmp_path)

        # Fast re-check fails, conservative poll then settles.
        with (
            patch(
                "hsync_client.transfer.is_file_stable",
                side_effect=[False, True],
            ) as mock_stable,
            patch("hsync_client.transfer.CONFIG", {"MAX_WAIT_TIMEOUT": 5}),
            patch("hsync_client.transfer.time_sleep"),
        ):
            assert _wait_for_stable_file(file, from_closed=True) is True
            assert mock_stable.call_count == 2

    def test_non_closed_skips_fast_path(self, tmp_path):
        """Without an on_closed trigger only the conservative poll runs."""
        from hsync_client.transfer import _wait_for_stable_file

        file = self._make_file(tmp_path)

        with (
            patch("hsync_client.transfer.is_file_stable", return_value=True) as mock_stable,
            patch("hsync_client.transfer.CONFIG", {"MAX_WAIT_TIMEOUT": 5}),
            patch("hsync_client.transfer.time_sleep"),
        ):
            assert _wait_for_stable_file(file, from_closed=False) is True
            # Polled with the default interval (no fast-path argument).
            mock_stable.assert_called_once_with(file)

    def test_returns_false_when_never_settles(self, tmp_path):
        """A file that never stabilizes within the window defers the upload."""
        from hsync_client.transfer import _wait_for_stable_file

        file = self._make_file(tmp_path)

        with (
            patch("hsync_client.transfer.is_file_stable", return_value=False),
            patch("hsync_client.transfer.CONFIG", {"MAX_WAIT_TIMEOUT": 2}),
            patch("hsync_client.transfer.time_sleep"),
        ):
            assert _wait_for_stable_file(file, from_closed=False) is False


class TestNothingToDo:
    """Tests for nothing_to_do function."""

    def test_logs_filepath(self):
        """Logs the filepath for debugging."""
        from hsync_client.transfer import nothing_to_do

        file = File(filepath="/path/to/file.txt", sha256sum="abc123", size=100)
        # Just verify it doesn't raise
        nothing_to_do(file)
