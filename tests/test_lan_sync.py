# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for LANSync class in LAN module."""

import socket
from unittest.mock import MagicMock, patch

from hsync_common.protocol.handshake import PROTOCOL_VERSION_LAN


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
        from unittest.mock import patch

        from hsync_client.lan import LANSync

        lan = LANSync(cid="test123", bucket="photos")
        lan._peer_manager = MagicMock()
        lan._peer_manager.get_alive_peers.return_value = []
        # Local node is master: presence never consults a remote master peer.
        lan._peer_manager.get_master_cid.return_value = "test123"
        lan._peer_manager.get_master.return_value = None

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
        from unittest.mock import patch

        from hsync_client.lan import LANSync

        lan = LANSync(cid="test123", bucket="photos")
        lan._peer_manager = MagicMock()

        peer = MagicMock()
        peer.cid = "peer-1"
        peer.address = "10.0.0.2"
        peer.port = 9000
        peer.connected = 0
        # Local node is master: presence never consults a remote master peer.
        lan._peer_manager.get_master_cid.return_value = "test123"
        lan._peer_manager.get_master.return_value = None
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

    def test_wait_for_discovery_connected_clients_logged_once(self):
        """A master with connected WS clients reports them instead of "alone"."""
        from unittest.mock import patch

        from hsync_client.lan import LANSync

        lan = LANSync(cid="test123", bucket="photos")
        lan._peer_manager = MagicMock()
        lan._peer_manager.get_alive_peers.return_value = []
        lan._peer_manager.get_master_cid.return_value = "test123"
        lan._peer_manager.get_master.return_value = None
        lan.set_connected_clients_provider(lambda: 2)

        with (
            patch("hsync_client.lan.time.sleep"),
            patch("hsync_client.lan.log") as mock_log,
        ):
            lan.wait_for_discovery(timeout=1)
            lan.wait_for_discovery(timeout=1)

        infos = [str(c.args[0]) for c in mock_log.info.call_args_list if c.args]
        connected_msgs = [m for m in infos if "2 client(s) connected" in m]
        alone_msgs = [m for m in infos if "this client is alone" in m]
        assert len(connected_msgs) == 1, "the connected count must log once, not on every poll"
        assert len(alone_msgs) == 0, "a master with connected clients is not alone"


class TestGetFreePort:
    """Tests for the get_free_port helper."""

    def test_returns_a_bindable_port(self):
        from hsync_client.lan import get_free_port

        port = get_free_port()
        assert isinstance(port, int)
        assert 0 < port < 65536
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", port))  # must still be free


class TestStartStop:
    """Tests for LANSync.start/stop with Zeroconf mocked out."""

    def _start(self, lan, **kwargs):
        with (
            patch("hsync_client.lan.Zeroconf") as mock_zc_cls,
            patch("hsync_client.lan.ServiceBrowser") as mock_browser_cls,
        ):
            pm = lan.start(**kwargs)
        return pm, mock_zc_cls, mock_browser_cls

    def test_start_without_registration(self):
        from hsync_client.lan import LANSync

        lan = LANSync(cid="test123", bucket="photos")
        pm, mock_zc_cls, mock_browser_cls = self._start(lan, register_service=False)
        try:
            assert pm is lan.peer_manager
            assert pm.local_cid == "test123"
            mock_zc_cls.assert_called_once()
            mock_browser_cls.assert_called_once()
            # No service registered yet.
            mock_zc_cls.return_value.register_service.assert_not_called()
            assert lan._service_info is None
            assert lan._running is True
        finally:
            lan.stop()

    def test_start_with_registration(self):
        from hsync_client.lan import LANSync

        lan = LANSync(cid="test123", bucket="photos")
        _pm, mock_zc_cls, _mb = self._start(lan, register_service=True)
        try:
            mock_zc_cls.return_value.register_service.assert_called_once()
            assert lan._service_info is not None
        finally:
            lan.stop()

    def test_stop_unregisters_and_closes(self):
        from hsync_client.lan import LANSync

        lan = LANSync(cid="test123", bucket="photos")
        with (
            patch("hsync_client.lan.Zeroconf") as mock_zc_cls,
            patch("hsync_client.lan.ServiceBrowser"),
        ):
            lan.start(register_service=True)
            lan.stop()
        zc = mock_zc_cls.return_value
        zc.unregister_service.assert_called_once()
        zc.close.assert_called_once()
        assert lan._running is False


