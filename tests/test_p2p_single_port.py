"""
Regression tests for the single-port P2P transport.

The unified listener serves HTTPS and WSS on the same port: one bind succeeds
or fails as a unit, so a master can never advertise readiness while its realtime
channel is dead. These tests pin the invariants: one port serves both planes, a
bind failure is fatal, the advertised port is the bound one, and a yielded
master refuses the realtime upgrade.
"""

import asyncio
import json
import socket
from unittest.mock import patch

import pytest
from aiohttp import WSMsgType, WSServerHandshakeError

from hsync_client.p2p_server import P2PServer, P2PServerMode
from hsync_client.p2p_websocket import P2PWebSocketServer, SlaveCidRegistry

CID = "c" * 21
CONFIG = {"P2P_KEY": None, "P2P_PEER_DOWNLOAD": False}


def _start(*, port: int = 0, use_https: bool = False) -> tuple[P2PServer, P2PWebSocketServer]:
    registry = SlaveCidRegistry(master_cid="m" * 21)
    ws = P2PWebSocketServer(port=port, cid_registry=registry)
    server = P2PServer(port=port, use_https=use_https, cid_guard=registry, websocket_server=ws)
    server.start()
    return server, ws


def _ws_url(server: P2PServer) -> str:
    return f"http://127.0.0.1:{server.port}/api/v1/buckets/bucket/events/{CID}"


@pytest.mark.asyncio
async def test_http_and_ws_share_one_port():
    """The REST data plane and the realtime channel answer on the same port."""
    import aiohttp

    server, _ws = _start()
    try:
        with patch("hsync_client.p2p_server.CONFIG", CONFIG):
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{server.port}/api/v1/health") as resp:
                    assert resp.status == 200
                async with session.ws_connect(_ws_url(server)) as ws:
                    assert (await ws.receive()).type.name == "TEXT"
    finally:
        server.stop()


def test_advertised_port_is_the_bound_port():
    """port=0 resolves to the real bound port, not the requested 0."""
    server, _ws = _start()
    try:
        assert server.port > 0
        assert server.websocket_server is not None
        # The WS control plane reports the same unified port.
        assert server.websocket_server.port == server.port
    finally:
        server.stop()


def test_bind_failure_is_fatal():
    """A taken port makes start() raise instead of serving silently."""
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("0.0.0.0", 0))
    occupied.listen(1)
    port = occupied.getsockname()[1]
    registry = SlaveCidRegistry(master_cid="m" * 21)
    ws = P2PWebSocketServer(port=port, cid_registry=registry)
    server = P2PServer(port=port, cid_guard=registry, websocket_server=ws)
    try:
        server.start()
    except OSError:
        # Bind failed before readiness: the worker pool must be released (no
        # leak) so a retry or a fatal exit does not strand an executor.
        assert server._executor is None
    else:
        server.stop()
        raise AssertionError("start() must raise when the port is already in use")
    finally:
        occupied.close()


@pytest.mark.asyncio
async def test_yielded_master_refuses_new_realtime_upgrade():
    """After a yield the listener stays up but the realtime upgrade is refused."""
    import aiohttp

    server, _ws = _start()
    try:
        with patch("hsync_client.p2p_server.CONFIG", CONFIG):
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(_ws_url(server)):
                    pass
                # Yield: the listener is up but the role is retired.
                server.stop_serving()
                with pytest.raises(WSServerHandshakeError) as excinfo:
                    await session.ws_connect(_ws_url(server))
                assert excinfo.value.status == 503
    finally:
        server.stop()


def test_mode_is_derived_from_the_serving_context():
    """A yielded listener reports no role: ``mode`` is never stale (ADR-0027)."""
    server = P2PServer(port=0, cid_guard=SlaveCidRegistry(master_cid="m" * 21))
    try:
        assert server.mode is P2PServerMode.master
        server.stop_serving()
        assert server.mode is None
    finally:
        server.stop()


@pytest.mark.asyncio
async def test_yield_closes_live_realtime_clients():
    """A yielded master drops its live WebSocket clients (no zombie events)."""
    import aiohttp
    from hsync_common.protocol.handshake import build_p2p_hello

    server, _ws = _start()
    try:
        with patch("hsync_client.p2p_server.CONFIG", CONFIG):
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(_ws_url(server)) as ws:
                    assert (await ws.receive()).type is WSMsgType.TEXT
                    await ws.send_str(json.dumps(build_p2p_hello(capabilities=[], cid=CID)))
                    for _ in range(50):
                        if server.websocket_server.connected_clients == 1:
                            break
                        await asyncio.sleep(0.05)
                    assert server.websocket_server.connected_clients == 1

                    server.stop_serving()
                    msg = await asyncio.wait_for(ws.receive(), timeout=5)
                    assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED)
    finally:
        server.stop()


