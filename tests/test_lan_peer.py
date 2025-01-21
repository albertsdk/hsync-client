# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for Peer data class in LAN module."""

import time

from hsync_client.lan import PEER_TIMEOUT, Peer


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
            version="0.1.0",
        )
        assert peer.version == "0.1.0"

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
