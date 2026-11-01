# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Announce and sample cadences: how fast a peer is discovered.

A peer that is already running learns about a new node only from an announce,
and a node that is joining sees the group only in a sample it asked for. Both
cadences therefore decide how quickly a sync can start, and both start with a
burst (the discovery equivalent of RFC 6762's query burst) before settling on
their steady interval.
"""

from __future__ import annotations

import threading
import time

import hsync_client.p2p_rendezvous_mdns as mdns_mod
from hsync_client.constants import (
    RENDEZVOUS_QUERY_ACTIVE_INTERVAL,
    RENDEZVOUS_QUERY_BURST_COUNT,
    RENDEZVOUS_QUERY_BURST_INTERVAL,
)
from hsync_client.net_watch import NetworkSignature
from hsync_client.p2p_peers import Peer, PeerManager
from hsync_client.p2p_rendezvous_rdv import RdvRendezvous


class TestMdnsAnnounceBurst:
    """The record is re-announced every second first, then at the heartbeat."""

    def test_the_loop_bursts_then_settles(self, monkeypatch) -> None:
        burst = 3
        monkeypatch.setattr(mdns_mod, "MDNS_ANNOUNCE_BURST_INTERVAL", 0.0)
        monkeypatch.setattr(mdns_mod, "MDNS_ANNOUNCE_BURST_COUNT", burst)
        plan = object.__new__(mdns_mod.MdnsRendezvous)
        plan._heartbeat_interval = 60.0
        plan._heartbeat_stop = threading.Event()
        plan._announce_burst = 0
        announces: list[int] = []
        plan._heartbeat = lambda: announces.append(1)
        plan._recover_mdns = lambda: None

        thread = threading.Thread(target=plan._heartbeat_loop, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5.0
        while len(announces) < burst and time.monotonic() < deadline:
            time.sleep(0.01)
        plan._heartbeat_stop.set()
        thread.join(timeout=5.0)

        assert len(announces) >= burst, "the announce burst did not run"
        assert plan._announce_burst == 0, "the burst never settled"

    def test_a_network_change_restarts_the_burst(self) -> None:
        plan = object.__new__(mdns_mod.MdnsRendezvous)
        plan._network_offline = False
        plan._local_ip = "10.0.0.5"
        plan._announce_burst = 0
        rebuilt: list[str] = []
        plan._rebuild_mdns = lambda ip: rebuilt.append(ip)

        plan._on_network_change(NetworkSignature(online=True, source_ip="10.0.0.9"))

        assert rebuilt == ["10.0.0.9"]
        assert plan._announce_burst == mdns_mod.MDNS_ANNOUNCE_BURST_COUNT


class TestRendezvousSampleCadence:
    """A joining node samples in a burst, then at the fastest budget-safe rate."""

    @staticmethod
    def _plan(peer_manager: PeerManager | None, refresh: float = 30.0) -> RdvRendezvous:
        plan = object.__new__(RdvRendezvous)
        plan._peer_manager = peer_manager
        plan._refresh_interval = refresh
        plan._burst_left = 0
        return plan

    def test_the_burst_runs_before_anything_else(self) -> None:
        plan = self._plan(None)
        plan._burst_left = RENDEZVOUS_QUERY_BURST_COUNT

        assert plan._query_interval() == RENDEZVOUS_QUERY_BURST_INTERVAL

    def test_a_node_without_a_master_samples_at_the_budget_floor(self) -> None:
        """Waiting a full lease cycle to notice a peer is what delayed the sync."""
        manager = PeerManager(local_cid="zzz999", bucket="photos")
        plan = self._plan(manager)

        assert plan._query_interval() == RENDEZVOUS_QUERY_ACTIVE_INTERVAL

    def test_a_complete_group_samples_at_the_lease_cadence(self) -> None:
        manager = PeerManager(local_cid="zzz999", bucket="photos", startup_time=200.0)
        manager.add_peer(
            Peer(
                cid="aaa111",
                bucket="photos",
                address="192.168.1.100",
                port=9753,
                startup_time=100.0,
            )
        )
        plan = self._plan(manager)

        assert manager.get_master_cid() == "aaa111"
        assert plan._query_interval() == 30.0
