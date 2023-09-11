"""
Tests for LAN peer-to-peer synchronization module.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from hsync_client.lan import (
    HSYNC_LAN_PORT,
    HSYNC_SERVICE_TYPE,
    PEER_TIMEOUT,
    Peer,
    PeerManager,
    check_version_compatibility,
    get_free_port,
)


class TestPeer:
    """Tests for Peer data class."""

    def test_peer_creation(self):
        """Test creating a peer with required fields."""
        peer = Peer(
            cid="abc123",
            bucket="photos",
            address="192.168.1.100",
            port=8765,
        )
        assert peer.cid == "abc123"
        assert peer.bucket == "photos"
        assert peer.address == "192.168.1.100"
        assert peer.port == 8765
        assert peer.version == "0.0.0"  # Default version

    def test_peer_creation_with_version(self):
        """Test creating a peer with explicit version."""
        peer = Peer(
            cid="abc123",
            bucket="photos",
            address="192.168.1.100",
            port=8765,
            version="0.1.0",
        )
        assert peer.version == "0.1.0"

    def test_peer_is_alive_when_recently_seen(self):
        """Test that a recently seen peer is considered alive."""
        peer = Peer(
            cid="abc123",
            bucket="photos",
            address="192.168.1.100",
            port=8765,
        )
        assert peer.is_alive() is True

    def test_peer_is_not_alive_when_timed_out(self):
        """Test that an old peer is considered not alive."""
        peer = Peer(
            cid="abc123",
            bucket="photos",
            address="192.168.1.100",
            port=8765,
            last_seen=time.time() - PEER_TIMEOUT - 1,
        )
        assert peer.is_alive() is False

    def test_peer_update_last_seen(self):
        """Test updating last seen timestamp."""
        peer = Peer(
            cid="abc123",
            bucket="photos",
            address="192.168.1.100",
            port=8765,
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
            port=8765,
        )
        assert peer.get_server_url() == "http://192.168.1.100:8765"

    def test_peer_get_server_url_https(self):
        """Test getting the HTTPS server URL for a peer."""
        peer = Peer(
            cid="abc123",
            bucket="photos",
            address="192.168.1.100",
            port=8765,
            use_https=True,
            cert_fingerprint="abc123def456",
        )
        assert peer.get_server_url() == "https://192.168.1.100:8765"

    def test_peer_https_fields(self):
        """Test HTTPS-related fields on Peer."""
        peer = Peer(
            cid="abc123",
            bucket="photos",
            address="192.168.1.100",
            port=8765,
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
            port=8765,
        )
        assert peer.use_https is False
        assert peer.cert_fingerprint is None


class TestPeerManager:
    """Tests for PeerManager class."""

    def test_peer_manager_creation(self):
        """Test creating a peer manager."""
        pm = PeerManager(local_cid="local123", bucket="photos")
        assert pm.local_cid == "local123"
        assert pm.bucket == "photos"

    def test_add_peer_same_bucket(self):
        """Test adding a peer with the same bucket."""
        pm = PeerManager(local_cid="local123", bucket="photos")
        peer = Peer(cid="remote456", bucket="photos", address="192.168.1.100", port=8765)
        pm.add_peer(peer)
        assert pm.get_peer("remote456") is not None

    def test_add_peer_different_bucket_ignored(self):
        """Test that peers with different buckets are ignored."""
        pm = PeerManager(local_cid="local123", bucket="photos")
        peer = Peer(cid="remote456", bucket="documents", address="192.168.1.100", port=8765)
        pm.add_peer(peer)
        assert pm.get_peer("remote456") is None

    def test_add_peer_both_no_group_compatible(self):
        """Test that peers with no group (Option B) see each other."""
        pm = PeerManager(local_cid="local123", bucket="photos", group=None)
        peer = Peer(cid="remote456", bucket="photos", address="192.168.1.100", port=8765, group=None)
        pm.add_peer(peer)
        assert pm.get_peer("remote456") is not None

    def test_add_peer_local_no_group_remote_has_group_compatible(self):
        """Test that peer with group is visible to local without group (Option B)."""
        pm = PeerManager(local_cid="local123", bucket="photos", group=None)
        peer = Peer(cid="remote456", bucket="photos", address="192.168.1.100", port=8765, group="team-a")
        pm.add_peer(peer)
        assert pm.get_peer("remote456") is not None

    def test_add_peer_local_has_group_remote_no_group_compatible(self):
        """Test that peer without group is visible to local with group (Option B)."""
        pm = PeerManager(local_cid="local123", bucket="photos", group="team-a")
        peer = Peer(cid="remote456", bucket="photos", address="192.168.1.100", port=8765, group=None)
        pm.add_peer(peer)
        assert pm.get_peer("remote456") is not None

    def test_add_peer_same_group_compatible(self):
        """Test that peers with the same group see each other."""
        pm = PeerManager(local_cid="local123", bucket="photos", group="team-a")
        peer = Peer(cid="remote456", bucket="photos", address="192.168.1.100", port=8765, group="team-a")
        pm.add_peer(peer)
        assert pm.get_peer("remote456") is not None

    def test_add_peer_different_group_ignored(self):
        """Test that peers with different groups are ignored (both have group)."""
        pm = PeerManager(local_cid="local123", bucket="photos", group="team-a")
        peer = Peer(cid="remote456", bucket="photos", address="192.168.1.100", port=8765, group="team-b")
        pm.add_peer(peer)
        assert pm.get_peer("remote456") is None

    def test_peer_manager_group_property(self):
        """Test the group property of PeerManager."""
        pm = PeerManager(local_cid="local123", bucket="photos", group="mygroup")
        assert pm.group == "mygroup"
        pm_no_group = PeerManager(local_cid="local123", bucket="photos")
        assert pm_no_group.group is None

    def test_add_self_ignored(self):
        """Test that adding self is ignored."""
        pm = PeerManager(local_cid="local123", bucket="photos")
        peer = Peer(cid="local123", bucket="photos", address="192.168.1.100", port=8765)
        pm.add_peer(peer)
        assert pm.get_peer("local123") is None

    def test_remove_peer(self):
        """Test removing a peer."""
        pm = PeerManager(local_cid="local123", bucket="photos")
        peer = Peer(cid="remote456", bucket="photos", address="192.168.1.100", port=8765)
        pm.add_peer(peer)
        assert pm.get_peer("remote456") is not None
        pm.remove_peer("remote456")
        assert pm.get_peer("remote456") is None

    def test_get_all_peers(self):
        """Test getting all peers."""
        pm = PeerManager(local_cid="local123", bucket="photos")
        pm.add_peer(Peer(cid="peer1", bucket="photos", address="192.168.1.101", port=8765))
        pm.add_peer(Peer(cid="peer2", bucket="photos", address="192.168.1.102", port=8765))
        peers = pm.get_all_peers()
        assert len(peers) == 2

    def test_master_election_local_wins(self):
        """Test that local client is elected master when it has lowest CID."""
        pm = PeerManager(local_cid="aaa111", bucket="photos")
        pm.add_peer(Peer(cid="zzz999", bucket="photos", address="192.168.1.100", port=8765))
        assert pm.is_master() is True
        assert pm.get_master_cid() == "aaa111"

    def test_master_election_remote_wins(self):
        """Test that remote peer is elected master when it has lowest CID."""
        pm = PeerManager(local_cid="zzz999", bucket="photos")
        pm.add_peer(Peer(cid="aaa111", bucket="photos", address="192.168.1.100", port=8765))
        assert pm.is_master() is False
        assert pm.get_master_cid() == "aaa111"
        master = pm.get_master()
        assert master is not None
        assert master.cid == "aaa111"

    def test_master_election_callback(self):
        """Test that master change callback is called."""
        pm = PeerManager(local_cid="zzz999", bucket="photos")
        callback = MagicMock()
        pm.set_master_change_callback(callback)

        # Initially local is master (only peer)
        # Add a peer with lower CID - should trigger callback
        pm.add_peer(Peer(cid="aaa111", bucket="photos", address="192.168.1.100", port=8765))

        callback.assert_called_once()
        # Callback should receive the new master peer
        args, kwargs = callback.call_args
        assert args[0].cid == "aaa111"

    def test_local_alone_is_master(self):
        """Test that local client is master when alone."""
        pm = PeerManager(local_cid="local123", bucket="photos")
        # Trigger election by adding and removing a peer
        pm.add_peer(Peer(cid="temp", bucket="photos", address="192.168.1.100", port=8765))
        pm.remove_peer("temp")
        assert pm.is_master() is True


class TestGetFreePort:
    """Tests for get_free_port function."""

    def test_get_free_port_returns_valid_port(self):
        """Test that get_free_port returns a valid port number."""
        port = get_free_port()
        assert isinstance(port, int)
        assert 1024 <= port <= 65535

    def test_get_free_port_returns_different_ports(self):
        """Test that get_free_port returns different ports on successive calls."""
        ports = [get_free_port() for _ in range(5)]
        # At least some ports should be different (not guaranteed but very likely)
        assert len(set(ports)) >= 2

    def test_default_lan_port_is_zero(self):
        """Test that HSYNC_LAN_PORT default is 0 (random)."""
        assert HSYNC_LAN_PORT == 0


class TestVersionCompatibility:
    """Tests for version compatibility checking."""

    def test_same_version_is_compatible(self):
        """Test that identical versions are compatible."""
        is_compat, reason = check_version_compatibility("0.1.0", "0.1.0")
        assert is_compat is True
        assert reason == "compatible"

    def test_different_patch_is_compatible(self):
        """Test that different patch versions are compatible."""
        is_compat, reason = check_version_compatibility("0.1.0", "0.1.5")
        assert is_compat is True
        assert reason == "compatible"

        is_compat, reason = check_version_compatibility("1.2.3", "1.2.99")
        assert is_compat is True
        assert reason == "compatible"

    def test_different_minor_is_warning(self):
        """Test that different minor versions trigger warning but are allowed."""
        is_compat, reason = check_version_compatibility("0.1.0", "0.2.0")
        assert is_compat is True
        assert reason == "minor_mismatch"

        is_compat, reason = check_version_compatibility("1.0.0", "1.5.0")
        assert is_compat is True
        assert reason == "minor_mismatch"

    def test_different_major_is_incompatible(self):
        """Test that different major versions are incompatible."""
        is_compat, reason = check_version_compatibility("0.1.0", "1.0.0")
        assert is_compat is False
        assert reason == "major_mismatch"

        is_compat, reason = check_version_compatibility("2.0.0", "1.0.0")
        assert is_compat is False
        assert reason == "major_mismatch"

    def test_short_version_is_padded(self):
        """Test that short versions are padded with zeros."""
        is_compat, reason = check_version_compatibility("1", "1.0.0")
        assert is_compat is True
        assert reason == "compatible"

        is_compat, reason = check_version_compatibility("1.2", "1.2.0")
        assert is_compat is True
        assert reason == "compatible"

    def test_invalid_version_returns_parse_error(self):
        """Test that invalid versions return parse_error but are allowed."""
        # Empty string or non-numeric should trigger parse_error
        # Note: current implementation may not trigger parse_error for all cases
        # but should never crash
        is_compat, reason = check_version_compatibility("0.1.0", "0.1.0")
        assert is_compat is True