@pytest.mark.asyncio
async def test_stop_p2p_serving_retires_role_before_dropping_clients():
    """The production yield path (`_stop_p2p_serving`) closes live clients."""
    import aiohttp
    from hsync_common.protocol.handshake import build_p2p_hello

    from hsync_client.p2p_handler import _stop_p2p_serving

    server, ws = _start()
    try:
        with patch("hsync_client.p2p_server.CONFIG", CONFIG):
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(_ws_url(server)) as conn:
                    assert (await conn.receive()).type is WSMsgType.TEXT
                    await conn.send_str(json.dumps(build_p2p_hello(capabilities=[], cid=CID)))
                    for _ in range(50):
                        if ws.connected_clients == 1:
                            break
                        await asyncio.sleep(0.05)
                    assert ws.connected_clients == 1

                    _stop_p2p_serving(ws, server)

                    # ADR-0027: the role is retired and the live realtime client
                    # is dropped (never served through the retired role).
                    assert server._context() is None
                    msg = await asyncio.wait_for(conn.receive(), timeout=5)
                    assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED)
    finally:
        server.stop()


@pytest.mark.asyncio
async def test_seeder_mode_serves_on_the_same_listener():
    """A production-shaped seeder (no WS attached) refuses the WS upgrade."""
    import aiohttp

    from hsync_client.p2p_server import P2PServerMode

    registry = SlaveCidRegistry(master_cid="m" * 21)
    server = P2PServer(
        port=0,
        mode=P2PServerMode.seeder,
        bucket="bucket",
        cid_guard=registry,
    )
    server.start()
    try:
        with patch("hsync_client.p2p_server.CONFIG", CONFIG):
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{server.port}/api/v1/health") as resp:
                    assert resp.status == 200
                # The seeder never exposes the realtime control plane: the route
                # is registered but answers 503 (not a 404 from the REST router).
                with pytest.raises(WSServerHandshakeError) as excinfo:
                    await session.ws_connect(_ws_url(server))
                assert excinfo.value.status == 503
    finally:
        server.stop()


@pytest.mark.asyncio
async def test_ws_route_accepts_the_canonical_path_only():
    """A wrong WS path is not upgraded (it falls through to the REST router)."""
    import aiohttp

    server, _ws = _start()
    try:
        with patch("hsync_client.p2p_server.CONFIG", CONFIG):
            async with aiohttp.ClientSession() as session:
                with pytest.raises(WSServerHandshakeError):
                    await session.ws_connect(f"http://127.0.0.1:{server.port}/not-the-events-path")
    finally:
        server.stop()


@pytest.mark.asyncio
async def test_concurrent_http_and_ws_do_not_starve():
    """The bounded executor and the WS loop coexist on one listener."""
    import aiohttp

    server, _ws = _start()
    try:
        with patch("hsync_client.p2p_server.CONFIG", CONFIG):
            async with aiohttp.ClientSession() as session:

                async def _health() -> int:
                    async with session.get(f"http://127.0.0.1:{server.port}/api/v1/health") as resp:
                        return resp.status

                async with session.ws_connect(_ws_url(server)):
                    statuses = await asyncio.gather(*[_health() for _ in range(5)])
                    assert statuses == [200] * 5
    finally:
        server.stop()


class _RecordingClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_str(self, message: str) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_publishing_gate_blocks_events_while_yielded():
    """A retired role pushes no event even before its clients are closed."""
    ws = P2PWebSocketServer(port=0, cid_registry=SlaveCidRegistry(master_cid="m" * 21))
    client = _RecordingClient()
    ws._clients.add(client)

    ws.set_publishing(True)
    await ws.broadcast({"name": "file.created"})
    assert len(client.sent) == 1

    ws.set_publishing(False)
    await ws.broadcast({"name": "file.created"})
    assert len(client.sent) == 1


@pytest.mark.asyncio
async def test_broadcast_stops_when_role_retired_mid_flight():
    """A retirement landing during a broadcast stops the remaining pushes.

    Both recording clients retire the role on send, so whichever is iterated
    first flips the epoch and the loop must stop: exactly one delivery total.
    """
    ws = P2PWebSocketServer(port=0, cid_registry=SlaveCidRegistry(master_cid="m" * 21))
    first, second = _RecordingClient(), _RecordingClient()
    ws._clients.update({first, second})
    ws.set_publishing(True)

    async def send_and_retire(client: _RecordingClient, message: str) -> None:
        client.sent.append(message)
        ws.set_publishing(False)

    first.send_str = lambda message: send_and_retire(first, message)  # type: ignore[method-assign]
    second.send_str = lambda message: send_and_retire(second, message)  # type: ignore[method-assign]

    await ws.broadcast({"name": "file.created"})

    assert len(first.sent) + len(second.sent) == 1
