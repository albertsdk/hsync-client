# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box tests for P2P resumable and compressed uploads.

Drives the *real* P2P master over its public HTTPS REST endpoints only. Verifies
the observable resume lifecycle (partial -> status -> resume) and compressed
upload decompression that today are covered only by white-box unit tests.

Parity with ``hsync-server/tests/test_bb_file_upload.py`` (TestUploadResumable,
TestUploadCompressed).

Requires HSYNC_BB_P2P=1 to run.
"""

import hashlib
import os

import pytest
import zstandard as zstd

from .p2p_helpers import (
    _p2p_auth_headers,
    _p2p_download,
    _p2p_serving_master,
    _p2p_upload,
)

_P2P_ENABLED = os.environ.get("HSYNC_BB_P2P") == "1"
_P2P_REASON = (
    "P2P e2e needs loopback mDNS + two live client instances; set HSYNC_BB_P2P=1 to enable"
)

pytestmark = pytest.mark.skipif(not _P2P_ENABLED, reason=_P2P_REASON)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _p2p_upload_status(base_url: str, bucket: str, filename: str, sha: str, size: int) -> dict:
    import requests

    r = requests.get(
        f"{base_url}/api/v1/buckets/{bucket}/files/upload-status",
        params={"filename": filename, "sha256sum": sha, "size": str(size)},
        headers=_p2p_auth_headers(),
        timeout=30.0,
        verify=False,
    )
    assert r.status_code == 200, f"upload-status failed: {r.status_code} {r.text}"
    return r.json()


class TestP2PResumableUpload:
    @pytest.mark.flaky(reruns=3, reruns_delay=5)
    def test_partial_then_status_then_resume_completes(
        self, p2p_master_dir, p2p_slave_dir, p2p_bucket
    ):
        # Given a file split into two chunks
        full = b"01234567"
        first, rest = full[:4], full[4:]
        sha = _sha(full)

        with _p2p_serving_master(p2p_master_dir, p2p_slave_dir, p2p_bucket) as p2p_listen_port:
            base = f"https://127.0.0.1:{p2p_listen_port}"

            # When sending the first chunk as a partial upload
            r1 = _p2p_upload(base, p2p_bucket, "res.bin", first, sha256sum=sha, size=len(full))

            # Then the master acknowledges the partial with the received offset
            assert r1.status_code == 202
            assert r1.json()["offset"] == len(first)

            # And the upload-status endpoint reports it as resumable from that offset
            status = _p2p_upload_status(base, p2p_bucket, "res.bin", sha, len(full))
            assert status["status"] == "resumable"
            assert status["offset"] == len(first)

            # When resuming with the remaining bytes
            r2 = _p2p_upload(
                base, p2p_bucket, "res.bin", rest, sha256sum=sha, offset=len(first), size=len(full)
            )

            # Then the upload completes and the downloaded content matches the original
            assert r2.status_code == 201, f"resume failed: {r2.status_code} {r2.text}"
            assert _p2p_download(base, p2p_bucket, "res.bin") == full


class TestP2PCompressedUpload:
    def test_compressed_payload_decompresses_to_declared_size(
        self, p2p_master_dir, p2p_slave_dir, p2p_bucket
    ):
        # Given a compressible payload and its zstd-compressed form
        original = b"compressible content " * 200
        sha = _sha(original)
        compressed = zstd.ZstdCompressor().compress(original)

        with _p2p_serving_master(p2p_master_dir, p2p_slave_dir, p2p_bucket) as p2p_listen_port:
            base = f"https://127.0.0.1:{p2p_listen_port}"

            # When uploading it as a compressed upload (size = decompressed size)
            r = _p2p_upload(
                base,
                p2p_bucket,
                "comp.bin",
                compressed,
                sha256sum=sha,
                size=len(original),
                compressed="true",
            )

            # Then the upload succeeds and the stored file decompresses to the original
            assert r.status_code == 201
            assert _p2p_download(base, p2p_bucket, "comp.bin") == original
