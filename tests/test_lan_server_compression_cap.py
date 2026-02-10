"""Regression tests for the LAN master compressed-download size cap.

The compressed download branch buffers the whole file in RAM to build the
zstd frame. Beyond ``LAN_COMPRESSION_MAX_FILE_BYTES`` the master must fall
back to the streaming UNCOMPRESSED path instead of spiking its memory (the
old implementation read up to MAX_INBOUND_SIZE — 10 GB by default — into
RAM, and refused even larger files with a hard 403).
"""

import time
from unittest.mock import patch

import pytest
from hsync_common.compression import decompress_data

from hsync_client.lan import get_free_port
from hsync_client.lan_server import LANServer

_CONTENT = b"0123456789" * 15_000  # 150 KB of trivially compressible data


@pytest.fixture
def server():
    """Create and start a test server."""
    port = get_free_port()
    srv = LANServer(port=port)
    srv.start()
    time.sleep(0.1)
    yield srv, port
    srv.stop()


def _download(port: int, filename: str) -> tuple[int, dict[str, str], bytes]:
    """GET a file from the master, returning (status, headers, body)."""
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", f"/api/v1/buckets/test/files/download?filename={filename}")
        response = conn.getresponse()
        body = response.read()
        headers = {k.lower(): v for k, v in response.getheaders()}
        return response.status, headers, body
    finally:
        conn.close()


class TestCompressedDownloadCap:
    def test_compressible_file_below_cap_is_zstd(self, server, tmp_path):
        """Below the cap (and above the LAN threshold) the body is zstd."""
        _, port = server
        (tmp_path / "big.txt").write_bytes(_CONTENT)

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
                "COMPRESSION_ENABLED": True,
            },
        ):
            status, headers, body = _download(port, "big.txt")

        assert status == 200
        assert headers.get("content-encoding") == "zstd"
        assert decompress_data(body) == _CONTENT

    def test_file_above_cap_is_served_uncompressed(self, server, tmp_path, monkeypatch):
        """Above the cap the master serves the streaming uncompressed path."""
        from hsync_client import lan_server

        monkeypatch.setattr(lan_server, "LAN_COMPRESSION_MAX_FILE_BYTES", 1024)
        _, port = server
        (tmp_path / "big.txt").write_bytes(_CONTENT)

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
                "COMPRESSION_ENABLED": True,
            },
        ):
            status, headers, body = _download(port, "big.txt")

        assert status == 200
        assert "content-encoding" not in headers
        assert body == _CONTENT
        assert headers.get("x-sha256")

    def test_compression_disabled_serves_uncompressed(self, server, tmp_path):
        """With compression disabled the plain streaming path serves bytes."""
        _, port = server
        (tmp_path / "big.txt").write_bytes(_CONTENT)

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
                "COMPRESSION_ENABLED": False,
            },
        ):
            status, headers, body = _download(port, "big.txt")

        assert status == 200
        assert "content-encoding" not in headers
        assert body == _CONTENT
