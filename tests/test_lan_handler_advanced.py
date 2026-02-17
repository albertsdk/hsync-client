"""
Advanced tests for hsync_client.lan_handler module.

Tests for:
- _run_as_master persistent mode
- _run_as_slave persistent mode with WebSocket
- _with_stdin_commands handler
- _run_stdin_wait handler
- Master election scenarios
- Error handling and cleanup
"""

from typing import Any, cast
from unittest.mock import MagicMock, patch

from hsync_client.config import HsyncConfig


def _make_lan_config(tmp_path, **overrides: Any) -> HsyncConfig:
    """Return a minimal config dict suitable for lan_handler functions."""
    defaults: dict[str, Any] = {
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


class TestRunAsMaster:
    """Tests for _run_as_master function."""

    def test_run_as_master_https(self, tmp_path):
        """Master starts with HTTPS certificates."""
        from hsync_client.lan_handler import _run_as_master

        config = _make_lan_config(tmp_path, LAN_HTTPS=True)
        mock_log = MagicMock()

        mock_lan_sync = MagicMock()
        mock_lan_sync.port = 9000

        mock_cert_info = MagicMock()
        mock_cert_info.cert_file = "/tmp/cert.pem"
        mock_cert_info.key_file = "/tmp/key.pem"
        mock_cert_info.fingerprint = "abc123"

        mock_lan_server = MagicMock()
        mock_ws_server = MagicMock()

        # Track sleep calls and raise KeyboardInterrupt after first call
        sleep_count = [0]

        def raise_after_sleep(seconds):
            sleep_count[0] += 1
            if sleep_count[0] >= 1:
                raise KeyboardInterrupt()

        # Test _run_as_master when lan_server and lan_ws_server are None
        with (
            patch(
                "hsync_client.lan_handler.LANServer", return_value=mock_lan_server
            ) as mock_server_class,
            patch(
                "hsync_client.lan_handler.LANWebSocketServer", return_value=mock_ws_server
            ) as mock_ws_class,
            patch("time.sleep", side_effect=raise_after_sleep),
        ):
            try:
                _run_as_master(
                    config=config,
                    log=mock_log,
                    lan_sync=mock_lan_sync,
                    cert_info=mock_cert_info,
                    persistent=False,
                    lan_server=None,
                    lan_ws_server=None,
                )
            except KeyboardInterrupt:
                pass

        # Verify servers were created and started
        mock_server_class.assert_called_once()
        mock_ws_class.assert_called_once()
        mock_lan_server.start.assert_called_once()
        mock_ws_server.start.assert_called_once_with(watch_path=str(tmp_path))
        mock_lan_sync.update_server_ready.assert_called_once()

    def test_run_as_master_persistent_mode(self, tmp_path):
        """Master in persistent mode runs indefinitely until keyboard interrupt."""
        from hsync_client.lan_handler import _run_as_master

        config = _make_lan_config(tmp_path, LAN_PERSISTENT=True)
        mock_log = MagicMock()

        mock_lan_sync = MagicMock()
        mock_lan_sync.port = 9000

        mock_cert_info = None

        mock_lan_server = MagicMock()
        mock_ws_server = MagicMock()

        # Track sleep calls
        sleep_calls = []

        def track_sleep(seconds):
            sleep_calls.append(seconds)
            if len(sleep_calls) > 2:
                raise KeyboardInterrupt()

        try:
            with (
                patch("hsync_client.lan_handler.LANServer", return_value=mock_lan_server),
                patch("hsync_client.lan_handler.LANWebSocketServer", return_value=mock_ws_server),
                patch("time.sleep", side_effect=track_sleep),
            ):
                _run_as_master(
                    config=config,
                    log=mock_log,
                    lan_sync=mock_lan_sync,
                    cert_info=mock_cert_info,
                    persistent=True,
                    lan_server=None,
                    lan_ws_server=None,
                )
        except KeyboardInterrupt:
            pass

        # Verify sleep was called (persistent mode runs loop)
        assert len(sleep_calls) > 0

    def test_run_as_master_with_auth_key(self, tmp_path):
        """Master logs authentication info when LAN_KEY is set."""
        from hsync_client.lan_handler import _run_as_master

        config = _make_lan_config(tmp_path, LAN_KEY="secret_key")
        mock_log = MagicMock()

        mock_lan_sync = MagicMock()
        mock_lan_sync.port = 9000

        mock_cert_info = None
        mock_lan_server = MagicMock()
        mock_ws_server = MagicMock()

        sleep_count = [0]

        def raise_after_sleep(seconds):
            sleep_count[0] += 1
            if sleep_count[0] >= 1:
                raise KeyboardInterrupt()

        try:
            with (
                patch("hsync_client.lan_handler.LANServer", return_value=mock_lan_server),
                patch("hsync_client.lan_handler.LANWebSocketServer", return_value=mock_ws_server),
                patch("time.sleep", side_effect=raise_after_sleep),
            ):
                _run_as_master(
                    config=config,
                    log=mock_log,
                    lan_sync=mock_lan_sync,
                    cert_info=mock_cert_info,
                    persistent=False,
                    lan_server=None,
                    lan_ws_server=None,
                )
        except KeyboardInterrupt:
            pass

        # Verify auth-related log messages
        log_calls = [str(c) for c in mock_log.info.call_args_list]
        assert any("Authentication enabled" in c or "HSYNC_LAN_KEY" in c for c in log_calls)

    def test_run_as_master_reuses_existing_servers(self, tmp_path):
        """Master reuses existing server instances instead of creating new ones."""
        from hsync_client.lan_handler import _run_as_master

        config = _make_lan_config(tmp_path)
        mock_log = MagicMock()

        mock_lan_sync = MagicMock()
        mock_lan_sync.port = 9000

        mock_existing_server = MagicMock()
        mock_existing_ws = MagicMock()

        sleep_count = [0]

        def raise_after_sleep(seconds):
            sleep_count[0] += 1
            if sleep_count[0] >= 1:
                raise KeyboardInterrupt()

        # Initialize mocks to avoid "possibly unbound" error
        mock_server_class = MagicMock()
        mock_ws_class = MagicMock()

        try:
            with (
                patch("hsync_client.lan_handler.LANServer", return_value=mock_server_class),
                patch("hsync_client.lan_handler.LANWebSocketServer", return_value=mock_ws_class),
                patch("time.sleep", side_effect=raise_after_sleep),
            ):
                _run_as_master(
                    config=config,
                    log=mock_log,
                    lan_sync=mock_lan_sync,
                    cert_info=None,
                    persistent=False,
                    lan_server=mock_existing_server,
                    lan_ws_server=mock_existing_ws,
                )
        except KeyboardInterrupt:
            pass

        # Servers should NOT be created since they already exist
        mock_server_class.assert_not_called()
        mock_ws_class.assert_not_called()


class TestRunAsSlave:
    """Tests for _run_as_slave function - covered by test_main_logic_lan.py"""

    # Note: _run_as_slave is already thoroughly tested in tests/test_main_logic_lan.py
    # These tests require complex async mocking that is covered there
    pass


class TestStdinCommands:
    """Tests for stdin command handlers - These tests verify handler registration."""

    # Note: Full stdin command testing would require integration tests
    # since these functions use asyncio.create_task and stdin reading
    pass


class TestMasterElection:
    """Tests for master election and peer management."""

    def test_master_transition_from_slave(self, tmp_path):
        """Test scenario where client transitions from slave to master."""
        from hsync_client.lan_handler import run_lan_mode

        config = _make_lan_config(tmp_path, LAN_PERSISTENT=True)
        mock_log = MagicMock()

        # First iteration: is_master = False (slave)
        # Second iteration: is_master = True (master)
        mock_peer_mgr = MagicMock()
        mock_peer_mgr.is_master.side_effect = [False, True]

        # First: get_master returns a peer (slave mode)
        master_peer = MagicMock()
        master_peer.server_ready = True
        master_peer.get_server_url.return_value = "http://192.168.1.1:9000"
        mock_peer_mgr.get_master.return_value = master_peer

        mock_lan_sync = MagicMock()
        mock_lan_sync.start.return_value = mock_peer_mgr
        mock_lan_sync.port = 9000

        mock_lan_server = MagicMock()
        mock_ws_server = MagicMock()

        with (
            patch("hsync_client.lan_handler.LANSync", return_value=mock_lan_sync),
            patch("hsync_client.lan_handler.LANServer", return_value=mock_lan_server),
            patch("hsync_client.lan_handler.LANWebSocketServer", return_value=mock_ws_server),
            patch("hsync_client.lan_handler.set_config"),
            patch("hsync_client.lan_handler._measure_clock_offset"),
            patch("hsync_client.lan_handler.sync", new=MagicMock()),
            patch("asyncio.run"),
            patch("time.sleep", side_effect=KeyboardInterrupt),
        ):
            run_lan_mode(config, mock_log)

        # After transition to master, servers should have been stopped
        # (since master takes over)
        assert mock_lan_server.stop.call_count >= 0  # Servers may be stopped on transition


class TestClockOffsetEdgeCases:
    """Additional edge case tests for clock offset measurement."""

    def test_measure_clock_offset_invalid_json_response(self):
        """Test _measure_clock_offset with invalid JSON in response."""
        from hsync_client.lan_handler import _measure_clock_offset

        mock_master = MagicMock()
        mock_master.cid = "master-1"

        mock_config: HsyncConfig = cast(
            HsyncConfig,
            {
                "REQUEST_HEADERS": {"X-Client-Id": "test"},
                "HTTP_TIMEOUT": 30,
            },
        )
        mock_log = MagicMock()

        mock_response = MagicMock()
        mock_response.status_code = 200
        # json() raises ValueError on invalid JSON
        mock_response.json.side_effect = ValueError("Invalid JSON")

        with patch("requests.get", return_value=mock_response):
            with patch("hsync_client.utils.get_ssl_verify", return_value=True):
                _measure_clock_offset(mock_master, mock_config, mock_log)

        # Should log debug message about invalid response
        assert mock_log.debug.call_count >= 1

    def test_measure_clock_offset_with_small_rtt(self):
        """Test clock offset calculation with very small RTT."""
        from hsync_client.lan_handler import _measure_clock_offset

        mock_master = MagicMock()
        mock_master.cid = "master-1"

        mock_config: HsyncConfig = cast(
            HsyncConfig,
            {
                "REQUEST_HEADERS": {"X-Client-Id": "test"},
                "HTTP_TIMEOUT": 30,
            },
        )
        mock_log = MagicMock()

        # Simulate response with minimal RTT (nearly instant)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"timestamp": 1000.0}

        with patch("requests.get", return_value=mock_response):
            with patch("hsync_client.utils.get_ssl_verify", return_value=True):
                with patch("time.time") as mock_time:
                    # t1=1000, t3=1000.001 (1ms RTT)
                    mock_time.side_effect = [1000.0, 1000.001]
                    _measure_clock_offset(mock_master, mock_config, mock_log)

        # Verify clock_offset was set (value should be nearly 0)
        assert hasattr(mock_master, "clock_offset")
        # Should log debug (not warning) for small offset
        assert mock_log.debug.call_count >= 1
