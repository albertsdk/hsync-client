# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Unit tests for the bootnode discovery plan (no network).

A fake client records the frames the plan sends and returns canned ``ack``/
``response`` frames, so the lease semantics (first register without endpoint,
second register with endpoint + capacity, refresh TTL-only, peer feed filtering,
leave/unmap on stop) are verified deterministically.
"""

from __future__ import annotations

from hsync_common.protocol.bootnode import (
    validate_boot_frame,
    validate_boot_response,
)

from hsync_client.boot_client import BootnodeUnreachable
from hsync_client.p2p_discovery_bootnode import BootnodeDiscovery
from hsync_client.p2p_peers import PeerManager
from hsync_client.stun import ObservedAddress

CID = "A1b2C3d4E5f6G7h8I9j0K"
OTHER = "B2c3D4e5F6g7H8i9J0k1L"
FP = "AA:BB:CC"
ACK = {
    "protocol_version": "boot/1",
    "type": "ack",
    "observed_endpoint": {"proto": "udp", "ip": "203.0.113.9", "port": 51000},
    "ttl": 60,
    "server_time": "2026-01-01T00:00:00Z",
}
RESPONSE = {
    "protocol_version": "boot/1",
    "type": "response",
    "peers": [
        {
            "cid": OTHER,
            "ip": "203.0.113.10",
            "port": 8443,
            "proto": "tcp",
            "cert_fp": FP,
            "bucket": "shared",
            "group": "home",
            "version": "1.0.0",
            "capacity": 4,
            "reachable": True,
        },
        {
            "cid": "C3d4E5f6G7h8I9j0K1l2M",
            "ip": "203.0.113.11",
            "port": 60000,
            "proto": "udp",
            "cert_fp": FP,
            "bucket": "shared",
            "group": "home",
            "version": "1.0.0",
            "capacity": 0,
            "reachable": True,
        },
        {
            "cid": "D4e5F6g7H8i9J0k1L2m3N",
            "ip": "203.0.113.12",
            "port": 8443,
            "proto": "tcp",
            "cert_fp": FP,
            "bucket": "shared",
            "group": "home",
            "version": "1.0.0",
            "capacity": 0,
            "reachable": False,
        },
    ],
    "count": 3,
    "ttl": 60,
}


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.calls: list[tuple[str, dict]] = []

    def register(self, frame):
        self.calls.append(("register", frame))
        return dict(ACK)

    def refresh(self, frame):
        self.calls.append(("refresh", frame))
        return dict(ACK)

    def leave(self, frame):
        self.calls.append(("leave", frame))
        return None

    def query(self, frame):
        self.calls.append(("query", frame))
        return validate_boot_response(dict(RESPONSE))


class FakeUpnp:
    def __init__(self):
        self.mapped: list[int] = []
        self.unmapped = False

    def map_port(self, port):
        self.mapped.append(port)
        return port

    def unmap(self):
        self.unmapped = True


def make_plan(**overrides):
    fake_upnp = FakeUpnp()
    kwargs = {
        "cid": CID,
        "bucket": "shared",
        "bootnode_host": "boot.example.net",
        "bootnode_port": 443,
        "port": 8443,
        "group": "home",
        "cert_fingerprint": FP,
        "p2p_key": "secret",
        "peer_download_enabled": True,
        "capacity": 7,
        "client_factory": FakeClient,
        "upnp_factory": lambda: fake_upnp,
    }
    kwargs.update(overrides)
    plan = BootnodeDiscovery(**kwargs)
    return plan, fake_upnp


class TestLease:
    def test_first_register_has_no_endpoint_and_zero_capacity(self):
        plan, _ = make_plan()
        plan._register(publish_endpoint=False)
        kind, frame = plan._client.calls[-1]
        assert kind == "register"
        assert frame["endpoints"] == []
        assert frame["capacity"] == 0
        assert validate_boot_frame(frame) is not None
        assert plan._observed == ObservedAddress(host="203.0.113.9", port=51000)

    def test_publish_endpoint_maps_upnp_and_sends_candidates(self):
        plan, fake_upnp = make_plan()
        plan.update_endpoint_ready(True)
        plan._publish_endpoint()
        assert fake_upnp.mapped == [8443]
        kind, frame = plan._client.calls[-1]
        assert kind == "register"
        assert frame["capacity"] == 7
        assert frame["endpoints"], "the second register must publish endpoints"
        assert validate_boot_frame(frame) is not None

    def test_capacity_is_zero_without_peer_download(self):
        plan, _ = make_plan(peer_download_enabled=False)
        plan._publish_endpoint()
        assert plan._client.calls[-1][1]["capacity"] == 0

    def test_refresh_carries_connected_only(self):
        plan, _ = make_plan()
        plan.update_server_ready()
        plan.set_connected_clients(3)
        plan._refresh()
        kind, frame = plan._client.calls[-1]
        assert kind == "refresh"
        assert "endpoints" not in frame
        assert frame["connected"] == 3

    def test_token_derived_from_key(self):
        plan, _ = make_plan()
        plan._register(publish_endpoint=False)
        assert plan._client.calls[-1][1]["bucket_token"] is not None

    def test_no_token_without_key(self):
        plan, _ = make_plan(p2p_key=None)
        plan._register(publish_endpoint=False)
        assert "bucket_token" not in plan._client.calls[-1][1]


class TestEndpointPublication:
    def test_endpoint_published_flag(self):
        plan, _ = make_plan()
        assert plan.endpoint_published() is False
        plan._register(publish_endpoint=False)
        assert plan.endpoint_published() is False
        plan._publish_endpoint()
        assert plan.endpoint_published() is True

    def test_mdns_plan_is_always_published(self):
        """The mDNS plan keeps today's readiness timing (no publication step)."""
        from hsync_client.p2p_discovery_mdns import MdnsDiscovery

        assert MdnsDiscovery(cid=CID, bucket="shared").endpoint_published() is True


