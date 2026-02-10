"""Regression tests for the LAN master HTTP/1.1 keep-alive behaviour.

The master previously answered HTTP/1.0 (connection closed after every
response), so every per-file request paid a full TCP+TLS handshake. With
``protocol_version = "HTTP/1.1"`` connections are reused — which requires
every response to carry an exact ``Content-Length`` — and idle connections
are reaped after ``LAN_HANDLER_IDLE_TIMEOUT_S`` so they do not pin master
thread slots forever.
"""

import http.client
import time
from unittest.mock import patch

import pytest

from hsync_client.lan import get_free_port
from hsync_client.lan_server import LANRequestHandler, LANServer


@pytest.fixture
def server():
    """Create and start a test server."""
    port = get_free_port()
    srv = LANServer(port=port)
    srv.start()
    time.sleep(0.1)
    yield srv, port
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
                "hsync_client.lan_server.CONFIG",
                {"LOCAL_BUCKET": "/tmp", "LAN_KEY": None},
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
                "hsync_client.lan_server.CONFIG",
                {"LOCAL_BUCKET": "/tmp", "LAN_KEY": None},
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
                "hsync_client.lan_server.CONFIG",
                {"LOCAL_BUCKET": "/tmp", "LAN_KEY": None},
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
        """An idle keep-alive connection is closed after the handler timeout.

        Without the reap, connections parked in client pools would hold a
        master thread + semaphore slot forever and eventually starve the
        bounded backlog.
        """
        port = get_free_port()
        with patch.object(LANRequestHandler, "timeout", 0.3):
            srv = LANServer(port=port)
            srv.start()
            time.sleep(0.1)
            try:
                with patch(
                    "hsync_client.lan_server.CONFIG",
                    {"LOCAL_BUCKET": "/tmp", "LAN_KEY": None},
                ):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    response = _health(conn)
                    response.read()
                    assert response.status == 200

                    time.sleep(0.8)  # > reap timeout (0.3s)

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
