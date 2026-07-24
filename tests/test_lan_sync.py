# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for LANSync class in LAN module."""


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
            cid="test123", bucket="photos", use_https=True, cert_fingerprint="abc123def456"
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

    def test_wait_for_discovery_announces_waiting_once(self):
        """The "waiting" notice is logged once; later polls drop to debug."""
        from unittest.mock import MagicMock, patch

        from hsync_client.lan import LANSync

        lan = LANSync(cid="test123", bucket="photos")
        lan._peer_manager = MagicMock()
        lan._peer_manager.get_alive_peers.return_value = []

        with (
            patch("hsync_client.lan.time.sleep"),
            patch("hsync_client.lan.log") as mock_log,
        ):
            lan.wait_for_discovery(timeout=1)
            lan.wait_for_discovery(timeout=1)

        waiting_info = [
            c
            for c in mock_log.info.call_args_list
            if c.args and "for peer discovery" in str(c.args[0])
        ]
        waiting_debug = [
            c
            for c in mock_log.debug.call_args_list
            if c.args and "for peer discovery" in str(c.args[0])
        ]
        assert len(waiting_info) == 1, "waiting notice must log once at info level"
        assert len(waiting_debug) == 1, "subsequent polls must drop to debug"

    def test_wait_for_discovery_logs_result_only_on_change(self):
        """Discovery results are logged only when the peer count changes."""
        from unittest.mock import MagicMock, patch

        from hsync_client.lan import LANSync

        lan = LANSync(cid="test123", bucket="photos")
        lan._peer_manager = MagicMock()

        peer = MagicMock()
        peer.cid = "peer-1"
        peer.address = "10.0.0.2"
        peer.port = 9000
        # Alone for two polls, then a peer appears on the third.
        lan._peer_manager.get_alive_peers.side_effect = [[], [], [peer]]

        with (
            patch("hsync_client.lan.time.sleep"),
            patch("hsync_client.lan.log") as mock_log,
        ):
            lan.wait_for_discovery(timeout=1)
            lan.wait_for_discovery(timeout=1)
            lan.wait_for_discovery(timeout=1)

        infos = [str(c.args[0]) for c in mock_log.info.call_args_list if c.args]
        alone_msgs = [m for m in infos if "this client is alone" in m]
        discovered_msgs = [m for m in infos if "Discovered 1 peer(s)" in m]
        assert len(alone_msgs) == 1, "the 'alone' state must log once, not on every poll"
        assert len(discovered_msgs) == 1, "a new peer count must log once on change"
