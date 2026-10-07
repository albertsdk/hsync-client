# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box tests for the P2P master delta-apply (upload-side delta).

Drives the *real* P2P master over its public HTTPS REST surface to verify the
observable delta-apply lifecycle (reconstruct + missing-base 404), which today
is covered only on the FastAPI server
(``hsync-server/tests/test_bb_delta_transfer.py`` — ``TestDeltaApplyHappy`` /
``TestDeltaApplyNegative``). The P2P master re-implements the endpoint in
``hsync_client.p2p_delta`` over the same shared ``hsync_common.delta`` core.

Requires HSYNC_BB_P2P=1 to run.
"""

import hashlib
import json
import os

import pytest
from hsync_common.delta import compute_signatures, find_matches, optimal_block_size

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


def _delta_apply(base_url: str, bucket: str, path: str, metadata: dict, literals: bytes):
    """POST the upload-delta multipart payload to the P2P master."""
    import requests

    return requests.post(
        f"{base_url}/api/v1/buckets/{bucket}/files/delta/apply",
        data={"path": path, "metadata": json.dumps(metadata)},
        files={"file": ("literal", literals, "application/octet-stream")},
        headers=_p2p_auth_headers(),
        timeout=30.0,
        verify=False,
    )


class TestP2PDeltaApplyHappy:
    def test_delta_apply_reconstructs_file_from_base(
        self, p2p_master_dir, p2p_slave_dir, p2p_bucket, bb_sample, tmp_path
    ):
        # Given a base file (v1) on the master and a newer version (v2) that
        # differs by a single 4 KB block
        s = bb_sample(suffix=".bin", prefix="apply", size=0)
        filename = s.name
        v1 = b"".join(bytes([i]) * 4096 for i in range(5))
        v2 = v1[:4096] + bytes([255]) * 4096 + v1[8192:]

        with _p2p_serving_master(p2p_master_dir, p2p_slave_dir, p2p_bucket) as p2p_listen_port:
            base_url = f"https://127.0.0.1:{p2p_listen_port}"
            up = _p2p_upload(base_url, p2p_bucket, filename, v1, sha256sum=_sha(v1))
            assert up.status_code == 201, f"{up.status_code} {up.text}. {s.ctx}"

            # When computing the upload delta (v1 -> v2) and posting it
            base_path = tmp_path / "base.bin"
            base_path.write_bytes(v1)
            block_size = optimal_block_size(len(v1))
            sigs = compute_signatures(str(base_path), block_size)
            ops, literals = find_matches(v2, sigs, block_size)
            metadata = {
                "cid": "bbclient0001bbcltBB10",
                "base_sha256": _sha(v1),
                "new_sha256": _sha(v2),
                "new_size": len(v2),
                "block_size": block_size,
                "compressed": False,
                "ops": [op.to_dict() for op in ops],
            }
            r = _delta_apply(base_url, p2p_bucket, filename, metadata, literals)

            # Then the master reconstructs v2 and reports success
            assert r.status_code == 200, f"{r.status_code} {r.text}. {s.ctx}"
            body = r.json()
            assert body["status"] == "ok"
            assert body["sha256"] == _sha(v2)
            assert body["size"] == len(v2)

            # And downloading the file returns the reconstructed content
            assert _p2p_download(base_url, p2p_bucket, filename) == v2


class TestP2PDeltaApplyNegative:
    def test_delta_apply_on_missing_base_returns_404(
        self, p2p_master_dir, p2p_slave_dir, p2p_bucket
    ):
        # Given a file that does not exist on the master
        with _p2p_serving_master(p2p_master_dir, p2p_slave_dir, p2p_bucket) as p2p_listen_port:
            base_url = f"https://127.0.0.1:{p2p_listen_port}"
            metadata = {
                "cid": "bbclient0001bbcltBB10",
                "base_sha256": _sha(b""),
                "new_sha256": _sha(b"x"),
                "new_size": 1,
                "block_size": 4096,
                "compressed": False,
                "ops": [{"type": "data", "offset": 0, "length": 1, "data_offset": 0}],
            }

            # When applying a delta to the missing base
            r = _delta_apply(base_url, p2p_bucket, "no_such_base.bin", metadata, b"x")

            # Then the master reports the base file as not found
            assert r.status_code == 404, r.text
