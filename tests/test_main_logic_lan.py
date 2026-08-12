"""
Tests for hsync_client.main_logic module - LAN mode functionality.

Contains TestRunLanMode class.
"""

from typing import cast
from unittest.mock import MagicMock, patch

from hsync_client.config import HsyncConfig
from hsync_client.identity import DeviceIdentity
from hsync_client.main_logic import run_lan_mode


def _make_lan_config(tmp_path: object, **overrides: object) -> HsyncConfig:
    """Return a minimal config dict suitable for run_lan_mode."""
    defaults: dict[str, object] = {
        "CID": "test-cid",
        "REMOTE_BUCKET": "test-bucket",
        "LAN_PORT": 9000,
        "LAN_GROUP": None,
        "LAN_FORCE_MASTER": False,
        "LAN_NEVER_MASTER": False,
        "SYNC_DIRECTION": "bidirectional",
        "LOCAL_BUCKET": str(tmp_path),
        "LAN_KEY": None,
        "DISCOVERY_TIMEOUT": 1,
        "DRYRUN": False,
        "LAN_PERSISTENT": False,
        "REQUEST_HEADERS": {"X-Client-Id": "test-cid"},
        "API": {"buckets": "", "events": ""},
        "SERVER_COMPRESSION_ENABLED": False,
        "HTTP_TIMEOUT": 30,
        "MASTER_CERT_FINGERPRINT": None,
    }
    defaults.update(overrides)
    return cast(HsyncConfig, defaults)


