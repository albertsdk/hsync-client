# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Wiring of the serving role's command channel (``p2p_handler._MasterChannel``)."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from hsync_common.protocol.peer_download import build_p2p_inventory_frame

from hsync_client.config import CONFIG
from hsync_client.p2p_handler import _MasterChannel
from hsync_client.p2p_inventory import P2PInventoryStore

CID_A = "A" * 21
DIGEST_A = "a" * 64
LOG = logging.getLogger("test-master-channel")


def _store(tmp_path: Path) -> P2PInventoryStore:
    store = P2PInventoryStore(tmp_path / "inventory.sqlite3", "share")
    store.activate()
    token = store.begin_session(CID_A, serve_slots=2)
    frame = build_p2p_inventory_frame(
        gen=1,
        seq=0,
        mode="snapshot",
        more=False,
        entries=[{"path": "a.bin", "sha256": DIGEST_A, "size": 4}],
    )
    assert store.apply_frame(CID_A, token, frame)["status"] == "ok"
    return store


class _FakeWsServer:
    """Control plane stand-in that records the served-share change hook."""

    def __init__(self) -> None:
        self.connected_clients = 1
        self.local_change_callback = None

    def set_local_change_callback(self, callback) -> None:
        self.local_change_callback = callback


def _channel(store: P2PInventoryStore) -> _MasterChannel:
    server = SimpleNamespace(inventory_store=store)
    peer_manager = SimpleNamespace(get_alive_peers=lambda: [])
    return _MasterChannel(server, _FakeWsServer(), peer_manager, LOG)


@pytest.fixture()
def events(monkeypatch):
    from hsync_client import master_status

    seen: list[str] = []

    def _emit(event, **_payload) -> None:
        seen.append(str(getattr(event, "value", event)))

    monkeypatch.setattr(master_status, "emit_event", _emit)
    monkeypatch.setattr(master_status, "emit_folder_size", lambda *_a, **_k: None)
    return seen


def _wait_for(events: list[str], name: str, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if name in events:
            return True
        time.sleep(0.01)
    return False


class TestMasterChannel:
    """The serving role must answer a front-end's PREVIEW/PEERS commands."""

    def test_start_wires_the_change_hooks_and_stop_clears_them(self, tmp_path, events):
        store = _store(tmp_path)
        channel = _channel(store)

        channel.start()
        assert store._change_callback is not None
        assert channel._p2p_ws_server.local_change_callback == channel.local_change
        channel.stop()

        assert store._change_callback is None
        assert channel._p2p_ws_server.local_change_callback is None
        channel.close()
        store.close()

    def test_a_local_change_reclassifies_with_a_fresh_listing(self, tmp_path, monkeypatch):
        """A file the devices uploaded must stop reading as ``to_download``.

        The cached local listing would otherwise keep the pre-transfer verdict
        until its TTL expired, and nothing else wakes the serving role when only
        its own folder changed.
        """
        from hsync_client import master_status

        monkeypatch.setitem(CONFIG, "LOCAL_BUCKET", str(tmp_path))
        store = _store(tmp_path)  # the device holds a.bin
        channel = _channel(store)
        status = channel._status
        local: list[dict] = []
        monkeypatch.setattr(status, "_local_provider", lambda: list(local))
        monkeypatch.setattr(status, "_local_cache_ttl_s", 60.0)
        monkeypatch.setattr(master_status, "emit_folder_size", lambda *_a, **_k: None)
        states: list[str] = []
        monkeypatch.setattr(
            master_status,
            "emit_event",
            lambda event, **payload: (
                states.append(payload.get("state", ""))
                if str(getattr(event, "value", event)) == "file_status"
                else None
            ),
        )

        status.run_now()  # the upload has not landed yet
        assert states == ["to_download"]

        local.append({"filepath": str(tmp_path / "a.bin"), "sha256sum": DIGEST_A, "size": 4})
        states.clear()
        status.run_now()
        assert states == ["to_download"]  # the warm cache keeps the old verdict

        requested: list[int] = []
        monkeypatch.setattr(status, "request", lambda: requested.append(1))
        channel.local_change()
        assert status._local_files is None
        assert requested == [1]

        states.clear()
        status.run_now()
        assert states == ["synced"]

        channel.close()
        store.close()

    def test_the_reader_answers_preview_with_a_classification(self, tmp_path, events, monkeypatch):
        monkeypatch.setitem(CONFIG, "LOCAL_BUCKET", str(tmp_path))
        store = _store(tmp_path)
        channel = _channel(store)
        channel.start()
        reader = channel._reader
        assert reader is not None
        monkeypatch.setattr(
            channel._status,
            "_local_listing",
            lambda: [{"filepath": str(tmp_path / "a.bin"), "sha256sum": DIGEST_A, "size": 4}],
        )

        reader._dispatch_in_thread(b'{"cmd": "PREVIEW"}\n')

        assert _wait_for(events, "status_pass_end") is True
        assert "status_pass_start" in events
        assert "file_status" in events
        channel.stop()
        channel.close()
        store.close()

    def test_an_inventory_change_requests_a_pass(self, tmp_path, events, monkeypatch):
        monkeypatch.setitem(CONFIG, "LOCAL_BUCKET", str(tmp_path))
        store = _store(tmp_path)
        channel = _channel(store)
        channel.start()

        # A device publishing its snapshot must refresh the classification.
        token = store.begin_session(CID_A, serve_slots=2)
        frame = build_p2p_inventory_frame(
            gen=2,
            seq=0,
            mode="snapshot",
            more=False,
            entries=[{"path": "b.bin", "sha256": DIGEST_A, "size": 1}],
        )
        assert store.apply_frame(CID_A, token, frame)["status"] == "ok"

        # An inventory-driven pass reports itself with the serving-side pair, never
        # with the preview markers (those belong to the passes a front-end asks for).
        assert _wait_for(events, "status_pass_end") is True
        assert "status_pass_start" in events
        assert "preview_end" not in events
        channel.stop()
        channel.close()
        store.close()
