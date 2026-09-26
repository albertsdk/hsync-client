"""
Tests for the P2P realtime WebSocket control plane.

The control plane is no longer a standalone ``websockets`` server on a second
port: it is a route on the unified HTTPS/WSS listener. These tests drive the
real route over an in-process :class:`P2PServer`, so they exercise the same
handshake the peers use instead of a mock socket.
"""

import asyncio
import json
from unittest.mock import patch

import pytest
from aiohttp import WSMsgType
from hsync_common.protocol.handshake import (
    PROTOCOL_VERSION_P2P,
    Capability,
    build_p2p_auth,
    build_p2p_hello,
)

from hsync_client.p2p_server import P2PServer
from hsync_client.p2p_websocket import P2PWebSocketServer, SlaveCidRegistry

MASTER_CID = "m" * 21
SLAVE_CID = "s" * 21


def _start(*, key: str | None = None, master_cid: str = MASTER_CID):
    registry = SlaveCidRegistry(master_cid=master_cid)
    ws = P2PWebSocketServer(port=0, master_cid=master_cid, cid_registry=registry)
    server = P2PServer(port=0, use_https=False, cid_guard=registry, websocket_server=ws)
    server.start()
    return server, ws


def _config(key: str | None) -> dict:
    return {"P2P_KEY": key, "P2P_PEER_DOWNLOAD": False}


def _url(server: P2PServer, cid: str = SLAVE_CID) -> str:
    return f"http://127.0.0.1:{server.port}/api/v1/buckets/bucket/events/{cid}"


async def _recv_json(ws, timeout: float = 10.0) -> dict:
    msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
    assert msg.type is WSMsgType.TEXT, msg
    return json.loads(msg.data)


class TestConstruction:
    def test_server_creation(self):
        server = P2PWebSocketServer(port=9999)
        assert server.port == 9999
        assert server.running is False

    def test_server_creation_with_https(self):
        # The control plane shares the listener's TLS; it carries no TLS state.
        server = P2PWebSocketServer(port=9999)
        assert server.port == 9999

    def test_server_initial_state(self):
        server = P2PWebSocketServer(port=9999)
        assert server._clients == set()
        assert server._loop is None
        assert server._running is False
        assert server._watcher is None
        assert server._event_handler is None
        assert server.connected_clients == 0

    def test_start_requires_loop(self):
        """The control plane shares the unified listener's loop."""
        server = P2PWebSocketServer(port=9999)
        with pytest.raises(RuntimeError):
            server.start()

    @staticmethod
    def test_peer_ip():
        assert P2PWebSocketServer._peer_ip(("1.2.3.4", 5)) == "1.2.3.4"
        assert P2PWebSocketServer._peer_ip("1.2.3.4") == "1.2.3.4"

    @staticmethod
    def test_resolve_serve_slots():
        assert P2PWebSocketServer._resolve_serve_slots(None, set()) == (None, None)
        assert P2PWebSocketServer._resolve_serve_slots("x", set()) == (
            None,
            "invalid_serve_slots",
        )
        assert P2PWebSocketServer._resolve_serve_slots(3, set()) == (
            None,
            "serve_slots_without_peer_download",
        )
        assert P2PWebSocketServer._resolve_serve_slots(3, {Capability.peer_download}) == (3, None)


