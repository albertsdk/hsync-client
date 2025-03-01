# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for PeerManager class in LAN module."""

import time
from unittest.mock import patch

from hsync_client.lan import PEER_TIMEOUT, Peer, PeerManager


class TestPeerManager:
    """Tests for PeerManager class."""

    def test_peer_manager_creation(self):
        """Test creating a peer manager."""
        pm = PeerManager(local_cid="local123", bucket="photos")
        assert pm.local_cid == "local123"
        assert pm.bucket == "photos"

    def test_peer_manager_group_property(self):
        """Test the group property of PeerManager."""
        pm = PeerManager(local_cid="local123", bucket="photos", group="mygroup")
        assert pm.group == "mygroup"
        pm_no_group = PeerManager(local_cid="local123", bucket="photos")
        assert pm_no_group.group is None

    def test_master_election_local_wins(self):
        """Test that local client is elected master when it started earliest."""
        pm = PeerManager(local_cid="aaa111", bucket="photos", startup_time=100.0)
        pm.add_peer(
            Peer(
                cid="zzz999",
                bucket="photos",
                address="192.168.1.100",
                port=9753,
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
                port=9753,
                startup_time=100.0,
            )
        )
        assert pm.is_master() is False
        assert pm.get_master_cid() == "aaa111"
        master = pm.get_master()
        assert master is not None
        assert master.cid == "aaa111"

    def test_local_alone_is_master(self):
        """Test that local client is master when alone."""
        pm = PeerManager(local_cid="local123", bucket="photos")
        # Trigger election by adding and removing a peer
        pm.add_peer(Peer(cid="temp", bucket="photos", address="192.168.1.100", port=9753))
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
                port=9753,
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
                port=9753,
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
                port=9753,
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
                port=9753,
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
                    port=9753,
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
                port=9753,
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
                port=9753,
            )
        )
        # Add dead peer
        pm.add_peer(
            Peer(
                cid="dead1",
                bucket="photos",
                address="192.168.1.101",
                port=9753,
                last_seen=time.time() - PEER_TIMEOUT - 10,
            )
        )

        alive = pm.get_alive_peers()
        assert len(alive) == 1
        assert alive[0].cid == "alive1"

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
                    port=9753,
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
                port=9753,
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
                    port=9753,
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
                port=9753,
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
                port=9753,
                force_master=True,
                startup_time=50.0,
            )
        )
        # Local incumbent keeps the master role (no takeover, no split-brain)
        assert pm.get_master_cid() == "local"

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
                    port=9753,
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
                    port=9753,
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


class TestNeverMaster:
    """Tests for the never_master exclusion in master election."""

    def test_local_never_master_not_elected_when_alone(self):
        """A lone never-master client elects no master (stays client)."""
        pm = PeerManager(local_cid="local123", bucket="photos", never_master=True)
        pm.add_peer(Peer(cid="temp", bucket="photos", address="192.168.1.100", port=9753))
        pm.remove_peer("temp")
        assert pm.is_master() is False
        assert pm.get_master_cid() is None

    def test_local_never_master_yields_to_peer(self):
        """A never-master local with an eligible peer makes that peer master."""
        pm = PeerManager(
            local_cid="aaa111",
            bucket="photos",
            startup_time=100.0,
            never_master=True,
        )
        pm.add_peer(
            Peer(
                cid="zzz999",
                bucket="photos",
                address="192.168.1.100",
                port=9753,
                startup_time=200.0,
            )
        )
        assert pm.is_master() is False
        assert pm.get_master_cid() == "zzz999"

    def test_peer_never_master_ignored_in_election(self):
        """A never-master peer is never elected even if it started first."""
        pm = PeerManager(local_cid="zzz999", bucket="photos", startup_time=200.0)
        pm.add_peer(
            Peer(
                cid="aaa111",
                bucket="photos",
                address="192.168.1.100",
                port=9753,
                startup_time=100.0,
                never_master=True,
            )
        )
        # Despite the earlier startup_time, the never-master peer loses;
        # the local client becomes master.
        assert pm.is_master() is True
        assert pm.get_master_cid() == "zzz999"

    def test_all_never_master_no_master_elected(self):
        """When every node is never-master, no master is elected."""
        pm = PeerManager(local_cid="local123", bucket="photos", never_master=True)
        pm.add_peer(
            Peer(
                cid="peer1",
                bucket="photos",
                address="192.168.1.100",
                port=9753,
                never_master=True,
            )
        )
        assert pm.is_master() is False
        assert pm.get_master_cid() is None


