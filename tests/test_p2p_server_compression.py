# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""The P2P master's compressed download: streamed, for a file of any size.

The frame used to be staged in a temporary file before its headers were sent,
which bounded the branch by the size worth staging and made larger files travel
identity. It is now produced straight into a chunked body, so compressing costs
one read chunk of memory instead of a copy of the payload on disk.
"""

import http.client
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
    yield srv, port
    srv.stop()


def _download(
    port: int,
    filename: str,
    *,
    accept_encoding: str,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """GET a file from the master, returning (status, headers, body)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        headers = {"Accept-Encoding": accept_encoding}
        headers.update(extra_headers or {})
        conn.request(
            "GET",
            f"/api/v1/buckets/test/files/download?filename={filename}",
            headers=headers,
        )
        response = conn.getresponse()
        body = response.read()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, body
    finally:
        conn.close()


def _serve(
    port: int,
    tmp_path,
    *,
    accept_encoding: str,
    extra_headers: dict[str, str] | None = None,
    compression: bool = True,
) -> tuple[int, dict[str, str], bytes]:
    """Download ``big.txt`` through a master configured for this test."""
    with patch(
        "hsync_client.p2p_server.CONFIG",
        {
            "LOCAL_BUCKET": str(tmp_path),
            "P2P_KEY": None,
            "COMPRESSION_ENABLED": compression,
        },
    ):
        return _download(
            port,
            "big.txt",
            accept_encoding=accept_encoding,
            extra_headers=extra_headers,
        )


class TestCompressedDownload:
    def test_a_compressible_file_streams_as_a_chunked_zstd_body(self, server, tmp_path):
        """The frame is produced into the response, not staged on disk."""
        _, port = server
        (tmp_path / "big.txt").write_bytes(_CONTENT)

        status, headers, body = _serve(port, tmp_path, accept_encoding="zstd")

        assert status == 200
        assert headers.get("content-encoding") == "zstd"
        # Unknown length by construction: the frame is written while it is built.
        assert headers.get("transfer-encoding") == "chunked"
        assert "content-length" not in headers
        # A compressed body cannot be byte-addressed, so it never claims ranges.
        assert "accept-ranges" not in headers
        assert decompress_data(body) == _CONTENT
        assert headers.get("x-checksum-sha256")

    def test_a_large_file_is_still_compressed(self, server, tmp_path):
        """No size cap: the branch streams whatever the file's size is."""
        _, port = server
        large = b"A" * (3 * 1024 * 1024)  # well past any staging budget
        (tmp_path / "big.txt").write_bytes(large)

        status, headers, body = _serve(port, tmp_path, accept_encoding="zstd")

        assert status == 200
        assert headers.get("content-encoding") == "zstd"
        assert decompress_data(body) == large

    def test_a_client_that_does_not_accept_zstd_gets_identity(self, server, tmp_path):
        """Compression is negotiated, never imposed."""
        _, port = server
        (tmp_path / "big.txt").write_bytes(_CONTENT)

        status, headers, body = _serve(port, tmp_path, accept_encoding="identity")

        assert status == 200
        assert "content-encoding" not in headers
        assert headers.get("content-length") == str(len(_CONTENT))
        assert headers.get("accept-ranges") == "bytes"
        assert body == _CONTENT

    def test_a_range_request_is_answered_identity(self, server, tmp_path):
        """A resume asks for bytes, so the compressed branch must not answer it."""
        _, port = server
        (tmp_path / "big.txt").write_bytes(_CONTENT)

        status, headers, body = _serve(
            port,
            tmp_path,
            accept_encoding="zstd",
            extra_headers={"Range": "bytes=10-19"},
        )

        assert status == 206
        assert "content-encoding" not in headers
        assert body == _CONTENT[10:20]

    def test_compression_disabled_serves_identity(self, server, tmp_path):
        """With the feature off the plain streaming path serves the bytes."""
        _, port = server
        (tmp_path / "big.txt").write_bytes(_CONTENT)

        status, headers, body = _serve(port, tmp_path, accept_encoding="zstd", compression=False)

        assert status == 200
        assert "content-encoding" not in headers
        assert body == _CONTENT
