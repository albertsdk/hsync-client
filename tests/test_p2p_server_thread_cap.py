# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for H5 — P2P master thread cap (BoundedThreadingHTTPServer).

These tests exercise the real server classes: the semaphore-based cap must be
observed at runtime, not merely re-implemented by the test itself.
"""

import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler

from hsync_client.p2p_server import (
    P2P_SERVER_MAX_THREADS,
    BoundedThreadingHTTPServer,
    P2PServer,
)


class _SlowHandler(BaseHTTPRequestHandler):
    """GET handler that records peak concurrency and holds each request open."""

    active = 0
    peak = 0
    guard = threading.Lock()

    def do_GET(self) -> None:  # noqa: N802 (stdlib API name)
        with _SlowHandler.guard:
            _SlowHandler.active += 1
            _SlowHandler.peak = max(_SlowHandler.peak, _SlowHandler.active)
        try:
            time.sleep(0.2)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        finally:
            with _SlowHandler.guard:
                _SlowHandler.active -= 1

    def log_message(self, *args: object) -> None:  # keep test output clean
        pass


def _serve_in_thread(server: BoundedThreadingHTTPServer) -> threading.Thread:
    """Run serve_forever() in a daemon thread and return the thread."""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def test_start_instantiates_bounded_server():
    """P2PServer.start() must bind BoundedThreadingHTTPServer, not the
    unbounded ThreadingHTTPServer.

    Regression guard for H5: a rewrite that went back to ThreadingHTTPServer
    would silently drop the thread cap while the rest of the suite (which no
    longer inspects bytecode) keeps passing.
    """
    server = P2PServer(port=0)  # ephemeral port; start() binds 0.0.0.0
    server.start()
    try:
        # Exact-type check: the wiring must use the bounded subclass.
        assert server._server.__class__ is BoundedThreadingHTTPServer
        # The ephemeral port was actually bound.
        bound_port = server._server.server_address[1]
        assert bound_port > 0
        # Backlog is capped to the same value as the semaphore.
        assert server._server.request_queue_size == P2P_SERVER_MAX_THREADS
    finally:
        server.stop()


def test_concurrent_requests_are_capped_at_max_threads():
    """At runtime, concurrent request threads never exceed max_threads.

    Five overlapping requests against a cap of two must never run more than
    two handler bodies at once. With the unbounded ThreadingHTTPServer every
    request would enter immediately and the observed peak would be 5; the
    bounded subclass must instead serialize excess requests on its semaphore.
    """
    _SlowHandler.active = 0
    _SlowHandler.peak = 0
    server = BoundedThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler, max_threads=2)
    server.daemon_threads = True
    serve_thread = _serve_in_thread(server)
    port = server.server_address[1]

    n_requests = 5
    statuses: list[int] = []

    def worker() -> None:
        url = f"http://127.0.0.1:{port}/"
        with urllib.request.urlopen(url, timeout=10) as resp:
            statuses.append(resp.status)

    try:
        threads = [threading.Thread(target=worker) for _ in range(n_requests)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        assert statuses == [200] * n_requests
        # The discriminating invariant: the cap was never exceeded, even
        # though all requests were fired concurrently.
        assert _SlowHandler.peak <= 2
        # The handler really served all 5 requests (peak state is meaningful).
        assert _SlowHandler.peak >= 1
    finally:
        server.shutdown()
        server.server_close()
        serve_thread.join(timeout=5)
