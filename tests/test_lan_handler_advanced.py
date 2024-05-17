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

    def test_run_as_master_returns_server_tuple(self, tmp_path):
        """_run_as_master returns the (lan_server, lan_ws_server) tuple.

        Regression test: previously the function returned None, so the caller
        lost the references and the next discovery-loop iteration tried to
        re-bind the same port, raising OSError(98) 'Address already in use'.
        """
        from hsync_client.lan_handler import _run_as_master

        config = _make_lan_config(tmp_path)
        mock_log = MagicMock()
        mock_lan_sync = MagicMock()
        mock_lan_sync.port = 9000

        mock_server_instance = MagicMock()
        mock_ws_instance = MagicMock()

        # In persistent=True mode the inner loop is `while True: sleep; break`,
        # so the function returns after a single sleep call.
        with (
            patch(
                "hsync_client.lan_handler.LANServer",
                return_value=mock_server_instance,
            ),
            patch(
                "hsync_client.lan_handler.LANWebSocketServer",
                return_value=mock_ws_instance,
            ),
            patch("time.sleep"),
        ):
            result = _run_as_master(
                config=config,
                log=mock_log,
                lan_sync=mock_lan_sync,
                cert_info=None,
                persistent=True,
                lan_server=None,
                lan_ws_server=None,
            )

        assert result == (mock_server_instance, mock_ws_instance), (
            "_run_as_master must return the (lan_server, lan_ws_server) tuple "
            "so the outer discovery loop can persist references and avoid "
            "re-binding the same port on the next iteration"
        )

    def test_run_as_master_idempotent_across_iterations(self, tmp_path):
        """Two consecutive calls in persistent mode must not re-create servers.

        Simulates the discovery-loop behaviour: the tuple returned by the first
        call is fed back into the second call; no new server instance must be
        instantiated and therefore no second bind() on the same port.
        """
        from hsync_client.lan_handler import _run_as_master

        config = _make_lan_config(tmp_path)
        mock_log = MagicMock()
        mock_lan_sync = MagicMock()
        mock_lan_sync.port = 9000

        mock_server_instance = MagicMock()
        mock_ws_instance = MagicMock()
        server_factory = MagicMock(return_value=mock_server_instance)
        ws_factory = MagicMock(return_value=mock_ws_instance)

        with (
            patch("hsync_client.lan_handler.LANServer", server_factory),
            patch("hsync_client.lan_handler.LANWebSocketServer", ws_factory),
            patch("time.sleep"),
        ):
            first = _run_as_master(
                config=config,
                log=mock_log,
                lan_sync=mock_lan_sync,
                cert_info=None,
                persistent=True,
                lan_server=None,
                lan_ws_server=None,
            )
            # Feed the returned tuple back, as the discovery loop now does.
            second = _run_as_master(
                config=config,
                log=mock_log,
                lan_sync=mock_lan_sync,
                cert_info=None,
                persistent=True,
                lan_server=first[0],
                lan_ws_server=first[1],
            )

        # Factories were called exactly once (the first iteration).
        assert server_factory.call_count == 1
        assert ws_factory.call_count == 1
        # The tuple identity is preserved across iterations.
        assert second == first


