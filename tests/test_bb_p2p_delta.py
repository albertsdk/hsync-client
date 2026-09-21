# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for the P2P delta-transfer use case.

Drives the *real* P2P master over its public HTTPS REST endpoints using only
the shared ``hsync_common.delta`` wire-contract library (the same public API a
real peer uses). Verifies that a newer file version on the master can be
reconstructed from a client-held base via delta signatures, literal payload and
``apply_delta`` — and that the negative paths are rejected.

Parity with ``hsync-server/tests/test_bb_delta_transfer.py``.

Requires HSYNC_BB_P2P=1 to run.
"""

import hashlib
import os

import pytest
import zstandard as zstd
from hsync_common.delta import apply_delta, compute_signatures, optimal_block_size
from hsync_common.models.delta import DeltaOp

from .p2p_helpers import (
    _p2p_auth_headers,
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


class TestP2PDeltaDownloadHappy:
    def test_delta_reconstructs_new_file_from_base(
        self, p2p_master_dir, p2p_slave_dir, p2p_bucket, tmp_path
    ):
        # Given a base (v1) the client already has, and a newer version (v2)
        # on the master that differs by a single block. Each base block is
        # distinct (different fill byte) so the matcher produces clean ops.
        v1 = b"".join(bytes([i]) * 4096 for i in range(5))  # 5 distinct 4KB blocks
        v2 = v1[:4096] + bytes([255]) * 4096 + v1[8192:]  # one 4KB block changed
        assert len(v1) == len(v2)

        with _p2p_serving_master(p2p_master_dir, p2p_slave_dir, p2p_bucket) as p2p_listen_port:
            base_url = f"https://127.0.0.1:{p2p_listen_port}"
            # Upload v2 to the master so it serves the newer version
            assert _p2p_upload(base_url, p2p_bucket, "delta.bin", v2).status_code == 201
            base_path = tmp_path / "base.bin"
            base_path.write_bytes(v1)

            # When computing base signatures and requesting a download delta
            block_size = optimal_block_size(len(v1))
            sigs = compute_signatures(str(base_path), block_size)
            match = _post_signatures(base_url, p2p_bucket, "delta.bin", block_size, sigs)
            assert match.status_code == 200
            body = match.json()
            assert body["delta"] is True

            # And fetching the literal payload referenced by the data_url
            data_url = body["data_url"]
            data_resp = _get_data_url(base_url, data_url)
            assert data_resp.status_code == 200
            literals = data_resp.content
            if body.get("compressed"):
                literals = zstd.ZstdDecompressor().decompress(literals)

            # Then applying the ops reconstructs the master's new file exactly
            ops = [
                DeltaOp(
                    type=o["type"],
                    offset=o["offset"],
                    length=o["length"],
                    data_offset=o["data_offset"],
                )
                for o in body["instructions"]["ops"]
            ]
            out_path = tmp_path / "rebuilt.bin"
            apply_delta(str(base_path), ops, literals, str(out_path))
            assert out_path.read_bytes() == v2
            assert _sha(out_path.read_bytes()) == body["new_sha256"]


class TestP2PDeltaNegative:
    def test_invalid_token_returns_404(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        # Given a non-existent delta data token
        with _p2p_serving_master(p2p_master_dir, p2p_slave_dir, p2p_bucket) as p2p_listen_port:
            base_url = f"https://127.0.0.1:{p2p_listen_port}"
            # When fetching it
            r = _get_data_url(
                base_url, f"/api/v1/buckets/{p2p_bucket}/files/delta/data/not-a-real-token"
            )
            # Then the master reports it as not found
            assert r.status_code == 404

    def test_signatures_for_missing_file_returns_404(
        self, p2p_master_dir, p2p_slave_dir, p2p_bucket
    ):
        # Given a master bucket with no such file
        with _p2p_serving_master(p2p_master_dir, p2p_slave_dir, p2p_bucket) as p2p_listen_port:
            base_url = f"https://127.0.0.1:{p2p_listen_port}"
            # When requesting its delta signatures
            r = _get_signatures(base_url, p2p_bucket, "nope.bin")
            # Then the master reports it as not found
            assert r.status_code == 404


# ---------------------------------------------------------------------------
# REST drivers (public endpoints only)
# ---------------------------------------------------------------------------


def _post_signatures(base_url, bucket, filename, block_size, sigs):
    import requests

    return requests.post(
        f"{base_url}/api/v1/buckets/{bucket}/files/delta/signatures",
        json={
            "path": filename,
            "block_size": block_size,
            "signatures": [s.to_dict() for s in sigs],
        },
        headers=_p2p_auth_headers(),
        timeout=30.0,
        verify=False,
    )


def _get_signatures(base_url, bucket, filename):
    import requests

    return requests.get(
        f"{base_url}/api/v1/buckets/{bucket}/files/delta/signatures",
        params={"path": filename},
        headers=_p2p_auth_headers(),
        timeout=30.0,
        verify=False,
    )


def _get_data_url(base_url, data_url):
    import requests

    return requests.get(
        f"{base_url}{data_url}",
        headers=_p2p_auth_headers(),
        timeout=30.0,
        verify=False,
    )