class TestRunLanMode:
    """Tests for run_lan_mode function."""

    def test_no_peers_non_persistent_exits(self, tmp_path):
        """Non-persistent mode with no peers exits with warning."""
        config = _make_lan_config(tmp_path)
        mock_log = MagicMock()

        mock_peer_mgr = MagicMock()
        mock_peer_mgr.get_alive_peers.return_value = []
        mock_peer_mgr.is_master.return_value = False
        mock_peer_mgr.get_master.return_value = None

        mock_lan_sync = MagicMock()
        mock_lan_sync.start.return_value = mock_peer_mgr
        mock_lan_sync.port = 9000

        with patch("hsync_client.lan_handler.LANSync", return_value=mock_lan_sync):
            run_lan_mode(config, mock_log)

        assert any("alone" in str(c) for c in mock_log.warning.call_args_list)
        mock_lan_sync.stop.assert_called_once()

    def test_master_starts_servers_non_persistent(self, tmp_path):
        """Master starts HTTP and WebSocket servers."""
        config = _make_lan_config(tmp_path)
        mock_log = MagicMock()

        mock_peer_mgr = MagicMock()
        mock_peer_mgr.get_alive_peers.return_value = [MagicMock()]
        # First call: is_master -> True, then sleep raises to break loop
        mock_peer_mgr.is_master.return_value = True

        mock_lan_sync = MagicMock()
        mock_lan_sync.start.return_value = mock_peer_mgr
        mock_lan_sync.port = 9000

        mock_lan_server = MagicMock()
        mock_ws_server = MagicMock()

        with (
            patch("hsync_client.lan_handler.LANSync", return_value=mock_lan_sync),
            patch("hsync_client.lan_handler.LANServer", return_value=mock_lan_server),
            patch("hsync_client.lan_handler.LANWebSocketServer", return_value=mock_ws_server),
            patch("time.sleep", side_effect=KeyboardInterrupt),
        ):
            run_lan_mode(config, mock_log)

        mock_lan_server.start.assert_called_once()
        mock_ws_server.start.assert_called_once_with(watch_path=str(tmp_path))
        mock_lan_sync.update_server_ready.assert_called_once()

    def test_slave_non_persistent_sync(self, tmp_path):
        """Client connects to master and runs sync in non-persistent mode."""
        config = _make_lan_config(tmp_path)
        mock_log = MagicMock()

        master_peer = MagicMock()
        master_peer.cid = "master-cid"
        master_peer.address = "192.168.1.1"
        master_peer.port = 9000
        master_peer.use_https = True
        master_peer.cert_fingerprint = "abcdef1234567890" * 4
        master_peer.compression_enabled = True
        master_peer.server_ready = True
        master_peer.get_server_url.return_value = "https://192.168.1.1:9000"

        mock_peer_mgr = MagicMock()
        mock_peer_mgr.get_alive_peers.return_value = [master_peer]
        mock_peer_mgr.is_master.return_value = False
        mock_peer_mgr.get_master.return_value = master_peer

        mock_lan_sync = MagicMock()
        mock_lan_sync.start.return_value = mock_peer_mgr
        mock_lan_sync.port = 9000

        with (
            patch("hsync_client.lan_handler.LANSync", return_value=mock_lan_sync),
            patch("hsync_client.lan_handler.set_config"),
            patch("hsync_client.lan_handler._measure_clock_offset"),
            patch("hsync_client.lan_handler._evaluate_master_pin", return_value=True),
            patch("hsync_client.lan_handler.sync", new=MagicMock()),
            patch("asyncio.run") as mock_run,
        ):
            run_lan_mode(config, mock_log)

        mock_run.assert_called_once()
        assert any("master" in str(c).lower() for c in mock_log.info.call_args_list)

    def test_slave_insecure_master_refuses(self, tmp_path):
        """Client refuses a master that does not serve HTTPS with a fingerprint."""
        # Secure-only LAN: a plain-HTTP master is rejected, no sync.
        config = _make_lan_config(tmp_path)
        mock_log = MagicMock()

        master_peer = MagicMock()
        master_peer.cid = "master-cid"
        master_peer.address = "192.168.1.1"
        master_peer.port = 9000
        master_peer.use_https = False
        master_peer.cert_fingerprint = None
        master_peer.server_ready = True

        mock_peer_mgr = MagicMock()
        mock_peer_mgr.get_alive_peers.return_value = [master_peer]
        mock_peer_mgr.is_master.return_value = False
        mock_peer_mgr.get_master.return_value = master_peer

        mock_lan_sync = MagicMock()
        mock_lan_sync.start.return_value = mock_peer_mgr
        mock_lan_sync.port = 9000

        with (
            patch("hsync_client.lan_handler.LANSync", return_value=mock_lan_sync),
            patch("hsync_client.lan_handler.set_config"),
            patch("hsync_client.lan_handler._measure_clock_offset"),
            patch("hsync_client.lan_handler.sync", new=MagicMock()),
            patch("asyncio.run") as mock_run,
        ):
            run_lan_mode(config, mock_log)

        # No sync attempted and an insecure-master warning was emitted.
        mock_run.assert_not_called()
        assert any("INSECURE MASTER" in str(c) for c in mock_log.warning.call_args_list)

    def test_slave_dryrun_shows_preview(self, tmp_path):
        """Client in dryrun mode shows preview and exits."""
        config = _make_lan_config(tmp_path, DRYRUN=True)
        mock_log = MagicMock()

        master_peer = MagicMock()
        master_peer.cid = "master-cid"
        master_peer.server_ready = True
        master_peer.use_https = True
        master_peer.cert_fingerprint = "abcdef1234567890" * 4
        master_peer.compression_enabled = False
        master_peer.get_server_url.return_value = "https://192.168.1.1:9000"

        mock_peer_mgr = MagicMock()
        mock_peer_mgr.get_alive_peers.return_value = [master_peer]
        mock_peer_mgr.is_master.return_value = False
        mock_peer_mgr.get_master.return_value = master_peer

        mock_lan_sync = MagicMock()
        mock_lan_sync.start.return_value = mock_peer_mgr
        mock_lan_sync.port = 9000

        with (
            patch("hsync_client.lan_handler.LANSync", return_value=mock_lan_sync),
            patch("hsync_client.lan_handler.set_config"),
            patch("hsync_client.lan_handler._measure_clock_offset"),
            patch("hsync_client.lan_handler._evaluate_master_pin", return_value=True),
            patch("hsync_client.lan_handler._run_preview") as mock_preview,
        ):
            run_lan_mode(config, mock_log)

        mock_preview.assert_called_once_with(mock_log)

    def test_keyboard_interrupt_cleans_up(self, tmp_path):
        """KeyboardInterrupt stops all servers and lan_sync."""
        config = _make_lan_config(tmp_path)
        mock_log = MagicMock()

        mock_peer_mgr = MagicMock()
        mock_peer_mgr.get_alive_peers.return_value = []
        mock_peer_mgr.is_master.return_value = False
        mock_peer_mgr.get_master.return_value = None

        mock_lan_sync = MagicMock()
        mock_lan_sync.start.return_value = mock_peer_mgr
        mock_lan_sync.port = 9000
        # Make wait_for_discovery raise to simulate Ctrl+C
        mock_lan_sync.wait_for_discovery.side_effect = KeyboardInterrupt

        with patch("hsync_client.lan_handler.LANSync", return_value=mock_lan_sync):
            run_lan_mode(config, mock_log)

        mock_lan_sync.stop.assert_called_once()
        assert any("Shutting down" in str(c) for c in mock_log.info.call_args_list)

    def test_loads_persistent_master_certificate(self, tmp_path):
        """LAN mode always loads the persistent master certificate."""
        config = _make_lan_config(tmp_path)
        mock_log = MagicMock()

        mock_peer_mgr = MagicMock()
        mock_peer_mgr.get_alive_peers.return_value = []
        mock_peer_mgr.is_master.return_value = False
        mock_peer_mgr.get_master.return_value = None

        mock_lan_sync = MagicMock()
        mock_lan_sync.start.return_value = mock_peer_mgr
        mock_lan_sync.port = 9000

        mock_cert_info = MagicMock()
        mock_cert_info.fingerprint = "abcdef1234567890" * 4

        with (
            patch("hsync_client.lan_handler.LANSync", return_value=mock_lan_sync),
            patch(
                "hsync_client.identity.load_or_create_device_identity",
                return_value=DeviceIdentity(
                    device_id="TestDevice1234567890ab", cert_info=mock_cert_info
                ),
            ) as mock_load,
        ):
            run_lan_mode(config, mock_log)

        mock_load.assert_called_once()

    def test_slave_waits_for_master_server_ready(self, tmp_path):
        """Client re-enters discovery loop when master not ready."""
        config = _make_lan_config(tmp_path)
        mock_log = MagicMock()

        master_not_ready = MagicMock()
        master_not_ready.cid = "master-cid"
        master_not_ready.server_ready = False

        master_ready = MagicMock()
        master_ready.cid = "master-cid"
        master_ready.server_ready = True
        master_ready.use_https = True
        master_ready.cert_fingerprint = "abcdef1234567890" * 4
        master_ready.compression_enabled = False
        master_ready.get_server_url.return_value = "https://192.168.1.1:9000"

        mock_peer_mgr = MagicMock()
        mock_peer_mgr.get_alive_peers.return_value = [MagicMock()]
        mock_peer_mgr.is_master.return_value = False
        # First call returns not-ready master, second returns ready master
        mock_peer_mgr.get_master.side_effect = [master_not_ready, master_ready]

        mock_lan_sync = MagicMock()
        mock_lan_sync.start.return_value = mock_peer_mgr
        mock_lan_sync.port = 9000

        with (
            patch("hsync_client.lan_handler.LANSync", return_value=mock_lan_sync),
            patch("hsync_client.lan_handler.set_config"),
            patch("hsync_client.lan_handler._measure_clock_offset"),
            patch("hsync_client.lan_handler._evaluate_master_pin", return_value=True),
            patch("hsync_client.lan_handler.sync", new=MagicMock()),
            patch("asyncio.run"),
        ):
            run_lan_mode(config, mock_log)

        # wait_for_discovery should be called twice (first loop: not ready, second: ready)
        assert mock_lan_sync.wait_for_discovery.call_count == 2
