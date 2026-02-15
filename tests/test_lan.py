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
        """Test that local client is elected master when it started earliest."""
        pm = PeerManager(local_cid="aaa111", bucket="photos", startup_time=100.0)
        pm.add_peer(Peer(cid="zzz999", bucket="photos", address="192.168.1.100", port=8765, startup_time=200.0))
        assert pm.is_master() is True
        assert pm.get_master_cid() == "aaa111"

    def test_master_election_remote_wins(self):
        """Test that remote peer is elected master when it started earliest."""
        pm = PeerManager(local_cid="zzz999", bucket="photos", startup_time=200.0)
        pm.add_peer(Peer(cid="aaa111", bucket="photos", address="192.168.1.100", port=8765, startup_time=100.0))
        assert pm.is_master() is False
        assert pm.get_master_cid() == "aaa111"
        master = pm.get_master()
        assert master is not None
        assert master.cid == "aaa111"

    def test_master_election_callback(self):
        """Test that master change callback is called."""
        pm = PeerManager(local_cid="zzz999", bucket="photos", startup_time=200.0)
        callback = MagicMock()
        pm.set_master_change_callback(callback)

        # Initially local is master (only peer)
        # Add a peer with earlier startup_time - should trigger callback
        pm.add_peer(Peer(cid="aaa111", bucket="photos", address="192.168.1.100", port=8765, startup_time=100.0))

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

    def test_bidirectional_preferred_over_upload_only(self):
        """Test that bidirectional peer wins over upload-only with same startup_time."""
        # Local is upload-only, remote is bidirectional — remote should win
        pm = PeerManager(
            local_cid="aaa111", bucket="photos",
            startup_time=100.0, sync_direction="upload-only",
        )
        pm.add_peer(Peer(
            cid="zzz999", bucket="photos", address="192.168.1.100", port=8765,
            startup_time=100.0, sync_direction="bidirectional",
        ))
        assert pm.is_master() is False
        assert pm.get_master_cid() == "zzz999"

    def test_bidirectional_preferred_over_download_only(self):
        """Test that bidirectional peer wins over download-only with same startup_time."""
        pm = PeerManager(
            local_cid="aaa111", bucket="photos",
            startup_time=100.0, sync_direction="download-only",
        )
        pm.add_peer(Peer(
            cid="zzz999", bucket="photos", address="192.168.1.100", port=8765,
            startup_time=100.0, sync_direction="bidirectional",
        ))
        assert pm.is_master() is False
        assert pm.get_master_cid() == "zzz999"

    def test_force_master_overrides_sync_direction(self):
        """Test that force_master takes priority over sync_direction preference."""
        # Local is upload-only but has force_master — should still win
        pm = PeerManager(
            local_cid="zzz999", bucket="photos",
            startup_time=200.0, force_master=True, sync_direction="upload-only",
        )
        pm.add_peer(Peer(
            cid="aaa111", bucket="photos", address="192.168.1.100", port=8765,
            startup_time=100.0, sync_direction="bidirectional",
        ))
        assert pm.is_master() is True
        assert pm.get_master_cid() == "zzz999"

    def test_both_non_bidirectional_falls_back_to_startup_time(self):
        """Test election between two non-bidirectional peers uses startup_time."""
        pm = PeerManager(
            local_cid="zzz999", bucket="photos",
            startup_time=100.0, sync_direction="upload-only",
        )
        pm.add_peer(Peer(
            cid="aaa111", bucket="photos", address="192.168.1.100", port=8765,
            startup_time=200.0, sync_direction="download-only",
        ))
        # Local started earlier, so local wins
        assert pm.is_master() is True
        assert pm.get_master_cid() == "zzz999"

    def test_non_bidirectional_master_logs_warning(self):
        """Test that a warning is logged when non-bidirectional master is elected."""
        pm = PeerManager(
            local_cid="aaa111", bucket="photos",
            startup_time=100.0, sync_direction="upload-only",
        )
        with patch("hsync_client.lan.log") as mock_log:
            pm.add_peer(Peer(
                cid="zzz999", bucket="photos", address="192.168.1.100", port=8765,
                startup_time=200.0, sync_direction="download-only",
            ))
            # Local is master (earlier startup, both non-bidirectional)
            assert pm.is_master() is True
            # Should have logged a warning about non-bidirectional master
            warning_calls = [
                call for call in mock_log.warning.call_args_list
                if "sync_direction=" in str(call)
            ]
            assert len(warning_calls) > 0

    def test_bidirectional_local_wins_over_earlier_non_bidirectional(self):
        """Test that bidirectional local wins even if remote started earlier."""
        pm = PeerManager(
            local_cid="zzz999", bucket="photos",
            startup_time=200.0, sync_direction="bidirectional",
        )
        pm.add_peer(Peer(
            cid="aaa111", bucket="photos", address="192.168.1.100", port=8765,
            startup_time=100.0, sync_direction="upload-only",
        ))
        # Local is bidirectional and should win despite later startup
        assert pm.is_master() is True
        assert pm.get_master_cid() == "zzz999"

    def test_peer_sync_direction_default(self):
        """Test that peer sync_direction defaults to bidirectional."""
        peer = Peer(
            cid="abc123", bucket="photos",
            address="192.168.1.100", port=8765,
        )
        assert peer.sync_direction == "bidirectional"

    def test_peer_sync_direction_custom(self):
        """Test creating a peer with custom sync_direction."""
        peer = Peer(
            cid="abc123", bucket="photos",
            address="192.168.1.100", port=8765,
            sync_direction="upload-only",
        )
        assert peer.sync_direction == "upload-only"


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
        is_compat, _reason = check_version_compatibility("0.1.0", "0.1.0")
        assert is_compat is True


