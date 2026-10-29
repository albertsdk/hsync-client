# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Presence counting for local-network p2p (mDNS, no rendezvous).

The pair is ``connected/total``, self included. These tests pin the arithmetic
for the master and the slave paths so a regression (e.g. the old relay-only
``1/2``) is caught without a live multi-device network.
"""

import pytest

import hsync_client.p2p_rendezvous_mdns as mdns
from hsync_client.p2p_peers import Peer, PeerManager
from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

LOCAL = "local-device"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(mdns, "get_local_ip", lambda: "127.0.0.1")
    monkeypatch.setattr(mdns, "get_free_port", lambda: 8443)


@pytest.fixture
def presence(monkeypatch) -> list[tuple[int, int]]:
    seen: list[tuple[int, int]] = []
    monkeypatch.setattr(
        mdns, "record_presence", lambda connected, total: seen.append((connected, total))
    )
    return seen


def _plan() -> MdnsRendezvous:
    plan = MdnsRendezvous(cid=LOCAL, bucket="shared", group="home", port=0)
    plan._peer_manager = PeerManager(local_cid=LOCAL, bucket="shared", group="home")
    return plan


def _peer(cid: str, *, connected: int = 0, prefer_master: bool = False) -> Peer:
    return Peer(
        cid=cid,
        bucket="shared",
        address="10.0.0.2",
        port=8443,
        group="home",
        connected=connected,
        prefer_master=prefer_master,
    )


class TestMasterPresence:
    def test_alone_is_one_of_one(self, presence):
        plan = _plan()
        plan.set_connected_clients_provider(lambda: 0)

        plan.emit_rendezvous_status()

        assert presence == [(1, 1)]

    def test_two_devices_is_two_of_two(self, presence):
        plan = _plan()
        plan.set_connected_clients_provider(lambda: 1)
        plan._peer_manager.add_peer(_peer("slave"))

        plan.emit_rendezvous_status()

        assert presence == [(2, 2)]

    def test_three_devices_is_three_of_three(self, presence):
        plan = _plan()
        plan.set_connected_clients_provider(lambda: 2)
        plan._peer_manager.add_peer(_peer("slave-a"))
        plan._peer_manager.add_peer(_peer("slave-b"))

        plan.emit_rendezvous_status()

        assert presence == [(3, 3)]

    def test_connected_is_clamped_to_the_known_total(self, presence):
        """A stale provider count can never report more connected than total."""
        plan = _plan()
        plan.set_connected_clients_provider(lambda: 5)
        plan._peer_manager.add_peer(_peer("slave"))

        plan.emit_rendezvous_status()

        assert presence == [(2, 2)]


class TestSlavePresence:
    def test_two_devices_is_two_of_two(self, presence):
        plan = _plan()
        plan._peer_manager.add_peer(_peer("master", connected=1, prefer_master=True))

        plan.emit_rendezvous_status()

        assert presence == [(2, 2)]

    def test_live_slave_link_never_reports_one_of_two(self, presence):
        """A live WSS link proves >=1 slave while the master's TXT still says 0."""
        plan = _plan()
        master = _peer("master", connected=0, prefer_master=True)
        plan._peer_manager.add_peer(master)
        plan._peer_manager.mark_peer_connected(master.cid)

        plan.emit_rendezvous_status()

        assert presence == [(2, 2)]

    def test_three_devices_is_three_of_three(self, presence):
        plan = _plan()
        plan._peer_manager.add_peer(_peer("master", connected=2, prefer_master=True))
        plan._peer_manager.add_peer(_peer("other-slave"))

        plan.emit_rendezvous_status()

        assert presence == [(3, 3)]
