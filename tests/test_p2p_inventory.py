# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Focused state-machine and publisher tests for P2P peer inventory."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import WSMsgType
from hsync_common.models.file import File
from hsync_common.protocol.handshake import build_p2p_hello
from hsync_common.protocol.peer_download import (
    build_p2p_inventory_ack,
    build_p2p_inventory_frame,
)

from hsync_client.p2p_inventory import P2PInventoryPublisher, P2PInventoryStore
from hsync_client.p2p_server import P2PServer
from hsync_client.p2p_websocket import P2PWebSocketServer

CID_A = "A" * 21
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def _query(path: str, digest: str) -> list[dict[str, str]]:
    return [{"path": path, "sha256": digest}]


async def _wait_for_frames(websocket: AckingWebSocket, count: int) -> None:
    async with asyncio.timeout(2):
        while len(websocket.frames) < count:
            await asyncio.sleep(0)


class AckingWebSocket:
    """Single-reader test transport that returns ACKs through ``handle_ack``."""

    def __init__(self, publisher: P2PInventoryPublisher) -> None:
        self.publisher = publisher
        self.frames: list[dict] = []
        self.closed = False

    async def send(self, message: str) -> None:
        frame = json.loads(message)
        self.frames.append(frame)
        self.publisher.handle_ack(
            build_p2p_inventory_ack(
                gen=frame["gen"],
                seq=frame["seq"],
                status="ok",
            )
        )

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


class SilentWebSocket:
    """Transport that never ACKs: reproduces a backlogged master receiver."""

    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.closed = False
        self.closed_code: int | None = None

    async def send(self, message: str) -> None:
        self.frames.append(json.loads(message))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.closed_code = code


def test_legacy_volatile_schema_is_recreated(tmp_path: Path) -> None:
    database = tmp_path / "inventory.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE inventory_sessions (cid TEXT PRIMARY KEY)")
    connection.execute("PRAGMA user_version = 0")
    connection.commit()
    connection.close()

    store = P2PInventoryStore(database, "bucket")
    token = store.begin_session(CID_A, serve_slots=2)
    snapshot = build_p2p_inventory_frame(
        gen=1,
        seq=0,
        mode="snapshot",
        more=False,
        entries=[{"path": "one.txt", "sha256": DIGEST_A, "size": 1}],
    )
    assert store.apply_frame(CID_A, token, snapshot)["status"] == "ok"
    store.close()


def test_store_commits_snapshot_atomically_and_applies_idempotent_delta(tmp_path: Path) -> None:
    store = P2PInventoryStore(tmp_path / "inventory.sqlite3", "bucket")
    store.activate()
    token = store.begin_session(CID_A, serve_slots=2)

    first = build_p2p_inventory_frame(
        gen=1,
        seq=0,
        mode="snapshot",
        more=True,
        entries=[{"path": "one.txt", "sha256": DIGEST_A, "size": 1}],
    )
    assert store.apply_frame(CID_A, token, first)["status"] == "ok"
    assert store.holders_for(_query("one.txt", DIGEST_A))[("one.txt", DIGEST_A)] == ()

    terminal = build_p2p_inventory_frame(
        gen=1,
        seq=1,
        mode="snapshot",
        more=False,
        entries=[{"path": "two.txt", "sha256": DIGEST_B, "size": 2}],
    )
    assert store.apply_frame(CID_A, token, terminal)["status"] == "ok"
    assert [
        h.cid for h in store.holders_for(_query("one.txt", DIGEST_A))[("one.txt", DIGEST_A)]
    ] == [CID_A]

    delta = build_p2p_inventory_frame(
        gen=1,
        seq=2,
        mode="delta",
        more=False,
        entries=[
            {"path": "one.txt", "sha256": DIGEST_B, "size": 3},
            {"path": "two.txt", "deleted": True},
        ],
    )
    assert store.apply_frame(CID_A, token, delta)["status"] == "ok"
    assert store.apply_frame(CID_A, token, delta)["status"] == "ok"
    assert store.holders_for(_query("one.txt", DIGEST_A))[("one.txt", DIGEST_A)] == ()
    holders = store.holders_for(_query("one.txt", DIGEST_B))[("one.txt", DIGEST_B)]
    assert [(h.cid, h.size) for h in holders] == [(CID_A, 3)]

    gap = build_p2p_inventory_frame(
        gen=1,
        seq=4,
        mode="delta",
        more=False,
        entries=[{"path": "three.txt", "sha256": DIGEST_A, "size": 1}],
    )
    assert store.apply_frame(CID_A, token, gap)["status"] == "resync"
    assert store.holders_for(_query("one.txt", DIGEST_B))[("one.txt", DIGEST_B)]
    store.close()


