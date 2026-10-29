# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for Peer data class in P2P module."""

import time

from hsync_client.constants import PEER_TIMEOUT
from hsync_client.p2p_peers import Peer, dial_candidates


class TestPeer:
    """Tests for Peer data class."""

    def test_peer_creation(self):
        """Test creating a peer with required fields."""
        peer = Peer(
            cid="abc123",
            bucket="photos",
            address="192.168.1.100",
            port=9753,
        )
        assert peer.cid == "abc123"
        assert peer.bucket == "photos"
        assert peer.address == "192.168.1.100"
        assert peer.port == 9753
        assert peer.version == "0.0.0"  # Default version

    def test_peer_creation_with_version(self):
        """Test creating a peer with explicit version."""
        peer = Peer(
            cid="abc123",
            bucket="photos",
            address="192.168.1.100",
            port=9753,
            version="1.0.0",
        )
        assert peer.version == "1.0.0"

    def test_peer_is_alive_when_recently_seen(self):
        """Test that a recently seen peer is considered alive."""
        peer = Peer(
            cid="abc123",
            bucket="photos",
            address="192.168.1.100",
            port=9753,
        )
        assert peer.is_alive() is True

    def test_peer_is_not_alive_when_timed_out(self):
        """Test that an old peer is considered not alive."""
        peer = Peer(
            cid="abc123",
            bucket="photos",
            address="192.168.1.100",
            port=9753,
            last_seen=time.time() - PEER_TIMEOUT - 1,
        )
        assert peer.is_alive() is False

    def test_peer_update_last_seen(self):
        """Test updating last seen timestamp."""
        peer = Peer(
            cid="abc123",
            bucket="photos",
            address="192.168.1.100",
            port=9753,
            last_seen=time.time() - 100,
        )
        assert peer.is_alive() is False
        peer.update_last_seen()
        assert peer.is_alive() is True

    def test_peer_is_alive_custom_timeout(self):
        """Test is_alive honours an explicit staleness threshold."""
        peer = Peer(
            cid="abc123",
            bucket="photos",
            address="192.168.1.100",
            port=9753,
            last_seen=time.time() - 10,
        )
        # Within the default 15s window, but stale under a custom 5s threshold.
        assert peer.is_alive() is True
        assert peer.is_alive(timeout=5) is False

    def test_peer_get_server_url(self):
        """Test getting the server URL for a peer."""
        peer = Peer(
            cid="abc123",
            bucket="photos",
            address="192.168.1.100",
            port=9753,
        )
        assert peer.get_server_url() == "http://192.168.1.100:9753"

    def test_peer_get_server_url_https(self):
        """Test getting the HTTPS server URL for a peer."""
        peer = Peer(
            cid="abc123",
            bucket="photos",
            address="192.168.1.100",
            port=9753,
            use_https=True,
            cert_fingerprint="abc123def456",
        )
        assert peer.get_server_url() == "https://192.168.1.100:9753"

    def test_peer_https_fields(self):
        """Test HTTPS-related fields on Peer."""
        peer = Peer(
            cid="abc123",
            bucket="photos",
            address="192.168.1.100",
            port=9753,
            use_https=True,
            cert_fingerprint="abc123def456789",
        )
        assert peer.use_https is True
        assert peer.cert_fingerprint == "abc123def456789"

    def test_peer_defaults_to_http(self):
        """Test that peer defaults to HTTP."""
        peer = Peer(
            cid="abc123",
            bucket="photos",
            address="192.168.1.100",
            port=9753,
        )
        assert peer.use_https is False
        assert peer.cert_fingerprint is None

    def test_peer_sync_direction_default(self):
        """Test that peer sync_direction defaults to bidirectional."""
        peer = Peer(
            cid="abc123",
            bucket="photos",
            address="192.168.1.100",
            port=9753,
        )
        assert peer.sync_direction == "bidirectional"

    def test_peer_sync_direction_custom(self):
        """Test creating a peer with custom sync_direction."""
        peer = Peer(
            cid="abc123",
            bucket="photos",
            address="192.168.1.100",
            port=9753,
            sync_direction="upload-only",
        )
        assert peer.sync_direction == "upload-only"


class TestDialCandidates:
    """The dial-time endpoint ordering (NAT-hairpin avoidance, ADR rdv/1)."""

    def test_same_subnet_candidate_is_tried_first(self):
        """A LAN candidate must outrank the public one for a same-NAT peer."""
        candidates = dial_candidates(
            (("203.0.113.10", 8443), ("192.168.1.42", 8443)),
            address="203.0.113.10",
            port=8443,
            local_ip="192.168.1.50",
        )
        assert candidates == (("192.168.1.42", 8443), ("203.0.113.10", 8443))

    def test_remote_candidates_keep_the_advertised_order(self):
        """With no same-subnet candidate the order is the peer's preference."""
        candidates = dial_candidates(
            (("203.0.113.10", 8443), ("192.168.1.42", 8443)),
            address="203.0.113.10",
            port=8443,
            local_ip="10.0.0.5",
        )
        assert candidates == (("203.0.113.10", 8443), ("192.168.1.42", 8443))

    def test_loopback_candidate_is_treated_as_local(self):
        candidates = dial_candidates(
            (("203.0.113.10", 8443), ("127.0.0.1", 8443)),
            address="203.0.113.10",
            port=8443,
            local_ip="10.0.0.5",
        )
        assert candidates == (("127.0.0.1", 8443), ("203.0.113.10", 8443))

    def test_missing_candidate_list_degrades_to_the_primary(self):
        """The mDNS plan has no candidate list: the primary endpoint is used."""
        assert dial_candidates((), address="192.168.1.100", port=9753, local_ip="10.0.0.5") == (
            ("192.168.1.100", 9753),
        )
        # A non-iterable value (a test double) must not raise.
        assert dial_candidates(object(), address="192.168.1.100", port=9753) == (
            ("192.168.1.100", 9753),
        )