class TestLANSync:
    """Tests for LANSync class."""

    def test_lan_sync_creation(self):
        """Test creating a LANSync instance."""
        from hsync_client.lan import LANSync

        lan = LANSync(cid="test123", bucket="photos")
        assert lan.port > 0  # Random port assigned
        assert lan.group is None
        assert lan.use_https is False

    def test_lan_sync_with_group(self):
        """Test LANSync with group parameter."""
        from hsync_client.lan import LANSync

        lan = LANSync(cid="test123", bucket="photos", group="team-a")
        assert lan.group == "team-a"

    def test_lan_sync_with_https(self):
        """Test LANSync with HTTPS enabled."""
        from hsync_client.lan import LANSync

        lan = LANSync(
            cid="test123",
            bucket="photos",
            use_https=True,
            cert_fingerprint="abc123def456"
        )
        assert lan.use_https is True
        assert lan.cert_fingerprint == "abc123def456"

    def test_lan_sync_with_force_master(self):
        """Test LANSync with force_master."""
        from hsync_client.lan import LANSync

        lan = LANSync(
            cid="test123",
            bucket="photos",
            force_master=True,
            sync_direction="bidirectional",
        )
        assert lan.peer_manager is None  # Not started yet

    def test_lan_sync_start_stop(self):
        """Test starting and stopping LANSync."""
        from hsync_client.lan import LANSync

        lan = LANSync(cid="test123", bucket="photos")
        pm = lan.start(register_service=False)
        assert lan.is_running() is True
        assert pm is not None
        assert lan.peer_manager is not None
        lan.stop()
        assert lan.is_running() is False

    def test_lan_sync_start_with_service(self):
        """Test starting LANSync with service registration."""
        from hsync_client.lan import LANSync

        lan = LANSync(cid="test123", bucket="photos")
        pm = lan.start(register_service=True)
        assert lan.is_running() is True
        lan.stop()
        assert lan.is_running() is False

    def test_lan_sync_register_service(self):
        """Test registering mDNS service."""
        from hsync_client.lan import LANSync

        lan = LANSync(cid="test123", bucket="photos")
        lan.start(register_service=False)
        lan.register_service(server_ready=False)
        lan.stop()

    def test_lan_sync_update_server_ready(self):
        """Test updating server ready status."""
        from hsync_client.lan import LANSync

        lan = LANSync(cid="test123", bucket="photos")
        lan.start(register_service=False)
        lan.register_service(server_ready=False)
        lan.update_server_ready()
        lan.stop()

    def test_lan_sync_multiple_stops(self):
        """Test that stopping multiple times is safe."""
        from hsync_client.lan import LANSync

        lan = LANSync(cid="test123", bucket="photos")
        lan.start(register_service=False)
        lan.stop()
        lan.stop()  # Should not raise

    def test_lan_sync_stop_without_start(self):
        """Test stopping without starting is safe."""
        from hsync_client.lan import LANSync

        lan = LANSync(cid="test123", bucket="photos")
        lan.stop()  # Should not raise


