"""
Tests for LAN WebSocket server and client functionality.
"""

import asyncio
import json
import time
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, Mock
from watchdog.events import FileCreatedEvent, FileModifiedEvent, FileDeletedEvent, DirCreatedEvent

import pytest

from hsync_client.lan_websocket import (
    LANWebSocketServer,
    LANFileEventHandler,
    SlaveFileEventHandler,
    _escape_braces,
    _handle_lan_event,
)


class TestEscapeBraces:
    """Tests for _escape_braces helper function."""

    def test_escape_single_braces(self):
        """Test escaping single curly braces."""
        assert _escape_braces("test {value}") == "test {{value}}"

    def test_escape_multiple_braces(self):
        """Test escaping multiple curly braces."""
        assert _escape_braces("{a} and {b}") == "{{a}} and {{b}}"

    def test_no_braces(self):
        """Test string without braces."""
        assert _escape_braces("no braces here") == "no braces here"

    def test_empty_string(self):
        """Test empty string."""
        assert _escape_braces("") == ""


class TestLANWebSocketServer:
    """Tests for LANWebSocketServer class."""

    def test_server_creation(self):
        """Test creating a WebSocket server instance."""
        server = LANWebSocketServer(port=9999)
        assert server.port == 9999
        assert server.is_running() is False

    def test_server_creation_with_https(self):
        """Test creating a WebSocket server with HTTPS."""
        server = LANWebSocketServer(
            port=9999,
            use_https=True,
            cert_file="/path/to/cert.pem",
            key_file="/path/to/key.pem",
        )
        assert server.port == 9999
        assert server._use_https is True
        assert server._cert_file == "/path/to/cert.pem"
        assert server._key_file == "/path/to/key.pem"

    def test_server_port_property(self):
        """Test server port property."""
        server = LANWebSocketServer(port=8888)
        assert server.port == 8888

    def test_server_initial_state(self):
        """Test server initial state."""
        server = LANWebSocketServer(port=9999)
        assert server._clients == set()
        assert server._loop is None
        assert server._thread is None
        assert server._server is None
        assert server._running is False
        assert server._observer is None
        assert server._event_handler is None

    @pytest.mark.asyncio
    async def test_broadcast_no_clients(self):
        """Test broadcast with no connected clients."""
        server = LANWebSocketServer(port=9999)
        # Should not raise an error
        await server.broadcast({"name": "test", "data": "value"})

    @pytest.mark.asyncio
    async def test_broadcast_to_clients(self):
        """Test broadcasting to connected clients."""
        server = LANWebSocketServer(port=9999)
        
        # Create mock clients
        client1 = AsyncMock()
        client2 = AsyncMock()
        server._clients = {client1, client2}
        
        event_dict = {"name": "added", "src": {"filepath": "test.txt"}}
        await server.broadcast(event_dict)
        
        # Both clients should receive the message
        expected_message = json.dumps(event_dict)
        client1.send.assert_called_once_with(expected_message)
        client2.send.assert_called_once_with(expected_message)

    @pytest.mark.asyncio
    async def test_broadcast_removes_disconnected_clients(self):
        """Test that broadcast removes disconnected clients."""
        server = LANWebSocketServer(port=9999)
        
        # Create mock clients - one working, one failing
        client1 = AsyncMock()
        client2 = AsyncMock()
        client2.send.side_effect = Exception("Connection lost")
        
        server._clients = {client1, client2}
        
        event_dict = {"name": "test"}
        await server.broadcast(event_dict)
        
        # client2 should be removed from the set
        assert client1 in server._clients
        assert client2 not in server._clients

    def test_broadcast_sync(self):
        """Test synchronous broadcast wrapper."""
        server = LANWebSocketServer(port=9999)
        server._loop = asyncio.new_event_loop()
        server._running = True
        
        event_dict = {"name": "test"}
        
        with patch.object(asyncio, 'run_coroutine_threadsafe') as mock_run:
            server.broadcast_sync(event_dict)
            mock_run.assert_called_once()

    def test_broadcast_sync_no_loop(self):
        """Test synchronous broadcast when loop is not ready."""
        server = LANWebSocketServer(port=9999)
        server._loop = None
        server._running = False
        
        # Should not raise an error
        server.broadcast_sync({"name": "test"})

    def test_start_already_running(self, tmp_path):
        """Test starting server when already running."""
        server = LANWebSocketServer(port=9999)
        server._running = True
        
        with patch('hsync_client.lan_websocket.CONFIG', {"LOCAL_BUCKET": str(tmp_path)}):
            server.start()
        
        # Should not start a new thread
        assert server._thread is None

    def test_stop_not_running(self):
        """Test stopping server when not running."""
        server = LANWebSocketServer(port=9999)
        server._running = False

        # Should not raise an error
        server.stop()


