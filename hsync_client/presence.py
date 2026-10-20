# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Shared presence sink for the structured ``peers`` UI event.

Both transports compute the same display-ready pair — ``connected`` clients
over ``total`` clients in the share, self included — and feed it here so the
``peers`` event has exactly one emission point:

- P2P mode: :meth:`hsync_client.p2p.MdnsDiscovery.wait_for_discovery` polls mDNS and
  the master's WebSocket client count on every discovery iteration.
- Server mode: the ``presence`` WebSocket frame broadcast by the server on
  every join/leave (``hsync_client.remote_websocket``).

A change is emitted once; re-emitting the current value (e.g. for the
``PEERS`` stdin command) is a deliberate no-op dedup bypass via
:func:`reemit_presence`.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from hsync_client.events import UiEventName, emit_event

if TYPE_CHECKING:
    from hsync_client.p2p_peers import Peer, PeerManager

__all__ = ["record_presence", "reemit_presence", "reset_presence", "slave_connected_count"]


_current: tuple[int, int] | None = None
_lock = threading.Lock()


def record_presence(connected: int, total: int) -> bool:
    """Store ``(connected, total)`` and emit a ``peers`` event when changed.

    Returns True when the event was emitted, False when the pair was unchanged.
    """
    global _current
    with _lock:
        if _current == (connected, total):
            return False
        _current = (connected, total)
    emit_event(UiEventName.peers, connected=connected, total=total)
    return True


def slave_connected_count(peer_manager: PeerManager, master: Peer) -> int:
    """Client count a slave derives from its master link, floored at 1 when live.

    A master advertises how many clients it serves (the mDNS TXT ``connected``
    value); a live WebSocket link to it proves at least this node is connected
    even before that advertised count converges. Shared by the discovery owner
    (``MdnsDiscovery.emit_discovery_status``) and the slave's out-of-band refresh
    (``p2p_handler._emit_slave_presence``) so the guard cannot drift.
    """
    published = master.connected
    if peer_manager.is_wss_connected(master.cid):
        published = max(published, 1)
    return published


def reemit_presence() -> bool:
    """Re-emit the last known presence pair (``PEERS`` stdin command).

    Returns False when no presence has been recorded yet (nothing to report).
    """
    with _lock:
        current = _current
    if current is None:
        return False
    connected, total = current
    emit_event(UiEventName.peers, connected=connected, total=total)
    return True


def reset_presence() -> None:
    """Forget the current presence (tests / shutdown)."""
    global _current
    with _lock:
        _current = None