def test_duplicate_terminal_snapshot_requires_identical_payload(tmp_path: Path) -> None:
    store = P2PInventoryStore(tmp_path / "inventory.sqlite3", "bucket")
    store.activate()
    token = store.begin_session(CID_A, serve_slots=2)
    terminal = build_p2p_inventory_frame(
        gen=1,
        seq=0,
        mode="snapshot",
        more=False,
        entries=[{"path": "one.txt", "sha256": DIGEST_A, "size": 1}],
    )
    assert store.apply_frame(CID_A, token, terminal)["status"] == "ok"
    assert store.apply_frame(CID_A, token, terminal)["status"] == "ok"

    altered = build_p2p_inventory_frame(
        gen=1,
        seq=0,
        mode="snapshot",
        more=False,
        entries=[{"path": "other.txt", "sha256": DIGEST_B, "size": 2}],
    )
    assert store.apply_frame(CID_A, token, altered)["status"] == "resync"
    assert store.holders_for(_query("one.txt", DIGEST_A))[("one.txt", DIGEST_A)]
    store.close()


def test_store_quota_counts_only_net_delta_rows_and_rejection_preserves_commit(
    tmp_path: Path,
) -> None:
    store = P2PInventoryStore(
        tmp_path / "inventory.sqlite3",
        "bucket",
        per_cid_quota=1,
        global_quota=1,
    )
    store.activate()
    token = store.begin_session(CID_A, serve_slots=2)
    snapshot = build_p2p_inventory_frame(
        gen=1,
        seq=0,
        mode="snapshot",
        more=False,
        entries=[{"path": "one.txt", "sha256": DIGEST_A, "size": 1}],
    )
    assert store.apply_frame(CID_A, token, snapshot)["status"] == "ok"

    replacement = build_p2p_inventory_frame(
        gen=1,
        seq=1,
        mode="delta",
        more=False,
        entries=[{"path": "one.txt", "sha256": DIGEST_B, "size": 2}],
    )
    assert store.apply_frame(CID_A, token, replacement)["status"] == "ok"

    overflow = build_p2p_inventory_frame(
        gen=1,
        seq=2,
        mode="delta",
        more=False,
        entries=[{"path": "two.txt", "sha256": DIGEST_A, "size": 1}],
    )
    rejected = store.apply_frame(CID_A, token, overflow)
    assert rejected["status"] == "rejected"
    assert rejected["reason"] == "quota"
    assert store.holders_for(_query("one.txt", DIGEST_B))[("one.txt", DIGEST_B)] == ()
    assert store.has_session(CID_A) is False
    assert store.row_counts() == (1, 0, 1)
    assert store.apply_frame(CID_A, token, replacement)["reason"] == "session_disabled"
    store.close()


