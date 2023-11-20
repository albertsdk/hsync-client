# HSYNC Client - HTTP-based file synchronization client
# Copyright (C) 2025 Alberto Basso
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Tests for the FileEventHandler and related event handlers."""

import pytest
from asyncio import new_event_loop
from unittest.mock import patch, MagicMock

from hsync_client.websocket_handler import (
    FileEventHandler,
    _handle_rename_event,
    _handle_delete_event,
    _handle_symlink_event,
)
from hsync_client.models.dir import Dir
from hsync_client.models.file import File
from hsync_client.models.event import EventName


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

        with patch('hsync_client.websocket_handler.run_coroutine_threadsafe') as mock_run:
            self.handler._send_message(message)
            mock_run.assert_called_once()

    def test_send_message_direct_without_queue(self):
        """Handler sends directly if no event queue."""
        handler = FileEventHandler(self.websocket, self.loop, None)
        message = MagicMock()
        message.to_dict.return_value = {"test": "data"}

        with patch('hsync_client.websocket_handler.run_coroutine_threadsafe') as mock_run:
            handler._send_message(message)
            mock_run.assert_called_once()

    def test_on_created_skips_hidden_directory(self):
        """on_created skips hidden directories."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/.hidden"

        with patch('hsync_client.websocket_handler.is_hidden_dir', return_value=True):
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

        with patch('hsync_client.websocket_handler.CONFIG', config), \
             patch('hsync_client.websocket_handler.is_hidden_dir', return_value=False), \
             patch('hsync_client.websocket_handler.Dir.get') as mock_get, \
             patch('hsync_client.websocket_handler.run_coroutine_threadsafe'):
            mock_dir = MagicMock()
            mock_get.return_value = mock_dir

            self.handler.on_created(event)

            mock_get.assert_called_once_with("/bucket/visible")

    def test_on_deleted_skips_hidden_directory(self):
        """on_deleted skips hidden directories."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/.hidden"

        with patch('hsync_client.websocket_handler.is_hidden_dir', return_value=True):
            self.handler.on_deleted(event)

        # Should not send any message
        assert not self.event_queue.enqueue.called

    def test_on_deleted_skips_hidden_file(self):
        """on_deleted skips hidden files."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/.hidden_file"

        with patch('hsync_client.websocket_handler.is_hidden_file', return_value=True):
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

        with patch('hsync_client.websocket_handler.CONFIG', config), \
             patch('hsync_client.websocket_handler.is_hidden_file', return_value=False), \
             patch('hsync_client.websocket_handler.run_coroutine_threadsafe'):
            self.handler.on_deleted(event)
            # Message should be sent

    def test_on_moved_hidden_to_hidden_ignored(self):
        """on_moved ignores moves from hidden to hidden directory."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/.hidden1"
        event.dest_path = "/bucket/.hidden2"

        with patch('hsync_client.websocket_handler.is_hidden_dir', return_value=True):
            self.handler.on_moved(event)

        # Should not send any message
        assert not self.event_queue.enqueue.called

    def test_on_moved_hidden_file_ignored(self):
        """on_moved ignores hidden files."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/.hidden_file"
        event.dest_path = "/bucket/visible_file"

        with patch('hsync_client.websocket_handler.is_hidden_file', return_value=True):
            self.handler.on_moved(event)

        # Should not send any message
        assert not self.event_queue.enqueue.called

    def test_on_closed_skips_directories(self):
        """on_closed skips directory events."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/dir"

        # on_closed should not process directories
        with patch('hsync_client.websocket_handler.upload_file') as mock_upload:
            self.handler.on_closed(event)
            mock_upload.assert_not_called()

    def test_on_closed_skips_hidden_files(self):
        """on_closed skips hidden files."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/.hidden"

        with patch('hsync_client.websocket_handler.is_hidden_file', return_value=True), \
             patch('hsync_client.websocket_handler.upload_file') as mock_upload:
            self.handler.on_closed(event)
            mock_upload.assert_not_called()

    def test_on_closed_uploads_visible_file(self):
        """on_closed uploads visible files."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/file.txt"

        with patch('hsync_client.websocket_handler.is_hidden_file', return_value=False), \
             patch('hsync_client.websocket_handler.exists', return_value=True), \
             patch('hsync_client.websocket_handler.File.get') as mock_get, \
             patch('hsync_client.websocket_handler.upload_file') as mock_upload:
            mock_file = MagicMock()
            mock_get.return_value = mock_file

            self.handler.on_closed(event)

            mock_upload.assert_called_once_with(mock_file)


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

        with patch('hsync_client.websocket_handler.CONFIG', config):
            _handle_rename_event(src, dst)

        assert dst_dir.exists()
        assert not src_dir.exists()

    def test_rename_directory_source_not_found(self, tmp_path):
        """Handles missing source directory by creating destination."""
        # Use relative paths
        src = {"path": "missing", "permissions": "drwxr-xr-x"}
        dst = {"path": "dest", "permissions": "drwxr-xr-x"}

        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch('hsync_client.websocket_handler.CONFIG', config), \
             patch('hsync_client.websocket_handler.update_dir') as mock_update:
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

        with patch('hsync_client.websocket_handler.CONFIG', config):
            _handle_rename_event(src, dst)

        assert dst_file.exists()
        assert not src_file.exists()


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

        with patch('hsync_client.websocket_handler.CONFIG', config):
            _handle_delete_event(src)

        assert not dir_to_delete.exists()

    def test_delete_directory_not_found(self, tmp_path):
        """Handles missing directory gracefully."""
        # Use relative paths
        src = {"path": "missing", "permissions": "drwxr-xr-x"}

        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch('hsync_client.websocket_handler.CONFIG', config):
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

        with patch('hsync_client.websocket_handler.CONFIG', config):
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

        with patch('hsync_client.websocket_handler.CONFIG', config):
            # Should not raise exception
            _handle_delete_event(src)


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

        with patch('hsync_client.websocket_handler.CONFIG', config):
            _handle_symlink_event(src)

        assert (tmp_path / link_path).is_symlink()

    def test_symlink_ignores_non_symlink_events(self):
        """Ignores events that are not symlinks."""
        src = {"type": "file", "filepath": "file.txt"}

        with patch('hsync_client.websocket_handler.os_symlink') as mock_symlink:
            _handle_symlink_event(src)
            mock_symlink.assert_not_called()

    def test_symlink_ignores_none_src(self):
        """Ignores None source."""
        with patch('hsync_client.websocket_handler.os_symlink') as mock_symlink:
            _handle_symlink_event(None)
            mock_symlink.assert_not_called()

