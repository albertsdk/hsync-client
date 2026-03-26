"""
Tests for LAN WebSocket server functionality.
"""

import asyncio
import contextlib
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hsync_client.lan_websocket import LANWebSocketServer


class TestLANWebSocketServer:
    """Tests for LANWebSocketServer class."""

    def test_server_creation(self):
        """Test creating a WebSocket server instance."""
        server = LANWebSocketServer(port=9999)
        assert server.port == 9999

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
        assert server._watcher is None
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
            with patch.object(asyncio, "run_coroutine_threadsafe") as mock_run:
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

        with patch("hsync_client.lan_websocket.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
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
        """Test _handle_client with successful auth (capability exchange)."""
        from hsync_common.protocol.handshake import PROTOCOL_VERSION_LAN

        server = LANWebSocketServer(port=9999)
        ws = AsyncMock()
        ws.remote_address = ("192.168.1.2", 5555)
        ws.recv.return_value = json.dumps(
            {
                "type": "auth",
                "token": "secret",
                "cid": "ValidClientId12345678",
                "protocol_version": PROTOCOL_VERSION_LAN,
                "capabilities": ["compression"],
            }
        )

        # Proper async iterator that yields nothing (immediate disconnect)
        async def _empty_aiter(self_ws):
            return
            yield  # noqa – makes this an async generator

        ws.__aiter__ = _empty_aiter

        config = {"LAN_KEY": "secret"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            await server._handle_client(ws)

        # Should have sent auth_required (with caps) and then auth ok (with caps)
        calls = [c.args[0] for c in ws.send.call_args_list]
        greeting = json.loads(calls[0])
        assert greeting["type"] == "auth_required"
        assert greeting["protocol_version"] == PROTOCOL_VERSION_LAN
        assert greeting["capabilities"] == ["tombstones", "vector_clocks"]
        assert json.loads(calls[1]) == {
            "type": "auth",
            "status": "ok",
            "protocol_version": PROTOCOL_VERSION_LAN,
            "capabilities": ["tombstones", "vector_clocks"],
        }
        # Client is discarded in the finally block after the async-for exits
        assert ws not in server._clients

    @pytest.mark.asyncio
    async def test_handle_client_auth_failed(self):
        """Test _handle_client with wrong auth token (failed frame)."""
        from hsync_common.protocol.handshake import PROTOCOL_VERSION_LAN

        server = LANWebSocketServer(port=9999)
        ws = AsyncMock()
        ws.remote_address = ("192.168.1.2", 5555)
        ws.recv.return_value = json.dumps(
            {
                "type": "auth",
                "token": "wrong",
                "cid": "BadClientId1234567890",
                "protocol_version": PROTOCOL_VERSION_LAN,
                "capabilities": [],
            }
        )

        config = {"LAN_KEY": "secret"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            await server._handle_client(ws)

        calls = [c.args[0] for c in ws.send.call_args_list]
        assert json.loads(calls[1]) == {
            "type": "auth",
            "status": "failed",
            "protocol_version": PROTOCOL_VERSION_LAN,
            "reason": "invalid_token",
        }
        ws.close.assert_called_once()
        assert ws not in server._clients

    def test_registry_rejects_master_cid(self):
        """The registry refuses to admit the master's own CID."""
        server = LANWebSocketServer(port=9999, master_cid="m" * 21)
        allowed, stale = server._cid_registry.try_reserve("m" * 21, "192.168.1.2", object())
        assert allowed is False
        assert stale is None

    @pytest.mark.asyncio
    async def test_handle_client_rejects_slave_with_master_cid(self):
        """A slave presenting the master's own CID is refused with cid_conflict."""
        from hsync_common.protocol.handshake import PROTOCOL_VERSION_LAN

        server = LANWebSocketServer(port=9999, master_cid="m" * 21)
        ws = AsyncMock()
        ws.remote_address = ("192.168.1.2", 5555)
        ws.recv.return_value = json.dumps(
            {
                "type": "auth",
                "token": "secret",
                "cid": "m" * 21,
                "protocol_version": PROTOCOL_VERSION_LAN,
                "capabilities": [],
            }
        )

        config = {"LAN_KEY": "secret"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            await server._handle_client(ws)

        calls = [c.args[0] for c in ws.send.call_args_list]
        assert json.loads(calls[1]) == {
            "type": "auth",
            "status": "failed",
            "protocol_version": PROTOCOL_VERSION_LAN,
            "reason": "cid_conflict",
        }
        ws.close.assert_called_once()
        assert ws not in server._clients
        # The master CID is never bound (always a conflict), so the registry
        # holds no entry for it after the refused attempt.
        assert server._cid_registry.request_conflict("m" * 21, "192.168.1.2") is True
        assert "m" * 21 not in server._cid_registry._entries

    @pytest.mark.asyncio
    async def test_handle_client_rejects_duplicate_slave_cid_from_other_host(self):
        """A second slave presenting a CID bound to a different host is refused."""
        from hsync_common.protocol.handshake import PROTOCOL_VERSION_LAN

        server = LANWebSocketServer(port=9999, master_cid="m" * 21)
        # CID already bound to a *different* source address (a cloned device).
        server._cid_registry.try_reserve("b" * 21, "192.168.1.9", object())
        ws = AsyncMock()
        ws.remote_address = ("192.168.1.3", 5555)
        ws.recv.return_value = json.dumps(
            {
                "type": "auth",
                "token": "secret",
                "cid": "b" * 21,
                "protocol_version": PROTOCOL_VERSION_LAN,
                "capabilities": [],
            }
        )

        config = {"LAN_KEY": "secret"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            await server._handle_client(ws)

        calls = [c.args[0] for c in ws.send.call_args_list]
        assert json.loads(calls[1]) == {
            "type": "auth",
            "status": "failed",
            "protocol_version": PROTOCOL_VERSION_LAN,
            "reason": "cid_conflict",
        }
        ws.close.assert_called_once()
        assert ws not in server._clients
        # The original binding must be untouched.
        assert server._cid_registry.request_conflict("b" * 21, "192.168.1.3") is True

    @pytest.mark.asyncio
    async def test_handle_client_same_host_reconnect_replaces_binding(self):
        """A slave reconnecting from the same address replaces its own binding."""
        from hsync_common.protocol.handshake import PROTOCOL_VERSION_LAN

        server = LANWebSocketServer(port=9999, master_cid="m" * 21)
        stale_ws = AsyncMock()
        server._cid_registry.try_reserve("b" * 21, "192.168.1.7", stale_ws)

        ws = AsyncMock()
        ws.remote_address = ("192.168.1.7", 4444)
        ws.recv.return_value = json.dumps(
            {
                "type": "auth",
                "token": "secret",
                "cid": "b" * 21,
                "protocol_version": PROTOCOL_VERSION_LAN,
                "capabilities": [],
            }
        )

        async def _empty_aiter(self_ws):
            return
            yield  # noqa – makes this an async generator

        ws.__aiter__ = _empty_aiter

        config = {"LAN_KEY": "secret"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            await server._handle_client(ws)

        # Authenticated (no cid_conflict failure) — status is "ok".
        calls = [c.args[0] for c in ws.send.call_args_list]
        assert json.loads(calls[1])["status"] == "ok"
        # The superseded connection was closed and the binding now points at ws.
        stale_ws.close.assert_awaited()
        assert server._cid_registry.request_conflict("b" * 21, "192.168.1.7") is False
        assert ws not in server._clients  # removed in finally after empty loop

    @pytest.mark.asyncio
    async def test_handle_client_auth_protocol_mismatch(self):
        """A slave speaking a different LAN protocol is refused (hard fail)."""
        from hsync_common.protocol.handshake import PROTOCOL_VERSION_LAN

        server = LANWebSocketServer(port=9999)
        ws = AsyncMock()
        ws.remote_address = ("192.168.1.2", 5555)
        ws.recv.return_value = json.dumps(
            {
                "type": "auth",
                "token": "secret",
                "cid": "ValidClientId12345678",
                "protocol_version": "lan/9",
                "capabilities": [],
            }
        )

        config = {"LAN_KEY": "secret"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            await server._handle_client(ws)

        calls = [c.args[0] for c in ws.send.call_args_list]
        assert json.loads(calls[1]) == {
            "type": "auth",
            "status": "failed",
            "protocol_version": PROTOCOL_VERSION_LAN,
            "reason": "protocol_incompatible",
        }
        ws.close.assert_called_once()
        assert ws not in server._clients

    @pytest.mark.asyncio
    async def test_handle_client_auth_timeout(self):
        """Test _handle_client when auth times out."""
        server = LANWebSocketServer(port=9999)
        ws = AsyncMock()
        ws.remote_address = ("192.168.1.2", 5555)
        ws.recv.side_effect = TimeoutError()

        config = {"LAN_KEY": "secret"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
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
        with patch("hsync_client.lan_websocket.CONFIG", config):
            await server._handle_client(ws)

        ws.close.assert_called_once()
        assert ws not in server._clients

    @pytest.mark.asyncio
    async def test_handle_client_no_auth_hello_valid_cid(self):
        """Test _handle_client with hello handshake and a valid CID."""
        from hsync_common.protocol.handshake import PROTOCOL_VERSION_LAN

        server = LANWebSocketServer(port=9999)
        ws = AsyncMock()
        ws.remote_address = ("192.168.1.2", 5555)
        ws.recv.return_value = json.dumps(
            {
                "type": "hello",
                "cid": "ValidClientId12345678",
                "protocol_version": PROTOCOL_VERSION_LAN,
                "capabilities": ["compression"],
            }
        )

        async def _empty_aiter(self_ws):
            return
            yield  # noqa

        ws.__aiter__ = _empty_aiter

        config = {}  # no LAN_KEY
        with patch("hsync_client.lan_websocket.CONFIG", config):
            await server._handle_client(ws)

        calls = [c.args[0] for c in ws.send.call_args_list]
        greeting = json.loads(calls[0])
        assert greeting["type"] == "hello"
        assert greeting["protocol_version"] == PROTOCOL_VERSION_LAN
        # Client is discarded in the finally block after the async-for exits
        assert ws not in server._clients

    @pytest.mark.asyncio
    async def test_handle_client_no_auth_hello_invalid_cid(self):
        """Test _handle_client hello with malformed CID — stays 'unknown'."""
        server = LANWebSocketServer(port=9999)
        ws = AsyncMock()
        ws.remote_address = ("192.168.1.2", 5555)
        ws.recv.return_value = json.dumps({"type": "hello", "cid": "bad cid!"})

        async def _empty_aiter(self_ws):
            return
            yield  # noqa

        ws.__aiter__ = _empty_aiter

        config = {}  # no LAN_KEY
        with patch("hsync_client.lan_websocket.CONFIG", config):
            await server._handle_client(ws)

        calls = [c.args[0] for c in ws.send.call_args_list]
        assert json.loads(calls[0])["type"] == "hello"
        assert ws not in server._clients

    @pytest.mark.asyncio
    async def test_handle_client_no_auth_hello_timeout(self):
        """Test _handle_client hello times out — stays 'unknown'."""
        server = LANWebSocketServer(port=9999)
        ws = AsyncMock()
        ws.remote_address = ("192.168.1.2", 5555)
        ws.recv.side_effect = TimeoutError()

        async def _empty_aiter(self_ws):
            return
            yield  # noqa

        ws.__aiter__ = _empty_aiter

        config = {}  # no LAN_KEY
        with patch("hsync_client.lan_websocket.CONFIG", config):
            await server._handle_client(ws)

        calls = [c.args[0] for c in ws.send.call_args_list]
        assert json.loads(calls[0])["type"] == "hello"
        assert ws not in server._clients

    @pytest.mark.asyncio
    async def test_handle_client_auth_invalid_cid(self):
        """Test _handle_client rejects auth with invalid CID format."""
        from hsync_common.protocol.handshake import PROTOCOL_VERSION_LAN

        server = LANWebSocketServer(port=9999)
        ws = AsyncMock()
        ws.remote_address = ("192.168.1.2", 5555)
        ws.recv.return_value = json.dumps({"type": "auth", "token": "secret", "cid": "bad\ncid"})

        config = {"LAN_KEY": "secret"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            await server._handle_client(ws)

        calls = [c.args[0] for c in ws.send.call_args_list]
        assert json.loads(calls[1]) == {
            "type": "auth",
            "status": "failed",
            "protocol_version": PROTOCOL_VERSION_LAN,
            "reason": "invalid_cid",
        }
        ws.close.assert_called_once()
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
            yield  # unreachable – make it an async generator

        ws.__aiter__ = lambda self: async_iter_raise()

        config = {}
        with patch("hsync_client.lan_websocket.CONFIG", config):
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

        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.lan_websocket.WatcherManager") as MockWatcher,
        ):
            MockWatcher.return_value = mock_obs
            server.start(watch_path=str(tmp_path))

        assert server._running is True
        assert server._thread is not None
        assert server._watcher is mock_obs
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
        server._watcher = mock_observer
        mock_thread = MagicMock()
        server._thread = mock_thread
        server._clients = set()  # no clients to close

        server.stop()

        assert server._running is False
        mock_observer.stop.assert_called_once()
        assert server._watcher is None
        mock_thread.join.assert_called_once()
        assert server._thread is None

    def test_start_creates_missing_watch_path(self, tmp_path):
        """start() recreates the watch_path if it has been removed externally.

        Regression test: previously watchdog.Observer.schedule() raised
        FileNotFoundError when the bucket directory had been deleted between
        the previous sync and the start of the watcher, crashing LAN mode.
        """
        missing = tmp_path / "deleted_bucket"
        assert not missing.exists()

        server = LANWebSocketServer(port=9999)
        config = {"LOCAL_BUCKET": str(missing)}
        mock_obs = MagicMock()

        def fake_run_loop():
            server._loop = MagicMock()
            while server._running:
                time.sleep(0.05)

        server._run_loop = fake_run_loop

        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.lan_websocket.WatcherManager") as MockWatcher,
        ):
            MockWatcher.return_value = mock_obs
            server.start(watch_path=str(missing))

        # The directory must have been created on the fly
        assert missing.exists() and missing.is_dir()
        mock_obs.start.assert_called_once()

        # Cleanup
        server._running = False
        server._thread.join(timeout=2.0)


class TestLANPeriodicResync:
    """Tests for _lan_periodic_resync."""

    @pytest.mark.asyncio
    async def test_calls_sync_periodically(self):
        """The LAN resync loop performs a full sync at the configured interval."""
        from hsync_client.lan_websocket import CONFIG, _lan_periodic_resync

        call_count = 0

        async def mock_sync(lan_mode: bool = False):
            nonlocal call_count
            call_count += 1
            assert lan_mode is True
            if call_count >= 2:
                raise RuntimeError("Stop after 2 calls")

        with (
            patch.dict(CONFIG, {"RESYNC": 0.01}),
            patch("hsync_client.sync.sync", mock_sync),
        ):
            with contextlib.suppress(Exception):
                await _lan_periodic_resync()

        assert call_count >= 2

    @pytest.mark.asyncio
    async def test_cancellation_is_propagated(self):
        """Cancelling the resync task raises CancelledError out of the loop."""
        from hsync_client.lan_websocket import CONFIG, _lan_periodic_resync

        async def slow_sync(lan_mode: bool = False):
            return None

        with (
            patch.dict(CONFIG, {"RESYNC": 10}),
            patch("hsync_client.sync.sync", slow_sync),
        ):
            task = asyncio.create_task(_lan_periodic_resync())
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


class TestLanWebsocketClientResyncWiring:
    """Tests that lan_websocket_client wires the periodic resync task."""

    @staticmethod
    def _make_resync_factory():
        """Return a MagicMock whose calls yield a long-lived coroutine."""

        def _coro(*_args, **_kwargs):
            async def _runner() -> None:
                await asyncio.sleep(10)

            return _runner()

        return MagicMock(side_effect=_coro)

    @pytest.mark.asyncio
    async def test_starts_resync_task_when_configured(self, tmp_path):
        """A truthy RESYNC spawns the periodic resync task (cancelled on teardown)."""
        import hsync_client.lan_websocket as ws

        mock_resync = self._make_resync_factory()

        def boom(*_args, **_kwargs):
            raise RuntimeError("stop connection")

        with (
            patch.dict(ws.CONFIG, {"LOCAL_BUCKET": str(tmp_path), "RESYNC": 60}, clear=False),
            patch.object(ws, "WatcherManager", MagicMock()),
            patch.object(ws, "LANSlaveFileEventHandler", MagicMock()),
            patch.object(ws, "_lan_periodic_resync", mock_resync),
            patch("websockets.connect", side_effect=boom),
        ):
            await ws.lan_websocket_client("master.local", 9000)

        mock_resync.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_resync_task_when_disabled(self, tmp_path):
        """RESYNC of 0 leaves the periodic resync task unstarted."""
        import hsync_client.lan_websocket as ws

        mock_resync = MagicMock()

        def boom(*_args, **_kwargs):
            raise RuntimeError("stop connection")

        with (
            patch.dict(ws.CONFIG, {"LOCAL_BUCKET": str(tmp_path), "RESYNC": 0}, clear=False),
            patch.object(ws, "WatcherManager", MagicMock()),
            patch.object(ws, "LANSlaveFileEventHandler", MagicMock()),
            patch.object(ws, "_lan_periodic_resync", mock_resync),
            patch("websockets.connect", side_effect=boom),
        ):
            await ws.lan_websocket_client("master.local", 9000)

        mock_resync.assert_not_called()


class TestLanWebsocketClientGreetingValidation:
    """The slave refuses connection on a protocol-violating greeting.

    The LAN handshake contract knows exactly two greeting frames:
    ``auth_required`` (with a LAN key) and ``hello`` (without). Any other
    greeting type is a protocol violation and must NOT degrade to a silent
    unauthenticated session.
    """

    @pytest.mark.asyncio
    async def test_unexpected_greeting_refuses_connection(self, tmp_path):
        import hsync_client.lan_websocket as ws

        sent: list[str] = []

        class _UnexpectedGreetingSocket:
            async def recv(self) -> str:
                return json.dumps({"type": "not-a-greeting"})

            async def send(self, message: str) -> None:
                sent.append(message)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

        with (
            patch.dict(
                ws.CONFIG,
                {
                    "LOCAL_BUCKET": str(tmp_path),
                    "RESYNC": 0,
                    "CID": "ValidClientId12345678",
                },
                clear=False,
            ),
            patch.object(ws, "WatcherManager", MagicMock()),
            patch.object(ws, "LANSlaveFileEventHandler", MagicMock()),
            patch("websockets.connect", return_value=_UnexpectedGreetingSocket()),
        ):
            await ws.lan_websocket_client("master.local", 9000)

        # No frame is sent back: the connection is refused outright instead of
        # falling through to an unauthenticated session.
        assert sent == []


class TestSlaveCidRegistry:
    """Unit tests for the shared duplicate-identity registry."""

    def test_reserve_and_release(self):
        from hsync_client.lan_websocket import SlaveCidRegistry

        reg = SlaveCidRegistry(master_cid="m" * 21)
        ws = object()
        allowed, stale = reg.try_reserve("a" * 21, "192.168.1.5", ws)
        assert allowed is True
        assert stale is None
        assert reg.request_conflict("a" * 21, "192.168.1.5") is False
        assert reg.request_conflict("a" * 21, "192.168.1.6") is True
        reg.release("a" * 21, ws)
        assert reg.request_conflict("a" * 21, "192.168.1.6") is False

    def test_different_ip_conflict(self):
        from hsync_client.lan_websocket import SlaveCidRegistry

        reg = SlaveCidRegistry()
        reg.try_reserve("a" * 21, "192.168.1.5", object())
        allowed, stale = reg.try_reserve("a" * 21, "192.168.1.9", object())
        assert allowed is False
        assert stale is None

    def test_same_ip_replaces_binding(self):
        from hsync_client.lan_websocket import SlaveCidRegistry

        reg = SlaveCidRegistry()
        old_ws = object()
        new_ws = object()
        allowed, stale = reg.try_reserve("a" * 21, "192.168.1.5", old_ws)
        assert (allowed, stale) == (True, None)
        allowed2, stale2 = reg.try_reserve("a" * 21, "192.168.1.5", new_ws)
        assert allowed2 is True
        assert stale2 is old_ws
        # The old binding's release must not evict the new one.
        reg.release("a" * 21, old_ws)
        assert reg.request_conflict("a" * 21, "192.168.1.6") is True
        reg.release("a" * 21, new_ws)
        assert reg.request_conflict("a" * 21, "192.168.1.6") is False

    def test_release_reports_ownership(self):
        """release returns True only when the caller is the current owner.

        Regression: a superseded connection (same-address reconnect) must not
        clear the WebSocket liveness mark of a CID the replacement still holds.
        """
        from hsync_client.lan_websocket import SlaveCidRegistry

        reg = SlaveCidRegistry()
        old_ws = object()
        new_ws = object()
        reg.try_reserve("a" * 21, "192.168.1.5", old_ws)
        # Same-address reconnect replaces the binding.
        reg.try_reserve("a" * 21, "192.168.1.5", new_ws)
        assert reg.release("a" * 21, old_ws) is False
        assert reg.release("a" * 21, new_ws) is True
        assert reg.release("a" * 21, new_ws) is False

    def test_connected_cids_snapshot(self):
        from hsync_client.lan_websocket import SlaveCidRegistry

        reg = SlaveCidRegistry()
        assert reg.connected_cids() == set()
        reg.try_reserve("a" * 21, "192.168.1.5", object())
        reg.try_reserve("b" * 21, "192.168.1.6", object())
        assert reg.connected_cids() == {"a" * 21, "b" * 21}
        reg.clear()
        assert reg.connected_cids() == set()

    def test_master_cid_never_reserved(self):
        from hsync_client.lan_websocket import SlaveCidRegistry

        reg = SlaveCidRegistry(master_cid="m" * 21)
        allowed, stale = reg.try_reserve("m" * 21, "192.168.1.5", object())
        assert allowed is False
        assert stale is None
        assert reg.request_conflict("m" * 21, "192.168.1.5") is True

    def test_ipv4_mapped_normalization(self):
        from hsync_client.lan_websocket import SlaveCidRegistry

        reg = SlaveCidRegistry()
        assert reg.normalize_ip("::ffff:192.168.1.5") == "192.168.1.5"
        assert reg.normalize_ip("192.168.1.5") == "192.168.1.5"
        assert reg.normalize_ip(None) == ""
        # Same logical host via the two encodings is not a conflict.
        reg.try_reserve("a" * 21, "::ffff:192.168.1.5", object())
        allowed, _ = reg.try_reserve("a" * 21, "192.168.1.5", object())
        assert allowed is True

    def test_should_report_once_per_signature(self):
        from hsync_client.lan_websocket import SlaveCidRegistry

        reg = SlaveCidRegistry()
        assert reg.should_report("a" * 21, "192.168.1.5") is True
        assert reg.should_report("a" * 21, "192.168.1.5") is False
        assert reg.should_report("a" * 21, "192.168.1.6") is True

    def test_clear_drops_bindings(self):
        from hsync_client.lan_websocket import SlaveCidRegistry

        reg = SlaveCidRegistry()
        reg.try_reserve("a" * 21, "192.168.1.5", object())
        reg.clear()
        assert reg.request_conflict("a" * 21, "192.168.1.9") is False
