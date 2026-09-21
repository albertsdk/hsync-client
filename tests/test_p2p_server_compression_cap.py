"""Regression tests for bounded P2P compressed-download staging."""

import tempfile
import time
from unittest.mock import patch

import pytest
from hsync_common.compression import decompress_data

from hsync_client.p2p_peers import get_free_port
from hsync_client.p2p_server import P2PServer

_CONTENT = b"0123456789" * 15_000  # 150 KB of trivially compressible data


@pytest.fixture
def server():
    """Create and start a test server."""
    port = get_free_port()
    srv = P2PServer(port=port)
    srv.start()
    time.sleep(0.1)
    yield srv, port
    srv.stop()


def _download(
    port: int,
    filename: str,
    *,
    accept_encoding: str = "identity",
) -> tuple[int, dict[str, str], bytes]:
    """GET a file from the master, returning (status, headers, body)."""
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(
            "GET",
            f"/api/v1/buckets/test/files/download?filename={filename}",
            headers={"Accept-Encoding": accept_encoding},
        )
        response = conn.getresponse()
        body = response.read()
        headers = {k.lower(): v for k, v in response.getheaders()}
        return response.status, headers, body
    finally:
        conn.close()


class TestCompressedDownloadCap:
    def test_compressible_file_below_cap_is_zstd(self, server, tmp_path):
        """Below the cap (and above the P2P threshold) the body is zstd."""
        _, port = server
        (tmp_path / "big.txt").write_bytes(_CONTENT)

        with (
            patch(
                "hsync_client.p2p_server.CONFIG",
                {
                    "LOCAL_BUCKET": str(tmp_path),
                    "P2P_KEY": None,
                    "COMPRESSION_ENABLED": True,
                },
            ),
            patch(
                "hsync_client.p2p_server.tempfile.TemporaryFile",
                wraps=tempfile.TemporaryFile,
            ) as mock_temporary_file,
        ):
            status, headers, body = _download(port, "big.txt", accept_encoding="zstd")

        assert status == 200
        assert headers.get("content-encoding") == "zstd"
        assert decompress_data(body) == _CONTENT
        mock_temporary_file.assert_called_once_with(mode="w+b")

    def test_file_above_cap_is_served_uncompressed(self, server, tmp_path, monkeypatch):
        """Above the cap the master serves the streaming uncompressed path."""
        from hsync_client import p2p_server

        monkeypatch.setattr(p2p_server, "P2P_COMPRESSION_MAX_FILE_BYTES", 1024)
        _, port = server
        (tmp_path / "big.txt").write_bytes(_CONTENT)

        with patch(
            "hsync_client.p2p_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "P2P_KEY": None,
                "COMPRESSION_ENABLED": True,
            },
        ):
            status, headers, body = _download(port, "big.txt", accept_encoding="zstd")

        assert status == 200
        assert "content-encoding" not in headers
        assert body == _CONTENT
        assert headers.get("x-checksum-sha256")

    def test_compression_disabled_serves_uncompressed(self, server, tmp_path):
        """With compression disabled the plain streaming path serves bytes."""
        _, port = server
        (tmp_path / "big.txt").write_bytes(_CONTENT)

        with patch(
            "hsync_client.p2p_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "P2P_KEY": None,
                "COMPRESSION_ENABLED": False,
            },
        ):
            status, headers, body = _download(port, "big.txt")

        assert status == 200
        assert "content-encoding" not in headers
        assert body == _CONTENT
