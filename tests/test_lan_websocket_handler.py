"""
Tests for LAN file event handler (master side) functionality.
"""

from unittest.mock import MagicMock, patch

import pytest
from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirModifiedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from hsync_client.lan_websocket import LANMasterFileEventHandler, LANWebSocketServer


class TestLANFileEventHandler:
    """Tests for LANMasterFileEventHandler class."""

    @pytest.fixture
    def ws_server(self):
        """Create a mock WebSocket server."""
        server = MagicMock(spec=LANWebSocketServer)
        return server

    @pytest.fixture
    def handler(self, ws_server):
        """Create a LANMasterFileEventHandler instance."""
        return LANMasterFileEventHandler(ws_server)

    def test_handler_creation(self, ws_server):
        """Test creating a file event handler."""
        handler = LANMasterFileEventHandler(ws_server)
        assert handler._ws_server is ws_server
        assert handler._last_broadcast_stat == {}
        assert handler._last_broadcast_hash == {}

    def test_on_created_directory(self, handler, ws_server, tmp_path):
        """Test handling directory creation event."""
        test_dir = tmp_path / "newdir"
        test_dir.mkdir()

        event = DirCreatedEvent(str(test_dir))

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "remote",
            "SYNC_DIRECTION": "bidirectional",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.Dir") as mock_dir,
        ):
            mock_dir_obj = MagicMock()
            mock_dir.get.return_value = mock_dir_obj

            handler.on_created(event)

            # Should broadcast the event
            ws_server.broadcast_sync.assert_called_once()

    def test_on_created_file_symlink(self, handler, ws_server, tmp_path):
        """Test handling symlink creation event."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        event = FileCreatedEvent(str(link))

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "remote",
            "SYNC_DIRECTION": "bidirectional",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.Symlink") as mock_symlink,
        ):
            mock_symlink_obj = MagicMock()
            mock_symlink.get.return_value = mock_symlink_obj

            handler.on_created(event)

            # Should broadcast the symlink event
            ws_server.broadcast_sync.assert_called_once()

    def test_on_created_os_cache_directory(self, handler, ws_server, tmp_path):
        """Test that OS cache directories (e.g. .DS_Store) are ignored."""
        cache_dir = tmp_path / ".DS_Store"
        cache_dir.mkdir()

        event = DirCreatedEvent(str(cache_dir))

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "remote",
        }

        with patch("hsync_client.file_event_handler.CONFIG", config):
            handler.on_created(event)

            # Should not broadcast
            ws_server.broadcast_sync.assert_not_called()

    def test_on_modified_directory(self, handler, ws_server, tmp_path):
        """Test handling directory modification event."""
        test_dir = tmp_path / "moddir"
        test_dir.mkdir()

        event = DirCreatedEvent(str(test_dir))
        event.event_type = "modified"

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "remote",
            "SYNC_DIRECTION": "bidirectional",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.Dir") as mock_dir,
        ):
            mock_dir_obj = MagicMock()
            mock_dir.get.return_value = mock_dir_obj

            handler.on_modified(event)

            # Should broadcast the event
            ws_server.broadcast_sync.assert_called_once()

    def test_on_closed_file(self, handler, ws_server, tmp_path):
        """Test handling file close event."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        event = FileModifiedEvent(str(test_file))

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "remote",
            "SYNC_DIRECTION": "bidirectional",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.file_event_handler.File") as mock_file,
        ):
            mock_file_obj = MagicMock()
            mock_file_obj.filepath = str(test_file)
            mock_file.get.return_value = mock_file_obj

            handler.on_closed(event)

            # Should broadcast the event
            ws_server.broadcast_sync.assert_called_once()

    def test_on_file_ready_logs_lan_modify_with_trigger(self, handler, ws_server, tmp_path):
        """_on_file_ready logs [LAN MODIFY] with the (closed)/(modified) trigger.

        Regression for the [LAN FILE] -> [LAN MODIFY] rename: the label must
        describe the broadcast ``modified`` event and the trigger suffix must
        distinguish an ``IN_CLOSE_WRITE`` fire (``closed``) from the
        ``on_modified`` debounce fallback (``modified``).
        """
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "remote",
            "SYNC_DIRECTION": "bidirectional",
        }

        file_obj = MagicMock()
        file_obj.filepath = str(test_file)

        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.lan_websocket.log") as mock_log,
        ):
            handler._on_file_ready(file_obj, str(test_file), from_closed=True)
            handler._on_file_ready(file_obj, str(test_file), from_closed=False)

            msgs = [str(c.args[0]) if c.args else str(c) for c in mock_log.info.call_args_list]
            assert any("[LAN MODIFY]" in m and "(closed)" in m for m in msgs), msgs
            assert any("[LAN MODIFY]" in m and "(modified)" in m for m in msgs), msgs

    def test_on_deleted_file(self, handler, ws_server, tmp_path):
        """Test handling file deletion event."""
        test_file = tmp_path / "deleted.txt"

        event = FileDeletedEvent(str(test_file))

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "remote",
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }

        with patch("hsync_client.file_event_handler.CONFIG", config):
            handler.on_deleted(event)

            # Should broadcast the event
            ws_server.broadcast_sync.assert_called_once()

    def test_on_deleted_ignore_delete_mode(self, handler, ws_server, tmp_path):
        """Test that deletions are ignored in ignore-delete mode."""
        test_file = tmp_path / "deleted.txt"

        event = FileDeletedEvent(str(test_file))

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "remote",
            "IGNORE_DELETE": True,
        }

        with patch("hsync_client.file_event_handler.CONFIG", config):
            handler.on_deleted(event)

            # Should not broadcast
            ws_server.broadcast_sync.assert_not_called()

    def test_send_event_download_only_mode(self, handler, ws_server):
        """Test that events are not sent in download-only mode."""
        from hsync_common.models.event import Event, EventName
        from hsync_common.models.file import File

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": "/test",
            "REMOTE_BUCKET": "remote",
            "SYNC_DIRECTION": "download-only",
        }

        file_obj = File("/test/file.txt")
        event = Event("test-client", "/test", "remote", EventName.added, file_obj)

        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler._broadcast_event(event)

            # Should not broadcast in download-only mode
            ws_server.broadcast_sync.assert_not_called()

    def test_send_event_normal_broadcasts(self, handler, ws_server):
        """Test that events are broadcast in normal mode."""
        from hsync_common.models.event import Event, EventName
        from hsync_common.models.file import File

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": "/test",
            "REMOTE_BUCKET": "remote",
            "SYNC_DIRECTION": "bidirectional",
        }

        file_obj = File("/test/file.txt")
        event = Event("test-client", "/test", "remote", EventName.added, file_obj)

        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler._broadcast_event(event)
            ws_server.broadcast_sync.assert_called_once()

    def test_on_any_event_os_cache_dir_skipped(self, handler, tmp_path):
        """Test on_any_event returns early for OS cache directory."""
        cache = tmp_path / ".DS_Store"
        cache.mkdir()
        event = DirCreatedEvent(str(cache))
        # Should not raise
        handler.on_any_event(event)

    def test_on_any_event_os_cache_file_skipped(self, handler, tmp_path):
        """Test on_any_event returns early for OS cache file."""
        cache = tmp_path / "Thumbs.db"
        cache.write_text("x")
        event = FileCreatedEvent(str(cache))
        handler.on_any_event(event)

    def test_on_any_event_visible_logs(self, handler, tmp_path):
        """Test on_any_event logs visible event."""
        visible = tmp_path / "file.txt"
        visible.write_text("x")
        event = FileCreatedEvent(str(visible))
        handler.on_any_event(event)  # just exercises the log line

    def test_on_created_dir_get_returns_none(self, handler, ws_server, tmp_path):
        """Test on_created when Dir.get returns None."""
        test_dir = tmp_path / "gone"
        test_dir.mkdir()
        event = DirCreatedEvent(str(test_dir))
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.Dir") as D,
        ):
            D.get.return_value = None
            handler.on_created(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_created_symlink_get_returns_none(self, handler, ws_server, tmp_path):
        """Test on_created when Symlink.get returns None for a symlink."""
        target = tmp_path / "tgt"
        target.write_text("x")
        link = tmp_path / "lnk"
        link.symlink_to(target)
        event = FileCreatedEvent(str(link))
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.Symlink") as S,
        ):
            S.get.return_value = None
            handler.on_created(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_created_regular_file_ignored(self, handler, ws_server, tmp_path):
        """Test on_created for regular file (non-symlink) does nothing."""
        f = tmp_path / "file.txt"
        f.write_text("x")
        event = FileCreatedEvent(str(f))
        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r"}
        with patch("hsync_client.file_event_handler.CONFIG", config):
            handler.on_created(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_modified_root_directory_skipped(self, handler, ws_server, tmp_path):
        """Test on_modified skips root LOCAL_BUCKET directory."""
        event = DirModifiedEvent(str(tmp_path))
        event.is_directory = True
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with patch("hsync_client.file_event_handler.CONFIG", config):
            handler.on_modified(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_modified_dir_get_returns_none(self, handler, ws_server, tmp_path):
        """Test on_modified when Dir.get returns None."""
        sub = tmp_path / "sub"
        sub.mkdir()
        event = DirModifiedEvent(str(sub))
        event.is_directory = True
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.Dir") as D,
        ):
            D.get.return_value = None
            handler.on_modified(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_modified_file_non_linux_debounce(self, handler, ws_server, tmp_path):
        """Test on_modified file debounce on non-Linux."""
        f = tmp_path / "file.txt"
        f.write_text("x")
        event = FileModifiedEvent(str(f))

        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.file_event_handler.SUPPORTS_ON_CLOSED", False),
            patch("hsync_client.file_event_handler.File") as F,
        ):
            F.get.return_value = MagicMock()
            handler.on_modified(event)
            ws_server.broadcast_sync.assert_called_once()

    def test_on_modified_file_non_linux_unchanged_stat_skips(self, handler, ws_server, tmp_path):
        """on_modified file skipped when stat (mtime, size) is unchanged.

        Replaces the old time-based debounce test: the two-stage stat+hash
        dedup suppresses the broadcast when the file's (mtime, size) matches
        the last propagated state.
        """
        f = tmp_path / "file.txt"
        f.write_text("x")

        event = FileModifiedEvent(str(f))

        # Pre-set the stat to the current file's (mtime, size) so stage-1
        # catches it as unchanged.
        st = f.stat()
        handler._last_broadcast_stat[str(f)] = (st.st_mtime_ns, st.st_size)

        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r"}
        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.SUPPORTS_ON_CLOSED", False),
        ):
            handler.on_modified(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_closed_directory_skipped(self, handler, ws_server, tmp_path):
        """Test on_closed skips directory events."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = str(tmp_path / "dir")
        handler.on_closed(event)
        ws_server.broadcast_sync.assert_not_called()

    def test_on_closed_os_cache_file_skipped(self, handler, ws_server, tmp_path):
        """Test on_closed skips OS cache files."""
        event = FileModifiedEvent(str(tmp_path / "Thumbs.db"))
        handler.on_closed(event)
        ws_server.broadcast_sync.assert_not_called()

    def test_handle_file_modified_not_exists(self, handler, ws_server, tmp_path):
        """Test _handle_file_modified returns early when file not exists."""
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with patch("hsync_client.file_event_handler.CONFIG", config):
            handler._process_file_ready(str(tmp_path / "gone.txt"))
            ws_server.broadcast_sync.assert_not_called()

    def test_handle_file_modified_file_get_none(self, handler, ws_server, tmp_path):
        """Test _handle_file_modified when File.get returns None."""
        f = tmp_path / "file.txt"
        f.write_text("x")
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.File") as F,
        ):
            F.get.return_value = None
            handler._process_file_ready(str(f))
            ws_server.broadcast_sync.assert_not_called()

    def test_on_moved_no_dest_path(self, handler, ws_server, tmp_path):
        """Test on_moved returns when dest_path is None."""
        event = MagicMock()
        event.src_path = str(tmp_path / "a.txt")
        event.dest_path = None
        event.is_directory = False
        handler.on_moved(event)
        ws_server.broadcast_sync.assert_not_called()

    def test_on_moved_os_cache_to_os_cache_dir(self, handler, ws_server, tmp_path):
        """Test on_moved returns for OS-cache-to-OS-cache directory."""
        event = DirMovedEvent(str(tmp_path / ".DS_Store"), str(tmp_path / "Thumbs.db"))
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }
        with patch("hsync_client.file_event_handler.CONFIG", config):
            handler.on_moved(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_moved_os_cache_to_visible_dir(self, handler, ws_server, tmp_path):
        """Test on_moved OS-cache->visible dir triggers on_created."""
        visible = tmp_path / "visible"
        visible.mkdir()
        event = DirMovedEvent(str(tmp_path / ".DS_Store"), str(visible))
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.Dir") as D,
        ):
            D.get.return_value = MagicMock()
            handler.on_moved(event)
            ws_server.broadcast_sync.assert_called_once()

    def test_on_moved_visible_to_os_cache_dir(self, handler, ws_server, tmp_path):
        """Test on_moved visible->OS-cache dir triggers on_deleted."""
        event = DirMovedEvent(str(tmp_path / "visible"), str(tmp_path / ".DS_Store"))
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }
        with patch("hsync_client.file_event_handler.CONFIG", config):
            handler.on_moved(event)
            ws_server.broadcast_sync.assert_called_once()

    def test_on_moved_dir_rename_dir_get_none(self, handler, ws_server, tmp_path):
        """Test on_moved dir rename when Dir.get returns None."""
        event = DirMovedEvent(str(tmp_path / "old"), str(tmp_path / "new"))
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.Dir") as D,
        ):
            D.get.return_value = None
            handler.on_moved(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_moved_dir_rename_success(self, handler, ws_server, tmp_path):
        """Test on_moved dir rename broadcasts event."""
        event = DirMovedEvent(str(tmp_path / "old"), str(tmp_path / "new"))
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.Dir") as D,
        ):
            mock_dir = MagicMock()
            D.get.return_value = mock_dir
            handler.on_moved(event)
            ws_server.broadcast_sync.assert_called_once()

    def test_on_moved_file_os_cache_src(self, handler, ws_server, tmp_path):
        """Test on_moved returns when file source is OS cache."""
        event = FileMovedEvent(str(tmp_path / "Thumbs.db"), str(tmp_path / "visible"))
        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r"}
        with patch("hsync_client.file_event_handler.CONFIG", config):
            handler.on_moved(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_moved_file_rename_success(self, handler, ws_server, tmp_path):
        """Test on_moved file rename broadcasts event."""
        event = FileMovedEvent(str(tmp_path / "old.txt"), str(tmp_path / "new.txt"))
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.File") as F,
        ):
            mock_file = MagicMock()
            F.get.return_value = mock_file
            handler.on_moved(event)
            ws_server.broadcast_sync.assert_called_once()

    def test_on_moved_file_rename_file_get_none(self, handler, ws_server, tmp_path):
        """Test on_moved file rename when File.get returns None."""
        event = FileMovedEvent(str(tmp_path / "old.txt"), str(tmp_path / "new.txt"))
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.File") as F,
        ):
            F.get.return_value = None
            handler.on_moved(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_deleted_os_cache_directory_skipped(self, handler, ws_server, tmp_path):
        """Test on_deleted skips OS cache directories."""
        event = DirDeletedEvent(str(tmp_path / ".DS_Store"))
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with patch("hsync_client.file_event_handler.CONFIG", config):
            handler.on_deleted(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_deleted_os_cache_file_skipped(self, handler, ws_server, tmp_path):
        """Test on_deleted skips OS cache files."""
        event = FileDeletedEvent(str(tmp_path / "Thumbs.db"))
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with patch("hsync_client.file_event_handler.CONFIG", config):
            handler.on_deleted(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_deleted_visible_directory(self, handler, ws_server, tmp_path):
        """Test on_deleted broadcasts visible directory deletion."""
        event = DirDeletedEvent(str(tmp_path / "mydir"))
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }
        with patch("hsync_client.file_event_handler.CONFIG", config):
            handler.on_deleted(event)
            ws_server.broadcast_sync.assert_called_once()