class TestRegisterService:
    """Tests for the mDNS TXT record built by _register_service."""

    def _started_lan(self, **kwargs):
        from hsync_client.lan import LANSync

        lan = LANSync(cid="test123", bucket="photos", **kwargs)
        with (
            patch("hsync_client.lan.Zeroconf") as mock_zc_cls,
            patch("hsync_client.lan.ServiceBrowser"),
        ):
            lan.start(register_service=False)
        return lan, mock_zc_cls.return_value

    def _registered_properties(self, lan, zc) -> dict[bytes, bytes]:
        lan.register_service()
        info = zc.register_service.call_args.args[0]
        return dict(info.properties)

    def test_basic_properties(self):
        lan, zc = self._started_lan()
        try:
            props = self._registered_properties(lan, zc)
            assert props[b"cid"] == b"test123"
            assert props[b"bucket"] == b"photos"
            assert props[b"protocol"] == PROTOCOL_VERSION_LAN.encode()
            assert props[b"sync_direction"] == b"bidirectional"
            assert b"startup_time" in props
            assert b"version" in props
            # Conditional keys stay absent when the feature is off.
            for key in (
                b"group",
                b"https",
                b"cert_fp",
                b"server_ready",
                b"force_master",
                b"never_master",
            ):
                assert key not in props
        finally:
            lan.stop()

    def test_optional_properties(self):
        lan, zc = self._started_lan(
            group="team-a",
            use_https=True,
            cert_fingerprint="ff00",
            force_master=True,
            sync_direction="upload-only",
        )
        try:
            props = self._registered_properties(lan, zc)
            assert props[b"group"] == b"team-a"
            assert props[b"https"] == b"true"
            assert props[b"cert_fp"] == b"ff00"
            assert props[b"force_master"] == b"true"
            assert props[b"sync_direction"] == b"upload-only"
            assert b"never_master" not in props
        finally:
            lan.stop()

    def test_never_master_property(self):
        lan, zc = self._started_lan(never_master=True)
        try:
            props = self._registered_properties(lan, zc)
            assert props[b"never_master"] == b"true"
        finally:
            lan.stop()

    def test_compression_and_delta_flags(self, monkeypatch):
        from hsync_client import lan as lan_module

        monkeypatch.setattr(
            lan_module,
            "CONFIG",
            {"COMPRESSION_ENABLED": True, "DELTA_ENABLED": True},
        )
        lan, zc = self._started_lan()
        try:
            props = self._registered_properties(lan, zc)
            assert props[b"compression"] == b"true"
            assert props[b"delta"] == b"true"
        finally:
            lan.stop()

    def test_register_service_twice_is_idempotent(self):
        lan, zc = self._started_lan()
        try:
            lan.register_service()
            lan.register_service()  # second call: log + return, no re-register
            assert zc.register_service.call_count == 1
        finally:
            lan.stop()

    def test_register_service_without_zeroconf_is_noop(self):
        from hsync_client.lan import LANSync

        lan = LANSync(cid="test123", bucket="photos")
        lan._register_service()  # _zeroconf is None: must not raise
        assert lan._service_info is None


class TestUpdateServerReady:
    """Tests for update_server_ready re-registration."""

    def test_update_without_registration_warns(self):
        from hsync_client.lan import LANSync

        lan = LANSync(cid="test123", bucket="photos")
        with patch("hsync_client.lan.log") as mock_log:
            lan.update_server_ready()  # never started: warn, don't raise
        assert len(mock_log.warning.call_args_list) == 1

    def test_update_reregisters_with_server_ready(self):
        from hsync_client.lan import LANSync

        lan = LANSync(cid="test123", bucket="photos")
        with (
            patch("hsync_client.lan.Zeroconf") as mock_zc_cls,
            patch("hsync_client.lan.ServiceBrowser"),
        ):
            lan.start(register_service=True)
            zc = mock_zc_cls.return_value
            lan.update_server_ready()
            try:
                zc.unregister_service.assert_called_once()
                assert zc.register_service.call_count == 2
                new_info = zc.register_service.call_args.args[0]
                assert dict(new_info.properties)[b"server_ready"] == b"true"
                # The local node marked itself incumbent (anti-takeover).
                assert lan.peer_manager._local_server_ready is True
            finally:
                lan.stop()


class TestGetLocalIp:
    """Tests for the _get_local_ip fallbacks."""

    def test_returns_an_ip_string(self):
        from hsync_client.lan import LANSync

        lan = LANSync(cid="test123", bucket="photos")
        ip = lan._get_local_ip()
        assert isinstance(ip, str)
        socket.inet_aton(ip)  # must be a valid dotted-quad

    def test_connect_failure_falls_back_to_loopback(self):
        from hsync_client.lan import LANSync

        lan = LANSync(cid="test123", bucket="photos")
        with patch("hsync_client.lan.socket.socket") as mock_socket:
            mock_socket.return_value.connect.side_effect = OSError("no route")
            assert lan._get_local_ip() == "127.0.0.1"

    def test_socket_failure_falls_back_to_loopback(self):
        from hsync_client.lan import LANSync

        lan = LANSync(cid="test123", bucket="photos")
        with patch("hsync_client.lan.socket.socket", side_effect=OSError("no sockets")):
            assert lan._get_local_ip() == "127.0.0.1"