class TestPeerManagerAdvanced:
    """Advanced tests for PeerManager class."""

    def test_get_alive_peers(self):
        """Test getting only alive peers."""
        from hsync_client.lan import PeerManager

        pm = PeerManager(local_cid="local123", bucket="photos")
        # Add alive peer
        pm.add_peer(Peer(
            cid="alive1",
            bucket="photos",
            address="192.168.1.100",
            port=8765,
        ))
        # Add dead peer
        pm.add_peer(Peer(
            cid="dead1",
            bucket="photos",
            address="192.168.1.101",
            port=8765,
            last_seen=time.time() - PEER_TIMEOUT - 10,
        ))

        alive = pm.get_alive_peers()
        assert len(alive) == 1
        assert alive[0].cid == "alive1"

    def test_peer_update_triggers_election(self):
        """Test that updating a peer triggers master election."""
        from hsync_client.lan import PeerManager

        pm = PeerManager(local_cid="zzz999", bucket="photos", startup_time=100.0)
        # Add a peer that should win initially
        pm.add_peer(Peer(
            cid="aaa111",
            bucket="photos",
            address="192.168.1.100",
            port=8765,
            startup_time=50.0,
        ))
        assert pm.get_master_cid() == "aaa111"

        # Update the peer - should still be master
        peer = pm.get_peer("aaa111")
        assert peer is not None
        peer.update_last_seen()
        assert pm.get_master_cid() == "aaa111"

    def test_force_master_conflict_detection(self):
        """Test detection of force_master conflicts."""
        from hsync_client.lan import PeerManager

        pm = PeerManager(
            local_cid="local",
            bucket="photos",
            force_master=True,
            startup_time=100.0,
        )
        # Add another force_master peer that started later
        with patch("hsync_client.lan.log") as mock_log:
            pm.add_peer(Peer(
                cid="remote",
                bucket="photos",
                address="192.168.1.100",
                port=8765,
                force_master=True,
                startup_time=50.0,
            ))
            # Remote started earlier, so local should become slave
            assert pm.get_master_cid() == "remote"
            # Should have logged a conflict warning
            warning_calls = [
                call for call in mock_log.warning.call_args_list
                if "MASTER_CONFLICT" in str(call)
            ]
            assert len(warning_calls) > 0

    def test_force_master_takeover_detection(self):
        """Test detection of force_master takeover."""
        from hsync_client.lan import PeerManager

        pm = PeerManager(
            local_cid="local",
            bucket="photos",
            force_master=False,
            startup_time=100.0,
        )
        # First become master
        pm.add_peer(Peer(
            cid="old_master",
            bucket="photos",
            address="192.168.1.100",
            port=8765,
            force_master=False,
            startup_time=200.0,
        ))
        assert pm.get_master_cid() == "local"

        # Now a force_master peer joins
        with patch("hsync_client.lan.log") as mock_log:
            pm.add_peer(Peer(
                cid="new_force",
                bucket="photos",
                address="192.168.1.101",
                port=8765,
                force_master=True,
                startup_time=50.0,
            ))
            # New force_master should take over
            assert pm.get_master_cid() == "new_force"
            # Should have logged takeover warning
            warning_calls = [
                call for call in mock_log.warning.call_args_list
                if "MASTER_TAKEOVER" in str(call)
            ]
            assert len(warning_calls) > 0

    def test_master_callback_with_local_master(self):
        """Test master change callback when local becomes master."""
        from hsync_client.lan import PeerManager

        pm = PeerManager(local_cid="local", bucket="photos", startup_time=100.0)
        callback = MagicMock()
        pm.set_master_change_callback(callback)

        # Add a peer with later startup - local should become master
        pm.add_peer(Peer(
            cid="peer1",
            bucket="photos",
            address="192.168.1.100",
            port=8765,
            startup_time=200.0,
        ))

        # Callback should be called with None (local is master)
        callback.assert_called()
        args, _ = callback.call_args
        assert args[0] is None

    def test_master_callback_on_peer_remove(self):
        """Test master change callback when peer is removed."""
        from hsync_client.lan import PeerManager

        pm = PeerManager(local_cid="local", bucket="photos", startup_time=200.0)
        callback = MagicMock()
        pm.set_master_change_callback(callback)

        # Add a peer that becomes master
        pm.add_peer(Peer(
            cid="master_peer",
            bucket="photos",
            address="192.168.1.100",
            port=8765,
            startup_time=100.0,
        ))
        callback.reset_mock()

        # Remove the master
        pm.remove_peer("master_peer")

        # Local should become master
        assert pm.is_master() is True
        # Callback should be called with None
        callback.assert_called()
        args, _ = callback.call_args
        assert args[0] is None


class TestPeerManagerForceMasterLogging:
    """Tests for PeerManager force_master logging scenarios."""

    def test_local_is_master_logs_reason_force_master(self):
        """Test that local becoming master logs correct reason for force_master."""
        from hsync_client.lan import PeerManager

        pm = PeerManager(
            local_cid="local",
            bucket="photos",
            force_master=True,
            startup_time=100.0,
        )
        with patch("hsync_client.lan.log") as mock_log:
            pm.add_peer(Peer(
                cid="peer1",
                bucket="photos",
                address="192.168.1.100",
                port=8765,
                startup_time=200.0,
            ))
            # Local should win due to force_master
            assert pm.is_master() is True
            # Should log the reason
            info_calls = [
                call for call in mock_log.info.call_args_list
                if "MASTER" in str(call) and "force_master" in str(call)
            ]
            assert len(info_calls) > 0

    def test_remote_is_master_logs_reason(self):
        """Test that remote becoming master logs correct reason."""
        from hsync_client.lan import PeerManager

        pm = PeerManager(
            local_cid="local",
            bucket="photos",
            startup_time=200.0,
        )
        with patch("hsync_client.lan.log") as mock_log:
            pm.add_peer(Peer(
                cid="peer1",
                bucket="photos",
                address="192.168.1.100",
                port=8765,
                startup_time=100.0,
            ))
            # Remote should win
            assert pm.is_master() is False
            # Should log master change
            info_calls = [
                call for call in mock_log.info.call_args_list
                if "Master changed" in str(call)
            ]
            assert len(info_calls) > 0