class TestPeerFeed:
    def test_only_reachable_tcp_peers_are_fed(self):
        plan, _ = make_plan()
        manager = PeerManager(local_cid=CID, bucket="shared", group="home")
        plan._peer_manager = manager
        plan._query_peers()
        peers = manager.get_alive_peers()
        assert [p.cid for p in peers] == [OTHER]
        assert peers[0].address == "203.0.113.10"
        assert peers[0].port == 8443
        # A relayed reachable TCP peer is a serving master: the slave must not
        # wait for a TXT server_ready flag the boot/1 wire does not carry.
        assert peers[0].server_ready is True

    def test_local_cid_is_never_added(self):
        plan, _ = make_plan(cid=OTHER)
        manager = PeerManager(local_cid=OTHER, bucket="shared", group="home")
        plan._peer_manager = manager
        plan._query_peers()
        assert manager.get_alive_peers() == []


class TestWaitForDiscoveryTolerance:
    def test_unreachable_bootnode_does_not_abort_the_wait(self):
        """A transient bootnode error must be tolerated, not crash the client."""
        plan, _ = make_plan()
        plan._peer_manager = PeerManager(local_cid=CID, bucket="shared", group="home")

        def boom(frame):
            raise BootnodeUnreachable("bootnode boot.example.net:443: ConnectionError")

        plan._client.query = boom
        # Must return on timeout instead of propagating the bootnode error.
        plan.wait_for_discovery(timeout=0.05)


class TestStop:
    def test_stop_sends_leave_and_unmaps(self):
        plan, fake_upnp = make_plan()
        plan._upnp = fake_upnp
        plan._external_port = 8443
        plan._running = True
        plan._thread = None
        plan.stop()
        assert any(kind == "leave" for kind, _ in plan._client.calls)
        assert fake_upnp.unmapped is True
