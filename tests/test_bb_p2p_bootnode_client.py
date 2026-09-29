# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box integration: ``BootnodeClient`` against the real ``hsync-bootnode``.

The bootnode is a separate repository. This test spawns its sibling binary on a
loopback UDP port (with a scrubbed environment and a ready file) and drives the
public ``boot/1`` wire through the client's QUIC client: register → ack
(STUN-echo), query → relayed peer, refresh → ack, leave. It asserts only the
wire contract, so it would pass against a Rust ``hsync-bootnode`` too.

Gated by ``HSYNC_BB_P2P=1`` (the client BB gate) and skipped when the sibling
bootnode is not built.
"""

from __future__ import annotations

import os

import pytest
from hsync_common.protocol.bootnode_trust import TrustPolicy

from hsync_client.boot_client import (
    BootnodeClient,
    build_leave_frame,
    build_query_frame,
    build_refresh_frame,
    build_register_frame,
)
from tests.p2p_helpers import (
    _bootnode_start,
    _reserve_udp_port,
    _stop_bootnode,
    _wait_for_bootnode,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("HSYNC_BB_P2P") != "1",
    reason="Bootnode client e2e needs a loopback bootnode; set HSYNC_BB_P2P=1 to enable",
)

CID = "A1b2C3d4E5f6G7h8I9j0K"
FP = "AA:BB:CC"
BUCKET = "shared"


@pytest.fixture
def bootnode_endpoint(tmp_path):
    """Start the sibling bootnode on loopback and yield its ``(host, port)``."""
    port = _reserve_udp_port()
    ready_file = tmp_path / "bootnode.ready"
    try:
        proc = _bootnode_start(port, ready_file)
    except FileNotFoundError:
        pytest.skip("hsync-bootnode is not built (sibling repo venv missing)")
    try:
        if not _wait_for_bootnode(proc, ready_file):
            output = proc.stdout.read() if proc.stdout else ""
            pytest.skip(f"bootnode did not become ready: {output}")
        yield "127.0.0.1", port
    finally:
        _stop_bootnode(proc)


def test_boot_1_roundtrip_against_real_bootnode(bootnode_endpoint):
    """register ack/STUN-echo, query relay, refresh and leave succeed."""
    host, port = bootnode_endpoint
    client = BootnodeClient(host, port, policy=TrustPolicy.none, timeout=10.0)

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
    assert relayed, "the bootnode must relay the peer it just registered"
    assert relayed[0]["capacity"] == 5
    assert relayed[0]["reachable"] is True

    refreshed = client.refresh(build_refresh_frame(cid=CID, bucket=BUCKET, connected=2))
    assert refreshed is not None

    client.leave(build_leave_frame(cid=CID, bucket=BUCKET))
