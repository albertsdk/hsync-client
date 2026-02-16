# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for the FileEventHandler and related event handlers."""

from asyncio import new_event_loop
from unittest.mock import MagicMock, patch

from hsync_client.websocket_handler import (
    FileEventHandler,
    _handle_delete_event,
    _handle_rename_event,
    _handle_symlink_event,
    _safe_json,
)


class TestSafeJson:
    """Tests for _safe_json helper function."""

    def test_escapes_curly_braces(self):
        """Curly braces are doubled for loguru safety."""
        result = _safe_json({"key": "value"})
        assert "{{" in result
        assert "}}" in result

    def test_plain_string(self):
        """Plain string without braces is serialized normally."""
        result = _safe_json("hello")
        assert result == '"hello"'

    def test_nested_object(self):
        """Nested objects have all braces escaped."""
        result = _safe_json({"a": {"b": 1}})
        # No unescaped single braces
        assert "{" not in result.replace("{{", "")
        assert "}" not in result.replace("}}", "")


class TestFileEventHandler:
    """Tests for FileEventHandler class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.websocket = MagicMock()
        self.loop = new_event_loop()
        self.event_queue = MagicMock()
        self.handler = FileEventHandler(self.websocket, self.loop, self.event_queue)

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

        with patch("hsync_client.websocket_handler.run_coroutine_threadsafe") as mock_run:
            self.handler._send_message(message)
            mock_run.assert_called_once()

    def test_send_message_direct_without_queue(self):
        """Handler sends directly if no event queue."""
        handler = FileEventHandler(self.websocket, self.loop, None)
        message = MagicMock()
        message.to_dict.return_value = {"test": "data"}

        with patch("hsync_client.websocket_handler.run_coroutine_threadsafe") as mock_run:
            handler._send_message(message)
            mock_run.assert_called_once()

    def test_send_message_skips_download_only(self):
        """_send_message does nothing in download-only mode."""
        message = MagicMock()
        config = {"SYNC_DIRECTION": "download-only"}

        with (
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler._send_message(message)
            mock_run.assert_not_called()

    # -- on_any_event --

    def test_on_any_event_logs_hidden_dir(self):
        """on_any_event logs debug for hidden directories."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/.hidden"
        event.dest_path = None

        with (
            patch("hsync_client.websocket_handler.is_hidden_dir", return_value=True),
            patch("hsync_client.websocket_handler.log") as mock_log,
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
            patch("hsync_client.websocket_handler.is_hidden_file", return_value=True),
            patch("hsync_client.websocket_handler.log") as mock_log,
        ):
            self.handler.on_any_event(event)
            assert mock_log.debug.called

    # -- on_created --

    def test_on_created_skips_hidden_directory(self):
        """on_created skips hidden directories."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/.hidden"

        with patch("hsync_client.websocket_handler.is_hidden_dir", return_value=True):
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
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.websocket_handler.Dir.get") as mock_get,
            patch("hsync_client.websocket_handler.run_coroutine_threadsafe"),
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

        with patch("hsync_client.websocket_handler.is_hidden_dir", return_value=True):
            self.handler.on_deleted(event)

        # Should not send any message
        assert not self.event_queue.enqueue.called

    def test_on_deleted_skips_hidden_file(self):
        """on_deleted skips hidden files."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/.hidden_file"

        with patch("hsync_client.websocket_handler.is_hidden_file", return_value=True):
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
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.is_hidden_file", return_value=False),
            patch("hsync_client.websocket_handler.run_coroutine_threadsafe"),
        ):
            self.handler.on_deleted(event)
            # Message should be sent

    def test_on_moved_hidden_to_hidden_ignored(self):
        """on_moved ignores moves from hidden to hidden directory."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/.hidden1"
        event.dest_path = "/bucket/.hidden2"

        with patch("hsync_client.websocket_handler.is_hidden_dir", return_value=True):
            self.handler.on_moved(event)

        # Should not send any message
        assert not self.event_queue.enqueue.called

    def test_on_moved_hidden_file_ignored(self):
        """on_moved ignores hidden files."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/.hidden_file"
        event.dest_path = "/bucket/visible_file"

        with patch("hsync_client.websocket_handler.is_hidden_file", return_value=True):
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
            patch("hsync_client.websocket_handler.is_hidden_dir", side_effect=side_effect),
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
            patch("hsync_client.websocket_handler.is_hidden_dir", side_effect=side_effect),
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
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.websocket_handler.Dir.get", return_value=mock_dir),
            patch("hsync_client.websocket_handler.run_coroutine_threadsafe") as mock_run,
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
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.websocket_handler.Dir.get", return_value=None),
            patch("hsync_client.websocket_handler.run_coroutine_threadsafe") as mock_run,
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
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.is_hidden_file", return_value=False),
            patch("hsync_client.websocket_handler.File.get", return_value=mock_file),
            patch("hsync_client.websocket_handler.run_coroutine_threadsafe") as mock_run,
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
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.is_hidden_file", return_value=False),
            patch("hsync_client.websocket_handler.File.get", return_value=None),
            patch("hsync_client.websocket_handler.run_coroutine_threadsafe") as mock_run,
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
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.is_hidden_file", return_value=False),
            patch("hsync_client.websocket_handler.run_coroutine_threadsafe") as mock_run,
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
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.websocket_handler.run_coroutine_threadsafe") as mock_run,
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
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.websocket_handler.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler.on_deleted(event)
            mock_run.assert_called_once()

    def test_on_closed_skips_directories(self):
        """on_closed skips directory events."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/dir"

        # on_closed should not process directories
        with patch("hsync_client.websocket_handler.upload_file") as mock_upload:
            self.handler.on_closed(event)
            mock_upload.assert_not_called()

    def test_on_closed_skips_hidden_files(self):
        """on_closed skips hidden files."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/.hidden"

        with (
            patch("hsync_client.websocket_handler.is_hidden_file", return_value=True),
            patch("hsync_client.websocket_handler.upload_file") as mock_upload,
        ):
            self.handler.on_closed(event)
            mock_upload.assert_not_called()

    def test_on_closed_uploads_visible_file(self):
        """on_closed uploads visible files."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/file.txt"

        with (
            patch("hsync_client.websocket_handler.is_hidden_file", return_value=False),
            patch("hsync_client.websocket_handler.exists", return_value=True),
            patch("hsync_client.websocket_handler.File.get") as mock_get,
            patch("hsync_client.websocket_handler.upload_file") as mock_upload,
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
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.websocket_handler.is_hidden_file", return_value=False),
            patch("hsync_client.websocket_handler.islink", return_value=True),
            patch("hsync_client.websocket_handler.Symlink.get") as mock_get,
            patch("hsync_client.websocket_handler.run_coroutine_threadsafe"),
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
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.websocket_handler.is_hidden_file", return_value=False),
            patch("hsync_client.websocket_handler.islink", return_value=True),
            patch("hsync_client.websocket_handler.Symlink.get", return_value=None),
            patch("hsync_client.websocket_handler.run_coroutine_threadsafe") as mock_run,
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
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.websocket_handler.is_same_paths", return_value=False),
            patch("hsync_client.websocket_handler.Dir.get") as mock_get,
            patch("hsync_client.websocket_handler.run_coroutine_threadsafe") as mock_run,
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
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.websocket_handler.is_same_paths", return_value=True),
            patch("hsync_client.websocket_handler.run_coroutine_threadsafe") as mock_run,
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
            patch("hsync_client.websocket_handler.SUPPORTS_ON_CLOSED", False),
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.is_hidden_file", return_value=False),
            patch("hsync_client.websocket_handler.time", return_value=100.0),
            patch("hsync_client.websocket_handler.exists", return_value=True),
            patch("hsync_client.websocket_handler.File.get") as mock_get,
            patch("hsync_client.websocket_handler.upload_file") as mock_upload,
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
            patch("hsync_client.websocket_handler.SUPPORTS_ON_CLOSED", False),
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.is_hidden_file", return_value=False),
            patch("hsync_client.websocket_handler.time", return_value=100.5),
            patch("hsync_client.websocket_handler.upload_file") as mock_upload,
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
            patch("hsync_client.websocket_handler.SUPPORTS_ON_CLOSED", False),
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.is_hidden_file", return_value=False),
            patch("hsync_client.websocket_handler.upload_file") as mock_upload,
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
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.is_hidden_file", return_value=False),
            patch("hsync_client.websocket_handler.upload_file") as mock_upload,
        ):
            self.handler.on_closed(event)
            mock_upload.assert_not_called()

    def test_on_closed_file_not_found_logs_warning(self):
        """on_closed logs warning when file was deleted before upload."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/gone.txt"

        with (
            patch("hsync_client.websocket_handler.is_hidden_file", return_value=False),
            patch("hsync_client.websocket_handler.exists", return_value=False),
            patch("hsync_client.websocket_handler.log") as mock_log,
        ):
            self.handler.on_closed(event)
            assert mock_log.warning.called

    def test_on_closed_exception_logs_error(self):
        """on_closed logs error when exception occurs."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/file.txt"

        with (
            patch("hsync_client.websocket_handler.is_hidden_file", return_value=False),
            patch("hsync_client.websocket_handler.exists", side_effect=OSError("boom")),
            patch("hsync_client.websocket_handler.log") as mock_log,
        ):
            self.handler.on_closed(event)
            assert mock_log.error.called


class TestHandleRenameEvent:
    """Tests for _handle_rename_event function."""

    def test_rename_directory_success(self, tmp_path):
        """Successfully renames directory when source exists."""
        src_dir = tmp_path / "source"
        src_dir.mkdir()
        dst_dir = tmp_path / "dest"

        # Use relative paths - functions add LOCAL_BUCKET prefix
        src = {"path": "source", "permissions": "drwxr-xr-x"}
        dst = {"path": "dest", "permissions": "drwxr-xr-x"}

        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.websocket_handler.CONFIG", config):
            _handle_rename_event(src, dst)

        assert dst_dir.exists()
        assert not src_dir.exists()

    def test_rename_directory_source_not_found(self, tmp_path):
        """Handles missing source directory by creating destination."""
        # Use relative paths
        src = {"path": "missing", "permissions": "drwxr-xr-x"}
        dst = {"path": "dest", "permissions": "drwxr-xr-x"}

        config = {"LOCAL_BUCKET": str(tmp_path)}

        with (
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.update_dir") as mock_update,
        ):
            _handle_rename_event(src, dst)
            mock_update.assert_called_once_with(dst)

    def test_rename_file_success(self, tmp_path):
        """Successfully renames file when source exists."""
        src_file = tmp_path / "source.txt"
        src_file.write_text("content")
        dst_file = tmp_path / "dest.txt"

        # Use relative paths
        src = {
            "filepath": "source.txt",
            "md5sum": "abc123",
            "mtime": 1000,
            "ctime": 1000,
            "size": 7,
            "permissions": "-rw-r--r--",
        }
        dst = {
            "filepath": "dest.txt",
            "md5sum": "abc123",
            "mtime": 1000,
            "ctime": 1000,
            "size": 7,
            "permissions": "-rw-r--r--",
        }

        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.websocket_handler.CONFIG", config):
            _handle_rename_event(src, dst)

        assert dst_file.exists()
        assert not src_file.exists()

    def test_rename_file_source_not_found(self, tmp_path):
        """Downloads destination file when source doesn't exist."""
        src = {
            "filepath": "missing.txt",
            "md5sum": "abc123",
            "mtime": 1000,
            "ctime": 1000,
            "size": 7,
            "permissions": "-rw-r--r--",
        }
        dst = {
            "filepath": "dest.txt",
            "md5sum": "abc123",
            "mtime": 1000,
            "ctime": 1000,
            "size": 7,
            "permissions": "-rw-r--r--",
        }

        config = {"LOCAL_BUCKET": str(tmp_path)}

        with (
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.download_file") as mock_dl,
        ):
            _handle_rename_event(src, dst)
            mock_dl.assert_called_once()


