# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Focused state-machine and publisher tests for LAN peer inventory."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hsync_common.models.file import File
from hsync_common.protocol.handshake import PROTOCOL_VERSION_LAN
from hsync_common.protocol.peer_download import (
    build_lan_inventory_ack,
    build_lan_inventory_frame,
)

from hsync_client.lan_inventory import LanInventoryPublisher, LanInventoryStore
from hsync_client.lan_websocket import LANWebSocketServer

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

    def __init__(self, publisher: LanInventoryPublisher) -> None:
        self.publisher = publisher
        self.frames: list[dict] = []
        self.closed = False

    async def send(self, message: str) -> None:
        frame = json.loads(message)
        self.frames.append(frame)
        self.publisher.handle_ack(
            build_lan_inventory_ack(
                gen=frame["gen"],
                seq=frame["seq"],
                status="ok",
            )
        )

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


def test_store_commits_snapshot_atomically_and_applies_idempotent_delta(tmp_path: Path) -> None:
    store = LanInventoryStore(tmp_path / "inventory.sqlite3", "bucket")
    store.activate()
    token = store.begin_session(CID_A)

    first = build_lan_inventory_frame(
        gen=1,
        seq=0,
        mode="snapshot",
        more=True,
        entries=[{"path": "one.txt", "sha256": DIGEST_A, "size": 1}],
    )
    assert store.apply_frame(CID_A, token, first)["status"] == "ok"
    assert store.holders_for(_query("one.txt", DIGEST_A))[("one.txt", DIGEST_A)] == ()

    terminal = build_lan_inventory_frame(
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

    delta = build_lan_inventory_frame(
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

    gap = build_lan_inventory_frame(
        gen=1,
        seq=4,
        mode="delta",
        more=False,
        entries=[{"path": "three.txt", "sha256": DIGEST_A, "size": 1}],
    )
    assert store.apply_frame(CID_A, token, gap)["status"] == "resync"
    assert store.holders_for(_query("one.txt", DIGEST_B))[("one.txt", DIGEST_B)]
    store.close()


def test_store_quota_counts_only_net_delta_rows_and_rejection_preserves_commit(
    tmp_path: Path,
) -> None:
    store = LanInventoryStore(
        tmp_path / "inventory.sqlite3",
        "bucket",
        per_cid_quota=1,
        global_quota=1,
    )
    store.activate()
    token = store.begin_session(CID_A)
    snapshot = build_lan_inventory_frame(
        gen=1,
        seq=0,
        mode="snapshot",
        more=False,
        entries=[{"path": "one.txt", "sha256": DIGEST_A, "size": 1}],
    )
    assert store.apply_frame(CID_A, token, snapshot)["status"] == "ok"

    replacement = build_lan_inventory_frame(
        gen=1,
        seq=1,
        mode="delta",
        more=False,
        entries=[{"path": "one.txt", "sha256": DIGEST_B, "size": 2}],
    )
    assert store.apply_frame(CID_A, token, replacement)["status"] == "ok"

    overflow = build_lan_inventory_frame(
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
    store = LanInventoryStore(tmp_path / "inventory.sqlite3", "bucket")
    store.activate()
    old_token = store.begin_session(CID_A, token="old-owner")
    snapshot = build_lan_inventory_frame(
        gen=1,
        seq=0,
        mode="snapshot",
        more=False,
        entries=[{"path": "one.txt", "sha256": DIGEST_A, "size": 1}],
    )
    assert store.apply_frame(CID_A, old_token, snapshot)["status"] == "ok"

    new_token = store.begin_session(CID_A, token="new-owner")
    assert store.end_session(CID_A, old_token) is False
    assert store.has_session(CID_A)
    assert store.holders_for(_query("one.txt", DIGEST_A))[("one.txt", DIGEST_A)]
    assert store.apply_frame(CID_A, old_token, snapshot)["reason"] == "old_owner"
    assert store.end_session(CID_A, new_token) is True
    assert store.holders_for(_query("one.txt", DIGEST_A))[("one.txt", DIGEST_A)] == ()
    store.close()


@pytest.mark.asyncio
async def test_publisher_uses_contiguous_sequences_across_multi_frame_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hsync_client.lan_inventory as inventory_module

    monkeypatch.setattr(inventory_module, "LAN_INVENTORY_MAX_ENTRIES", 2)
    publisher = LanInventoryPublisher(tmp_path)
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

    publisher = LanInventoryPublisher(tmp_path)
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
    store = LanInventoryStore(tmp_path / "inventory.sqlite3", "bucket")
    store.activate()
    server = LANWebSocketServer(port=9999, inventory_store=store)
    websocket = AsyncMock()
    websocket.remote_address = ("192.168.1.20", 5000)
    websocket.recv.return_value = json.dumps(
        {
            "type": "hello",
            "cid": CID_A,
            "protocol_version": PROTOCOL_VERSION_LAN,
            "capabilities": ["peer_download"],
        }
    )
    frame = build_lan_inventory_frame(
        gen=1,
        seq=0,
        mode="snapshot",
        more=False,
        entries=[{"path": "one.txt", "sha256": DIGEST_A, "size": 1}],
    )

    async def _messages(_websocket):
        yield json.dumps(frame)

    websocket.__aiter__ = _messages
    with patch("hsync_client.lan_websocket.CONFIG", {"LAN_KEY": None}):
        await server._handle_client(websocket)

    sent = [json.loads(call.args[0]) for call in websocket.send.call_args_list]
    assert sent[0]["type"] == "hello"
    assert sent[1] == {"type": "inventory_ack", "gen": 1, "seq": 0, "status": "ok"}
    assert not store.has_session(CID_A)
    store.close()


@pytest.mark.asyncio
async def test_wss_reconnect_installs_new_inventory_owner_before_closing_old(
    tmp_path: Path,
) -> None:
    store = LanInventoryStore(tmp_path / "inventory.sqlite3", "bucket")
    store.activate()
    server = LANWebSocketServer(port=9999, inventory_store=store)
    old_websocket = AsyncMock()
    new_websocket = AsyncMock()
    address = ("192.168.1.20", 5000)

    admitted, old_token, _ = await server._admit_slave(
        CID_A,
        address,
        old_websocket,
        authenticated=True,
        inventory_capable=True,
    )
    assert admitted and old_token is not None
    frame = build_lan_inventory_frame(
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
        inventory_capable=True,
    )
    assert admitted and new_token is not None and new_token != old_token
    old_websocket.close.assert_awaited_once()
    assert store.end_session(CID_A, old_token) is False
    assert store.holders_for(_query("one.txt", DIGEST_A))[("one.txt", DIGEST_A)]
    assert store.end_session(CID_A, new_token) is True
    store.close()


@pytest.mark.asyncio
async def test_master_wss_configures_bounded_message_size() -> None:
    server = LANWebSocketServer(port=9999)
    served = MagicMock()
    served.close = MagicMock()
    served.wait_closed = AsyncMock()

    class ServeContext:
        async def __aenter__(self):
            return served

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    server._running = False
    with patch("websockets.serve", return_value=ServeContext()) as serve:
        await server._run_server()

    from hsync_client.constants import WS_MAX_MESSAGE_BYTES

    assert serve.call_args.kwargs["max_size"] == WS_MAX_MESSAGE_BYTES
