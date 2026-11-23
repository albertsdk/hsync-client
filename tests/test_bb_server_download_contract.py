# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box tests for the file-download response contract (server mode).

ADR-0053: a compressed (zstd) body is streamed without a ``Content-Length`` and
carries ``X-Uncompressed-Size`` for progress/pre-allocation; a ``Range`` request
is always answered with raw bytes and ``Accept-Ranges: bytes`` so a resume is
exact. Observed only through the public REST download endpoint.

Requires ``HSYNC_BB_SERVER=1`` to run.
"""

import os

import pytest

from .conftest import BB_CLIENT_CID

_BB_SERVER_ENABLED = os.environ.get("HSYNC_BB_SERVER") == "1"
_BB_SERVER_REASON = "Black-box server-mode tests require HSYNC_BB_SERVER=1"

pytestmark = pytest.mark.skipif(not _BB_SERVER_ENABLED, reason=_BB_SERVER_REASON)


def _compressible_payload() -> bytes:
    """A payload well above the 10 KiB compression threshold and highly compressible."""
    return b"hsync compressible line for the download contract\n" * 4096


def _get(live_server, bucket: str, headers: dict | None = None):
    """One GET of the download endpoint, response read (no decompression assumed)."""
    import requests

    base, api_key = live_server
    merged = {"Authorization": f"Bearer {api_key}", **(headers or {})}
    return requests.get(
        f"{base}/api/v1/buckets/{bucket}/files/download",
        params={"filename": "payload.txt", "cid": BB_CLIENT_CID},
        headers=merged,
        timeout=30.0,
        verify=False,
    )


class TestDownloadContract:
    def test_compressed_download_advertises_the_uncompressed_size(
        self, live_server, bb_bucket, bb_server_upload
    ):
        """A zstd body is chunked but still tells the client the final size."""
        # Given a compressible file in the bucket
        content = _compressible_payload()
        bb_server_upload(bb_bucket, "payload.txt", content)

        # When it is requested with zstd accepted
        response = _get(live_server, bb_bucket, {"Accept-Encoding": "zstd"})

        # Then the body is compressed, streamed (no length) and carries the size
        assert response.status_code == 200, response.text
        assert response.headers.get("Content-Encoding") == "zstd"
        assert "Content-Length" not in response.headers
        assert response.headers.get("X-Uncompressed-Size") == str(len(content))

    def test_a_range_request_is_identity_without_the_hint(
        self, live_server, bb_bucket, bb_server_upload
    ):
        """A Range (resume) is answered raw, so the byte offset is exact."""
        # Given a compressible file in the bucket
        content = _compressible_payload()
        bb_server_upload(bb_bucket, "payload.txt", content)

        # When a byte range is requested, even with zstd accepted
        response = _get(
            live_server,
            bb_bucket,
            {"Accept-Encoding": "zstd", "Range": "bytes=0-99"},
        )

        # Then the answer is the raw prefix, never a compressed body
        assert response.status_code == 206, response.text
        assert response.headers.get("Content-Encoding") in (None, "", "identity")
        assert response.headers.get("X-Uncompressed-Size") is None
        assert response.headers.get("Accept-Ranges") == "bytes"
        assert response.content == content[:100]