class TestLANFileEventHandler:
    """Tests for LANFileEventHandler class."""

    @pytest.fixture
    def ws_server(self):
        """Create a mock WebSocket server."""
        server = MagicMock(spec=LANWebSocketServer)
        return server

    @pytest.fixture
    def handler(self, ws_server):
        """Create a LANFileEventHandler instance."""
        return LANFileEventHandler(ws_server)

    def test_handler_creation(self, ws_server):
        """Test creating a file event handler."""
        handler = LANFileEventHandler(ws_server)
        assert handler._ws_server is ws_server
        assert handler._last_modified == {}

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

        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.lan_websocket.Dir') as mock_dir:
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

        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.lan_websocket.Symlink') as mock_symlink:
            mock_symlink_obj = MagicMock()
            mock_symlink.get.return_value = mock_symlink_obj

            handler.on_created(event)

            # Should broadcast the symlink event
            ws_server.broadcast_sync.assert_called_once()

    def test_on_created_hidden_directory(self, handler, ws_server, tmp_path):
        """Test that hidden directories are ignored."""
        hidden_dir = tmp_path / ".hidden"
        hidden_dir.mkdir()

        event = DirCreatedEvent(str(hidden_dir))

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "remote",
        }

        with patch('hsync_client.lan_websocket.CONFIG', config):
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

        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.lan_websocket.Dir') as mock_dir:
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

        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.lan_websocket.File') as mock_file:
            mock_file_obj = MagicMock()
            mock_file_obj.filepath = str(test_file)
            mock_file.get.return_value = mock_file_obj

            handler.on_closed(event)

            # Should broadcast the event
            ws_server.broadcast_sync.assert_called_once()

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

        with patch('hsync_client.lan_websocket.CONFIG', config):
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

        with patch('hsync_client.lan_websocket.CONFIG', config):
            handler.on_deleted(event)

            # Should not broadcast
            ws_server.broadcast_sync.assert_not_called()

    def test_send_event_download_only_mode(self, handler, ws_server):
        """Test that events are not sent in download-only mode."""
        from hsync_client.models.event import Event, EventName
        from hsync_client.models.file import File

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": "/test",
            "REMOTE_BUCKET": "remote",
            "SYNC_DIRECTION": "download-only",
        }

        file_obj = File("/test/file.txt")
        event = Event("test-client", "/test", "remote", EventName.added, file_obj)

        with patch('hsync_client.lan_websocket.CONFIG', config):
            handler._send_event(event)

            # Should not broadcast in download-only mode
            ws_server.broadcast_sync.assert_not_called()


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

        with patch('hsync_client.lan_websocket.CONFIG', config):
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

        with patch('hsync_client.lan_websocket.CONFIG', config):
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

        with patch('hsync_client.lan_websocket.CONFIG', config):
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

        with patch('hsync_client.lan_websocket.CONFIG', config):
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


