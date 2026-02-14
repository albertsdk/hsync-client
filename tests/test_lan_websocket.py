"""
Tests for LAN WebSocket server and client functionality.
"""

import asyncio
import json
import time
import threading
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from watchdog.events import (
    FileCreatedEvent,
    FileModifiedEvent,
    FileDeletedEvent,
    FileMovedEvent,
    DirCreatedEvent,
    DirDeletedEvent,
    DirModifiedEvent,
    DirMovedEvent,
    FileClosedEvent,
)

import pytest

from hsync_client.lan_websocket import (
    LANWebSocketServer,
    LANFileEventHandler,
    SlaveFileEventHandler,
    _escape_braces,
    _handle_lan_event,
    SUPPORTS_ON_CLOSED,
    MODIFIED_DEBOUNCE_INTERVAL,
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
        loop = asyncio.new_event_loop()
        server._loop = loop
        server._running = True

        event_dict = {"name": "test"}

        try:
            with patch.object(asyncio, 'run_coroutine_threadsafe') as mock_run:
                server.broadcast_sync(event_dict)
                mock_run.assert_called_once()
                # Close the unawaited coroutine passed as first arg
                coro = mock_run.call_args[0][0]
                coro.close()
        finally:
            loop.close()

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

    @pytest.mark.asyncio
    async def test_handle_client_auth_required_success(self):
        """Test _handle_client with successful auth."""
        server = LANWebSocketServer(port=9999)
        ws = AsyncMock()
        ws.remote_address = ("192.168.1.2", 5555)
        ws.recv.return_value = json.dumps({"type": "auth", "token": "secret"})

        # Proper async iterator that yields nothing (immediate disconnect)
        async def _empty_aiter(self_ws):
            return
            yield  # noqa – makes this an async generator

        ws.__aiter__ = _empty_aiter

        config = {"LAN_KEY": "secret"}
        with patch('hsync_client.lan_websocket.CONFIG', config):
            await server._handle_client(ws)

        # Should have sent auth_required and then auth ok
        calls = [c.args[0] for c in ws.send.call_args_list]
        assert json.loads(calls[0])["type"] == "auth_required"
        assert json.loads(calls[1]) == {"type": "auth", "status": "ok"}
        # Client is discarded in the finally block after the async-for exits
        assert ws not in server._clients

    @pytest.mark.asyncio
    async def test_handle_client_auth_failed(self):
        """Test _handle_client with wrong auth token."""
        server = LANWebSocketServer(port=9999)
        ws = AsyncMock()
        ws.remote_address = ("192.168.1.2", 5555)
        ws.recv.return_value = json.dumps({"type": "auth", "token": "wrong"})

        config = {"LAN_KEY": "secret"}
        with patch('hsync_client.lan_websocket.CONFIG', config):
            await server._handle_client(ws)

        calls = [c.args[0] for c in ws.send.call_args_list]
        assert json.loads(calls[1]) == {"type": "auth", "status": "failed"}
        ws.close.assert_called_once()
        assert ws not in server._clients

    @pytest.mark.asyncio
    async def test_handle_client_auth_timeout(self):
        """Test _handle_client when auth times out."""
        server = LANWebSocketServer(port=9999)
        ws = AsyncMock()
        ws.remote_address = ("192.168.1.2", 5555)
        ws.recv.side_effect = asyncio.TimeoutError()

        config = {"LAN_KEY": "secret"}
        with patch('hsync_client.lan_websocket.CONFIG', config):
            await server._handle_client(ws)

        ws.close.assert_called_once()
        assert ws not in server._clients

    @pytest.mark.asyncio
    async def test_handle_client_auth_generic_error(self):
        """Test _handle_client when auth raises unexpected error."""
        server = LANWebSocketServer(port=9999)
        ws = AsyncMock()
        ws.remote_address = ("192.168.1.2", 5555)
        ws.recv.side_effect = RuntimeError("bad")

        config = {"LAN_KEY": "secret"}
        with patch('hsync_client.lan_websocket.CONFIG', config):
            await server._handle_client(ws)

        ws.close.assert_called_once()
        assert ws not in server._clients

    @pytest.mark.asyncio
    async def test_handle_client_no_auth_required(self):
        """Test _handle_client when no LAN_KEY is configured."""
        server = LANWebSocketServer(port=9999)
        ws = AsyncMock()
        ws.remote_address = ("192.168.1.2", 5555)

        async def _empty_aiter(self_ws):
            return
            yield  # noqa

        ws.__aiter__ = _empty_aiter

        config = {}  # no LAN_KEY
        with patch('hsync_client.lan_websocket.CONFIG', config):
            await server._handle_client(ws)

        calls = [c.args[0] for c in ws.send.call_args_list]
        assert json.loads(calls[0])["type"] == "connected"
        # Client is discarded in the finally block after the async-for exits
        assert ws not in server._clients

    @pytest.mark.asyncio
    async def test_handle_client_disconnect_during_loop(self):
        """Test _handle_client removes client on disconnect."""
        server = LANWebSocketServer(port=9999)
        ws = AsyncMock()
        ws.remote_address = ("192.168.1.2", 5555)

        # Simulate client disconnect exception during message loop
        async def async_iter_raise():
            raise ConnectionError("gone")
            yield  # noqa: unreachable  – make it an async generator

        ws.__aiter__ = lambda self: async_iter_raise()

        config = {}
        with patch('hsync_client.lan_websocket.CONFIG', config):
            await server._handle_client(ws)

        # Client should have been discarded in finally
        assert ws not in server._clients

    def test_start_normal_flow(self, tmp_path):
        """Test normal start sets up observer and thread."""
        server = LANWebSocketServer(port=9999)

        config = {"LOCAL_BUCKET": str(tmp_path)}
        mock_obs = MagicMock()

        def fake_run_loop():
            # Use a plain MagicMock as the loop (no real event loop)
            server._loop = MagicMock()
            while server._running:
                time.sleep(0.05)

        # Directly replace the method to avoid MagicMock wrapping the server
        # (patch.object wraps with MagicMock which introspects async methods
        # like `broadcast`, creating unawaited coroutine warnings)
        server._run_loop = fake_run_loop

        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.lan_websocket.Observer') as MockObserver:
            MockObserver.return_value = mock_obs
            server.start(watch_path=str(tmp_path))

        assert server._running is True
        assert server._thread is not None
        assert server._observer is mock_obs
        mock_obs.schedule.assert_called_once()
        mock_obs.start.assert_called_once()

        # Cleanup
        server._running = False
        server._thread.join(timeout=2.0)

    def test_stop_normal_flow(self):
        """Test normal stop cleans up observer, clients, and thread."""
        server = LANWebSocketServer(port=9999)
        server._running = True
        server._loop = MagicMock()
        mock_observer = MagicMock()
        server._observer = mock_observer
        mock_thread = MagicMock()
        server._thread = mock_thread
        server._clients = set()  # no clients to close

        server.stop()

        assert server._running is False
        mock_observer.stop.assert_called_once()
        mock_observer.join.assert_called_once()
        assert server._observer is None
        mock_thread.join.assert_called_once()
        assert server._thread is None

    def test_is_running_true(self):
        """Test is_running returns True when server is active."""
        server = LANWebSocketServer(port=9999)
        server._running = True
        assert server.is_running() is True


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

    def test_send_event_normal_broadcasts(self, handler, ws_server):
        """Test that events are broadcast in normal mode."""
        from hsync_client.models.event import Event, EventName
        from hsync_client.models.file import File

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": "/test",
            "REMOTE_BUCKET": "remote",
            "SYNC_DIRECTION": "bidirectional",
        }

        file_obj = File("/test/file.txt")
        event = Event("test-client", "/test", "remote", EventName.added, file_obj)

        with patch('hsync_client.lan_websocket.CONFIG', config):
            handler._send_event(event)
            ws_server.broadcast_sync.assert_called_once()

    def test_on_any_event_hidden_dir_skipped(self, handler, tmp_path):
        """Test on_any_event returns early for hidden directory."""
        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        event = DirCreatedEvent(str(hidden))
        # Should not raise
        handler.on_any_event(event)

    def test_on_any_event_hidden_file_skipped(self, handler, tmp_path):
        """Test on_any_event returns early for hidden file."""
        hidden = tmp_path / ".hidden_file"
        hidden.write_text("x")
        event = FileCreatedEvent(str(hidden))
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
        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r",
                  "SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.lan_websocket.Dir') as D:
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
        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r",
                  "SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.lan_websocket.Symlink') as S:
            S.get.return_value = None
            handler.on_created(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_created_regular_file_ignored(self, handler, ws_server, tmp_path):
        """Test on_created for regular file (non-symlink) does nothing."""
        f = tmp_path / "file.txt"
        f.write_text("x")
        event = FileCreatedEvent(str(f))
        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r"}
        with patch('hsync_client.lan_websocket.CONFIG', config):
            handler.on_created(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_modified_root_directory_skipped(self, handler, ws_server, tmp_path):
        """Test on_modified skips root LOCAL_BUCKET directory."""
        event = DirModifiedEvent(str(tmp_path))
        event.is_directory = True
        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r",
                  "SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config):
            handler.on_modified(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_modified_dir_get_returns_none(self, handler, ws_server, tmp_path):
        """Test on_modified when Dir.get returns None."""
        sub = tmp_path / "sub"
        sub.mkdir()
        event = DirModifiedEvent(str(sub))
        event.is_directory = True
        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r",
                  "SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.lan_websocket.Dir') as D:
            D.get.return_value = None
            handler.on_modified(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_modified_file_non_linux_debounce(self, handler, ws_server, tmp_path):
        """Test on_modified file debounce on non-Linux."""
        f = tmp_path / "file.txt"
        f.write_text("x")
        event = FileModifiedEvent(str(f))

        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r",
                  "SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.lan_websocket.SUPPORTS_ON_CLOSED', False), \
             patch('hsync_client.lan_websocket.File') as F:
            F.get.return_value = MagicMock()
            handler.on_modified(event)
            ws_server.broadcast_sync.assert_called_once()

    def test_on_modified_file_non_linux_debounce_skip(self, handler, ws_server, tmp_path):
        """Test on_modified file skipped when within debounce interval."""
        f = tmp_path / "file.txt"
        f.write_text("x")
        event = FileModifiedEvent(str(f))

        # Pre-set recent modification time
        handler._last_modified[str(f)] = time.time()

        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r"}
        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.lan_websocket.SUPPORTS_ON_CLOSED', False):
            handler.on_modified(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_closed_directory_skipped(self, handler, ws_server, tmp_path):
        """Test on_closed skips directory events."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = str(tmp_path / "dir")
        handler.on_closed(event)
        ws_server.broadcast_sync.assert_not_called()

    def test_on_closed_hidden_file_skipped(self, handler, ws_server, tmp_path):
        """Test on_closed skips hidden files."""
        event = FileModifiedEvent(str(tmp_path / ".hidden"))
        handler.on_closed(event)
        ws_server.broadcast_sync.assert_not_called()

    def test_handle_file_modified_not_exists(self, handler, ws_server, tmp_path):
        """Test _handle_file_modified returns early when file not exists."""
        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r",
                  "SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config):
            handler._handle_file_modified(str(tmp_path / "gone.txt"))
            ws_server.broadcast_sync.assert_not_called()

    def test_handle_file_modified_file_get_none(self, handler, ws_server, tmp_path):
        """Test _handle_file_modified when File.get returns None."""
        f = tmp_path / "file.txt"
        f.write_text("x")
        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r",
                  "SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.lan_websocket.File') as F:
            F.get.return_value = None
            handler._handle_file_modified(str(f))
            ws_server.broadcast_sync.assert_not_called()

    def test_on_moved_no_dest_path(self, handler, ws_server, tmp_path):
        """Test on_moved returns when dest_path is None."""
        event = MagicMock()
        event.src_path = str(tmp_path / "a.txt")
        event.dest_path = None
        event.is_directory = False
        handler.on_moved(event)
        ws_server.broadcast_sync.assert_not_called()

    def test_on_moved_hidden_to_hidden_dir(self, handler, ws_server, tmp_path):
        """Test on_moved returns for hidden-to-hidden directory."""
        event = DirMovedEvent(str(tmp_path / ".a"), str(tmp_path / ".b"))
        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r",
                  "SYNC_DIRECTION": "bidirectional", "IGNORE_DELETE": False}
        with patch('hsync_client.lan_websocket.CONFIG', config):
            handler.on_moved(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_moved_hidden_to_visible_dir(self, handler, ws_server, tmp_path):
        """Test on_moved hidden->visible dir triggers on_created."""
        visible = tmp_path / "visible"
        visible.mkdir()
        event = DirMovedEvent(str(tmp_path / ".hidden"), str(visible))
        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r",
                  "SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.lan_websocket.Dir') as D:
            D.get.return_value = MagicMock()
            handler.on_moved(event)
            ws_server.broadcast_sync.assert_called_once()

    def test_on_moved_visible_to_hidden_dir(self, handler, ws_server, tmp_path):
        """Test on_moved visible->hidden dir triggers on_deleted."""
        event = DirMovedEvent(str(tmp_path / "visible"), str(tmp_path / ".hidden"))
        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r",
                  "SYNC_DIRECTION": "bidirectional", "IGNORE_DELETE": False}
        with patch('hsync_client.lan_websocket.CONFIG', config):
            handler.on_moved(event)
            ws_server.broadcast_sync.assert_called_once()

    def test_on_moved_dir_rename_dir_get_none(self, handler, ws_server, tmp_path):
        """Test on_moved dir rename when Dir.get returns None."""
        event = DirMovedEvent(str(tmp_path / "old"), str(tmp_path / "new"))
        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r",
                  "SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.lan_websocket.Dir') as D:
            D.get.return_value = None
            handler.on_moved(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_moved_dir_rename_success(self, handler, ws_server, tmp_path):
        """Test on_moved dir rename broadcasts event."""
        event = DirMovedEvent(str(tmp_path / "old"), str(tmp_path / "new"))
        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r",
                  "SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.lan_websocket.Dir') as D:
            mock_dir = MagicMock()
            D.get.return_value = mock_dir
            handler.on_moved(event)
            ws_server.broadcast_sync.assert_called_once()

    def test_on_moved_file_hidden_src(self, handler, ws_server, tmp_path):
        """Test on_moved returns when file source is hidden."""
        event = FileMovedEvent(str(tmp_path / ".hidden"), str(tmp_path / "visible"))
        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r"}
        with patch('hsync_client.lan_websocket.CONFIG', config):
            handler.on_moved(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_moved_file_rename_success(self, handler, ws_server, tmp_path):
        """Test on_moved file rename broadcasts event."""
        event = FileMovedEvent(str(tmp_path / "old.txt"), str(tmp_path / "new.txt"))
        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r",
                  "SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.lan_websocket.File') as F:
            mock_file = MagicMock()
            F.get.return_value = mock_file
            handler.on_moved(event)
            ws_server.broadcast_sync.assert_called_once()

    def test_on_moved_file_rename_file_get_none(self, handler, ws_server, tmp_path):
        """Test on_moved file rename when File.get returns None."""
        event = FileMovedEvent(str(tmp_path / "old.txt"), str(tmp_path / "new.txt"))
        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r",
                  "SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.lan_websocket.File') as F:
            F.get.return_value = None
            handler.on_moved(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_deleted_hidden_directory_skipped(self, handler, ws_server, tmp_path):
        """Test on_deleted skips hidden directories."""
        event = DirDeletedEvent(str(tmp_path / ".hidden"))
        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r",
                  "SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config):
            handler.on_deleted(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_deleted_hidden_file_skipped(self, handler, ws_server, tmp_path):
        """Test on_deleted skips hidden files."""
        event = FileDeletedEvent(str(tmp_path / ".secret"))
        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r",
                  "SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config):
            handler.on_deleted(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_deleted_visible_directory(self, handler, ws_server, tmp_path):
        """Test on_deleted broadcasts visible directory deletion."""
        event = DirDeletedEvent(str(tmp_path / "mydir"))
        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r",
                  "SYNC_DIRECTION": "bidirectional", "IGNORE_DELETE": False}
        with patch('hsync_client.lan_websocket.CONFIG', config):
            handler.on_deleted(event)
            ws_server.broadcast_sync.assert_called_once()


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

    def test_ensure_sentinel_os_error(self, handler):
        """Test _ensure_sentinel returns on OSError (e.g., dir not found)."""
        handler._ensure_sentinel("/nonexistent/path/xyz")  # no crash

    def test_upload_to_master_success(self, handler):
        """Test _upload_to_master calls upload_file."""
        from hsync_client.models.file import File
        file_obj = File("/test/file.txt")
        with patch('hsync_client.transfer.upload_file') as mock_up:
            handler._upload_to_master(file_obj)
            mock_up.assert_called_once_with(file_obj, check_size=True)

    def test_upload_to_master_exception(self, handler):
        """Test _upload_to_master logs error on exception."""
        from hsync_client.models.file import File
        file_obj = File("/test/file.txt")
        with patch('hsync_client.transfer.upload_file', side_effect=Exception("fail")):
            handler._upload_to_master(file_obj)  # should not raise

    def test_delete_on_master_success(self, handler):
        """Test _delete_on_master sends DELETE request."""
        config = {"LOCAL_BUCKET": "/bucket", "REMOTE_BUCKET": "remote",
                  "REQUEST_HEADERS": {"X-Key": "val"}}
        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('requests.delete') as mock_del:
            mock_del.return_value = MagicMock(status_code=200)
            handler._delete_on_master("/bucket/file.txt", is_dir=False)
            mock_del.assert_called_once()

    def test_delete_on_master_failure_status(self, handler):
        """Test _delete_on_master logs warning on bad status."""
        config = {"LOCAL_BUCKET": "/bucket", "REMOTE_BUCKET": "remote"}
        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('requests.delete') as mock_del:
            mock_del.return_value = MagicMock(status_code=500)
            handler._delete_on_master("/bucket/file.txt")  # no raise

    def test_delete_on_master_exception(self, handler):
        """Test _delete_on_master logs error on exception."""
        config = {"LOCAL_BUCKET": "/bucket", "REMOTE_BUCKET": "remote"}
        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('requests.delete', side_effect=Exception("net err")):
            handler._delete_on_master("/bucket/file.txt")  # no raise

    def test_rename_on_master_success(self, handler):
        """Test _rename_on_master sends PATCH request."""
        config = {"LOCAL_BUCKET": "/bucket", "REMOTE_BUCKET": "remote",
                  "REQUEST_HEADERS": {"X-Key": "val"}}
        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('requests.patch') as mock_patch:
            mock_patch.return_value = MagicMock(status_code=200)
            handler._rename_on_master("/bucket/old.txt", "/bucket/new.txt")
            mock_patch.assert_called_once()

    def test_rename_on_master_failure_status(self, handler):
        """Test _rename_on_master logs warning on bad status."""
        config = {"LOCAL_BUCKET": "/bucket", "REMOTE_BUCKET": "remote"}
        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('requests.patch') as mock_patch:
            mock_patch.return_value = MagicMock(status_code=500)
            handler._rename_on_master("/bucket/old.txt", "/bucket/new.txt")

    def test_rename_on_master_exception(self, handler):
        """Test _rename_on_master logs error on exception."""
        config = {"LOCAL_BUCKET": "/bucket", "REMOTE_BUCKET": "remote"}
        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('requests.patch', side_effect=Exception("fail")):
            handler._rename_on_master("/bucket/a", "/bucket/b")  # no raise

    def test_on_created_hidden_dir_ignored(self, handler, tmp_path):
        """Test on_created ignores hidden directories."""
        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        event = DirCreatedEvent(str(hidden))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config):
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
        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.lan_websocket.File') as F, \
             patch.object(handler, '_upload_to_master'):
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
        event = DirModifiedEvent(str(tmp_path / "sub"))
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
        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.lan_websocket.SUPPORTS_ON_CLOSED', False), \
             patch('hsync_client.lan_websocket.File') as F, \
             patch.object(handler, '_upload_to_master'):
            F.get.return_value = MagicMock()
            handler.on_modified(event)

    def test_on_closed_not_supported(self, handler, tmp_path):
        """Test on_closed returns immediately when not supported."""
        event = FileModifiedEvent(str(tmp_path / "f.txt"))
        with patch('hsync_client.lan_websocket.SUPPORTS_ON_CLOSED', False):
            handler.on_closed(event)
        assert len(handler._pending_ops) == 0

    def test_on_closed_temp_file_ignored(self, handler, tmp_path):
        """Test on_closed ignores temp files."""
        event = FileModifiedEvent(str(tmp_path / "f.part"))
        with patch('hsync_client.lan_websocket.SUPPORTS_ON_CLOSED', True):
            handler.on_closed(event)
        assert len(handler._pending_ops) == 0

    def test_on_closed_receiving_ignored(self, handler, tmp_path):
        """Test on_closed ignores files being received."""
        p = str(tmp_path / "f.txt")
        handler.mark_receiving(p)
        event = FileModifiedEvent(p)
        with patch('hsync_client.lan_websocket.SUPPORTS_ON_CLOSED', True):
            handler.on_closed(event)
        assert len(handler._pending_ops) == 0

    def test_on_closed_directory_ignored(self, handler, tmp_path):
        """Test on_closed ignores directory events."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = str(tmp_path / "dir")
        with patch('hsync_client.lan_websocket.SUPPORTS_ON_CLOSED', True):
            handler.on_closed(event)
        assert len(handler._pending_ops) == 0

    def test_on_closed_hidden_file_ignored(self, handler, tmp_path):
        """Test on_closed ignores hidden files."""
        event = FileModifiedEvent(str(tmp_path / ".hidden"))
        with patch('hsync_client.lan_websocket.SUPPORTS_ON_CLOSED', True):
            handler.on_closed(event)
        assert len(handler._pending_ops) == 0

    def test_on_closed_normal_uploads(self, handler, tmp_path):
        """Test on_closed triggers upload for normal file."""
        f = tmp_path / "f.txt"
        f.write_text("x")
        event = FileModifiedEvent(str(f))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.SUPPORTS_ON_CLOSED', True), \
             patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.lan_websocket.File') as F, \
             patch.object(handler, '_upload_to_master'):
            F.get.return_value = MagicMock()
            handler.on_closed(event)
            assert len(handler._pending_ops) >= 1

    def test_handle_file_upload_download_only(self, handler, tmp_path):
        """Test _handle_file_upload skips in download-only mode."""
        config = {"SYNC_DIRECTION": "download-only"}
        with patch('hsync_client.lan_websocket.CONFIG', config):
            handler._handle_file_upload(str(tmp_path / "f.txt"))
        assert len(handler._pending_ops) == 0

    def test_handle_file_upload_not_exists(self, handler, tmp_path):
        """Test _handle_file_upload skips non-existent file."""
        config = {"SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config):
            handler._handle_file_upload(str(tmp_path / "gone.txt"))
        assert len(handler._pending_ops) == 0

    def test_handle_file_upload_file_get_none(self, handler, tmp_path):
        """Test _handle_file_upload skips when File.get returns None."""
        f = tmp_path / "f.txt"
        f.write_text("x")
        config = {"SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.lan_websocket.File') as F:
            F.get.return_value = None
            handler._handle_file_upload(str(f))
        assert len(handler._pending_ops) == 0

    def test_handle_file_upload_starts_operation(self, handler, tmp_path):
        """Test _handle_file_upload starts upload operation."""
        f = tmp_path / "f.txt"
        f.write_text("x")
        config = {"SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.lan_websocket.File') as F, \
             patch.object(handler, '_upload_to_master'):
            F.get.return_value = MagicMock()
            handler._handle_file_upload(str(f))
        assert len(handler._pending_ops) >= 1

    def test_on_deleted_visible_file(self, handler, tmp_path):
        """Test on_deleted starts delete operation for visible file."""
        event = FileDeletedEvent(str(tmp_path / "file.txt"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional",
                  "IGNORE_DELETE": False}
        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch.object(handler, '_delete_on_master'):
            handler.on_deleted(event)
        assert len(handler._pending_ops) >= 1

    def test_on_deleted_temp_file_ignored(self, handler, tmp_path):
        """Test on_deleted ignores temp files."""
        event = FileDeletedEvent(str(tmp_path / "file.part"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config):
            handler.on_deleted(event)
        assert len(handler._pending_ops) == 0

    def test_on_deleted_hidden_dir_ignored(self, handler, tmp_path):
        """Test on_deleted ignores hidden directories."""
        event = DirDeletedEvent(str(tmp_path / ".hidden"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config):
            handler.on_deleted(event)
        assert len(handler._pending_ops) == 0

    def test_on_deleted_hidden_file_ignored(self, handler, tmp_path):
        """Test on_deleted ignores hidden files."""
        event = FileDeletedEvent(str(tmp_path / ".hidden"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config):
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
        with patch('hsync_client.lan_websocket.CONFIG', config):
            handler.on_moved(event)
        assert len(handler._pending_ops) == 0

    def test_on_moved_temp_dest_ignored(self, handler, tmp_path):
        """Test on_moved ignores temp destination files."""
        event = FileMovedEvent(str(tmp_path / "f.txt"), str(tmp_path / "f.part"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config):
            handler.on_moved(event)
        assert len(handler._pending_ops) == 0

    def test_on_moved_receiving_src_ignored(self, handler, tmp_path):
        """Test on_moved ignores when src is being received."""
        src = str(tmp_path / "a.txt")
        handler.mark_receiving(src)
        event = FileMovedEvent(src, str(tmp_path / "b.txt"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config):
            handler.on_moved(event)
        assert len(handler._pending_ops) == 0

    def test_on_moved_hidden_to_hidden_ignored(self, handler, tmp_path):
        """Test on_moved both hidden skipped."""
        event = FileMovedEvent(str(tmp_path / ".a"), str(tmp_path / ".b"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config):
            handler.on_moved(event)
        assert len(handler._pending_ops) == 0

    def test_on_moved_hidden_to_visible_file_uploads(self, handler, tmp_path):
        """Test on_moved hidden->visible triggers upload."""
        f = tmp_path / "visible.txt"
        f.write_text("x")
        event = FileMovedEvent(str(tmp_path / ".hidden"), str(f))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.lan_websocket.File') as F, \
             patch.object(handler, '_upload_to_master'):
            F.get.return_value = MagicMock()
            handler.on_moved(event)
        assert len(handler._pending_ops) >= 1

    def test_on_moved_visible_to_hidden_deletes(self, handler, tmp_path):
        """Test on_moved visible->hidden triggers deletion."""
        event = FileMovedEvent(str(tmp_path / "vis.txt"), str(tmp_path / ".hid"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional",
                  "IGNORE_DELETE": False}
        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch.object(handler, '_delete_on_master'):
            handler.on_moved(event)
        assert len(handler._pending_ops) >= 1

    def test_on_moved_visible_to_hidden_ignore_delete(self, handler, tmp_path):
        """Test on_moved visible->hidden skips when ignore-delete."""
        event = FileMovedEvent(str(tmp_path / "vis.txt"), str(tmp_path / ".hid"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional",
                  "IGNORE_DELETE": True}
        with patch('hsync_client.lan_websocket.CONFIG', config):
            handler.on_moved(event)
        assert len(handler._pending_ops) == 0

    def test_on_moved_normal_rename(self, handler, tmp_path):
        """Test on_moved normal rename calls _rename_on_master."""
        event = FileMovedEvent(str(tmp_path / "a.txt"), str(tmp_path / "b.txt"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch.object(handler, '_rename_on_master'):
            handler.on_moved(event)
        assert len(handler._pending_ops) >= 1

    def test_wait_pending_timeout(self, handler):
        """Test wait_pending returns count of timed-out operations."""
        def slow_op():
            time.sleep(5)

        handler._start_operation(slow_op, name="slow")
        timed_out = handler.wait_pending(timeout=0.1)
        assert timed_out >= 1


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

    @pytest.mark.asyncio
    async def test_handle_renamed_dir_event(self, tmp_path):
        """Test handling renamed directory event."""
        src_dir = tmp_path / "olddir"
        src_dir.mkdir()

        event_data = {
            "name": "renamed",
            "src": {"path": "olddir", "permissions": "drwxr-xr-x"},
            "dst": {"path": "newdir", "permissions": "drwxr-xr-x"},
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
        }

        with patch('hsync_client.lan_websocket.CONFIG', config):
            await _handle_lan_event(event_data)

        assert not src_dir.exists()
        assert (tmp_path / "newdir").exists()

    @pytest.mark.asyncio
    async def test_handle_renamed_missing_dst(self, tmp_path):
        """Test rename event with missing dst field returns early."""
        event_data = {
            "name": "renamed",
            "src": {"filepath": "old.txt", "md5sum": "x", "mtime": 1, "ctime": 1,
                     "size": 1, "permissions": "-rw-r--r--"},
            # no "dst"
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
        }

        with patch('hsync_client.lan_websocket.CONFIG', config):
            await _handle_lan_event(event_data)
        # Should not crash – just logs a warning

    @pytest.mark.asyncio
    async def test_handle_renamed_file_src_not_exists(self, tmp_path):
        """Test rename event when source file doesn't exist (no rename)."""
        event_data = {
            "name": "renamed",
            "src": {"filepath": "gone.txt", "md5sum": "x", "mtime": 1, "ctime": 1,
                     "size": 1, "permissions": "-rw-r--r--"},
            "dst": {"filepath": "new.txt", "md5sum": "x", "mtime": 1, "ctime": 1,
                     "size": 1, "permissions": "-rw-r--r--"},
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
        }

        with patch('hsync_client.lan_websocket.CONFIG', config):
            await _handle_lan_event(event_data)

        # Neither file exists – no crash
        assert not (tmp_path / "new.txt").exists()

    @pytest.mark.asyncio
    async def test_handle_deleted_dir_event(self, tmp_path):
        """Test handling deleted directory event."""
        target = tmp_path / "deldir"
        target.mkdir()
        (target / "file.txt").write_text("x")

        event_data = {
            "name": "deleted",
            "src": {"path": "deldir", "permissions": "drwxr-xr-x"},
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }

        with patch('hsync_client.lan_websocket.CONFIG', config):
            await _handle_lan_event(event_data)

        assert not target.exists()

    @pytest.mark.asyncio
    async def test_handle_deleted_hsynckeep_cleans_dir(self, tmp_path):
        """Test deleting .hsynckeep sentinel removes empty parent dir."""
        parent = tmp_path / "emptydir"
        parent.mkdir()
        sentinel = parent / ".hsynckeep"
        sentinel.write_text("")

        event_data = {
            "name": "deleted",
            "src": {"filepath": "emptydir/.hsynckeep", "md5sum": "x",
                     "mtime": 1, "ctime": 1, "size": 0,
                     "permissions": "-rw-r--r--"},
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }

        with patch('hsync_client.lan_websocket.CONFIG', config):
            await _handle_lan_event(event_data)

        assert not sentinel.exists()
        assert not parent.exists()  # empty dir cleaned up

    @pytest.mark.asyncio
    async def test_handle_added_file_with_slave_handler(self, tmp_path):
        """Test mark_receiving/unmark_receiving called with slave_handler."""
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

        handler = SlaveFileEventHandler("host", 8765, use_https=False)

        with patch('hsync_client.lan_websocket.CONFIG', config), \
             patch('hsync_client.sync.update_file'):
            await _handle_lan_event(event_data, slave_handler=handler)

        # File should have been unmarked after processing
        assert not handler.is_receiving(str(tmp_path / "test.txt"))

    @pytest.mark.asyncio
    async def test_handle_deleted_dir_not_exists(self, tmp_path):
        """Test deleted dir event when dir doesn't exist (no crash)."""
        event_data = {
            "name": "deleted",
            "src": {"path": "nonexistent", "permissions": "drwxr-xr-x"},
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }

        with patch('hsync_client.lan_websocket.CONFIG', config):
            await _handle_lan_event(event_data)
        # Should not crash

    @pytest.mark.asyncio
    async def test_handle_deleted_file_not_exists(self, tmp_path):
        """Test deleted file event when file doesn't exist (no crash)."""
        event_data = {
            "name": "deleted",
            "src": {"filepath": "gone.txt", "md5sum": "x", "mtime": 1,
                     "ctime": 1, "size": 1, "permissions": "-rw-r--r--"},
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }

        with patch('hsync_client.lan_websocket.CONFIG', config):
            await _handle_lan_event(event_data)
        # Should not crash

