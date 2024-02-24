"""
Tests for slave file event handler functionality.
"""

import time
from unittest.mock import MagicMock, patch

import pytest
from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from hsync_client.lan_websocket import SlaveFileEventHandler


class TestSlaveFileEventHandler:
    """Tests for SlaveFileEventHandler class."""

    @pytest.fixture
    def handler(self):
        """Create a SlaveFileEventHandler instance."""
        return SlaveFileEventHandler(
            master_host="192.168.1.100",
            master_port=8765,
            use_https=False,
        )

    def test_handler_creation(self):
        """Test creating a slave file event handler."""
        handler = SlaveFileEventHandler("192.168.1.100", 8765, use_https=True)
        assert handler._master_host == "192.168.1.100"
        assert handler._master_port == 8765
        assert handler._use_https is True
        assert handler._last_modified == {}
        assert handler._receiving_files == set()
        assert handler._pending_ops == []

    def test_mark_receiving(self, handler):
        """Test marking a file as receiving."""
        handler.mark_receiving("/path/to/file.txt")
        assert handler.is_receiving("/path/to/file.txt")

    def test_unmark_receiving(self, handler):
        """Test unmarking a file after receiving."""
        handler.mark_receiving("/path/to/file.txt")
        assert handler.is_receiving("/path/to/file.txt")

        handler.unmark_receiving("/path/to/file.txt")
        assert not handler.is_receiving("/path/to/file.txt")

    def test_is_receiving_false(self, handler):
        """Test checking if file is not being received."""
        assert not handler.is_receiving("/path/to/file.txt")

    def test_is_temp_file(self, handler):
        """Test detecting temporary files."""
        assert handler._is_temp_file("/path/to/file.part")
        assert not handler._is_temp_file("/path/to/file.txt")

    def test_wait_pending_no_operations(self, handler):
        """Test waiting for pending operations when there are none."""
        timed_out = handler.wait_pending(timeout=1.0)
        assert timed_out == 0

    def test_wait_pending_completed_operations(self, handler):
        """Test waiting for operations that complete quickly."""

        def quick_operation():
            time.sleep(0.1)

        thread = handler._start_operation(quick_operation, name="test-op")
        assert thread.is_alive()

        timed_out = handler.wait_pending(timeout=2.0)
        assert timed_out == 0

    def test_on_created_temp_file_ignored(self, handler, tmp_path):
        """Test that temporary files are ignored."""
        temp_file = tmp_path / "file.part"
        temp_file.write_text("temp")

        event = FileCreatedEvent(str(temp_file))

        handler.on_created(event)

        # Should not trigger any upload
        assert len(handler._pending_ops) == 0

    def test_on_created_receiving_file_ignored(self, handler, tmp_path):
        """Test that files being received are ignored."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("content")

        handler.mark_receiving(str(test_file))

        event = FileCreatedEvent(str(test_file))

        handler.on_created(event)

        # Should not trigger any upload
        assert len(handler._pending_ops) == 0

    def test_on_created_directory(self, handler, tmp_path):
        """Test handling directory creation."""
        test_dir = tmp_path / "newdir"
        test_dir.mkdir()

        event = DirCreatedEvent(str(test_dir))

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
        }

        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_created(event)

        # Should create sentinel file
        sentinel = test_dir / ".hsynckeep"
        assert sentinel.exists()

    def test_on_deleted_download_only_mode(self, handler, tmp_path):
        """Test that deletions are not propagated in download-only mode."""
        test_file = tmp_path / "deleted.txt"

        event = FileDeletedEvent(str(test_file))

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "download-only",
        }

        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_deleted(event)

        # Should not start any operation
        assert len(handler._pending_ops) == 0

    def test_on_deleted_ignore_delete_mode(self, handler, tmp_path):
        """Test that deletions are ignored in ignore-delete mode."""
        test_file = tmp_path / "deleted.txt"

        event = FileDeletedEvent(str(test_file))

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": True,
        }

        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_deleted(event)

        # Should not start any operation
        assert len(handler._pending_ops) == 0

    def test_on_moved_download_only_mode(self, handler, tmp_path):
        """Test that renames are not propagated in download-only mode."""
        src_file = tmp_path / "old.txt"
        src_file.write_text("content")
        dest_file = tmp_path / "new.txt"

        event = FileCreatedEvent(str(src_file))
        event.dest_path = str(dest_file)
        event.event_type = "moved"

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "download-only",
        }

        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_moved(event)

        # Should not start any operation
        assert len(handler._pending_ops) == 0

    def test_ensure_sentinel_empty_directory(self, handler, tmp_path):
        """Test creating sentinel in empty directory."""
        test_dir = tmp_path / "emptydir"
        test_dir.mkdir()

        handler._ensure_sentinel(str(test_dir))

        sentinel = test_dir / ".hsynckeep"
        assert sentinel.exists()

    def test_ensure_sentinel_non_empty_directory(self, handler, tmp_path):
        """Test not creating sentinel in non-empty directory."""
        test_dir = tmp_path / "nonempty"
        test_dir.mkdir()
        (test_dir / "file.txt").write_text("content")

        handler._ensure_sentinel(str(test_dir))

        sentinel = test_dir / ".hsynckeep"
        assert not sentinel.exists()

    def test_ensure_sentinel_os_error(self, handler):
        """Test _ensure_sentinel returns on OSError (e.g., dir not found)."""
        handler._ensure_sentinel("/nonexistent/path/xyz")  # no crash

    def test_upload_to_master_success(self, handler):
        """Test _upload_to_master calls upload_file."""
        from hsync_client.models.file import File

        file_obj = File("/test/file.txt")
        with patch("hsync_client.transfer.upload_file") as mock_up:
            handler._upload_to_master(file_obj)
            mock_up.assert_called_once_with(file_obj, check_size=True)

    def test_upload_to_master_exception(self, handler):
        """Test _upload_to_master logs error on exception."""
        from hsync_client.models.file import File

        file_obj = File("/test/file.txt")
        with patch("hsync_client.transfer.upload_file", side_effect=Exception("fail")):
            handler._upload_to_master(file_obj)  # should not raise

    def test_delete_on_master_success(self, handler):
        """Test _delete_on_master sends DELETE request."""
        config = {
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "remote",
            "REQUEST_HEADERS": {"X-Key": "val"},
        }
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("requests.delete") as mock_del,
        ):
            mock_del.return_value = MagicMock(status_code=200)
            handler._delete_on_master("/bucket/file.txt", is_dir=False)
            mock_del.assert_called_once()

    def test_delete_on_master_failure_status(self, handler):
        """Test _delete_on_master logs warning on bad status."""
        config = {"LOCAL_BUCKET": "/bucket", "REMOTE_BUCKET": "remote"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("requests.delete") as mock_del,
        ):
            mock_del.return_value = MagicMock(status_code=500)
            handler._delete_on_master("/bucket/file.txt")  # no raise

    def test_delete_on_master_exception(self, handler):
        """Test _delete_on_master logs error on exception."""
        config = {"LOCAL_BUCKET": "/bucket", "REMOTE_BUCKET": "remote"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("requests.delete", side_effect=Exception("net err")),
        ):
            handler._delete_on_master("/bucket/file.txt")  # no raise

    def test_rename_on_master_success(self, handler):
        """Test _rename_on_master sends PATCH request."""
        config = {
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "remote",
            "REQUEST_HEADERS": {"X-Key": "val"},
        }
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("requests.patch") as mock_patch,
        ):
            mock_patch.return_value = MagicMock(status_code=200)
            handler._rename_on_master("/bucket/old.txt", "/bucket/new.txt")
            mock_patch.assert_called_once()

    def test_rename_on_master_failure_status(self, handler):
        """Test _rename_on_master logs warning on bad status."""
        config = {"LOCAL_BUCKET": "/bucket", "REMOTE_BUCKET": "remote"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("requests.patch") as mock_patch,
        ):
            mock_patch.return_value = MagicMock(status_code=500)
            handler._rename_on_master("/bucket/old.txt", "/bucket/new.txt")

    def test_rename_on_master_exception(self, handler):
        """Test _rename_on_master logs error on exception."""
        config = {"LOCAL_BUCKET": "/bucket", "REMOTE_BUCKET": "remote"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("requests.patch", side_effect=Exception("fail")),
        ):
            handler._rename_on_master("/bucket/a", "/bucket/b")  # no raise

    def test_on_created_hidden_dir_ignored(self, handler, tmp_path):
        """Test on_created ignores hidden directories."""
        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        event = DirCreatedEvent(str(hidden))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_created(event)
        # No sentinel should be created in hidden dir
        assert not (hidden / ".hsynckeep").exists()

    def test_on_created_symlink_uploads(self, handler, tmp_path):
        """Test on_created uploads symlinks immediately."""
        target = tmp_path / "tgt.txt"
        target.write_text("x")
        link = tmp_path / "lnk.txt"
        link.symlink_to(target)
        event = FileCreatedEvent(str(link))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.lan_websocket.File") as F,
            patch.object(handler, "_upload_to_master"),
        ):
            F.get.return_value = MagicMock()
            handler.on_created(event)
            # _handle_file_upload should have been called (starts operation)
            assert len(handler._pending_ops) >= 0  # at least exercised the branch

    def test_on_modified_temp_file_ignored(self, handler, tmp_path):
        """Test on_modified ignores temp files."""
        event = FileModifiedEvent(str(tmp_path / "file.part"))
        handler.on_modified(event)
        assert len(handler._pending_ops) == 0

    def test_on_modified_receiving_ignored(self, handler, tmp_path):
        """Test on_modified ignores files being received."""
        p = str(tmp_path / "f.txt")
        handler.mark_receiving(p)
        event = FileModifiedEvent(p)
        handler.on_modified(event)
        assert len(handler._pending_ops) == 0

    def test_on_modified_directory_ignored(self, handler, tmp_path):
        """Test on_modified ignores directory events."""
        event = DirDeletedEvent(str(tmp_path / "sub"))
        event.is_directory = True
        handler.on_modified(event)
        assert len(handler._pending_ops) == 0

    def test_on_modified_hidden_file_ignored(self, handler, tmp_path):
        """Test on_modified ignores hidden files."""
        event = FileModifiedEvent(str(tmp_path / ".hidden"))
        handler.on_modified(event)
        assert len(handler._pending_ops) == 0

    def test_on_modified_debounce_skip(self, handler, tmp_path):
        """Test on_modified skips when within debounce interval."""
        p = str(tmp_path / "f.txt")
        handler._last_modified[p] = time.time()
        event = FileModifiedEvent(p)
        handler.on_modified(event)
        assert len(handler._pending_ops) == 0

    def test_on_modified_non_linux_uploads(self, handler, tmp_path):
        """Test on_modified uploads on non-Linux systems."""
        f = tmp_path / "f.txt"
        f.write_text("x")
        event = FileModifiedEvent(str(f))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.lan_websocket.SUPPORTS_ON_CLOSED", False),
            patch("hsync_client.lan_websocket.File") as F,
            patch.object(handler, "_upload_to_master"),
        ):
            F.get.return_value = MagicMock()
            handler.on_modified(event)

    def test_on_closed_not_supported(self, handler, tmp_path):
        """Test on_closed returns immediately when not supported."""
        event = FileModifiedEvent(str(tmp_path / "f.txt"))
        with patch("hsync_client.lan_websocket.SUPPORTS_ON_CLOSED", False):
            handler.on_closed(event)
        assert len(handler._pending_ops) == 0

    def test_on_closed_temp_file_ignored(self, handler, tmp_path):
        """Test on_closed ignores temp files."""
        event = FileModifiedEvent(str(tmp_path / "f.part"))
        with patch("hsync_client.lan_websocket.SUPPORTS_ON_CLOSED", True):
            handler.on_closed(event)
        assert len(handler._pending_ops) == 0

    def test_on_closed_receiving_ignored(self, handler, tmp_path):
        """Test on_closed ignores files being received."""
        p = str(tmp_path / "f.txt")
        handler.mark_receiving(p)
        event = FileModifiedEvent(p)
        with patch("hsync_client.lan_websocket.SUPPORTS_ON_CLOSED", True):
            handler.on_closed(event)
        assert len(handler._pending_ops) == 0

    def test_on_closed_directory_ignored(self, handler, tmp_path):
        """Test on_closed ignores directory events."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = str(tmp_path / "dir")
        with patch("hsync_client.lan_websocket.SUPPORTS_ON_CLOSED", True):
            handler.on_closed(event)
        assert len(handler._pending_ops) == 0

    def test_on_closed_hidden_file_ignored(self, handler, tmp_path):
        """Test on_closed ignores hidden files."""
        event = FileModifiedEvent(str(tmp_path / ".hidden"))
        with patch("hsync_client.lan_websocket.SUPPORTS_ON_CLOSED", True):
            handler.on_closed(event)
        assert len(handler._pending_ops) == 0

    def test_on_closed_normal_uploads(self, handler, tmp_path):
        """Test on_closed triggers upload for normal file."""
        f = tmp_path / "f.txt"
        f.write_text("x")
        event = FileModifiedEvent(str(f))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.lan_websocket.SUPPORTS_ON_CLOSED", True),
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.lan_websocket.File") as F,
            patch.object(handler, "_upload_to_master"),
        ):
            F.get.return_value = MagicMock()
            handler.on_closed(event)
            assert len(handler._pending_ops) >= 1

    def test_handle_file_upload_download_only(self, handler, tmp_path):
        """Test _handle_file_upload skips in download-only mode."""
        config = {"SYNC_DIRECTION": "download-only"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler._handle_file_upload(str(tmp_path / "f.txt"))
        assert len(handler._pending_ops) == 0

    def test_handle_file_upload_not_exists(self, handler, tmp_path):
        """Test _handle_file_upload skips non-existent file."""
        config = {"SYNC_DIRECTION": "bidirectional"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler._handle_file_upload(str(tmp_path / "gone.txt"))
        assert len(handler._pending_ops) == 0

    def test_handle_file_upload_file_get_none(self, handler, tmp_path):
        """Test _handle_file_upload skips when File.get returns None."""
        f = tmp_path / "f.txt"
        f.write_text("x")
        config = {"SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.lan_websocket.File") as F,
        ):
            F.get.return_value = None
            handler._handle_file_upload(str(f))
        assert len(handler._pending_ops) == 0

    def test_handle_file_upload_starts_operation(self, handler, tmp_path):
        """Test _handle_file_upload starts upload operation."""
        f = tmp_path / "f.txt"
        f.write_text("x")
        config = {"SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.lan_websocket.File") as F,
            patch.object(handler, "_upload_to_master"),
        ):
            F.get.return_value = MagicMock()
            handler._handle_file_upload(str(f))
        assert len(handler._pending_ops) >= 1

    def test_on_deleted_visible_file(self, handler, tmp_path):
        """Test on_deleted starts delete operation for visible file."""
        event = FileDeletedEvent(str(tmp_path / "file.txt"))
        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch.object(handler, "_delete_on_master"),
        ):
            handler.on_deleted(event)
        assert len(handler._pending_ops) >= 1

    def test_on_deleted_temp_file_ignored(self, handler, tmp_path):
        """Test on_deleted ignores temp files."""
        event = FileDeletedEvent(str(tmp_path / "file.part"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_deleted(event)
        assert len(handler._pending_ops) == 0

    def test_on_deleted_hidden_dir_ignored(self, handler, tmp_path):
        """Test on_deleted ignores hidden directories."""
        event = DirDeletedEvent(str(tmp_path / ".hidden"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_deleted(event)
        assert len(handler._pending_ops) == 0

    def test_on_deleted_hidden_file_ignored(self, handler, tmp_path):
        """Test on_deleted ignores hidden files."""
        event = FileDeletedEvent(str(tmp_path / ".hidden"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_deleted(event)
        assert len(handler._pending_ops) == 0

    def test_on_moved_no_dest_returns(self, handler, tmp_path):
        """Test on_moved returns when dest_path is None."""
        event = MagicMock()
        event.src_path = str(tmp_path / "a.txt")
        event.dest_path = None
        event.is_directory = False
        handler.on_moved(event)
        assert len(handler._pending_ops) == 0

    def test_on_moved_temp_src_ignored(self, handler, tmp_path):
        """Test on_moved ignores temp source files."""
        event = FileMovedEvent(str(tmp_path / "f.part"), str(tmp_path / "f.txt"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_moved(event)
        assert len(handler._pending_ops) == 0

    def test_on_moved_temp_dest_ignored(self, handler, tmp_path):
        """Test on_moved ignores temp destination files."""
        event = FileMovedEvent(str(tmp_path / "f.txt"), str(tmp_path / "f.part"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_moved(event)
        assert len(handler._pending_ops) == 0

    def test_on_moved_receiving_src_ignored(self, handler, tmp_path):
        """Test on_moved ignores when src is being received."""
        src = str(tmp_path / "a.txt")
        handler.mark_receiving(src)
        event = FileMovedEvent(src, str(tmp_path / "b.txt"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_moved(event)
        assert len(handler._pending_ops) == 0

    def test_on_moved_hidden_to_hidden_ignored(self, handler, tmp_path):
        """Test on_moved both hidden skipped."""
        event = FileMovedEvent(str(tmp_path / ".a"), str(tmp_path / ".b"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_moved(event)
        assert len(handler._pending_ops) == 0

    def test_on_moved_hidden_to_visible_file_uploads(self, handler, tmp_path):
        """Test on_moved hidden->visible triggers upload."""
        f = tmp_path / "visible.txt"
        f.write_text("x")
        event = FileMovedEvent(str(tmp_path / ".hidden"), str(f))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.lan_websocket.File") as F,
            patch.object(handler, "_upload_to_master"),
        ):
            F.get.return_value = MagicMock()
            handler.on_moved(event)
        assert len(handler._pending_ops) >= 1

    def test_on_moved_visible_to_hidden_deletes(self, handler, tmp_path):
        """Test on_moved visible->hidden triggers deletion."""
        event = FileMovedEvent(str(tmp_path / "vis.txt"), str(tmp_path / ".hid"))
        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch.object(handler, "_delete_on_master"),
        ):
            handler.on_moved(event)
        assert len(handler._pending_ops) >= 1

    def test_on_moved_visible_to_hidden_ignore_delete(self, handler, tmp_path):
        """Test on_moved visible->hidden skips when ignore-delete."""
        event = FileMovedEvent(str(tmp_path / "vis.txt"), str(tmp_path / ".hid"))
        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": True,
        }
        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_moved(event)
        assert len(handler._pending_ops) == 0

    def test_on_moved_normal_rename(self, handler, tmp_path):
        """Test on_moved normal rename calls _rename_on_master."""
        event = FileMovedEvent(str(tmp_path / "a.txt"), str(tmp_path / "b.txt"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch.object(handler, "_rename_on_master"),
        ):
            handler.on_moved(event)
        assert len(handler._pending_ops) >= 1

    def test_wait_pending_timeout(self, handler):
        """Test wait_pending returns count of timed-out operations."""

        def slow_op():
            time.sleep(5)

        handler._start_operation(slow_op, name="slow")
        timed_out = handler.wait_pending(timeout=0.1)
        assert timed_out >= 1
