# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for LAN resumable and compressed uploads.

Drives the *real* LAN master over its public HTTPS REST endpoints only. Verifies
the observable resume lifecycle (partial -> status -> resume) and compressed
upload decompression that today are covered only by white-box unit tests.

Parity with ``hsync-server/tests/test_bb_file_upload.py`` (TestUploadResumable,
TestUploadCompressed).

Requires HSYNC_BB_LAN=1 to run.
"""

import hashlib
import os

import pytest
import zstandard as zstd

from .lan_helpers import (
    _lan_auth_headers,
    _lan_download,
    _lan_serving_master,
    _lan_upload,
)

_LAN_ENABLED = os.environ.get("HSYNC_BB_LAN") == "1"
_LAN_REASON = (
    "LAN e2e needs loopback mDNS + two live client instances; set HSYNC_BB_LAN=1 to enable"
)

pytestmark = pytest.mark.skipif(not _LAN_ENABLED, reason=_LAN_REASON)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _lan_upload_status(base_url: str, bucket: str, filename: str, sha: str, size: int) -> dict:
    import requests

    r = requests.get(
        f"{base_url}/api/v1/buckets/{bucket}/files/upload-status",
        params={"filename": filename, "sha256sum": sha, "size": str(size)},
        headers=_lan_auth_headers(),
        timeout=30.0,
        verify=False,
    )
    assert r.status_code == 200, f"upload-status failed: {r.status_code} {r.text}"
    return r.json()


class TestLanResumableUpload:
    def test_partial_then_status_then_resume_completes(
        self, lan_master_dir, lan_slave_dir, lan_bucket
    ):
        # Given a file split into two chunks
        full = b"01234567"
        first, rest = full[:4], full[4:]
        sha = _sha(full)

        with _lan_serving_master(lan_master_dir, lan_slave_dir, lan_bucket) as lan_port:
            base = f"https://127.0.0.1:{lan_port}"

            # When sending the first chunk as a partial upload
            r1 = _lan_upload(base, lan_bucket, "res.bin", first, sha256sum=sha, size=len(full))

            # Then the master acknowledges the partial with the received offset
            assert r1.status_code == 202
            assert r1.json()["offset"] == len(first)

            # And the upload-status endpoint reports it as resumable from that offset
            status = _lan_upload_status(base, lan_bucket, "res.bin", sha, len(full))
            assert status["status"] == "resumable"
            assert status["offset"] == len(first)

            # When resuming with the remaining bytes
            r2 = _lan_upload(
                base, lan_bucket, "res.bin", rest, sha256sum=sha, offset=len(first), size=len(full)
            )

            # Then the upload completes and the downloaded content matches the original
            assert r2.status_code == 201
            assert _lan_download(base, lan_bucket, "res.bin") == full


class TestLanCompressedUpload:
    def test_compressed_payload_decompresses_to_declared_size(
        self, lan_master_dir, lan_slave_dir, lan_bucket
    ):
        # Given a compressible payload and its zstd-compressed form
        original = b"compressible content " * 200
        sha = _sha(original)
        compressed = zstd.ZstdCompressor().compress(original)

        with _lan_serving_master(lan_master_dir, lan_slave_dir, lan_bucket) as lan_port:
            base = f"https://127.0.0.1:{lan_port}"

            # When uploading it as a compressed upload (size = decompressed size)
            r = _lan_upload(
                base,
                lan_bucket,
                "comp.bin",
                compressed,
                sha256sum=sha,
                size=len(original),
                compressed="true",
            )

            # Then the upload succeeds and the stored file decompresses to the original
            assert r.status_code == 201
            assert _lan_download(base, lan_bucket, "comp.bin") == original