def test_old_owner_disconnect_cannot_purge_reconnected_session(tmp_path: Path) -> None:
    store = P2PInventoryStore(tmp_path / "inventory.sqlite3", "bucket")
    store.activate()
    old_token = store.begin_session(CID_A, serve_slots=2, token="old-owner")
    snapshot = build_p2p_inventory_frame(
        gen=1,
        seq=0,
        mode="snapshot",
        more=False,
        entries=[{"path": "one.txt", "sha256": DIGEST_A, "size": 1}],
    )
    assert store.apply_frame(CID_A, old_token, snapshot)["status"] == "ok"

    new_token = store.begin_session(CID_A, serve_slots=2, token="new-owner")
    assert store.end_session(CID_A, old_token) is False
    assert store.has_session(CID_A)
    assert store.holders_for(_query("one.txt", DIGEST_A))[("one.txt", DIGEST_A)]
    assert store.apply_frame(CID_A, old_token, snapshot)["reason"] == "old_owner"
    assert store.end_session(CID_A, new_token) is True
    assert store.holders_for(_query("one.txt", DIGEST_A))[("one.txt", DIGEST_A)] == ()
    store.close()


@pytest.mark.asyncio
async def test_ack_timeout_stops_publisher_without_closing_channel(tmp_path: Path) -> None:
    """Regression: an unacknowledged frame must not tear down the WSS channel.

    Under a burst the master's receiver can be backlogged for tens of seconds;
    the old behaviour closed the websocket with 1011 after one ACK timeout,
    which killed the event channel carried by the same connection and forced a
    full resync on both peers. The publisher now degrades to "stopped" (its
    master-side session expires via TTL) while the channel stays open.
    """
    (tmp_path / "file.txt").write_text("content")
    publisher = P2PInventoryPublisher(tmp_path, ack_timeout_s=0.05)
    websocket = SilentWebSocket()
    task = publisher.start(websocket)

    with contextlib.suppress(asyncio.TimeoutError):
        async with asyncio.timeout(2):
            await task

    assert websocket.closed is False, "an ACK timeout must not close the WSS channel"
    assert publisher._disabled is True
    assert len(websocket.frames) >= 2  # snapshot terminal sent, resent once
    await publisher.stop()


@pytest.mark.asyncio
async def test_publisher_uses_contiguous_sequences_across_multi_frame_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hsync_client.p2p_inventory as inventory_module

    monkeypatch.setattr(inventory_module, "P2P_INVENTORY_MAX_ENTRIES", 2)
    publisher = P2PInventoryPublisher(tmp_path)
    publisher.replace_snapshot([])
    websocket = AckingWebSocket(publisher)
    publisher.start(websocket)
    await _wait_for_frames(websocket, 1)

    for index in range(3):
        path = tmp_path / f"file-{index}.txt"
        path.write_text(str(index))
        file = File.get(str(path))
        assert file is not None
        publisher.upsert(file)

    await _wait_for_frames(websocket, 3)
    delta_frames = [frame for frame in websocket.frames if frame["mode"] == "delta"]
    assert [frame["seq"] for frame in delta_frames] == [1, 2]
    assert [len(frame["entries"]) for frame in delta_frames] == [2, 1]
    await publisher.stop()


@pytest.mark.asyncio
async def test_publisher_directory_rename_uses_component_prefix(tmp_path: Path) -> None:
    src = tmp_path / "docs"
    src.mkdir()
    file_path = src / "readme.txt"
    file_path.write_text("content")
    file = File.get(str(file_path))
    assert file is not None

    publisher = P2PInventoryPublisher(tmp_path)
    publisher.replace_snapshot([file])
    websocket = AckingWebSocket(publisher)
    publisher.start(websocket)
    await _wait_for_frames(websocket, 1)

    dst = tmp_path / "archive"
    src.rename(dst)
    publisher.rename_prefix(str(src), str(dst))
    await _wait_for_frames(websocket, 2)
    entries = websocket.frames[-1]["entries"]
    assert {entry["path"] for entry in entries} == {
        "docs/readme.txt",
        "archive/readme.txt",
    }
    assert all("\\" not in entry["path"] for entry in entries)
    await publisher.stop()


