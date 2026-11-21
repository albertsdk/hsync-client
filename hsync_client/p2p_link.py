# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Which link state a *serving* p2p master reports.

A slave syncs over HTTPS *before* it opens its control WebSocket, and it tears
that socket down on every reconnect, so a master can have zero WebSocket clients
while a peer is demonstrably using it. The data plane is the proof: the serving
HTTP handlers stamp every authorized request here and the status reports
``connected`` while a stamp is fresh. The hold bridges the pauses inside one
sync (an inventory publish, a listing, a large file) without keeping a departed
peer's link alive for long.
"""

from __future__ import annotations

import threading
import time

from hsync_client.events import set_link_status

# 30 s: bridges the gaps between the requests of one sync pass while a peer that
# simply left is reported as gone within half a minute.
MASTER_LINK_HOLD_SECONDS = 30.0

_last_activity = 0.0
_lock = threading.Lock()


def note_master_datapath_activity(now: float | None = None) -> None:
    """Record a peer's data-plane request and publish the link as connected.

    Called from the serving HTTP handlers: a WebSocket client is not the only
    evidence of a live peer. ``set_link_status`` dedupes repeats, so a
    per-request call costs one comparison and publishes only the transition.
    """
    global _last_activity
    ts = time.monotonic() if now is None else now
    with _lock:
        _last_activity = ts
    set_link_status("connected")


def peer_activity_fresh(now: float | None = None) -> bool:
    """True when a peer used the data plane within ``MASTER_LINK_HOLD_SECONDS``."""
    ts = time.monotonic() if now is None else now
    with _lock:
        last = _last_activity
    return last > 0.0 and ts - last <= MASTER_LINK_HOLD_SECONDS


def master_link_status(connected_clients: int, now: float | None = None) -> str:
    """Link status of a serving master: ``connected`` or ``waiting``.

    A WebSocket client is a live link, and so is a peer that talked to the data
    plane within the hold (it may be syncing before its socket exists, or
    reconnecting after the sync tore it down). ``waiting`` therefore means
    exactly "no peer is talking to me".
    """
    if connected_clients > 0 or peer_activity_fresh(now):
        return "connected"
    return "waiting"


def reset_master_datapath_activity() -> None:
    """Forget the stamp (test seam: the tracker is process-wide)."""
    global _last_activity
    with _lock:
        _last_activity = 0.0
