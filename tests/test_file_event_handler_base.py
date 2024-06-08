# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for RemoteFileEventHandler."""

from asyncio import new_event_loop
from unittest.mock import MagicMock, patch

from hsync_client.remote_websocket import RemoteFileEventHandler


class TestFileEventHandler:
    """Tests for RemoteFileEventHandler class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.websocket = MagicMock()
        self.loop = new_event_loop()
        self.event_queue = MagicMock()
        self.handler = RemoteFileEventHandler(self.websocket, self.loop, self.event_queue)

    def teardown_method(self):
        """Clean up event loop."""
        self.loop.close()

    def test_init_stores_websocket_and_loop(self):
        """Handler stores websocket and loop references."""
        assert self.handler.websocket == self.websocket
        assert self.handler.loop == self.loop
        assert self.handler.event_queue == self.event_queue

    def test_send_message_uses_event_queue(self):
        """Handler uses event queue for message delivery."""
        message = MagicMock()
        message.to_dict.return_value = {"test": "data"}

        with patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run:
            self.handler._broadcast_event(message)
            mock_run.assert_called_once()

    def test_send_message_direct_without_queue(self):
        """Handler sends directly if no event queue."""
        handler = RemoteFileEventHandler(self.websocket, self.loop, None)
        message = MagicMock()
        message.to_dict.return_value = {"test": "data"}

        with patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run:
            handler._broadcast_event(message)
            mock_run.assert_called_once()

    def test_send_message_skips_download_only(self):
        """_broadcast_event does nothing in download-only mode."""
        message = MagicMock()
        config = {"SYNC_DIRECTION": "download-only"}

        with (
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler._broadcast_event(message)
            mock_run.assert_not_called()

    # -- on_any_event --

    def test_on_any_event_logs_hidden_dir(self):
        """on_any_event logs debug for hidden directories."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/.hidden"
        event.dest_path = None

        with (
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=True),
            patch("hsync_client.file_event_handler.log") as mock_log,
        ):
            self.handler.on_any_event(event)
            assert mock_log.debug.called

    def test_on_any_event_logs_hidden_file(self):
        """on_any_event logs debug for hidden files."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/.hidden_file"
        event.dest_path = None

        with (
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=True),
            patch("hsync_client.file_event_handler.log") as mock_log,
        ):
            self.handler.on_any_event(event)
            assert mock_log.debug.called

    # -- on_created --

    def test_on_created_skips_hidden_directory(self):
        """on_created skips hidden directories."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/.hidden"

        with patch("hsync_client.file_event_handler.is_hidden_dir", return_value=True):
            self.handler.on_created(event)

        # Should not send any message
        assert not self.event_queue.enqueue.called

    def test_on_created_broadcasts_visible_directory(self):
        """on_created broadcasts event for visible directories."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/visible"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.Dir.get") as mock_get,
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe"),
        ):
            mock_dir = MagicMock()
            mock_get.return_value = mock_dir

            self.handler.on_created(event)

            mock_get.assert_called_once_with("/bucket/visible")

    def test_on_deleted_skips_hidden_directory(self):
        """on_deleted skips hidden directories."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/.hidden"

        with patch("hsync_client.file_event_handler.is_hidden_dir", return_value=True):
            self.handler.on_deleted(event)

        # Should not send any message
        assert not self.event_queue.enqueue.called

    def test_on_deleted_skips_hidden_file(self):
        """on_deleted skips hidden files."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/.hidden_file"

        with patch("hsync_client.file_event_handler.is_hidden_file", return_value=True):
            self.handler.on_deleted(event)

        # Should not send any message
        assert not self.event_queue.enqueue.called

    def test_on_deleted_broadcasts_visible_file(self):
        """on_deleted broadcasts event for visible files."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/visible.txt"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe"),
        ):
            self.handler.on_deleted(event)
            # Message should be sent

    def test_on_moved_hidden_to_hidden_ignored(self):
        """on_moved ignores moves from hidden to hidden directory."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/.hidden1"
        event.dest_path = "/bucket/.hidden2"

        with patch("hsync_client.file_event_handler.is_hidden_dir", return_value=True):
            self.handler.on_moved(event)

        # Should not send any message
        assert not self.event_queue.enqueue.called

    def test_on_moved_hidden_file_ignored(self):
        """on_moved ignores hidden files."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/.hidden_file"
        event.dest_path = "/bucket/visible_file"

        with patch("hsync_client.file_event_handler.is_hidden_file", return_value=True):
            self.handler.on_moved(event)

        # Should not send any message
        assert not self.event_queue.enqueue.called

    def test_on_moved_hidden_to_visible_triggers_created(self):
        """on_moved from hidden to visible directory triggers on_created."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/.hidden"
        event.dest_path = "/bucket/visible"

        def side_effect(path):
            return path == "/bucket/.hidden"

        with (
            patch("hsync_client.file_event_handler.is_hidden_dir", side_effect=side_effect),
            patch.object(self.handler, "on_created") as mock_created,
        ):
            self.handler.on_moved(event)
            mock_created.assert_called_once()

    def test_on_moved_visible_to_hidden_triggers_deleted(self):
        """on_moved from visible to hidden directory triggers on_deleted."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/visible"
        event.dest_path = "/bucket/.hidden"

        def side_effect(path):
            return path == "/bucket/.hidden"

        with (
            patch("hsync_client.file_event_handler.is_hidden_dir", side_effect=side_effect),
            patch.object(self.handler, "on_deleted") as mock_deleted,
        ):
            self.handler.on_moved(event)
            mock_deleted.assert_called_once()

    def test_on_moved_dir_rename_sends_message(self):
        """on_moved sends rename event for visible directory rename."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/old_dir"
        event.dest_path = "/bucket/new_dir"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        mock_dir = MagicMock()

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.Dir.get", return_value=mock_dir),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler.on_moved(event)
            mock_run.assert_called_once()

    def test_on_moved_dir_rename_no_dst_dir_get(self):
        """on_moved does not send message when Dir.get returns None."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/old_dir"
        event.dest_path = "/bucket/new_dir"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.Dir.get", return_value=None),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler.on_moved(event)
            mock_run.assert_not_called()

    def test_on_moved_file_rename_sends_message(self):
        """on_moved sends rename event for visible file rename."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/old.txt"
        event.dest_path = "/bucket/new.txt"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        mock_file = MagicMock()

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.file_event_handler.File.get", return_value=mock_file),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler.on_moved(event)
            mock_run.assert_called_once()

    def test_on_moved_file_rename_no_dst_file_get(self):
        """on_moved does not send message when File.get returns None."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/old.txt"
        event.dest_path = "/bucket/new.txt"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.file_event_handler.File.get", return_value=None),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler.on_moved(event)
            mock_run.assert_not_called()

    # -- on_deleted ignore-delete --

    def test_on_deleted_ignore_delete_skips_file(self):
        """on_deleted skips file deletion when IGNORE_DELETE is True."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/file.txt"

        config = {"IGNORE_DELETE": True}

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler.on_deleted(event)
            mock_run.assert_not_called()

    def test_on_deleted_ignore_delete_skips_dir(self):
        """on_deleted skips directory deletion when IGNORE_DELETE is True."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/subdir"

        config = {"IGNORE_DELETE": True}

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler.on_deleted(event)
            mock_run.assert_not_called()

    def test_on_deleted_broadcasts_visible_dir(self):
        """on_deleted broadcasts event for visible directory deletion."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/subdir"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
            "IGNORE_DELETE": False,
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler.on_deleted(event)
            mock_run.assert_called_once()

    def test_on_closed_skips_directories(self):
        """on_closed skips directory events."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/dir"

        # on_closed should not process directories
        with patch("hsync_client.remote_websocket.upload_file") as mock_upload:
            self.handler.on_closed(event)
            mock_upload.assert_not_called()

    def test_on_closed_skips_hidden_files(self):
        """on_closed skips hidden files."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/.hidden"

        with (
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=True),
            patch("hsync_client.remote_websocket.upload_file") as mock_upload,
        ):
            self.handler.on_closed(event)
            mock_upload.assert_not_called()

    def test_on_closed_uploads_visible_file(self):
        """on_closed uploads visible files."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/file.txt"

        with (
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.file_event_handler.exists", return_value=True),
            patch("hsync_client.file_event_handler.File.get") as mock_get,
            patch("hsync_client.remote_websocket.upload_file") as mock_upload,
        ):
            mock_file = MagicMock()
            mock_get.return_value = mock_file

            self.handler.on_closed(event)

            mock_upload.assert_called_once_with(mock_file)

    # -- on_created symlink --

    def test_on_created_broadcasts_symlink(self):
        """on_created broadcasts symlink event for visible symlinks."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/link.txt"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.file_event_handler.islink", return_value=True),
            patch("hsync_client.file_event_handler.Symlink.get") as mock_get,
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe"),
        ):
            mock_symlink = MagicMock()
            mock_get.return_value = mock_symlink
            self.handler.on_created(event)
            mock_get.assert_called_once_with("/bucket/link.txt")

    def test_on_created_skips_symlink_when_get_returns_none(self):
        """on_created does not send message when Symlink.get returns None."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/link.txt"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.file_event_handler.islink", return_value=True),
            patch("hsync_client.file_event_handler.Symlink.get", return_value=None),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler.on_created(event)
            mock_run.assert_not_called()

    # -- on_modified --

    def test_on_modified_broadcasts_visible_directory(self):
        """on_modified broadcasts event for visible non-root directory."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/subdir"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.is_same_paths", return_value=False),
            patch("hsync_client.file_event_handler.Dir.get") as mock_get,
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            mock_dir = MagicMock()
            mock_get.return_value = mock_dir
            self.handler.on_modified(event)
            mock_get.assert_called_once_with("/bucket/subdir")
            mock_run.assert_called_once()

    def test_on_modified_skips_root_directory(self):
        """on_modified skips the root bucket directory."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket"

        config = {"LOCAL_BUCKET": "/bucket"}

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.is_same_paths", return_value=True),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler.on_modified(event)
            mock_run.assert_not_called()

    def test_on_modified_non_linux_uploads_file(self):
        """on_modified uploads file on non-Linux (fallback path)."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/file.txt"

        config = {"SYNC_DIRECTION": "bidirectional"}

        with (
            patch("hsync_client.file_event_handler.SUPPORTS_ON_CLOSED", False),
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.file_event_handler.time", return_value=100.0),
            patch("hsync_client.file_event_handler.exists", return_value=True),
            patch("hsync_client.file_event_handler.File.get") as mock_get,
            patch("hsync_client.remote_websocket.upload_file") as mock_upload,
        ):
            mock_file = MagicMock()
            mock_get.return_value = mock_file
            self.handler.on_modified(event)
            mock_upload.assert_called_once_with(mock_file)

    def test_on_modified_non_linux_debounces(self):
        """on_modified debounces rapid modifications on non-Linux."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/file.txt"

        config = {"SYNC_DIRECTION": "bidirectional"}

        # Pre-set last modified to current time to trigger debounce
        self.handler._last_modified["/bucket/file.txt"] = 100.0

        with (
            patch("hsync_client.file_event_handler.SUPPORTS_ON_CLOSED", False),
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.file_event_handler.time", return_value=100.5),
            patch("hsync_client.remote_websocket.upload_file") as mock_upload,
        ):
            self.handler.on_modified(event)
            mock_upload.assert_not_called()

    def test_on_modified_non_linux_download_only_skips(self):
        """on_modified skips upload in download-only mode on non-Linux."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/file.txt"

        config = {"SYNC_DIRECTION": "download-only"}

        with (
            patch("hsync_client.file_event_handler.SUPPORTS_ON_CLOSED", False),
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.remote_websocket.upload_file") as mock_upload,
        ):
            self.handler.on_modified(event)
            mock_upload.assert_not_called()

    # -- on_closed edge cases --

    def test_on_closed_download_only_skips(self):
        """on_closed skips upload in download-only mode."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/file.txt"

        config = {"SYNC_DIRECTION": "download-only"}

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.remote_websocket.upload_file") as mock_upload,
        ):
            self.handler.on_closed(event)
            mock_upload.assert_not_called()

    def test_on_closed_file_not_found_logs_warning(self):
        """on_closed logs warning when file was deleted before upload."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/gone.txt"

        with (
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.file_event_handler.exists", return_value=False),
            patch("hsync_client.file_event_handler.log") as mock_log,
        ):
            self.handler.on_closed(event)
            assert mock_log.warning.called

    def test_on_closed_exception_logs_error(self):
        """on_closed logs error when exception occurs."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/file.txt"

        with (
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.file_event_handler.exists", side_effect=OSError("boom")),
            patch("hsync_client.file_event_handler.log") as mock_log,
        ):
            self.handler.on_closed(event)
            assert mock_log.error.called
