# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box integration: ``RendezvousClient`` against the real ``hsync-rendezvous``.

The rendezvous is a separate repository. This test spawns its sibling binary on a
loopback UDP port (with a scrubbed environment and a ready file) and drives the
public ``rdv/1`` wire through the client's QUIC client: register → ack
(STUN-echo), query → relayed peer, refresh → ack, leave. It asserts only the
wire contract, so it would pass against a C ``hsync-rendezvous`` too.

Gated by ``HSYNC_BB_P2P=1`` (the client BB gate) and skipped when the sibling
rendezvous is not built.
"""

from __future__ import annotations

import os

import pytest
from hsync_common.protocol.tls_trust import TrustPolicy

from hsync_client.rdv_client import (
    RendezvousClient,
    build_leave_frame,
    build_query_frame,
    build_refresh_frame,
    build_register_frame,
)
from tests.p2p_helpers import (
    _rendezvous_start,
    _reserve_udp_port,
    _stop_rendezvous,
    _wait_for_rendezvous,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("HSYNC_BB_P2P") != "1",
    reason="Rendezvous client e2e needs a loopback rendezvous; set HSYNC_BB_P2P=1 to enable",
)

CID = "A1b2C3d4E5f6G7h8I9j0K"
FP = "aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899"
BUCKET = "shared"


@pytest.fixture
def rendezvous_endpoint(tmp_path):
    """Start the sibling rendezvous on loopback and yield its ``(host, port)``."""
    port = _reserve_udp_port()
    ready_file = tmp_path / "rendezvous.ready"
    try:
        proc = _rendezvous_start(port, ready_file)
    except FileNotFoundError:
        pytest.skip("hsync-rendezvous is not built (sibling repo venv missing)")
    try:
        if not _wait_for_rendezvous(proc, ready_file):
            output = proc.stdout.read() if proc.stdout else ""
            pytest.skip(f"rendezvous did not become ready: {output}")
        yield "127.0.0.1", port
    finally:
        _stop_rendezvous(proc)


def test_boot_1_roundtrip_against_real_rendezvous(rendezvous_endpoint):
    """register ack/STUN-echo, query relay, refresh and leave succeed."""
    host, port = rendezvous_endpoint
    client = RendezvousClient(host, port, policy=TrustPolicy.none, timeout=10.0)

    ack = client.register(
        build_register_frame(
            cid=CID,
            bucket=BUCKET,
            cert_fp=FP,
            version="1.0.0",
            capacity=5,
            endpoints=[{"proto": "tcp", "host": "198.51.100.7", "port": 8443}],
        )
    )
    assert ack is not None
    assert ack["observed_endpoint"]["ip"] == "127.0.0.1"
    assert ack["observed_endpoint"]["port"] > 0
    assert ack["ttl"] > 0

    response = client.query(build_query_frame(bucket=BUCKET))
    assert response is not None
    relayed = [peer for peer in response["peers"] if peer["cid"] == CID]
    assert relayed, "the rendezvous must relay the peer it just registered"
    assert relayed[0]["capacity"] == 5
    assert relayed[0]["reachable"] is True

    refreshed = client.refresh(build_refresh_frame(cid=CID, bucket=BUCKET, connected=2))
    assert refreshed is not None

    client.leave(build_leave_frame(cid=CID, bucket=BUCKET))
