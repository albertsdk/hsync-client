# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

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

from hsync_client.events import UiEventName, emit_event

__all__ = ["record_presence", "reemit_presence", "reset_presence"]


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