class TestPeerManagerProperties:
    """Tests for the simple property accessors."""

    def test_flag_properties(self):
        """force_master/never_master/startup_time are exposed as properties."""
        pm = PeerManager(
            local_cid="local123",
            bucket="photos",
            force_master=True,
            never_master=True,
            startup_time=1234.5,
        )
        assert pm.force_master is True
        assert pm.never_master is True
        assert pm.startup_time == 1234.5

    def test_startup_time_defaults_to_now(self):
        """A non-positive startup_time is replaced with the current time."""
        pm = PeerManager(local_cid="local123", bucket="photos", startup_time=0.0)
        assert pm.startup_time > 0


class TestAddPeerFiltering:
    """Tests for the add_peer gates (bucket/group/self)."""

    def test_peer_with_different_bucket_is_ignored(self):
        pm = PeerManager(local_cid="local123", bucket="photos")
        pm.add_peer(Peer(cid="peer1", bucket="videos", address="192.168.1.100", port=9753))
        assert pm.get_alive_peers() == []

    def test_peer_with_different_group_is_ignored(self):
        pm = PeerManager(local_cid="local123", bucket="photos", group="team-a")
        pm.add_peer(
            Peer(cid="peer1", bucket="photos", address="192.168.1.100", port=9753, group="team-b")
        )
        assert pm.get_alive_peers() == []

    def test_peer_without_group_does_not_match_grouped_local(self):
        """None is a distinct partition: a group-less peer is not compatible."""
        pm = PeerManager(local_cid="local123", bucket="photos", group="team-a")
        pm.add_peer(Peer(cid="peer1", bucket="photos", address="192.168.1.100", port=9753))
        assert pm.get_alive_peers() == []

    def test_self_is_ignored(self):
        pm = PeerManager(local_cid="local123", bucket="photos")
        pm.add_peer(Peer(cid="local123", bucket="photos", address="192.168.1.100", port=9753))
        assert pm.get_alive_peers() == []


class TestAddPeerUpdate:
    """Tests for the update branch of add_peer (already-known CID)."""

    def test_existing_peer_fields_are_updated(self):
        pm = PeerManager(local_cid="local123", bucket="photos")
        pm.add_peer(Peer(cid="peer1", bucket="photos", address="192.168.1.100", port=9753))
        pm.add_peer(
            Peer(
                cid="peer1",
                bucket="photos",
                address="192.168.1.200",
                port=8888,
                version="1.2.3",
                group=None,
                use_https=True,
                cert_fingerprint="fp",
                compression_enabled=True,
                delta_enabled=True,
                server_ready=True,
                clock_offset=1.5,
                force_master=True,
                never_master=False,
                startup_time=42.0,
                sync_direction="upload-only",
            )
        )
        peers = pm.get_alive_peers()
        assert len(peers) == 1
        peer = peers[0]
        assert peer.address == "192.168.1.200"
        assert peer.port == 8888
        assert peer.version == "1.2.3"
        assert peer.use_https is True
        assert peer.cert_fingerprint == "fp"
        assert peer.compression_enabled is True
        assert peer.delta_enabled is True
        assert peer.server_ready is True
        assert peer.clock_offset == 1.5
        assert peer.force_master is True
        assert peer.startup_time == 42.0
        assert peer.sync_direction == "upload-only"

    def test_update_refreshes_last_seen(self):
        pm = PeerManager(local_cid="local123", bucket="photos")
        pm.add_peer(
            Peer(
                cid="peer1",
                bucket="photos",
                address="192.168.1.100",
                port=9753,
                last_seen=time.time() - PEER_TIMEOUT - 1,
            )
        )
        assert pm.get_alive_peers() == []
        pm.add_peer(Peer(cid="peer1", bucket="photos", address="192.168.1.100", port=9753))
        assert len(pm.get_alive_peers()) == 1

    def test_server_ready_transition_is_logged(self):
        """The 'server is now ready' info line fires on the False→True edge."""
        pm = PeerManager(local_cid="local123", bucket="photos")
        pm.add_peer(
            Peer(
                cid="peer1", bucket="photos", address="192.168.1.100", port=9753, server_ready=False
            )
        )
        with patch("hsync_client.lan.log") as mock_log:
            pm.add_peer(
                Peer(
                    cid="peer1",
                    bucket="photos",
                    address="192.168.1.100",
                    port=9753,
                    server_ready=True,
                )
            )
        ready_calls = [c for c in mock_log.info.call_args_list if "now ready" in str(c)]
        assert len(ready_calls) == 1


