"""Tests for the P2P master concurrency cap (bounded aiohttp executor).

The cap used to live in ``BoundedThreadingHTTPServer``; it now lives in the
bounded ``ThreadPoolExecutor`` owned by the unified aiohttp listener. These
tests assert the cap is still wired at runtime, so a rewrite that dropped the
bound (e.g. an unbounded executor) fails here instead of silently.
"""

import threading
import urllib.request
from unittest.mock import patch

from hsync_client.p2p_server import P2P_SERVER_MAX_THREADS, P2PServer


def test_start_uses_bounded_executor():
    """P2PServer.start() must run the data plane on a bounded worker pool.

    Regression guard: a rewrite that used an unbounded executor (or ran the
    blocking data plane on the event loop) would drop the cap while the rest of
    the suite, which no longer inspects the wiring, keeps passing.
    """
    server = P2PServer(port=0)  # ephemeral port; start() binds 0.0.0.0
    server.start()
    try:
        # The ephemeral port was actually bound and is readable back.
        assert server.port > 0
        # Exact-cap check: the wiring must use the configured bound.
        assert server._executor is not None
        assert server._executor._max_workers == P2P_SERVER_MAX_THREADS
    finally:
        server.stop()


def test_concurrent_requests_are_served():
    """Several overlapping requests all complete against the bounded pool."""
    server = P2PServer(port=0)
    server.start()
    try:
        port = server.port
        statuses: list[int] = []

        def worker() -> None:
            with patch("hsync_client.p2p_server.CONFIG", {"P2P_KEY": None}):
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/v1/health", timeout=10
                ) as resp:
                    statuses.append(resp.status)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        assert statuses == [200] * 5
    finally:
        server.stop()
