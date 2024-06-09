# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for PeerManager class in LAN module."""

import time
from unittest.mock import MagicMock, patch

from hsync_client.lan import PEER_TIMEOUT, Peer, PeerManager


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
        peer = Peer(
            cid="remote456", bucket="photos", address="192.168.1.100", port=8765, group=None
        )
        pm.add_peer(peer)
        assert pm.get_peer("remote456") is not None

    def test_add_peer_local_no_group_remote_has_group_compatible(self):
        """Test that peer with group is visible to local without group (Option B)."""
        pm = PeerManager(local_cid="local123", bucket="photos", group=None)
        peer = Peer(
            cid="remote456", bucket="photos", address="192.168.1.100", port=8765, group="team-a"
        )
        pm.add_peer(peer)
        assert pm.get_peer("remote456") is not None

    def test_add_peer_local_has_group_remote_no_group_compatible(self):
        """Test that peer without group is visible to local with group (Option B)."""
        pm = PeerManager(local_cid="local123", bucket="photos", group="team-a")
        peer = Peer(
            cid="remote456", bucket="photos", address="192.168.1.100", port=8765, group=None
        )
        pm.add_peer(peer)
        assert pm.get_peer("remote456") is not None

    def test_add_peer_same_group_compatible(self):
        """Test that peers with the same group see each other."""
        pm = PeerManager(local_cid="local123", bucket="photos", group="team-a")
        peer = Peer(
            cid="remote456", bucket="photos", address="192.168.1.100", port=8765, group="team-a"
        )
        pm.add_peer(peer)
        assert pm.get_peer("remote456") is not None

    def test_add_peer_different_group_ignored(self):
        """Test that peers with different groups are ignored (both have group)."""
        pm = PeerManager(local_cid="local123", bucket="photos", group="team-a")
        peer = Peer(
            cid="remote456", bucket="photos", address="192.168.1.100", port=8765, group="team-b"
        )
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
        pm.add_peer(
            Peer(
                cid="zzz999",
                bucket="photos",
                address="192.168.1.100",
                port=8765,
                startup_time=200.0,
            )
        )
        assert pm.is_master() is True
        assert pm.get_master_cid() == "aaa111"

    def test_master_election_remote_wins(self):
        """Test that remote peer is elected master when it started earliest."""
        pm = PeerManager(local_cid="zzz999", bucket="photos", startup_time=200.0)
        pm.add_peer(
            Peer(
                cid="aaa111",
                bucket="photos",
                address="192.168.1.100",
                port=8765,
                startup_time=100.0,
            )
        )
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
        pm.add_peer(
            Peer(
                cid="aaa111",
                bucket="photos",
                address="192.168.1.100",
                port=8765,
                startup_time=100.0,
            )
        )

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
            local_cid="aaa111",
            bucket="photos",
            startup_time=100.0,
            sync_direction="upload-only",
        )
        pm.add_peer(
            Peer(
                cid="zzz999",
                bucket="photos",
                address="192.168.1.100",
                port=8765,
                startup_time=100.0,
                sync_direction="bidirectional",
            )
        )
        assert pm.is_master() is False
        assert pm.get_master_cid() == "zzz999"

    def test_bidirectional_preferred_over_download_only(self):
        """Test that bidirectional peer wins over download-only with same startup_time."""
        pm = PeerManager(
            local_cid="aaa111",
            bucket="photos",
            startup_time=100.0,
            sync_direction="download-only",
        )
        pm.add_peer(
            Peer(
                cid="zzz999",
                bucket="photos",
                address="192.168.1.100",
                port=8765,
                startup_time=100.0,
                sync_direction="bidirectional",
            )
        )
        assert pm.is_master() is False
        assert pm.get_master_cid() == "zzz999"

    def test_force_master_overrides_sync_direction(self):
        """Test that force_master takes priority over sync_direction preference."""
        # Local is upload-only but has force_master — should still win
        pm = PeerManager(
            local_cid="zzz999",
            bucket="photos",
            startup_time=200.0,
            force_master=True,
            sync_direction="upload-only",
        )
        pm.add_peer(
            Peer(
                cid="aaa111",
                bucket="photos",
                address="192.168.1.100",
                port=8765,
                startup_time=100.0,
                sync_direction="bidirectional",
            )
        )
        assert pm.is_master() is True
        assert pm.get_master_cid() == "zzz999"

    def test_both_non_bidirectional_falls_back_to_startup_time(self):
        """Test election between two non-bidirectional peers uses startup_time."""
        pm = PeerManager(
            local_cid="zzz999",
            bucket="photos",
            startup_time=100.0,
            sync_direction="upload-only",
        )
        pm.add_peer(
            Peer(
                cid="aaa111",
                bucket="photos",
                address="192.168.1.100",
                port=8765,
                startup_time=200.0,
                sync_direction="download-only",
            )
        )
        # Local started earlier, so local wins
        assert pm.is_master() is True
        assert pm.get_master_cid() == "zzz999"

    def test_non_bidirectional_master_logs_warning(self):
        """Test that a warning is logged when non-bidirectional master is elected."""
        pm = PeerManager(
            local_cid="aaa111",
            bucket="photos",
            startup_time=100.0,
            sync_direction="upload-only",
        )
        with patch("hsync_client.lan.log") as mock_log:
            pm.add_peer(
                Peer(
                    cid="zzz999",
                    bucket="photos",
                    address="192.168.1.100",
                    port=8765,
                    startup_time=200.0,
                    sync_direction="download-only",
                )
            )
            # Local is master (earlier startup, both non-bidirectional)
            assert pm.is_master() is True
            # Should have logged a warning about non-bidirectional master
            warning_calls = [
                call for call in mock_log.warning.call_args_list if "sync_direction=" in str(call)
            ]
            assert len(warning_calls) > 0

    def test_bidirectional_local_wins_over_earlier_non_bidirectional(self):
        """Test that bidirectional local wins even if remote started earlier."""
        pm = PeerManager(
            local_cid="zzz999",
            bucket="photos",
            startup_time=200.0,
            sync_direction="bidirectional",
        )
        pm.add_peer(
            Peer(
                cid="aaa111",
                bucket="photos",
                address="192.168.1.100",
                port=8765,
                startup_time=100.0,
                sync_direction="upload-only",
            )
        )
        # Local is bidirectional and should win despite later startup
        assert pm.is_master() is True
        assert pm.get_master_cid() == "zzz999"

    def test_get_alive_peers(self):
        """Test getting only alive peers."""
        from hsync_client.lan import PeerManager

        pm = PeerManager(local_cid="local123", bucket="photos")
        # Add alive peer
        pm.add_peer(
            Peer(
                cid="alive1",
                bucket="photos",
                address="192.168.1.100",
                port=8765,
            )
        )
        # Add dead peer
        pm.add_peer(
            Peer(
                cid="dead1",
                bucket="photos",
                address="192.168.1.101",
                port=8765,
                last_seen=time.time() - PEER_TIMEOUT - 10,
            )
        )

        alive = pm.get_alive_peers()
        assert len(alive) == 1
        assert alive[0].cid == "alive1"

    def test_peer_update_triggers_election(self):
        """Test that updating a peer triggers master election."""
        from hsync_client.lan import PeerManager

        pm = PeerManager(local_cid="zzz999", bucket="photos", startup_time=100.0)
        # Add a peer that should win initially
        pm.add_peer(
            Peer(
                cid="aaa111",
                bucket="photos",
                address="192.168.1.100",
                port=8765,
                startup_time=50.0,
            )
        )
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
            pm.add_peer(
                Peer(
                    cid="remote",
                    bucket="photos",
                    address="192.168.1.100",
                    port=8765,
                    force_master=True,
                    startup_time=50.0,
                )
            )
            # Remote started earlier, so local should become client
            assert pm.get_master_cid() == "remote"
            # Should have logged a conflict warning
            warning_calls = [
                call for call in mock_log.warning.call_args_list if "MASTER_CONFLICT" in str(call)
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
        pm.add_peer(
            Peer(
                cid="old_master",
                bucket="photos",
                address="192.168.1.100",
                port=8765,
                force_master=False,
                startup_time=200.0,
            )
        )
        assert pm.get_master_cid() == "local"

        # Now a force_master peer joins
        with patch("hsync_client.lan.log") as mock_log:
            pm.add_peer(
                Peer(
                    cid="new_force",
                    bucket="photos",
                    address="192.168.1.101",
                    port=8765,
                    force_master=True,
                    startup_time=50.0,
                )
            )
            # New force_master should take over
            assert pm.get_master_cid() == "new_force"
            # Should have logged takeover warning
            warning_calls = [
                call for call in mock_log.warning.call_args_list if "MASTER_TAKEOVER" in str(call)
            ]
            assert len(warning_calls) > 0

    def test_server_ready_incumbent_wins_over_force_master(self):
        """An already-serving (server_ready) peer wins over a force_master newcomer."""
        from hsync_client.lan import PeerManager

        pm = PeerManager(
            local_cid="local",
            bucket="photos",
            force_master=True,
            startup_time=10.0,
        )
        # A remote peer is already serving (incumbent master)
        pm.add_peer(
            Peer(
                cid="incumbent",
                bucket="photos",
                address="192.168.1.100",
                port=8765,
                server_ready=True,
                startup_time=200.0,
            )
        )
        # Despite local force_master and earlier startup, the incumbent wins
        assert pm.get_master_cid() == "incumbent"

    def test_local_incumbent_not_taken_over(self):
        """A local incumbent master is not displaced by a force_master newcomer."""
        from hsync_client.lan import PeerManager

        pm = PeerManager(
            local_cid="local",
            bucket="photos",
            startup_time=100.0,
        )
        # Local becomes master and starts serving
        pm.set_local_server_ready(True)
        assert pm.get_master_cid() == "local"

        # A force_master peer joins later
        pm.add_peer(
            Peer(
                cid="new_force",
                bucket="photos",
                address="192.168.1.101",
                port=8765,
                force_master=True,
                startup_time=50.0,
            )
        )
        # Local incumbent keeps the master role (no takeover, no split-brain)
        assert pm.get_master_cid() == "local"

    def test_master_callback_with_local_master(self):
        """Test master change callback when local becomes master."""
        from hsync_client.lan import PeerManager

        pm = PeerManager(local_cid="local", bucket="photos", startup_time=100.0)
        callback = MagicMock()
        pm.set_master_change_callback(callback)

        # Add a peer with later startup - local should become master
        pm.add_peer(
            Peer(
                cid="peer1",
                bucket="photos",
                address="192.168.1.100",
                port=8765,
                startup_time=200.0,
            )
        )

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
        pm.add_peer(
            Peer(
                cid="master_peer",
                bucket="photos",
                address="192.168.1.100",
                port=8765,
                startup_time=100.0,
            )
        )
        callback.reset_mock()

        # Remove the master
        pm.remove_peer("master_peer")

        # Local should become master
        assert pm.is_master() is True
        # Callback should be called with None
        callback.assert_called()
        args, _ = callback.call_args
        assert args[0] is None

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
            pm.add_peer(
                Peer(
                    cid="peer1",
                    bucket="photos",
                    address="192.168.1.100",
                    port=8765,
                    startup_time=200.0,
                )
            )
            # Local should win due to force_master
            assert pm.is_master() is True
            # Should log the reason
            info_calls = [
                call
                for call in mock_log.info.call_args_list
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
            pm.add_peer(
                Peer(
                    cid="peer1",
                    bucket="photos",
                    address="192.168.1.100",
                    port=8765,
                    startup_time=100.0,
                )
            )
            # Remote should win
            assert pm.is_master() is False
            # Should log master change
            info_calls = [
                call for call in mock_log.info.call_args_list if "Master changed" in str(call)
            ]
            assert len(info_calls) > 0
