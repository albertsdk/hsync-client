"""
Tests for hsync_client.main_logic module - LAN mode functionality.

Contains TestRunLanMode class.
"""

from typing import cast
from unittest.mock import MagicMock, patch

from hsync_client.config import HsyncConfig
from hsync_client.main_logic import _run_lan_mode


def _make_lan_config(tmp_path: object, **overrides: object) -> HsyncConfig:
    """Return a minimal config dict suitable for _run_lan_mode."""
    defaults: dict[str, object] = {
        "CID": "test-cid",
        "REMOTE_BUCKET": "test-bucket",
        "LAN_PORT": 9000,
        "LAN_GROUP": None,
        "LAN_HTTPS": False,
        "LAN_FORCE_MASTER": False,
        "SYNC_DIRECTION": "bidirectional",
        "LOCAL_BUCKET": str(tmp_path),
        "LAN_KEY": None,
        "DISCOVERY_TIMEOUT": 0,
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
    """Tests for _run_lan_mode function."""

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

        with patch("hsync_client.main_logic.LANSync", return_value=mock_lan_sync):
            _run_lan_mode(config, mock_log)

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
            patch("hsync_client.main_logic.LANSync", return_value=mock_lan_sync),
            patch("hsync_client.main_logic.LANServer", return_value=mock_lan_server),
            patch("hsync_client.main_logic.LANWebSocketServer", return_value=mock_ws_server),
            patch("time.sleep", side_effect=KeyboardInterrupt),
        ):
            _run_lan_mode(config, mock_log)

        mock_lan_server.start.assert_called_once()
        mock_ws_server.start.assert_called_once_with(watch_path=str(tmp_path))
        mock_lan_sync.update_server_ready.assert_called_once()

    def test_slave_non_persistent_sync(self, tmp_path):
        """Slave connects to master and runs sync in non-persistent mode."""
        config = _make_lan_config(tmp_path)
        mock_log = MagicMock()

        master_peer = MagicMock()
        master_peer.cid = "master-cid"
        master_peer.address = "192.168.1.1"
        master_peer.port = 9000
        master_peer.use_https = False
        master_peer.cert_fingerprint = None
        master_peer.compression_enabled = True
        master_peer.server_ready = True
        master_peer.get_server_url.return_value = "http://192.168.1.1:9000"

        mock_peer_mgr = MagicMock()
        mock_peer_mgr.get_alive_peers.return_value = [master_peer]
        mock_peer_mgr.is_master.return_value = False
        mock_peer_mgr.get_master.return_value = master_peer

        mock_lan_sync = MagicMock()
        mock_lan_sync.start.return_value = mock_peer_mgr
        mock_lan_sync.port = 9000

        with (
            patch("hsync_client.main_logic.LANSync", return_value=mock_lan_sync),
            patch("hsync_client.main_logic.set_config"),
            patch("hsync_client.main_logic._measure_clock_offset"),
            patch("hsync_client.main_logic.sync", new=MagicMock()),
            patch("hsync_client.main_logic.run") as mock_run,
        ):
            _run_lan_mode(config, mock_log)

        mock_run.assert_called_once()
        assert any("master" in str(c).lower() for c in mock_log.info.call_args_list)

    def test_slave_dryrun_shows_preview(self, tmp_path):
        """Slave in dryrun mode shows preview and exits."""
        config = _make_lan_config(tmp_path, DRYRUN=True)
        mock_log = MagicMock()

        master_peer = MagicMock()
        master_peer.cid = "master-cid"
        master_peer.server_ready = True
        master_peer.use_https = False
        master_peer.cert_fingerprint = None
        master_peer.compression_enabled = False
        master_peer.get_server_url.return_value = "http://192.168.1.1:9000"

        mock_peer_mgr = MagicMock()
        mock_peer_mgr.get_alive_peers.return_value = [master_peer]
        mock_peer_mgr.is_master.return_value = False
        mock_peer_mgr.get_master.return_value = master_peer

        mock_lan_sync = MagicMock()
        mock_lan_sync.start.return_value = mock_peer_mgr
        mock_lan_sync.port = 9000

        with (
            patch("hsync_client.main_logic.LANSync", return_value=mock_lan_sync),
            patch("hsync_client.main_logic.set_config"),
            patch("hsync_client.main_logic._measure_clock_offset"),
            patch("hsync_client.main_logic._run_preview") as mock_preview,
        ):
            _run_lan_mode(config, mock_log)

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

        with patch("hsync_client.main_logic.LANSync", return_value=mock_lan_sync):
            _run_lan_mode(config, mock_log)

        mock_lan_sync.stop.assert_called_once()
        assert any("Shutting down" in str(c) for c in mock_log.info.call_args_list)

    def test_https_generates_certificates(self, tmp_path):
        """LAN_HTTPS=True triggers self-signed certificate generation."""
        config = _make_lan_config(tmp_path, LAN_HTTPS=True)
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
            patch("hsync_client.main_logic.LANSync", return_value=mock_lan_sync),
            patch(
                "hsync_client.ssl_utils.generate_self_signed_cert", return_value=mock_cert_info
            ) as mock_gen,
            patch(
                "hsync_client.ssl_utils.save_cert_to_temp_files", return_value=mock_cert_info
            ) as mock_save,
        ):
            _run_lan_mode(config, mock_log)

        mock_gen.assert_called_once()
        mock_save.assert_called_once_with(mock_cert_info)

    def test_slave_waits_for_master_server_ready(self, tmp_path):
        """Slave re-enters discovery loop when master not ready."""
        config = _make_lan_config(tmp_path)
        mock_log = MagicMock()

        master_not_ready = MagicMock()
        master_not_ready.cid = "master-cid"
        master_not_ready.server_ready = False

        master_ready = MagicMock()
        master_ready.cid = "master-cid"
        master_ready.server_ready = True
        master_ready.use_https = False
        master_ready.cert_fingerprint = None
        master_ready.compression_enabled = False
        master_ready.get_server_url.return_value = "http://192.168.1.1:9000"

        mock_peer_mgr = MagicMock()
        mock_peer_mgr.get_alive_peers.return_value = [MagicMock()]
        mock_peer_mgr.is_master.return_value = False
        # First call returns not-ready master, second returns ready master
        mock_peer_mgr.get_master.side_effect = [master_not_ready, master_ready]

        mock_lan_sync = MagicMock()
        mock_lan_sync.start.return_value = mock_peer_mgr
        mock_lan_sync.port = 9000

        with (
            patch("hsync_client.main_logic.LANSync", return_value=mock_lan_sync),
            patch("hsync_client.main_logic.set_config"),
            patch("hsync_client.main_logic._measure_clock_offset"),
            patch("hsync_client.main_logic.sync", new=MagicMock()),
            patch("hsync_client.main_logic.run"),
        ):
            _run_lan_mode(config, mock_log)

        # wait_for_discovery should be called twice (first loop: not ready, second: ready)
        assert mock_lan_sync.wait_for_discovery.call_count == 2
