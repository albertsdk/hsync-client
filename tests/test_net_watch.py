# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Unit tests for the shared network-change monitor (no real network).

The probe is injected, so every transition is driven by the test: the real
implementation samples the host's source IP with a UDP ``connect``.
"""

from __future__ import annotations

import threading

from hsync_client import net_watch
from hsync_client.net_watch import (
    OFFLINE_SOURCE_IP,
    NetworkChangeMonitor,
    NetworkSignature,
    network_signature,
)

ONLINE = NetworkSignature(source_ip="192.168.1.10", online=True)
OFFLINE = NetworkSignature(source_ip=OFFLINE_SOURCE_IP, online=False)


class _Probe:
    """Deterministic signature source the test changes by hand."""

    def __init__(self, signature: NetworkSignature) -> None:
        self.signature = signature
        self._lock = threading.Lock()

    def __call__(self) -> NetworkSignature:
        with self._lock:
            return self.signature

    def set(self, signature: NetworkSignature) -> None:
        with self._lock:
            self.signature = signature


class TestNetworkSignature:
    """The loopback fallback ``get_local_ip`` returns means "offline"."""

    def test_loopback_fallback_is_offline(self, monkeypatch):
        monkeypatch.setattr(net_watch, "get_local_ip", lambda: OFFLINE_SOURCE_IP)
        assert network_signature() == OFFLINE

    def test_routable_address_is_online(self, monkeypatch):
        monkeypatch.setattr(net_watch, "get_local_ip", lambda: "192.168.1.10")
        assert network_signature() == ONLINE


class TestPollOnce:
    """Only transitions are published, and one subscriber cannot mute another."""

    def test_unchanged_signature_notifies_nobody(self):
        monitor = NetworkChangeMonitor(interval=0.01, probe=_Probe(ONLINE))
        seen: list[NetworkSignature] = []
        monitor.subscribe(seen.append)
        assert monitor.poll_once() is False
        assert seen == []

    def test_change_notifies_with_the_new_signature_and_becomes_current(self):
        probe = _Probe(ONLINE)
        monitor = NetworkChangeMonitor(interval=0.01, probe=probe)
        seen: list[NetworkSignature] = []
        monitor.subscribe(seen.append)
        moved = NetworkSignature(source_ip="10.9.8.7", online=True)
        probe.set(moved)
        assert monitor.poll_once() is True
        assert seen == [moved]
        assert monitor.signature == moved

    def test_going_offline_is_published_too(self):
        probe = _Probe(ONLINE)
        monitor = NetworkChangeMonitor(interval=0.01, probe=probe)
        seen: list[NetworkSignature] = []
        monitor.subscribe(seen.append)
        probe.set(OFFLINE)
        assert monitor.poll_once() is True
        assert seen == [OFFLINE]

    def test_a_failing_subscriber_does_not_stop_the_others(self):
        probe = _Probe(ONLINE)
        monitor = NetworkChangeMonitor(interval=0.01, probe=probe)

        def boom(_signature: NetworkSignature) -> None:
            raise RuntimeError("boom")

        seen: list[NetworkSignature] = []
        monitor.subscribe(boom)
        monitor.subscribe(seen.append)
        probe.set(OFFLINE)
        assert monitor.poll_once() is True
        assert seen == [OFFLINE]

    def test_unsubscribe_stops_notifications(self):
        probe = _Probe(ONLINE)
        monitor = NetworkChangeMonitor(interval=0.01, probe=probe)
        seen: list[NetworkSignature] = []
        monitor.subscribe(seen.append)
        monitor.unsubscribe(seen.append)
        probe.set(OFFLINE)
        assert monitor.poll_once() is True
        assert seen == []


class TestThread:
    """The polling thread publishes a change and stops cleanly."""

    def test_start_polls_and_stop_ends_the_thread(self):
        probe = _Probe(ONLINE)
        monitor = NetworkChangeMonitor(interval=0.01, probe=probe)
        seen = threading.Event()
        monitor.subscribe(lambda _signature: seen.set())
        monitor.start()
        monitor.start()  # idempotent: one thread only
        try:
            probe.set(OFFLINE)
            assert seen.wait(2.0), "the polling thread never published the change"
        finally:
            monitor.stop()
            monitor.stop()  # idempotent
        assert monitor._thread is None