@pytest.mark.asyncio
async def test_master_wss_routes_inventory_to_store_and_returns_ack(tmp_path: Path) -> None:
    store = P2PInventoryStore(tmp_path / "inventory.sqlite3", "bucket")
    store.activate()
    ws_server = P2PWebSocketServer(port=0, inventory_store=store)
    server = P2PServer(port=0, use_https=False, websocket_server=ws_server)
    server.start()
    frame = build_p2p_inventory_frame(
        gen=1,
        seq=0,
        mode="snapshot",
        more=False,
        entries=[{"path": "one.txt", "sha256": DIGEST_A, "size": 1}],
    )

    import aiohttp

    try:
        with patch("hsync_client.p2p_websocket.CONFIG", {"P2P_KEY": None}):
            async with aiohttp.ClientSession() as session:
                url = f"http://127.0.0.1:{server.port}/api/v1/buckets/bucket/events/{CID_A}"
                async with session.ws_connect(url) as ws:
                    greeting = json.loads((await ws.receive()).data)
                    assert greeting["type"] == "hello"
                    await ws.send_str(
                        json.dumps(
                            build_p2p_hello(
                                capabilities=["peer_download"],
                                cid=CID_A,
                                serve_slots=2,
                            )
                        )
                    )
                    await ws.send_str(json.dumps(frame))
                    ack = json.loads((await asyncio.wait_for(ws.receive(), timeout=10)).data)
                    assert ack == {"type": "inventory_ack", "gen": 1, "seq": 0, "status": "ok"}
                    assert store.has_session(CID_A)
            for _ in range(60):
                if not store.has_session(CID_A):
                    break
                await asyncio.sleep(0.05)
            assert not store.has_session(CID_A)
    finally:
        server.stop()
        store.close()


@pytest.mark.asyncio
async def test_wss_reconnect_installs_new_inventory_owner_before_closing_old(
    tmp_path: Path,
) -> None:
    store = P2PInventoryStore(tmp_path / "inventory.sqlite3", "bucket")
    store.activate()
    server = P2PWebSocketServer(port=9999, inventory_store=store)
    old_websocket = AsyncMock()
    new_websocket = AsyncMock()
    address = ("192.168.1.20", 5000)

    admitted, old_token, _ = await server._admit_slave(
        CID_A,
        address,
        old_websocket,
        authenticated=True,
        serve_slots=2,
    )
    assert admitted and old_token is not None
    frame = build_p2p_inventory_frame(
        gen=1,
        seq=0,
        mode="snapshot",
        more=False,
        entries=[{"path": "one.txt", "sha256": DIGEST_A, "size": 1}],
    )
    assert store.apply_frame(CID_A, old_token, frame)["status"] == "ok"

    admitted, new_token, _ = await server._admit_slave(
        CID_A,
        address,
        new_websocket,
        authenticated=True,
        serve_slots=2,
    )
    assert admitted and new_token is not None and new_token != old_token
    old_websocket.close.assert_awaited_once()
    assert store.end_session(CID_A, old_token) is False
    assert store.holders_for(_query("one.txt", DIGEST_A))[("one.txt", DIGEST_A)]
    assert store.end_session(CID_A, new_token) is True
    store.close()


@pytest.mark.asyncio
async def test_master_wss_configures_bounded_message_size() -> None:
    from hsync_client.constants import WS_MAX_MESSAGE_BYTES

    server = P2PWebSocketServer(port=9999)
    fake_ws = MagicMock()
    fake_ws.prepare = AsyncMock()
    fake_ws.send_str = AsyncMock()
    fake_ws.close = AsyncMock()
    fake_ws.receive = AsyncMock(return_value=SimpleNamespace(type=WSMsgType.CLOSE, data=None))

    async def _empty_iter():
        if False:  # pragma: no cover - never yields
            yield None

    fake_ws.__aiter__ = _empty_iter

    request = SimpleNamespace(remote="192.168.1.20")
    with patch("hsync_client.p2p_websocket.web.WebSocketResponse", return_value=fake_ws) as factory:
        await server.handle(request)

    assert factory.call_args.kwargs["max_msg_size"] == WS_MAX_MESSAGE_BYTES