class TestHandleLanEvent:
    """Tests for _handle_lan_event function."""

    @pytest.mark.asyncio
    async def test_handle_added_file_event(self, tmp_path):
        """Test handling added file event."""
        event_data = {
            "name": "added",
            "src": {
                "filepath": "test.txt",
                "md5sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 10,
                "permissions": "-rw-r--r--",
            },
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
        }

        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.sync.update_file') as mock_update:
            await _handle_lan_event(event_data)

            mock_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_added_directory_event(self, tmp_path):
        """Test handling added directory event."""
        event_data = {
            "name": "added",
            "src": {
                "path": "newdir",
                "permissions": "drwxr-xr-x",
            },
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
        }

        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.sync.update_dir') as mock_update:
            await _handle_lan_event(event_data)

            mock_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_modified_file_event(self, tmp_path):
        """Test handling modified file event."""
        event_data = {
            "name": "modified",
            "src": {
                "filepath": "modified.txt",
                "md5sum": "def456",
                "mtime": 2000,
                "ctime": 1000,
                "size": 20,
                "permissions": "-rw-r--r--",
            },
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
        }

        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.sync.update_file') as mock_update:
            await _handle_lan_event(event_data)

            mock_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_renamed_file_event(self, tmp_path):
        """Test handling renamed file event."""
        # Create source file
        src_file = tmp_path / "old.txt"
        src_file.write_text("content")

        event_data = {
            "name": "renamed",
            "src": {
                "filepath": "old.txt",
                "md5sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 7,
                "permissions": "-rw-r--r--",
            },
            "dst": {
                "filepath": "new.txt",
                "md5sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 7,
                "permissions": "-rw-r--r--",
            },
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
        }

        with patch('hsync_client.lan_websocket.CONFIG', config):
            await _handle_lan_event(event_data)

        # File should be renamed
        assert not src_file.exists()
        assert (tmp_path / "new.txt").exists()

    @pytest.mark.asyncio
    async def test_handle_deleted_file_event(self, tmp_path):
        """Test handling deleted file event."""
        # Create file to delete
        test_file = tmp_path / "todelete.txt"
        test_file.write_text("content")

        event_data = {
            "name": "deleted",
            "src": {
                "filepath": "todelete.txt",
                "md5sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 7,
                "permissions": "-rw-r--r--",
            },
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }

        with patch('hsync_client.lan_websocket.CONFIG', config):
            await _handle_lan_event(event_data)

        # File should be deleted
        assert not test_file.exists()

    @pytest.mark.asyncio
    async def test_handle_deleted_ignore_delete_mode(self, tmp_path):
        """Test that deletions are ignored in ignore-delete mode."""
        # Create file
        test_file = tmp_path / "keepme.txt"
        test_file.write_text("content")

        event_data = {
            "name": "deleted",
            "src": {
                "filepath": "keepme.txt",
                "md5sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 7,
                "permissions": "-rw-r--r--",
            },
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": True,
        }

        with patch('hsync_client.lan_websocket.CONFIG', config):
            await _handle_lan_event(event_data)

        # File should still exist
        assert test_file.exists()

    @pytest.mark.asyncio
    async def test_handle_event_upload_only_mode(self, tmp_path):
        """Test that events are ignored in upload-only mode."""
        event_data = {
            "name": "added",
            "src": {
                "filepath": "test.txt",
                "md5sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 10,
                "permissions": "-rw-r--r--",
            },
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "upload-only",
        }

        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.sync.update_file') as mock_update:
            await _handle_lan_event(event_data)

            # Should not update in upload-only mode
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_event_missing_src(self):
        """Test handling event with missing src field."""
        event_data = {
            "name": "added",
            # Missing src field
        }

        config = {
            "LOCAL_BUCKET": "/tmp",
            "SYNC_DIRECTION": "bidirectional",
        }

        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.sync.update_file') as mock_update:
            await _handle_lan_event(event_data)

            # Should not process event
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_symlink_event(self, tmp_path):
        """Test handling symlink event."""
        event_data = {
            "name": "symlink",
            "src": {
                "filepath": "link.txt",
                "md5sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 10,
                "permissions": "lrwxrwxrwx",
            },
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
        }

        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.sync.update_file') as mock_update:
            await _handle_lan_event(event_data)

            mock_update.assert_called_once()