@pytest.mark.asyncio
class TestHandshake:
    async def test_hello_handshake_without_key(self):
        server, _ws = _start(key=None)
        try:
            with (
                patch("hsync_client.p2p_websocket.CONFIG", _config(None)),
                patch("hsync_client.p2p_server.CONFIG", _config(None)),
            ):
                import aiohttp

                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(_url(server)) as ws:
                        greeting = await _recv_json(ws)
                        assert greeting["type"] == "hello"
                        assert greeting["protocol_version"] == PROTOCOL_VERSION_P2P
                        await ws.send_str(
                            json.dumps(build_p2p_hello(capabilities=[], cid=SLAVE_CID))
                        )
                        # The hello path is admitted without a reply.
                        for _ in range(50):
                            if server.websocket_server.connected_clients == 1:
                                break
                            await asyncio.sleep(0.05)
                        assert server.websocket_server.connected_clients == 1
        finally:
            server.stop()

    async def test_auth_required_success(self):
        server, _ws = _start(key="secret")
        try:
            with (
                patch("hsync_client.p2p_websocket.CONFIG", _config("secret")),
                patch("hsync_client.p2p_server.CONFIG", _config("secret")),
            ):
                import aiohttp

                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(_url(server)) as ws:
                        greeting = await _recv_json(ws)
                        assert greeting["type"] == "auth_required"
                        await ws.send_str(
                            json.dumps(
                                build_p2p_auth(token="secret", cid=SLAVE_CID, capabilities=[])
                            )
                        )
                        reply = await _recv_json(ws)
                        assert reply["type"] == "auth"
                        assert reply["status"] == "ok"
        finally:
            server.stop()

    async def test_auth_bad_token(self):
        server, _ws = _start(key="secret")
        try:
            with (
                patch("hsync_client.p2p_websocket.CONFIG", _config("secret")),
                patch("hsync_client.p2p_server.CONFIG", _config("secret")),
            ):
                import aiohttp

                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(_url(server)) as ws:
                        assert (await _recv_json(ws))["type"] == "auth_required"
                        await ws.send_str(
                            json.dumps(
                                build_p2p_auth(token="wrong", cid=SLAVE_CID, capabilities=[])
                            )
                        )
                        reply = await _recv_json(ws)
                        assert reply["status"] == "failed"
                        assert reply["reason"] == "invalid_token"
        finally:
            server.stop()

    async def test_auth_protocol_mismatch(self):
        server, _ws = _start(key="secret")
        try:
            with (
                patch("hsync_client.p2p_websocket.CONFIG", _config("secret")),
                patch("hsync_client.p2p_server.CONFIG", _config("secret")),
            ):
                import aiohttp

                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(_url(server)) as ws:
                        assert (await _recv_json(ws))["type"] == "auth_required"
                        frame = build_p2p_auth(token="secret", cid=SLAVE_CID, capabilities=[])
                        frame["protocol_version"] = "lan/999"
                        await ws.send_str(json.dumps(frame))
                        reply = await _recv_json(ws)
                        assert reply["reason"] == "protocol_incompatible"
        finally:
            server.stop()

    async def test_auth_invalid_cid(self):
        server, _ws = _start(key="secret")
        try:
            with (
                patch("hsync_client.p2p_websocket.CONFIG", _config("secret")),
                patch("hsync_client.p2p_server.CONFIG", _config("secret")),
            ):
                import aiohttp

                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(_url(server, cid="nope")) as ws:
                        assert (await _recv_json(ws))["type"] == "auth_required"
                        frame = build_p2p_auth(token="secret", cid="nope", capabilities=[])
                        await ws.send_str(json.dumps(frame))
                        reply = await _recv_json(ws)
                        assert reply["reason"] == "invalid_cid"
        finally:
            server.stop()

    async def test_duplicate_master_cid_refused(self):
        server, _ws = _start(key="secret")
        try:
            with (
                patch("hsync_client.p2p_websocket.CONFIG", _config("secret")),
                patch("hsync_client.p2p_server.CONFIG", _config("secret")),
            ):
                import aiohttp

                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(_url(server, cid=MASTER_CID)) as ws:
                        assert (await _recv_json(ws))["type"] == "auth_required"
                        await ws.send_str(
                            json.dumps(
                                build_p2p_auth(token="secret", cid=MASTER_CID, capabilities=[])
                            )
                        )
                        reply = await _recv_json(ws)
                        assert reply["reason"] == "cid_conflict"
        finally:
            server.stop()

    async def test_disconnect_clears_client(self):
        server, _ws = _start(key=None)
        try:
            with (
                patch("hsync_client.p2p_websocket.CONFIG", _config(None)),
                patch("hsync_client.p2p_server.CONFIG", _config(None)),
            ):
                import aiohttp

                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(_url(server)) as ws:
                        assert (await _recv_json(ws))["type"] == "hello"
                        await ws.send_str(
                            json.dumps(build_p2p_hello(capabilities=[], cid=SLAVE_CID))
                        )
                        for _ in range(50):
                            if server.websocket_server.connected_clients == 1:
                                break
                            await asyncio.sleep(0.05)
                        assert server.websocket_server.connected_clients == 1

                for _ in range(60):
                    if server.websocket_server.connected_clients == 0:
                        break
                    await asyncio.sleep(0.05)
                assert server.websocket_server.connected_clients == 0
        finally:
            server.stop()


@pytest.mark.asyncio
class TestBroadcast:
    async def test_broadcast_no_clients(self):
        ws = P2PWebSocketServer(port=9999)
        await ws.broadcast({"name": "test"})

    async def test_broadcast_sync_without_loop_is_noop(self):
        ws = P2PWebSocketServer(port=9999)
        ws.broadcast_sync({"name": "test"})
