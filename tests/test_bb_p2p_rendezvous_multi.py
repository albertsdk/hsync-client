# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Two independent rendezvouss: tables are per node, the union is client state.

The client can be configured with several rendezvous servers and merges what it
learns from each of them. This suite proves the contract that makes that safe and
useful, against two *real* sibling rendezvouss on loopback:

1. a peer registered on node A is visible **only** through A, and one registered
   on B only through B — the nodes never share state (`rdv/1` §Transport);
2. a client that queries both gets both, and merging them is a client-side union
   keyed by CID (so two nodes describing the same peer is still one candidate);
3. a node that goes away leaves the other one's view intact (availability), which
   is the reason a client leases every reachable server.

Gated by ``HSYNC_BB_P2P=1`` and skipped when the sibling rendezvous is not built.
"""

from __future__ import annotations

import os

import pytest
from hsync_common.protocol.tls_trust import TrustPolicy

from hsync_client.rdv_client import (
    RendezvousClient,
    build_leave_frame,
    build_query_frame,
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
    reason="Two-node rendezvous needs real rendezvouss; set HSYNC_BB_P2P=1 to enable",
)

CID_C = "C1b2C3d4E5f6G7h8I9j0K"
CID_D = "D1b2C3d4E5f6G7h8I9j0K"
FP = "aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899"
BUCKET = "shared"


def _node(tmp_path, name: str):
    """Start one loopback rendezvous; return ``(proc, (host, port))``."""
    port = _reserve_udp_port()
    ready_file = tmp_path / f"{name}.ready"
    proc = _rendezvous_start(port, ready_file)
    if not _wait_for_rendezvous(proc, ready_file):
        _stop_rendezvous(proc)
        pytest.skip(f"rendezvous {name} did not become ready")
    return proc, ("127.0.0.1", port)


def _client(endpoint, *, tofu: bool = True) -> RendezvousClient:
    """A client pinned to one node (the loopback certs are ephemeral per run)."""
    policy = TrustPolicy.accept_new if tofu else TrustPolicy.strict
    return RendezvousClient(endpoint[0], endpoint[1], policy=policy)


def _register(client: RendezvousClient, cid: str) -> None:
    ack = client.register(
        build_register_frame(
            cid=cid,
            bucket=BUCKET,
            cert_fp=FP,
            version="1.0.0",
            endpoints=[{"proto": "tcp", "host": "198.51.100.20", "port": 8443}],
        )
    )
    assert ack is not None, "register was not acknowledged"


def _peers(client: RendezvousClient) -> set[str]:
    response = client.query(build_query_frame(bucket=BUCKET, limit=20))
    assert response is not None, "query was dropped"
    assert not response.get("error"), response.get("error")
    return {str(peer["cid"]) for peer in response["peers"]}


def test_each_node_only_knows_its_own_peers(tmp_path):
    """Two live nodes never share their tables: the union lives in the client."""
    proc_a, node_a = _node(tmp_path, "a")
    try:
        proc_b, node_b = _node(tmp_path, "b")
        try:
            _register(_client(node_a), CID_C)
            _register(_client(node_b), CID_D)

            assert _peers(_client(node_a)) == {CID_C}
            assert _peers(_client(node_b)) == {CID_D}

            # The fan-out the client performs: query every configured node and
            # union the samples (deduplicated by CID).
            merged = _peers(_client(node_a)) | _peers(_client(node_b))
            assert merged == {CID_C, CID_D}
        finally:
            _stop_rendezvous(proc_b)
    finally:
        _stop_rendezvous(proc_a)


def test_a_dead_node_does_not_hide_the_other_nodes_peers(tmp_path):
    """Availability: the surviving node still answers for its own peers."""
    proc_a, node_a = _node(tmp_path, "a")
    proc_b, node_b = _node(tmp_path, "b")
    try:
        _register(_client(node_a), CID_C)
        _register(_client(node_b), CID_D)
    finally:
        _stop_rendezvous(proc_b)

    # Node A is untouched by B's disappearance, and B's peers are simply gone
    # from B's table (the client re-registers there when it comes back).
    assert _peers(_client(node_a)) == {CID_C}
    _stop_rendezvous(proc_a)


def test_the_same_peer_seen_through_both_nodes_is_one_candidate(tmp_path):
    """One CID registered on both nodes dedupes when the samples are merged."""
    proc_a, node_a = _node(tmp_path, "a")
    try:
        proc_b, node_b = _node(tmp_path, "b")
        try:
            _register(_client(node_a), CID_C)
            _register(_client(node_b), CID_C)

            peers = _peers(_client(node_a)) | _peers(_client(node_b))
            assert peers == {CID_C}
        finally:
            _stop_rendezvous(proc_b)
    finally:
        _stop_rendezvous(proc_a)


def test_a_leave_on_one_node_does_not_touch_the_other(tmp_path):
    """``leave`` is scoped to the node that receives it.

    Whether the addressed node forgets the record is covered by the single-node
    roundtrip suite (`test_bb_p2p_rendezvous_client`); what this suite owns is that
    the *other* node keeps serving its own table, so one lease ending (or one
    node being poked) never removes a peer from the client's union.
    """
    proc_a, node_a = _node(tmp_path, "a")
    try:
        proc_b, node_b = _node(tmp_path, "b")
        try:
            _register(_client(node_a), CID_C)
            _register(_client(node_b), CID_C)

            _client(node_a).leave(build_leave_frame(cid=CID_C, bucket=BUCKET))

            assert _peers(_client(node_b)) == {CID_C}
        finally:
            _stop_rendezvous(proc_b)
    finally:
        _stop_rendezvous(proc_a)
