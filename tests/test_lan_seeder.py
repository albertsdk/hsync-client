"""Public HTTP contract tests for the read-only LAN peer seeder."""

from __future__ import annotations

import hashlib
import socket
from pathlib import Path
from unittest.mock import patch

import pytest
import requests
from hsync_common.constants import HEADER_CHECKSUM_SHA256

from hsync_client.lan_server import LANServer, LANServerMode


def _free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


@pytest.fixture
def seeder(tmp_path):
    port = _free_port()
    server = LANServer(
        port=port,
        mode=LANServerMode.seeder,
        bucket="test",
        peer_read_cap=1,
    )
    server.start()
    try:
        yield server, port, tmp_path
    finally:
        server.stop()


def _params(path: str, content: bytes) -> dict[str, str]:
    return {
        "filename": path,
        "sha256": hashlib.sha256(content).hexdigest(),
        "cid": "S" * 21,
    }


def _url(port: int, suffix: str = "/api/v1/buckets/test/files/download") -> str:
    return f"http://127.0.0.1:{port}{suffix}"


def test_exact_hash_download_and_canonical_header(seeder):
    _server, port, bucket = seeder
    content = b"peer payload"
    (bucket / "file.bin").write_bytes(content)
    with patch("hsync_client.lan_server.CONFIG", {"LOCAL_BUCKET": str(bucket), "LAN_KEY": None}):
        response = requests.get(_url(port), params=_params("file.bin", content), timeout=5)

    assert response.status_code == 200
    assert response.content == content
    assert response.headers[HEADER_CHECKSUM_SHA256] == hashlib.sha256(content).hexdigest()
    assert "X-SHA256" not in response.headers


@pytest.mark.parametrize(
    "params",
    [
        {"filename": "file.bin", "sha256": "a" * 64},
        {"filename": "file.bin", "sha256": "a" * 64, "cid": "short"},
        {
            "filename": "file.bin",
            "sha256": "a" * 64,
            "cid": "S" * 21,
            "extra": "no",
        },
    ],
)
def test_invalid_or_non_exact_query_is_400(seeder, params):
    _server, port, bucket = seeder
    (bucket / "file.bin").write_bytes(b"payload")
    with patch("hsync_client.lan_server.CONFIG", {"LOCAL_BUCKET": str(bucket), "LAN_KEY": None}):
        response = requests.get(_url(port), params=params, timeout=5)
    assert response.status_code == 400


def test_repeated_query_parameter_is_400(seeder):
    _server, port, bucket = seeder
    content = b"payload"
    (bucket / "file.bin").write_bytes(content)
    params = list(_params("file.bin", content).items()) + [("cid", "T" * 21)]
    with patch("hsync_client.lan_server.CONFIG", {"LOCAL_BUCKET": str(bucket), "LAN_KEY": None}):
        response = requests.get(_url(port), params=params, timeout=5)
    assert response.status_code == 400


def test_stale_expected_hash_is_409(seeder):
    _server, port, bucket = seeder
    (bucket / "file.bin").write_bytes(b"current")
    with patch("hsync_client.lan_server.CONFIG", {"LOCAL_BUCKET": str(bucket), "LAN_KEY": None}):
        response = requests.get(_url(port), params=_params("file.bin", b"stale"), timeout=5)
    assert response.status_code == 409


def test_range_resume_is_identity_and_hash_bound(seeder):
    _server, port, bucket = seeder
    content = b"0123456789"
    (bucket / "file.bin").write_bytes(content)
    with patch("hsync_client.lan_server.CONFIG", {"LOCAL_BUCKET": str(bucket), "LAN_KEY": None}):
        response = requests.get(
            _url(port),
            params=_params("file.bin", content),
            headers={"Range": "bytes=4-", "Accept-Encoding": "identity"},
            timeout=5,
        )
    assert response.status_code == 206
    assert response.content == content[4:]
    assert response.headers["Content-Range"] == "bytes 4-9/10"
    assert "Content-Encoding" not in response.headers