class TestHandleDeleteEvent:
    """Tests for _handle_delete_event function."""

    def test_delete_directory_success(self, tmp_path):
        """Successfully deletes existing directory."""
        dir_to_delete = tmp_path / "to_delete"
        dir_to_delete.mkdir()
        (dir_to_delete / "file.txt").write_text("content")

        # Use relative paths - functions add LOCAL_BUCKET prefix
        src = {"path": "to_delete", "permissions": "drwxr-xr-x"}

        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.websocket_handler.CONFIG", config):
            _handle_delete_event(src)

        assert not dir_to_delete.exists()

    def test_delete_directory_not_found(self, tmp_path):
        """Handles missing directory gracefully."""
        # Use relative paths
        src = {"path": "missing", "permissions": "drwxr-xr-x"}

        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.websocket_handler.CONFIG", config):
            # Should not raise exception
            _handle_delete_event(src)

    def test_delete_file_success(self, tmp_path):
        """Successfully deletes existing file."""
        file_to_delete = tmp_path / "to_delete.txt"
        file_to_delete.write_text("content")

        # Use relative paths
        src = {
            "filepath": "to_delete.txt",
            "md5sum": "abc123",
            "mtime": 1000,
            "ctime": 1000,
            "size": 7,
            "permissions": "-rw-r--r--",
        }

        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.websocket_handler.CONFIG", config):
            _handle_delete_event(src)

        assert not file_to_delete.exists()

    def test_delete_file_not_found(self, tmp_path):
        """Handles missing file gracefully."""
        # Use relative paths
        src = {
            "filepath": "missing.txt",
            "md5sum": "abc123",
            "mtime": 1000,
            "ctime": 1000,
            "size": 7,
            "permissions": "-rw-r--r--",
        }

        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.websocket_handler.CONFIG", config):
            # Should not raise exception
            _handle_delete_event(src)

    def test_delete_ignore_delete_mode(self, tmp_path):
        """Skips deletion when IGNORE_DELETE is True."""
        file_to_keep = tmp_path / "keep_me.txt"
        file_to_keep.write_text("content")

        src = {
            "filepath": "keep_me.txt",
            "md5sum": "abc123",
            "mtime": 1000,
            "ctime": 1000,
            "size": 7,
            "permissions": "-rw-r--r--",
        }

        config = {"LOCAL_BUCKET": str(tmp_path), "IGNORE_DELETE": True}

        with patch("hsync_client.websocket_handler.CONFIG", config):
            _handle_delete_event(src)

        # File should still exist
        assert file_to_keep.exists()

    def test_delete_hsynckeep_cleans_empty_parent(self, tmp_path):
        """Deleting .hsynckeep removes empty parent directory."""
        subdir = tmp_path / "emptydir"
        subdir.mkdir()
        sentinel = subdir / ".hsynckeep"
        sentinel.write_text("")

        src = {
            "filepath": "emptydir/.hsynckeep",
            "md5sum": "d41d8cd98f00b204e9800998ecf8427e",
            "mtime": 1000,
            "ctime": 1000,
            "size": 0,
            "permissions": "-rw-r--r--",
        }

        config = {"LOCAL_BUCKET": str(tmp_path), "IGNORE_DELETE": False}

        with patch("hsync_client.websocket_handler.CONFIG", config):
            _handle_delete_event(src)

        assert not sentinel.exists()
        assert not subdir.exists()


class TestHandleSymlinkEvent:
    """Tests for _handle_symlink_event function."""

    def test_symlink_creation_success(self, tmp_path):
        """Successfully creates symlink."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link_path = "link.txt"

        src = {
            "type": "symlink",
            "filepath": link_path,
            "target": str(target),
        }

        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.websocket_handler.CONFIG", config):
            _handle_symlink_event(src)

        assert (tmp_path / link_path).is_symlink()

    def test_symlink_ignores_non_symlink_events(self):
        """Ignores events that are not symlinks."""
        src = {"type": "file", "filepath": "file.txt"}

        with patch("hsync_client.websocket_handler.os_symlink") as mock_symlink:
            _handle_symlink_event(src)
            mock_symlink.assert_not_called()

    def test_symlink_ignores_none_src(self):
        """Ignores None source."""
        with patch("hsync_client.websocket_handler.os_symlink") as mock_symlink:
            _handle_symlink_event(None)
            mock_symlink.assert_not_called()
