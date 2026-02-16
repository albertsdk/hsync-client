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
        lan.start(register_service=True)
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
