# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Presence credit for the master during the initial (pre-WebSocket) sync.

The slave talks to the master over HTTPS for the whole initial sync and only
opens the realtime WebSocket afterwards. The presence pair must reflect that
live link from the start, otherwise the UI sits on ``1/N`` for the entire
initial sync (minutes for a large share).
"""

import hsync_client.p2p_handler as handler
from hsync_client.p2p_handler import _run_as_slave
from hsync_client.p2p_peers import Peer, PeerManager

LOCAL = "slave"
MASTER = "master"


class _Log:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _config() -> dict:
    return {
        "CID": LOCAL,
        "REMOTE_BUCKET": "shared",
        "LOCAL_BUCKET": "/tmp",
        "P2P_PEER_DOWNLOAD": True,
        "HTTP_TIMEOUT": 30,
        "P2P_KEY": "secret",
        "REQUEST_HEADERS": {},
        "API": {"buckets": "", "events": ""},
        "DRYRUN": False,
    }


def _manager() -> PeerManager:
    pm = PeerManager(local_cid=LOCAL, bucket="shared", group="home")
    pm.add_peer(
        Peer(
            cid=MASTER,
            bucket="shared",
            address="10.0.0.2",
            port=8443,
            group="home",
            use_https=True,
            cert_fingerprint="aa:bb",
            server_ready=True,
            prefer_master=True,
            connected=0,
        )
    )
    return pm


def _patch_plumbing(monkeypatch, sync_impl, records) -> None:
    monkeypatch.setattr(handler, "_evaluate_master_pin", lambda *a, **k: True)
    monkeypatch.setattr(handler, "_select_master_endpoint", lambda *a, **k: ("10.0.0.2", 8443))
    monkeypatch.setattr(handler, "_refresh_p2p_network_tuning", lambda *a, **k: None)
    monkeypatch.setattr(handler, "set_config", lambda *a, **k: None)
    monkeypatch.setattr(handler, "emit_event", lambda *a, **k: None)
    monkeypatch.setattr(handler, "log_initial_sync_start", lambda *a, **k: None)
    monkeypatch.setattr(handler, "P2PSourceResolver", lambda *a, **k: object())
    monkeypatch.setattr(handler, "record_presence", lambda c, t: records.append((c, t)))
    monkeypatch.setattr(handler, "sync", sync_impl)


def test_master_is_credited_during_the_initial_sync(monkeypatch):
    pm = _manager()
    assert pm.get_master() is not None
    seen: dict[str, bool] = {}
    records: list[tuple[int, int]] = []

    async def fake_sync(**kwargs):
        seen["connected_during_sync"] = pm.is_wss_connected(MASTER)
        return None

    _patch_plumbing(monkeypatch, fake_sync, records)

    result = _run_as_slave(_config(), _Log(), one_shot=True, peer_manager=pm)

    assert result == (False, 0)
    assert seen["connected_during_sync"] is True
    assert (2, 2) in records


def test_failed_initial_sync_clears_the_credit(monkeypatch):
    pm = _manager()
    assert pm.get_master() is not None
    seen: dict[str, bool] = {}
    records: list[tuple[int, int]] = []

    async def failing_sync(**kwargs):
        seen["connected_during_sync"] = pm.is_wss_connected(MASTER)
        raise handler.NetworkError("master offline")

    _patch_plumbing(monkeypatch, failing_sync, records)

    result = _run_as_slave(_config(), _Log(), one_shot=True, peer_manager=pm)

    assert seen["connected_during_sync"] is True
    assert pm.is_wss_connected(MASTER) is False
    assert result == (False, 1)  # ExitCode.FATAL
