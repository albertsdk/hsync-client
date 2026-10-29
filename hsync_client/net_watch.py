# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Network-change detection shared by the long-lived sync roles.

A daemon that loses and regains connectivity must recover **without a process
restart**. Two things break silently otherwise, and both leave the peer in the
desktop's "waiting" state until the process is restarted:

* an announced record keeps carrying the pre-outage address (a DHCP
  reassignment replaces it), so peers dial a dead endpoint;
* the sockets the rendezvous stack opened before the outage can be bound to an
  address that no longer exists, and nothing reconciles them.

The OS exposes no portable "network changed" callback, so one daemon thread
samples the local source IP and publishes the change. The sample is a UDP
``connect`` (``net_utils.get_local_ip``): the kernel picks the outgoing
interface without sending a packet.

``get_network_monitor`` owns the single process-wide instance; the consumers
subscribe instead of each running its own timer:

* ``p2p_rendezvous_mdns`` — rebuilds the Zeroconf stack and re-registers the
  record on the fresh address;
* ``p2p_rendezvous_rendezvous`` — wakes the lease loop so a changed address is
  republished immediately;
* ``remote_websocket`` — ends the reconnect backoff as soon as the link is back.

Callbacks run on the monitor thread: they must be short and must not wait for
another thread that itself waits for the monitor.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from loguru import logger as log

from hsync_client.net_utils import get_local_ip

# Poll cadence: a few seconds keep the recovery prompt while the probe stays
# negligible (one UDP socket per poll, no traffic).
NETWORK_POLL_INTERVAL_S = 5.0

# ``get_local_ip`` returns the loopback fallback when the host has no route.
OFFLINE_SOURCE_IP = "127.0.0.1"

__all__ = [
    "NETWORK_POLL_INTERVAL_S",
    "OFFLINE_SOURCE_IP",
    "NetworkChangeMonitor",
    "NetworkSignature",
    "get_network_monitor",
    "network_signature",
]


@dataclass(frozen=True, slots=True)
class NetworkSignature:
    """The host's network posture: what the monitor compares across polls."""

    source_ip: str
    online: bool


def network_signature() -> NetworkSignature:
    """Sample the local source IP and derive the connectivity posture."""
    source_ip = get_local_ip()
    return NetworkSignature(source_ip=source_ip, online=source_ip != OFFLINE_SOURCE_IP)


class NetworkChangeMonitor:
    """Poll the local address and notify subscribers whenever it changes.

    Only *transitions* are published: a poll whose signature equals the last
    one notifies nobody, so a subscriber never sees the same state twice. The
    current state is read through :attr:`signature`; it is not replayed to a
    late subscriber.
    """

    def __init__(
        self,
        interval: float = NETWORK_POLL_INTERVAL_S,
        probe: Callable[[], NetworkSignature] = network_signature,
    ) -> None:
        """
        Initialize the monitor without starting its thread.

        Args:
            interval: Seconds between polls (clamped to a sane minimum).
            probe: Signature source; override in tests to drive transitions
                deterministically.
        """
        self._interval = max(0.1, float(interval))
        self._probe = probe
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[NetworkSignature], None]] = []
        self._signature = probe()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def signature(self) -> NetworkSignature:
        """The last sampled posture."""
        return self._signature

    def start(self) -> None:
        """Start the polling thread (idempotent)."""
        with self._lock:
            if self._thread is not None:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._poll_forever, name="hsync-net-watch", daemon=True
            )
            thread = self._thread
        thread.start()

    def stop(self) -> None:
        """Stop the polling thread (idempotent)."""
        with self._lock:
            thread = self._thread
            self._thread = None
            self._stop.set()
        if thread is not None:
            thread.join(timeout=2.0)

    def subscribe(self, callback: Callable[[NetworkSignature], None]) -> None:
        """Register *callback* for future changes (duplicate adds are ignored)."""
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[NetworkSignature], None]) -> None:
        """Drop a previously registered callback (idempotent)."""
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def poll_once(self) -> bool:
        """Sample once and notify subscribers; return whether the state changed."""
        signature = self._probe()
        with self._lock:
            previous = self._signature
            if signature == previous:
                return False
            self._signature = signature
            subscribers = list(self._subscribers)
        if signature.online:
            came_from = "offline" if not previous.online else previous.source_ip
            log.info(
                f"Network link restored: local address {signature.source_ip} (was {came_from})"
            )
        else:
            log.warning(f"Network connection lost: no local address (was {previous.source_ip})")
        for callback in subscribers:
            try:
                callback(signature)
            except Exception:
                # One broken consumer must never stop the others or the thread.
                log.exception("Network-change subscriber failed")
        return True

    def _poll_forever(self) -> None:
        """Poll at the configured cadence until :meth:`stop` is called."""
        while not self._stop.wait(self._interval):
            self.poll_once()


_monitor: NetworkChangeMonitor | None = None
_monitor_lock = threading.Lock()


def get_network_monitor() -> NetworkChangeMonitor:
    """Return the process-wide monitor, starting its polling thread on first use."""
    global _monitor
    with _monitor_lock:
        if _monitor is None:
            _monitor = NetworkChangeMonitor()
            _monitor.start()
        return _monitor