class TestSetLocalServerReady:
    """Tests for the set_local_server_ready early-return guard."""

    def test_same_value_is_a_noop(self):
        pm = PeerManager(local_cid="local123", bucket="photos")
        with patch.object(pm, "_elect_master") as mock_elect:
            pm.set_local_server_ready(False)  # already False: no re-election
            mock_elect.assert_not_called()

    def test_change_triggers_election(self):
        pm = PeerManager(local_cid="local123", bucket="photos")
        with patch.object(pm, "_elect_master") as mock_elect:
            pm.set_local_server_ready(True)
            mock_elect.assert_called_once()


class TestMasterChangeCallback:
    """Tests for the _on_master_change notification."""

    def test_callback_receives_remote_master_peer(self):
        pm = PeerManager(local_cid="zzz999", bucket="photos", startup_time=200.0)
        callback_calls: list = []
        pm._on_master_change = lambda peer: callback_calls.append(peer)
        pm.add_peer(
            Peer(
                cid="aaa111",
                bucket="photos",
                address="192.168.1.100",
                port=9753,
                startup_time=100.0,
            )
        )
        assert len(callback_calls) == 1
        assert callback_calls[0].cid == "aaa111"

    def test_callback_receives_none_when_local_is_master(self):
        pm = PeerManager(local_cid="aaa111", bucket="photos", startup_time=100.0)
        callback_calls: list = []
        pm._on_master_change = lambda peer: callback_calls.append(peer)
        pm.add_peer(
            Peer(
                cid="zzz999",
                bucket="photos",
                address="192.168.1.100",
                port=9753,
                startup_time=200.0,
            )
        )
        assert callback_calls == [None]

    def test_callback_receives_none_when_master_becomes_none(self):
        """When the master leaves the eligible set, the callback gets None."""
        pm = PeerManager(local_cid="local123", bucket="photos", never_master=True)
        callback_calls: list = []
        pm._on_master_change = lambda peer: callback_calls.append(peer)
        pm.add_peer(
            Peer(
                cid="peer1", bucket="photos", address="192.168.1.100", port=9753, startup_time=100.0
            )
        )
        assert callback_calls[0].cid == "peer1"
        # The master re-announces itself as never_master: no eligible node
        # remains, so the master becomes None and the callback fires again.
        pm.add_peer(
            Peer(
                cid="peer1",
                bucket="photos",
                address="192.168.1.100",
                port=9753,
                startup_time=100.0,
                never_master=True,
            )
        )
        assert pm.get_master_cid() is None
        assert callback_calls[-1] is None


