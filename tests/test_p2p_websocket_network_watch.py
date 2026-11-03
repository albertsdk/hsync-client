# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Slave WSS resilience: network-change close and per-class liveness timeouts.

Two failure modes the field log exposed when the local interface changes or the
path is a high-RTT mobile link:

* a WSS bound to an interface that no longer exists blackholes until the
  keepalive ping times out (tens of seconds) — ``_NetworkCloseWaker`` ends that
  window at once;
* the fixed keepalive/open timeouts are too tight for a mobile path — they are
  now derived from the autotune network class.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import hsync_client.p2p_websocket as ws
from hsync_client.constants import (
    WS_MOBILE_OPEN_TIMEOUT_S,
    WS_MOBILE_PING_INTERVAL_S,
    WS_MOBILE_PING_TIMEOUT_S,
    WS_OPEN_TIMEOUT_S,
    WS_PING_INTERVAL_S,
    WS_PING_TIMEOUT_S,
)
from hsync_client.net_watch import NetworkSignature


class _FakeMonitor:
    """Capture the subscribed callback so a test can drive a transition."""

    def __init__(self) -> None:
        self.callback = None

    def subscribe(self, callback) -> None:
        self.callback = callback

    def unsubscribe(self, callback) -> None:
        # Bound methods are re-created on each access, so compare by equality
        # (the real monitor uses list membership, i.e. ``==``).
        if self.callback == callback:
            self.callback = None


class TestWSLivenessTimeouts:
    def test_default_and_mobile_classes(self):
        with patch.dict(ws.CONFIG, {"P2P_NET_CLASS": "unknown"}, clear=False):
            assert ws._ws_liveness_timeouts() == (
                WS_OPEN_TIMEOUT_S,
                WS_PING_INTERVAL_S,
                WS_PING_TIMEOUT_S,
            )
        with patch.dict(ws.CONFIG, {"P2P_NET_CLASS": "mobile"}, clear=False):
            assert ws._ws_liveness_timeouts() == (
                WS_MOBILE_OPEN_TIMEOUT_S,
                WS_MOBILE_PING_INTERVAL_S,
                WS_MOBILE_PING_TIMEOUT_S,
            )

    def test_missing_class_keeps_the_defaults(self):
        with patch.dict(ws.CONFIG, {}, clear=True):
            assert ws._ws_liveness_timeouts() == (
                WS_OPEN_TIMEOUT_S,
                WS_PING_INTERVAL_S,
                WS_PING_TIMEOUT_S,
            )


class TestNetworkCloseWaker:
    def test_a_network_change_wakes_the_waker_and_close_unsubscribes(self):
        monitor = _FakeMonitor()
        with patch.object(ws, "get_network_monitor", lambda: monitor):

            async def main() -> None:
                waker = ws._NetworkCloseWaker(asyncio.get_running_loop())
                assert monitor.callback is not None
                monitor.callback(NetworkSignature(source_ip="10.0.0.2", online=True))
                await asyncio.wait_for(waker.wait(), timeout=1.0)
                waker.close()
                assert monitor.callback is None

            asyncio.run(main())

    def test_closing_the_waker_closes_the_socket(self):
        class _Waker:
            async def wait(self) -> None:
                return

        class _Socket:
            def __init__(self) -> None:
                self.closed = False

            async def close(self) -> None:
                self.closed = True

        async def main() -> None:
            socket = _Socket()
            await ws._close_ws_on_network_change(socket, _Waker())
            assert socket.closed is True

        asyncio.run(main())
