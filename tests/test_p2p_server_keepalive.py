"""Regression tests for the P2P master HTTP/1.1 keep-alive behaviour.

The master previously answered HTTP/1.0 (connection closed after every
response), so every per-file request paid a full TCP+TLS handshake. With
HTTP/1.1 connections are reused — which requires every response to carry an
exact ``Content-Length`` — and idle connections are reaped after
``P2P_HANDLER_IDLE_TIMEOUT_S`` so they do not pin a worker slot forever.
"""

import http.client
import time
from unittest.mock import patch

import pytest

from hsync_client.p2p_peers import get_free_port
from hsync_client.p2p_server import P2PServer


@pytest.fixture
def server():
    """Create and start a test server."""
    port = get_free_port()
    srv = P2PServer(port=port)
    srv.start()
    yield srv, srv.port
    srv.stop()


def _health(conn: http.client.HTTPConnection) -> http.client.HTTPResponse:
    conn.request("GET", "/api/v1/health")
    return conn.getresponse()


class TestKeepAlive:
    def test_connection_is_reused_across_requests(self, server):
        """A second request on the SAME connection succeeds.

        With HTTP/1.0 the master closed the connection after the first
        response, so this second request used to fail with
        RemoteDisconnected — the regression this test pins.
        """
        _, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            with patch(
                "hsync_client.p2p_server.CONFIG",
                {"LOCAL_BUCKET": "/tmp", "P2P_KEY": None},
            ):
                response1 = _health(conn)
                body1 = response1.read()
                assert response1.status == 200
                assert response1.version == 11  # HTTP/1.1, not 1.0

                response2 = _health(conn)
                body2 = response2.read()
                assert response2.status == 200
                assert body1 == body2
        finally:
            conn.close()

    def test_json_response_has_exact_content_length(self, server):
        """Every JSON body is framed with Content-Length (keep-alive must)."""
        _, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            with patch(
                "hsync_client.p2p_server.CONFIG",
                {"LOCAL_BUCKET": "/tmp", "P2P_KEY": None},
            ):
                response = _health(conn)
                body = response.read()
                assert response.status == 200
                declared = response.getheader("Content-Length")
                assert declared is not None
                assert int(declared) == len(body)
        finally:
            conn.close()

    def test_error_response_has_exact_content_length(self, server):
        """Error JSON (404) is framed with Content-Length too."""
        _, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            with patch(
                "hsync_client.p2p_server.CONFIG",
                {"LOCAL_BUCKET": "/tmp", "P2P_KEY": None},
            ):
                conn.request("GET", "/api/v1/definitely-not-an-endpoint")
                response = conn.getresponse()
                body = response.read()
                assert response.status == 404
                declared = response.getheader("Content-Length")
                assert declared is not None
                assert int(declared) == len(body)
        finally:
            conn.close()

    def test_idle_connection_is_reaped(self):
        """An idle keep-alive connection is closed after the idle timeout.

        Without the reap, connections parked in client pools would hold a
        worker slot forever and eventually starve the bounded executor.
        """
        port = get_free_port()
        with patch("hsync_client.p2p_server.P2P_HANDLER_IDLE_TIMEOUT_S", 0.3):
            srv = P2PServer(port=port)
            srv.start()
            try:
                with patch(
                    "hsync_client.p2p_server.CONFIG",
                    {"LOCAL_BUCKET": "/tmp", "P2P_KEY": None},
                ):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    response = _health(conn)
                    response.read()
                    assert response.status == 200

                    time.sleep(1.2)  # > idle timeout (0.3s)

                    with pytest.raises(
                        (
                            http.client.RemoteDisconnected,
                            ConnectionError,
                            BrokenPipeError,
                            http.client.BadStatusLine,
                        )
                    ):
                        _health(conn)
                        conn.getresponse().read()
            finally:
                srv.stop()


class TestRoleHandover:
    """A retired role must never answer on a lingering keep-alive connection.

    Regression: the slave seeder and the master share the client's listen port.
    On promotion the serving role is swapped on the same listener, but an
    established HTTP/1.1 keep-alive connection (pooled by the peer) must not
    keep being served by the retired role — so a master-style download
    (``filename``+``cid``, no ``sha256``) was rejected with 400 "Invalid seeder
    query" and the failover never resumed sync (black-box:
    ``test_master_death_reelects_successor``).
    """

    def test_handover_window_refuses_in_flight_requests(self, server):
        """While the role is being retired, a pooled request is refused (503).

        ``stop_serving()`` is the yield: the listener stays up but
        serves nothing, so a request already in flight cannot be handled under
        the retired role (ADR-0027).
        """
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            with patch(
                "hsync_client.p2p_server.CONFIG",
                {"LOCAL_BUCKET": "/tmp", "P2P_KEY": None, "REMOTE_BUCKET": "b"},
            ):
                first = _health(conn)
                assert first.status == 200  # pooled connection
                first.read()  # drain before reusing the connection

                # Handover in progress: the listener retired its serving role.
                srv.stop_serving()

                conn.request("GET", "/api/v1/health")
                response = conn.getresponse()
                response.read()
                assert response.status == 503, (
                    "a request on a retired-role connection must be refused, "
                    f"not served (got {response.status})"
                )
        finally:
            conn.close()
