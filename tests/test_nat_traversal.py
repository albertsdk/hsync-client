# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Unit tests for the bootnode endpoint candidates (NAT traversal)."""

from __future__ import annotations

from hsync_client.nat_traversal import CandidateEndpoint, candidate_endpoints
from hsync_client.stun import ObservedAddress


def _wire(candidates):
    return [c.as_wire() for c in candidates]


def test_observed_ip_with_listen_port_never_the_quic_source_port():
    """Regression: the observed QUIC/UDP source port must not leak as a TCP port."""
    observed = ObservedAddress(host="203.0.113.9", port=51000)
    candidates = candidate_endpoints(local_ip="192.168.1.5", listen_port=8443, observed=observed)
    assert candidates[0] == CandidateEndpoint("tcp", "203.0.113.9", 8443)
    assert 51000 not in [c.port for c in candidates]
    assert _wire(candidates)[0] == {"proto": "tcp", "host": "203.0.113.9", "port": 8443}


def test_upnp_external_port_used_for_both_public_and_local_hosts():
    observed = ObservedAddress(host="203.0.113.9", port=51000)
    candidates = candidate_endpoints(
        local_ip="192.168.1.5", listen_port=8443, observed=observed, external_port=9443
    )
    assert CandidateEndpoint("tcp", "203.0.113.9", 9443) in candidates
    assert CandidateEndpoint("tcp", "192.168.1.5", 9443) in candidates


def test_local_only_without_observed():
    candidates = candidate_endpoints(local_ip="192.168.1.5", listen_port=8443)
    assert candidates == [CandidateEndpoint("tcp", "192.168.1.5", 8443)]


def test_duplicates_are_collapsed():
    observed = ObservedAddress(host="192.168.1.5", port=51000)
    candidates = candidate_endpoints(
        local_ip="192.168.1.5", listen_port=8443, observed=observed, external_port=8443
    )
    assert candidates == [CandidateEndpoint("tcp", "192.168.1.5", 8443)]