class TestForceMasterConflictWon:
    """Cover the 'we won the force_master conflict' branch."""

    def test_conflict_won_logs_and_emits(self):
        pm = PeerManager(
            local_cid="local",
            bucket="photos",
            force_master=True,
            startup_time=50.0,
        )
        with (
            patch("hsync_client.lan.log") as mock_log,
            patch("hsync_client.lan.emit_event") as mock_emit,
        ):
            pm.add_peer(
                Peer(
                    cid="remote",
                    bucket="photos",
                    address="192.168.1.100",
                    port=9753,
                    force_master=True,
                    startup_time=100.0,
                )
            )
            assert pm.get_master_cid() == "local"
            warning_calls = [
                c for c in mock_log.warning.call_args_list if "MASTER_CONFLICT" in str(c)
            ]
            assert len(warning_calls) > 0
            conflict_events = [
                c
                for c in mock_emit.call_args_list
                if c.kwargs.get("conflict_type") == "conflict_won"
            ]
            assert len(conflict_events) == 1
            assert conflict_events[0].kwargs["peer_cid"] == "remote"


class TestRemoteMasterLogging:
    """Cover remote-master log branches (warnings and unknown peer)."""

    def test_remote_non_bidirectional_master_logs_warning(self):
        # Both nodes are non-bidirectional, so the earlier startup wins and
        # the remote master triggers the "may limit synchronization" warning.
        pm = PeerManager(
            local_cid="zzz999",
            bucket="photos",
            startup_time=200.0,
            sync_direction="download-only",
        )
        with patch("hsync_client.lan.log") as mock_log:
            pm.add_peer(
                Peer(
                    cid="aaa111",
                    bucket="photos",
                    address="192.168.1.100",
                    port=9753,
                    startup_time=100.0,
                    sync_direction="upload-only",
                )
            )
            assert pm.get_master_cid() == "aaa111"
            warning_calls = [
                c for c in mock_log.warning.call_args_list if "sync_direction=" in str(c)
            ]
            assert len(warning_calls) > 0

    def test_remote_force_master_logs_reason(self):
        """A remote force_master master is logged with the force_master reason."""
        pm = PeerManager(local_cid="zzz999", bucket="photos", startup_time=200.0)
        with patch("hsync_client.lan.log") as mock_log:
            pm.add_peer(
                Peer(
                    cid="aaa111",
                    bucket="photos",
                    address="192.168.1.100",
                    port=9753,
                    startup_time=100.0,
                    force_master=True,
                )
            )
            assert pm.get_master_cid() == "aaa111"
            info_calls = [
                c
                for c in mock_log.info.call_args_list
                if "Master changed" in str(c) and "force_master" in str(c)
            ]
            assert len(info_calls) > 0

    def test_master_change_with_peer_missing_from_registry_logs_unknown(self):
        """If the elected master is not found in _peers, log falls back to 'unknown'."""
        pm = PeerManager(local_cid="zzz999", bucket="photos", startup_time=200.0)
        pm.add_peer(
            Peer(
                cid="aaa111",
                bucket="photos",
                address="192.168.1.100",
                port=9753,
                startup_time=100.0,
            )
        )
        assert pm.get_master_cid() == "aaa111"

        class _NullGetDict(dict):
            """Registry that still lists candidates but fails .get() lookups."""

            def get(self, key, default=None):
                return default

        pm._peers = _NullGetDict(pm._peers)
        with patch("hsync_client.lan.log") as mock_log:
            pm.add_peer(
                Peer(
                    cid="bbb222",
                    bucket="photos",
                    address="192.168.1.101",
                    port=9753,
                    startup_time=50.0,
                )
            )
            assert pm.get_master_cid() == "bbb222"
            info_calls = [
                c
                for c in mock_log.info.call_args_list
                if "Master changed" in str(c) and "unknown" in str(c)
            ]
            assert len(info_calls) > 0


class TestGetMasterEdgeCases:
    def test_get_master_returns_none_when_no_master(self):
        pm = PeerManager(local_cid="local123", bucket="photos", never_master=True)
        pm.add_peer(
            Peer(
                cid="peer1", bucket="photos", address="192.168.1.100", port=9753, never_master=True
            )
        )
        assert pm.get_master_cid() is None
        assert pm.get_master() is None
