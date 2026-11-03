# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Unit tests for the rendezvous endpoint candidates (NAT traversal)."""

from __future__ import annotations

from hsync_client.constants import MAX_ADVERTISED_ENDPOINTS
from hsync_client.nat_traversal import CandidateEndpoint, candidate_endpoints
from hsync_client.stun import ObservedAddress


def _wire(candidates):
    return [c.as_wire() for c in candidates]


def test_observed_ip_with_listen_port_never_the_quic_source_port():
    """Regression: the observed QUIC/UDP source port must not leak as a TCP port."""
    observed = ObservedAddress(host="203.0.113.9", port=51000)
    candidates = candidate_endpoints(local_ips=["192.168.1.5"], listen_port=8443, observed=observed)
    assert candidates[0] == CandidateEndpoint("tcp", "203.0.113.9", 8443)
    assert 51000 not in [c.port for c in candidates]
    assert _wire(candidates)[0] == {"proto": "tcp", "host": "203.0.113.9", "port": 8443}


def test_upnp_external_port_used_for_both_public_and_local_hosts():
    observed = ObservedAddress(host="203.0.113.9", port=51000)
    candidates = candidate_endpoints(
        local_ips=["192.168.1.5"], listen_port=8443, observed=observed, external_port=9443
    )
    assert CandidateEndpoint("tcp", "203.0.113.9", 9443) in candidates
    assert CandidateEndpoint("tcp", "192.168.1.5", 9443) in candidates


def test_local_only_without_observed():
    candidates = candidate_endpoints(local_ips=["192.168.1.5"], listen_port=8443)
    assert candidates == [CandidateEndpoint("tcp", "192.168.1.5", 8443)]


def test_duplicates_are_collapsed():
    observed = ObservedAddress(host="192.168.1.5", port=51000)
    candidates = candidate_endpoints(
        local_ips=["192.168.1.5"], listen_port=8443, observed=observed, external_port=8443
    )
    assert candidates == [CandidateEndpoint("tcp", "192.168.1.5", 8443)]


def test_every_interface_is_advertised():
    """A multi-homed node publishes a candidate per interface, primary first.

    Advertising only the default-route address left a peer on one of the node's
    other networks unable to dial it directly (it fell back to the relay).
    """
    candidates = candidate_endpoints(local_ips=["10.145.201.139", "192.168.1.5"], listen_port=8443)
    assert candidates == [
        CandidateEndpoint("tcp", "10.145.201.139", 8443),
        CandidateEndpoint("tcp", "192.168.1.5", 8443),
    ]


def test_the_upnp_port_pairs_only_with_the_default_route_interface():
    """The UPnP mapping belongs to the primary interface's router, not the others."""
    observed = ObservedAddress(host="203.0.113.9", port=51000)
    candidates = candidate_endpoints(
        local_ips=["10.145.201.139", "192.168.1.5"],
        listen_port=8443,
        observed=observed,
        external_port=9443,
    )
    assert CandidateEndpoint("tcp", "10.145.201.139", 9443) in candidates
    assert CandidateEndpoint("tcp", "192.168.1.5", 8443) in candidates
    assert CandidateEndpoint("tcp", "192.168.1.5", 9443) not in candidates


def test_the_list_is_capped_at_the_rendezvous_relay_bound():
    """The node relays only the first four endpoints, so do not send more.

    The list is ordered by usefulness, so the public address and the default-route
    interface survive and the overflow interfaces are the ones dropped.
    """
    observed = ObservedAddress(host="203.0.113.9", port=51000)
    candidates = candidate_endpoints(
        local_ips=["10.0.0.1", "192.168.1.5", "192.168.1.6", "192.168.1.7", "192.168.1.8"],
        listen_port=8443,
        observed=observed,
    )
    assert len(candidates) == MAX_ADVERTISED_ENDPOINTS
    assert candidates[0] == CandidateEndpoint("tcp", "203.0.113.9", 8443)
    assert CandidateEndpoint("tcp", "10.0.0.1", 8443) in candidates
    assert CandidateEndpoint("tcp", "192.168.1.8", 8443) not in candidates