def test_key_authentication_applies_to_reads(seeder):
    _server, port, bucket = seeder
    content = b"private"
    (bucket / "file.bin").write_bytes(content)
    config = {"LOCAL_BUCKET": str(bucket), "LAN_KEY": "secret"}
    with patch("hsync_client.lan_server.CONFIG", config):
        denied = requests.get(_url(port), params=_params("file.bin", content), timeout=5)
        allowed = requests.get(
            _url(port),
            params=_params("file.bin", content),
            headers={"Authorization": "Bearer secret"},
            timeout=5,
        )
    assert denied.status_code == 401
    assert allowed.status_code == 200


def test_read_cap_returns_503_before_file_open(seeder):
    server, port, bucket = seeder
    content = b"payload"
    (bucket / "file.bin").write_bytes(content)
    assert server._peer_read_slots is not None
    server._peer_read_slots.acquire()
    try:
        with patch(
            "hsync_client.lan_server.CONFIG", {"LOCAL_BUCKET": str(bucket), "LAN_KEY": None}
        ):
            response = requests.get(_url(port), params=_params("file.bin", content), timeout=5)
    finally:
        server._peer_read_slots.release()
    assert response.status_code == 503


def test_unsupported_routes_and_methods_are_405(seeder):
    _server, port, bucket = seeder
    untouched = Path(bucket) / "file.bin"
    untouched.write_bytes(b"payload")
    with patch("hsync_client.lan_server.CONFIG", {"LOCAL_BUCKET": str(bucket), "LAN_KEY": None}):
        route = requests.get(_url(port, "/api/v1/buckets/test/files"), timeout=5)
        mutation = requests.delete(
            _url(port, "/api/v1/buckets/test/files"), params={"path": "file.bin"}, timeout=5
        )
    assert route.status_code == 405
    assert mutation.status_code == 405
    assert untouched.read_bytes() == b"payload"


def test_unsupported_body_closes_connection_before_pipelined_bytes(seeder):
    """An unread mutation body cannot be parsed as another seeder request."""
    _server, port, bucket = seeder
    request = (
        b"POST /api/v1/buckets/test/files HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Length: 4\r\n"
        b"Connection: keep-alive\r\n\r\n"
        b"junk"
        b"GET /api/v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
    )

    with (
        patch("hsync_client.lan_server.CONFIG", {"LOCAL_BUCKET": str(bucket), "LAN_KEY": None}),
        socket.create_connection(("127.0.0.1", port), timeout=5) as connection,
    ):
        connection.sendall(request)
        connection.shutdown(socket.SHUT_WR)
        response = bytearray()
        while chunk := connection.recv(4096):
            response.extend(chunk)

    assert response.count(b"HTTP/1.1 ") == 1
    assert b" 405 " in response


def test_symlink_is_never_followed(seeder, tmp_path):
    _server, port, bucket = seeder
    outside = tmp_path.parent / "outside-secret"
    outside.write_bytes(b"secret")
    (bucket / "link.bin").symlink_to(outside)
    with patch("hsync_client.lan_server.CONFIG", {"LOCAL_BUCKET": str(bucket), "LAN_KEY": None}):
        response = requests.get(_url(port), params=_params("link.bin", b"secret"), timeout=5)
    assert response.status_code == 403


def test_uppercase_hash_is_rejected_by_exact_wire_contract(seeder):
    _server, port, bucket = seeder
    content = b"exact"
    (bucket / "upper.bin").write_bytes(content)
    params = _params("upper.bin", content)
    params["sha256"] = params["sha256"].upper()

    with patch("hsync_client.lan_server.CONFIG", {"LOCAL_BUCKET": str(bucket), "LAN_KEY": None}):
        response = requests.get(_url(port), params=params, timeout=5)

    assert response.status_code == 400
