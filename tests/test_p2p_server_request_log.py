# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Regression: the unified aiohttp transport logs every incoming request.

The former ``ThreadingHTTPServer`` data plane emitted one DEBUG line per
request through ``BaseHTTPRequestHandler.send_response`` → ``log_request``.
The aiohttp bridge overrides ``send_response`` (it only records the status), so
it must invoke ``log_request`` explicitly — otherwise the request line (method,
full path **and query string**, status) disappears from the logs, including the
seeder-side evidence that a peer download was content-addressed
(``filename=…&sha256=…``) that the P2P black-box download test relies on.
"""

from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import HttpVersion

from hsync_client.p2p_server import _AiohttpTransport


def _build_transport(path: str) -> _AiohttpTransport:
    request = SimpleNamespace(
        method="GET",
        version=HttpVersion(1, 1),
        rel_url=SimpleNamespace(raw_path_qs=path),
        headers={},
        remote="192.168.1.102",
    )
    return _AiohttpTransport(request, None, SimpleNamespace())


def test_send_response_logs_the_request_line_with_its_query_string():
    query = (
        "/api/v1/buckets/bucket/files/download"
        "?filename=peer-source.bin&sha256=ab12&cid=2Sj6kGCTEYxRKUjlX1opB"
    )
    transport = _build_transport(query)

    with patch("hsync_client.p2p_server.log") as mock_log:
        transport.send_response(200)

    mock_log.debug.assert_called_once_with(f'192.168.1.102 - "GET {query} HTTP/1.1" 200 -')


def test_send_response_normalizes_an_http_status_to_its_code():
    transport = _build_transport("/api/v1/health")

    with patch("hsync_client.p2p_server.log") as mock_log:
        transport.send_response(HTTPStatus.OK)

    mock_log.debug.assert_called_once_with('192.168.1.102 - "GET /api/v1/health HTTP/1.1" 200 -')
