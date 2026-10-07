# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box tests for the P2P master upload rejection paths.

Drives the *real* P2P master over its public HTTPS REST surface to pin the
rejection contract of the stdlib P2P server (``hsync_client.p2p_server``),
which is a separate implementation from the FastAPI server. Status codes
therefore differ in places: traversal and engine-artifact names are rejected
with 403 ``Access denied`` here (not 400 as on the central server).

Parity with ``hsync-server/tests/test_bb_file_upload.py``
(``TestUploadNegative``).

Requires HSYNC_BB_P2P=1 to run.
"""

import hashlib
import os

import pytest
import zstandard as zstd

from .p2p_helpers import (
    _p2p_auth_headers,
    _p2p_cid,
    _p2p_serving_master,
    _p2p_upload,
)

_P2P_ENABLED = os.environ.get("HSYNC_BB_P2P") == "1"
_P2P_REASON = (
    "P2P e2e needs loopback mDNS + two live client instances; set HSYNC_BB_P2P=1 to enable"
)

pytestmark = pytest.mark.skipif(not _P2P_ENABLED, reason=_P2P_REASON)


def _upload_without_checksum(base_url: str, bucket: str, filename: str, content: bytes):
    """Upload a file while omitting the ``sha256sum`` multipart field."""
    import requests

    return requests.post(
        f"{base_url}/api/v1/buckets/{bucket}/files",
        data={
            "cid": _p2p_cid("neg"),
            "path": "",
            "permissions": "644",
            "offset": "0",
            "size": "0",
            "compressed": "false",
        },
        files={"file": (filename, content, "application/octet-stream")},
        headers=_p2p_auth_headers(),
        timeout=30.0,
        verify=False,
    )


def _compress(content: bytes) -> bytes:
    """zstd-compress *content* for a compressed-upload negative test."""
    return zstd.ZstdCompressor().compress(content)


class TestP2PUploadNegative:
    """Rejection paths of the P2P master's public upload endpoint."""

    def test_missing_checksum_rejected(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        # Given a serving P2P master
        with _p2p_serving_master(p2p_master_dir, p2p_slave_dir, p2p_bucket) as p2p_listen_port:
            base = f"https://127.0.0.1:{p2p_listen_port}"

            # When uploading without the required sha256sum field
            r = _upload_without_checksum(base, p2p_bucket, "nosh.bin", b"x")

            # Then the master rejects the incomplete multipart upload
            assert r.status_code == 400, r.text
            assert "sha256sum" in r.text.lower()

    def test_invalid_checksum_format_rejected(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        # Given a serving P2P master
        with _p2p_serving_master(p2p_master_dir, p2p_slave_dir, p2p_bucket) as p2p_listen_port:
            base = f"https://127.0.0.1:{p2p_listen_port}"

            # When uploading with a non-hex sha256sum value
            r = _p2p_upload(base, p2p_bucket, "badhex.bin", b"x", sha256sum="not-a-hex")

            # Then the master rejects the malformed digest
            assert r.status_code == 400, r.text
            assert "sha256sum" in r.text.lower()

    def test_checksum_mismatch_rejected(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        # Given a serving P2P master
        with _p2p_serving_master(p2p_master_dir, p2p_slave_dir, p2p_bucket) as p2p_listen_port:
            base = f"https://127.0.0.1:{p2p_listen_port}"

            # When uploading bytes whose digest does not match the declared one
            r = _p2p_upload(base, p2p_bucket, "mismatch.bin", b"payload", sha256sum="0" * 64)

            # Then the master refuses to commit the corrupt content
            assert r.status_code == 400, r.text
            assert "checksum mismatch" in r.text.lower()

    def test_path_traversal_rejected(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        # Given a serving P2P master
        with _p2p_serving_master(p2p_master_dir, p2p_slave_dir, p2p_bucket) as p2p_listen_port:
            base = f"https://127.0.0.1:{p2p_listen_port}"

            # When uploading with a path that escapes the bucket
            r = _p2p_upload(base, p2p_bucket, "x.bin", b"escape", path="../other-bucket")

            # Then the master confines the write and denies the request
            assert r.status_code == 403, r.text
            assert "access denied" in r.text.lower()

    def test_internal_engine_filename_rejected(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        # Given a serving P2P master
        with _p2p_serving_master(p2p_master_dir, p2p_slave_dir, p2p_bucket) as p2p_listen_port:
            base = f"https://127.0.0.1:{p2p_listen_port}"

            # When uploading a filename owned by the sync engine
            r = _p2p_upload(base, p2p_bucket, ".hsyncignore", b"internal")

            # Then the master refuses to let a peer rewrite engine artifacts
            assert r.status_code == 403, r.text
            assert "access denied" in r.text.lower()

    def test_compressed_with_offset_rejected(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        # Given a serving P2P master and a zstd payload
        with _p2p_serving_master(p2p_master_dir, p2p_slave_dir, p2p_bucket) as p2p_listen_port:
            base = f"https://127.0.0.1:{p2p_listen_port}"
            original = b"compressible content " * 20
            compressed = _compress(original)

            # When uploading it as compressed AND resumable (offset > 0)
            r = _p2p_upload(
                base,
                p2p_bucket,
                "c.bin",
                compressed,
                sha256sum=hashlib.sha256(original).hexdigest(),
                size=len(original),
                compressed="true",
                offset=1,
            )

            # Then the master rejects the incompatible combination
            assert r.status_code == 400, r.text

    def test_compressed_missing_size_rejected(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        # Given a serving P2P master and a zstd payload with no declared size
        with _p2p_serving_master(p2p_master_dir, p2p_slave_dir, p2p_bucket) as p2p_listen_port:
            base = f"https://127.0.0.1:{p2p_listen_port}"
            original = b"compressible content " * 20
            compressed = _compress(original)

            # When uploading it compressed without the required size field
            r = _p2p_upload(
                base,
                p2p_bucket,
                "c2.bin",
                compressed,
                sha256sum=hashlib.sha256(original).hexdigest(),
                compressed="true",
            )

            # Then the master rejects the missing decompressed-size bound
            assert r.status_code == 400, r.text