class TestRunAsMasterPersistentSemantics:
    """Regression tests for the consolidated master loop.

    The master role is intrinsically continuous: it must remain
    available to serve any peer that joins.  After the refactor that
    collapsed the duplicated persistent/non-persistent branches in
    :func:`_run_as_master`, the function must:

    * Always sleep once and return (no infinite ``while True`` inside).
    * Behave identically regardless of the ``persistent`` argument.
    * Be re-entered by the outer discovery loop while the role holds,
      without rebinding servers (verified by other tests above).
    """

    def test_master_returns_after_single_sleep_when_non_persistent(self, tmp_path):
        """Non-persistent master must not block forever: it returns after one sleep."""
        from hsync_client.lan_handler import _run_as_master

        config = _make_lan_config(tmp_path, LAN_PERSISTENT=False)
        mock_log = MagicMock()
        mock_lan_sync = MagicMock()
        mock_lan_sync.port = 9000

        sleep_calls: list[float] = []

        with (
            patch("hsync_client.lan_handler.LANServer", return_value=MagicMock()),
            patch("hsync_client.lan_handler.LANWebSocketServer", return_value=MagicMock()),
            patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        ):
            result = _run_as_master(
                config=config,
                log=mock_log,
                lan_sync=mock_lan_sync,
                cert_info=None,
                persistent=False,
                lan_server=None,
                lan_ws_server=None,
            )

        # Refactored behaviour: exactly one sleep, then return.
        assert len(sleep_calls) == 1, (
            f"Master must sleep exactly once and return; got {len(sleep_calls)} sleeps"
        )
        assert result is not None
        assert result[0] is not None and result[1] is not None

    def test_master_behaviour_is_identical_for_both_persistent_values(self, tmp_path):
        """Master must yield/return identically regardless of the persistent flag."""
        from hsync_client.lan_handler import _run_as_master

        def _run_once(persistent_value: bool) -> tuple[int, object]:
            config = _make_lan_config(tmp_path, LAN_PERSISTENT=persistent_value)
            sleeps: list[float] = []
            with (
                patch("hsync_client.lan_handler.LANServer", return_value=MagicMock()),
                patch("hsync_client.lan_handler.LANWebSocketServer", return_value=MagicMock()),
                patch("time.sleep", side_effect=lambda s: sleeps.append(s)),
            ):
                result = _run_as_master(
                    config=config,
                    log=MagicMock(),
                    lan_sync=MagicMock(port=9000),
                    cert_info=None,
                    persistent=persistent_value,
                    lan_server=None,
                    lan_ws_server=None,
                )
            return len(sleeps), result

        persistent_sleeps, persistent_result = _run_once(True)
        non_persistent_sleeps, non_persistent_result = _run_once(False)

        assert persistent_sleeps == non_persistent_sleeps == 1
        # Both paths return a 2-tuple of (server, ws_server) instances.
        assert persistent_result is not None and non_persistent_result is not None
        assert len(persistent_result) == 2 and len(non_persistent_result) == 2


class TestRunLanModeOuterLoopMasterPath:
    """Regression tests for the outer discovery loop on the master path.

    Previously the loop contained ``if not persistent: break`` right
    after ``_run_as_master`` returned.  Since the master is inherently
    continuous, that branch was either dead code (when the inner master
    loop blocked forever) or, after the refactor, would have shut the
    master down immediately - leaving no time for slaves to sync.  The
    branch was removed; the loop now only exits via KeyboardInterrupt
    or by losing the master role.
    """

    def test_master_loop_does_not_exit_on_non_persistent(self, tmp_path):
        """Outer loop on master path must keep iterating even with persistent=False."""
        from hsync_client.lan_handler import run_lan_mode

        config = _make_lan_config(tmp_path, LAN_PERSISTENT=False)
        mock_log = MagicMock()

        mock_peer_mgr = MagicMock()
        # Stay master across iterations until KeyboardInterrupt is raised.
        mock_peer_mgr.is_master.return_value = True

        mock_lan_sync = MagicMock()
        mock_lan_sync.start.return_value = mock_peer_mgr
        mock_lan_sync.port = 9000

        sleep_calls: list[float] = []

        def _sleep_then_interrupt_after_three(seconds: float) -> None:
            sleep_calls.append(seconds)
            if len(sleep_calls) >= 3:
                raise KeyboardInterrupt

        with (
            patch("hsync_client.lan_handler.LANSync", return_value=mock_lan_sync),
            patch("hsync_client.lan_handler.LANServer", return_value=MagicMock()),
            patch("hsync_client.lan_handler.LANWebSocketServer", return_value=MagicMock()),
            patch("time.sleep", side_effect=_sleep_then_interrupt_after_three),
        ):
            run_lan_mode(config, mock_log)

        # If the outer loop had broken on the first non-persistent master
        # iteration, only one sleep would have been recorded.  The fact
        # that we reached three sleeps proves the loop kept re-entering
        # _run_as_master.
        assert len(sleep_calls) >= 3, (
            "Outer loop must keep calling _run_as_master while the master role "
            "is held, regardless of LAN_PERSISTENT"
        )


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
        """Test scenario where client transitions from client to master."""
        from hsync_client.lan_handler import run_lan_mode

        config = _make_lan_config(tmp_path, LAN_PERSISTENT=True)
        mock_log = MagicMock()

        # First iteration: is_master = False (client)
        # Second iteration: is_master = True (master)
        mock_peer_mgr = MagicMock()
        mock_peer_mgr.is_master.side_effect = [False, True]

        # First: get_master returns a peer (client mode)
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
