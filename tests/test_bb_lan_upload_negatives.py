# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for the LAN master upload rejection paths.

Drives the *real* LAN master over its public HTTPS REST surface to pin the
rejection contract of the stdlib LAN server (``hsync_client.lan_server``),
which is a separate implementation from the FastAPI server. Status codes
therefore differ in places: traversal and engine-artifact names are rejected
with 403 ``Access denied`` here (not 400 as on the central server).

Parity with ``hsync-server/tests/test_bb_file_upload.py``
(``TestUploadNegative``).

Requires HSYNC_BB_LAN=1 to run.
"""

import hashlib
import os

import pytest
import zstandard as zstd

from .lan_helpers import (
    _lan_auth_headers,
    _lan_cid,
    _lan_serving_master,
    _lan_upload,
)

_LAN_ENABLED = os.environ.get("HSYNC_BB_LAN") == "1"
_LAN_REASON = (
    "LAN e2e needs loopback mDNS + two live client instances; set HSYNC_BB_LAN=1 to enable"
)

pytestmark = pytest.mark.skipif(not _LAN_ENABLED, reason=_LAN_REASON)


def _upload_without_checksum(base_url: str, bucket: str, filename: str, content: bytes):
    """Upload a file while omitting the ``sha256sum`` multipart field."""
    import requests

    return requests.post(
        f"{base_url}/api/v1/buckets/{bucket}/files",
        data={
            "cid": _lan_cid("neg"),
            "path": "",
            "permissions": "644",
            "offset": "0",
            "size": "0",
            "compressed": "false",
        },
        files={"file": (filename, content, "application/octet-stream")},
        headers=_lan_auth_headers(),
        timeout=30.0,
        verify=False,
    )


def _compress(content: bytes) -> bytes:
    """zstd-compress *content* for a compressed-upload negative test."""
    return zstd.ZstdCompressor().compress(content)


class TestLanUploadNegative:
    """Rejection paths of the LAN master's public upload endpoint."""

    def test_missing_checksum_rejected(self, lan_master_dir, lan_slave_dir, lan_bucket):
        # Given a serving LAN master
        with _lan_serving_master(lan_master_dir, lan_slave_dir, lan_bucket) as lan_port:
            base = f"https://127.0.0.1:{lan_port}"

            # When uploading without the required sha256sum field
            r = _upload_without_checksum(base, lan_bucket, "nosh.bin", b"x")

            # Then the master rejects the incomplete multipart upload
            assert r.status_code == 400, r.text
            assert "sha256sum" in r.text.lower()

    def test_invalid_checksum_format_rejected(self, lan_master_dir, lan_slave_dir, lan_bucket):
        # Given a serving LAN master
        with _lan_serving_master(lan_master_dir, lan_slave_dir, lan_bucket) as lan_port:
            base = f"https://127.0.0.1:{lan_port}"

            # When uploading with a non-hex sha256sum value
            r = _lan_upload(base, lan_bucket, "badhex.bin", b"x", sha256sum="not-a-hex")

            # Then the master rejects the malformed digest
            assert r.status_code == 400, r.text
            assert "sha256sum" in r.text.lower()

    def test_checksum_mismatch_rejected(self, lan_master_dir, lan_slave_dir, lan_bucket):
        # Given a serving LAN master
        with _lan_serving_master(lan_master_dir, lan_slave_dir, lan_bucket) as lan_port:
            base = f"https://127.0.0.1:{lan_port}"

            # When uploading bytes whose digest does not match the declared one
            r = _lan_upload(base, lan_bucket, "mismatch.bin", b"payload", sha256sum="0" * 64)

            # Then the master refuses to commit the corrupt content
            assert r.status_code == 400, r.text
            assert "checksum mismatch" in r.text.lower()

    def test_path_traversal_rejected(self, lan_master_dir, lan_slave_dir, lan_bucket):
        # Given a serving LAN master
        with _lan_serving_master(lan_master_dir, lan_slave_dir, lan_bucket) as lan_port:
            base = f"https://127.0.0.1:{lan_port}"

            # When uploading with a path that escapes the bucket
            r = _lan_upload(base, lan_bucket, "x.bin", b"escape", path="../other-bucket")

            # Then the master confines the write and denies the request
            assert r.status_code == 403, r.text
            assert "access denied" in r.text.lower()

    def test_internal_engine_filename_rejected(self, lan_master_dir, lan_slave_dir, lan_bucket):
        # Given a serving LAN master
        with _lan_serving_master(lan_master_dir, lan_slave_dir, lan_bucket) as lan_port:
            base = f"https://127.0.0.1:{lan_port}"

            # When uploading a filename owned by the sync engine
            r = _lan_upload(base, lan_bucket, ".hsyncignore", b"internal")

            # Then the master refuses to let a peer rewrite engine artifacts
            assert r.status_code == 403, r.text
            assert "access denied" in r.text.lower()

    def test_compressed_with_offset_rejected(self, lan_master_dir, lan_slave_dir, lan_bucket):
        # Given a serving LAN master and a zstd payload
        with _lan_serving_master(lan_master_dir, lan_slave_dir, lan_bucket) as lan_port:
            base = f"https://127.0.0.1:{lan_port}"
            original = b"compressible content " * 20
            compressed = _compress(original)

            # When uploading it as compressed AND resumable (offset > 0)
            r = _lan_upload(
                base,
                lan_bucket,
                "c.bin",
                compressed,
                sha256sum=hashlib.sha256(original).hexdigest(),
                size=len(original),
                compressed="true",
                offset=1,
            )

            # Then the master rejects the incompatible combination
            assert r.status_code == 400, r.text

    def test_compressed_missing_size_rejected(self, lan_master_dir, lan_slave_dir, lan_bucket):
        # Given a serving LAN master and a zstd payload with no declared size
        with _lan_serving_master(lan_master_dir, lan_slave_dir, lan_bucket) as lan_port:
            base = f"https://127.0.0.1:{lan_port}"
            original = b"compressible content " * 20
            compressed = _compress(original)

            # When uploading it compressed without the required size field
            r = _lan_upload(
                base,
                lan_bucket,
                "c2.bin",
                compressed,
                sha256sum=hashlib.sha256(original).hexdigest(),
                compressed="true",
            )

            # Then the master rejects the missing decompressed-size bound
            assert r.status_code == 400, r.text
