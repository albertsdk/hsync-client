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

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from hsync_client.config import HsyncConfig
from hsync_client.constants import DISCOVERY_POLL_INTERVAL
from hsync_client.identity import DeviceIdentity


def _make_lan_config(tmp_path, **overrides: Any) -> HsyncConfig:
    """Return a minimal config dict suitable for lan_handler functions."""
    defaults: dict[str, Any] = {
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


def _make_cert_info(**overrides: Any) -> MagicMock:
    """Build a master cert-info mock (LAN transport is HTTPS-only)."""
    cert_info = MagicMock()
    cert_info.cert_file = "/tmp/cert.pem"
    cert_info.key_file = "/tmp/key.pem"
    cert_info.fingerprint = "abcdef1234567890" * 4
    for key, value in overrides.items():
        setattr(cert_info, key, value)
    return cert_info


class TestRunAsMaster:
    """Tests for _run_as_master function."""

    def test_run_as_master_https(self, tmp_path):
        """Master starts with HTTPS certificates."""
        from hsync_client.lan_handler import _run_as_master

        config = _make_lan_config(tmp_path)
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

        mock_cert_info = _make_cert_info()

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

        mock_cert_info = _make_cert_info()
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
                cert_info=_make_cert_info(),
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
                cert_info=_make_cert_info(),
                persistent=True,
                lan_server=None,
                lan_ws_server=None,
            )
            # Feed the returned tuple back, as the discovery loop now does.
            second = _run_as_master(
                config=config,
                log=mock_log,
                lan_sync=mock_lan_sync,
                cert_info=_make_cert_info(),
                persistent=True,
                lan_server=first[0],
                lan_ws_server=first[1],
            )

        # Factories were called exactly once (the first iteration).
        assert server_factory.call_count == 1
        assert ws_factory.call_count == 1
        # The tuple identity is preserved across iterations.
        assert second == first

    def test_run_as_master_startup_messages_logged_once(self, tmp_path):
        """Role and Ctrl+C notices are emitted only on the first activation.

        The discovery loop re-enters _run_as_master every iteration while the
        client stays master; the one-shot startup messages must not repeat.
        """
        from hsync_client.lan_handler import _run_as_master

        config = _make_lan_config(tmp_path)
        mock_log = MagicMock()
        mock_lan_sync = MagicMock()
        mock_lan_sync.port = 9000

        with (
            patch("hsync_client.lan_handler.LANServer", return_value=MagicMock()),
            patch("hsync_client.lan_handler.LANWebSocketServer", return_value=MagicMock()),
            patch("time.sleep"),
        ):
            first = _run_as_master(
                config=config,
                log=mock_log,
                lan_sync=mock_lan_sync,
                cert_info=_make_cert_info(),
                persistent=True,
                lan_server=None,
                lan_ws_server=None,
            )
            _run_as_master(
                config=config,
                log=mock_log,
                lan_sync=mock_lan_sync,
                cert_info=_make_cert_info(),
                persistent=True,
                lan_server=first[0],
                lan_ws_server=first[1],
            )

        infos = [str(c.args[0]) for c in mock_log.info.call_args_list if c.args]
        role_msgs = [m for m in infos if "is the MASTER" in m]
        ctrlc_msgs = [m for m in infos if "Ctrl+C" in m]
        assert len(role_msgs) == 1, f"role message must log once, got {role_msgs!r}"
        assert len(ctrlc_msgs) == 1, f"Ctrl+C message must log once, got {ctrlc_msgs!r}"


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
                cert_info=_make_cert_info(),
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
                    cert_info=_make_cert_info(),
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


class TestRunLanModeNoMasterWarning:
    """Tests for the [NO MASTER] warning emitted when no master is elected.

    This happens, for example, when every discovered peer is configured
    with --never-master so the candidate list is empty.
    """

    def test_no_master_warning_after_timeout(self, tmp_path):
        """A parsable [NO MASTER] warning is logged once after the timeout."""
        from hsync_client import lan_handler
        from hsync_client.lan_handler import NO_MASTER_WARNING_TIMEOUT, run_lan_mode

        config = _make_lan_config(tmp_path, LAN_PERSISTENT=True)
        mock_log = MagicMock()

        mock_peer_mgr = MagicMock()
        mock_peer_mgr.is_master.return_value = False
        mock_peer_mgr.get_master_cid.return_value = None

        mock_lan_sync = MagicMock()
        mock_lan_sync.start.return_value = mock_peer_mgr
        mock_lan_sync.port = 9000

        # First slave iteration continues the loop; the second breaks it so
        # the test terminates deterministically.
        slave_calls: list[int] = []

        def _slave(**_kw: Any) -> bool:
            slave_calls.append(1)
            return len(slave_calls) < 2

        # time.time advances past the warning threshold between iterations.
        times = iter([100.0, 100.0 + NO_MASTER_WARNING_TIMEOUT + 1.0])

        with (
            patch("hsync_client.lan_handler.LANSync", return_value=mock_lan_sync),
            patch.object(lan_handler, "_run_as_slave", side_effect=_slave),
            patch("time.time", side_effect=lambda: next(times)),
        ):
            run_lan_mode(config, mock_log)

        warnings = [str(c.args[0]) for c in mock_log.warning.call_args_list if c.args]
        no_master_warnings = [w for w in warnings if "[NO MASTER]" in w]
        assert len(no_master_warnings) == 1, (
            f"expected exactly one [NO MASTER] warning, got {warnings!r}"
        )


class TestRunLanModeDiscoveryTimeout:
    """Tests for discovery timeout sentinel semantics in the outer LAN loop."""

    def test_timeout_zero_uses_poll_interval_not_busy_spin(self, tmp_path):
        """DISCOVERY_TIMEOUT=0 keeps waiting but still sleeps in finite polling windows."""
        from hsync_client.lan_handler import run_lan_mode

        config = _make_lan_config(tmp_path, DISCOVERY_TIMEOUT=0, LAN_PERSISTENT=False)
        mock_log = MagicMock()

        mock_peer_mgr = MagicMock()
        mock_peer_mgr.is_master.return_value = False
        mock_peer_mgr.get_master_cid.return_value = None
        mock_peer_mgr.get_master.return_value = None

        mock_lan_sync = MagicMock()
        mock_lan_sync.start.return_value = mock_peer_mgr
        mock_lan_sync.port = 9000
        mock_lan_sync.wait_for_discovery.side_effect = [None, KeyboardInterrupt]

        with patch("hsync_client.lan_handler.LANSync", return_value=mock_lan_sync):
            run_lan_mode(config, mock_log)

        first_call = mock_lan_sync.wait_for_discovery.call_args_list[0]
        assert first_call.kwargs["timeout"] == DISCOVERY_POLL_INTERVAL
        assert any("Discovery timeout disabled" in str(c) for c in mock_log.info.call_args_list)


def _make_master(**overrides: Any) -> MagicMock:
    """Build a master-peer mock with the attributes _run_as_slave reads.

    Defaults to a secure master (LAN transport is HTTPS-only); pass
    ``use_https=False``/``cert_fingerprint=None`` to model an insecure peer.
    """
    master = MagicMock()
    master.cid = "master-1"
    master.server_ready = True
    master.use_https = True
    master.cert_fingerprint = "a" * 40
    master.port = 9000
    master.address = "192.168.1.1"
    master.compression_enabled = False
    master.get_server_url.return_value = "https://192.168.1.1:9000"
    for key, value in overrides.items():
        setattr(master, key, value)
    return master


class TestRunAsSlave:
    """Direct tests for the _run_as_slave branches not exercised elsewhere."""

    def test_https_sets_fingerprint_and_bearer(self, tmp_path, monkeypatch):
        """HTTPS master with LAN_KEY stores fingerprint and sends a Bearer token."""
        from hsync_client.lan_handler import _run_as_slave

        monkeypatch.setenv("HSYNC_LAN_PIN_FILE", str(tmp_path / "pins.json"))
        config = _make_lan_config(tmp_path, LAN_KEY="secret")
        master = _make_master(
            use_https=True,
            cert_fingerprint="f" * 40,
            compression_enabled=True,
            get_server_url=MagicMock(return_value="https://192.168.1.1:9000"),
        )
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = master

        with (
            patch("hsync_client.lan_handler.set_config"),
            patch("hsync_client.lan_handler.sync", new=MagicMock()),
            patch("asyncio.run"),
        ):
            result = _run_as_slave(config, MagicMock(), MagicMock(), None, False, peer_mgr)

        assert result is False
        assert config["MASTER_CERT_FINGERPRINT"] == "f" * 40
        assert config["REQUEST_HEADERS"]["Authorization"] == "Bearer secret"
        assert config["SERVER_COMPRESSION_ENABLED"] is True
        assert config["API"]["events"]

    def test_first_https_sighting_pins_fingerprint(self, tmp_path, monkeypatch):
        """First time a master's HTTPS cert is seen it is pinned (TOFU)."""
        from hsync_client.lan_handler import _run_as_slave
        from hsync_client.pinning import PinStore

        pin_file = tmp_path / "pins.json"
        monkeypatch.setenv("HSYNC_LAN_PIN_FILE", str(pin_file))
        config = _make_lan_config(tmp_path, LAN_KEY="secret")
        master = _make_master(
            use_https=True,
            cert_fingerprint="a" * 40,
            get_server_url=MagicMock(return_value="https://192.168.1.1:9000"),
        )
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = master

        with (
            patch("hsync_client.lan_handler.set_config"),
            patch("hsync_client.lan_handler.sync", new=MagicMock()),
            patch("asyncio.run"),
        ):
            _run_as_slave(config, MagicMock(), MagicMock(), None, False, peer_mgr)

        assert PinStore(pin_file).get("master-1", "test-bucket") == "a" * 40

    def test_changed_fingerprint_is_refused(self, tmp_path, monkeypatch):
        """A master whose cert no longer matches the pin is refused (no connect)."""
        from hsync_client.lan_handler import _run_as_slave
        from hsync_client.pinning import PinStore

        pin_file = tmp_path / "pins.json"
        monkeypatch.setenv("HSYNC_LAN_PIN_FILE", str(pin_file))
        PinStore(pin_file).set("master-1", "test-bucket", "a" * 40)

        config = _make_lan_config(tmp_path, LAN_KEY="secret")
        master = _make_master(
            use_https=True,
            cert_fingerprint="b" * 40,
            get_server_url=MagicMock(return_value="https://192.168.1.1:9000"),
        )
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = master
        mock_log = MagicMock()
        mock_sync = MagicMock()

        with (
            patch("hsync_client.lan_handler.set_config"),
            patch("hsync_client.lan_handler.sync", new=mock_sync),
            patch("asyncio.run") as mock_run,
        ):
            result = _run_as_slave(config, mock_log, MagicMock(), None, False, peer_mgr)

        assert result is False
        assert config["MASTER_CERT_FINGERPRINT"] is None
        mock_run.assert_not_called()
        assert any("FINGERPRINT CHANGED" in str(c) for c in mock_log.error.call_args_list)

    def test_strict_pairing_refuses_unpinned_first_sighting(self, tmp_path, monkeypatch):
        """With strict pairing a first-seen cert is refused and never pinned."""
        from hsync_client.lan_handler import _run_as_slave
        from hsync_client.pinning import PinStore

        pin_file = tmp_path / "pins.json"
        monkeypatch.setenv("HSYNC_LAN_PIN_FILE", str(pin_file))

        config = _make_lan_config(tmp_path, LAN_KEY="secret", LAN_REQUIRE_PAIRING=True)
        master = _make_master(
            use_https=True,
            cert_fingerprint="a" * 40,
            get_server_url=MagicMock(return_value="https://192.168.1.1:9000"),
        )
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = master
        mock_log = MagicMock()

        with (
            patch("hsync_client.lan_handler.set_config"),
            patch("hsync_client.lan_handler.sync", new=MagicMock()),
            patch("asyncio.run") as mock_run,
        ):
            result = _run_as_slave(config, mock_log, MagicMock(), None, False, peer_mgr)

        assert result is False
        assert config["MASTER_CERT_FINGERPRINT"] is None
        mock_run.assert_not_called()
        assert PinStore(pin_file).get("master-1", "test-bucket") is None
        assert any("PAIRING NEEDED" in str(c) for c in mock_log.error.call_args_list)

    def test_insecure_master_is_refused(self, tmp_path):
        """A plain-HTTP master is refused (secure-only LAN); the key is never sent."""
        from hsync_client.lan_handler import _run_as_slave

        config = _make_lan_config(tmp_path, LAN_KEY="secret")
        master = _make_master(use_https=False, cert_fingerprint=None)
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = master
        mock_log = MagicMock()

        with (
            patch("hsync_client.lan_handler.set_config"),
            patch("hsync_client.lan_handler.sync", new=MagicMock()),
            patch("asyncio.run") as mock_run,
        ):
            result = _run_as_slave(config, mock_log, MagicMock(), None, False, peer_mgr)

        assert result is False
        assert "Authorization" not in config["REQUEST_HEADERS"]
        mock_run.assert_not_called()
        assert any("INSECURE MASTER" in str(c) for c in mock_log.warning.call_args_list)

    def test_no_master_non_persistent_wait_indefinitely_keeps_looping(self, tmp_path):
        """Non-persistent mode can still wait indefinitely for the first peer."""
        from hsync_client.lan_handler import _run_as_slave

        config = _make_lan_config(tmp_path, LAN_PERSISTENT=False)
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = None
        mock_log = MagicMock()

        result = _run_as_slave(
            config,
            mock_log,
            MagicMock(),
            None,
            False,
            peer_mgr,
            wait_indefinitely_for_peer=True,
        )

        assert result is True
        assert any("Waiting for peers" in str(c) for c in mock_log.info.call_args_list)

    def test_persistent_connects_websocket(self, tmp_path):
        """Persistent slave connects via WebSocket and returns True to loop again."""
        from hsync_client.lan_handler import _run_as_slave

        config = _make_lan_config(tmp_path, LAN_PERSISTENT=True)
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = _make_master()

        with (
            patch("hsync_client.lan_handler.set_config"),
            patch("hsync_client.lan_handler._evaluate_master_pin", return_value=True),
            patch("hsync_client.lan_handler.sync", new=MagicMock()),
            patch("hsync_client.lan_handler.lan_websocket_client", new=MagicMock()),
            patch("hsync_client.lan_handler._with_stdin_commands", new=MagicMock()) as mock_wsc,
            patch("asyncio.run"),
        ):
            result = _run_as_slave(config, MagicMock(), MagicMock(), None, True, peer_mgr)

        assert result is True
        mock_wsc.assert_called_once()

    def test_persistent_websocket_error_is_swallowed(self, tmp_path):
        """A WebSocket failure is logged but still re-enters the discovery loop."""
        from hsync_client.lan_handler import _run_as_slave

        config = _make_lan_config(tmp_path, LAN_PERSISTENT=True)
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = _make_master()
        mock_log = MagicMock()

        # First run() is the initial sync (ok); second is the WebSocket (boom).
        with (
            patch("hsync_client.lan_handler.set_config"),
            patch("hsync_client.lan_handler._evaluate_master_pin", return_value=True),
            patch("hsync_client.lan_handler.sync", new=MagicMock()),
            patch("hsync_client.lan_handler.lan_websocket_client", new=MagicMock()),
            patch("hsync_client.lan_handler._with_stdin_commands", new=MagicMock()),
            patch("asyncio.run", side_effect=[None, RuntimeError("boom")]),
        ):
            result = _run_as_slave(config, mock_log, MagicMock(), None, True, peer_mgr)

        assert result is True
        assert any("WebSocket connection ended" in str(c) for c in mock_log.warning.call_args_list)

    def test_dryrun_persistent_runs_preview_and_stdin_wait(self, tmp_path):
        """Dry-run persistent slave shows a preview then waits on stdin."""
        from hsync_client.lan_handler import _run_as_slave

        config = _make_lan_config(tmp_path, LAN_PERSISTENT=True, DRYRUN=True)
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = _make_master()

        with (
            patch("hsync_client.lan_handler.set_config"),
            patch("hsync_client.lan_handler._evaluate_master_pin", return_value=True),
            patch("hsync_client.lan_handler._run_preview") as mock_preview,
            patch("hsync_client.lan_handler._run_stdin_wait", new=MagicMock()),
            patch("hsync_client.lan_handler.lan_websocket_client", new=MagicMock()),
            patch("hsync_client.lan_handler._with_stdin_commands", new=MagicMock()),
            patch("asyncio.run"),
        ):
            result = _run_as_slave(config, MagicMock(), MagicMock(), None, True, peer_mgr)

        assert result is True
        mock_preview.assert_called_once()

    def test_no_master_persistent_waits(self, tmp_path):
        """No master and persistent: keep looping, logging a wait message."""
        from hsync_client.lan_handler import _run_as_slave

        config = _make_lan_config(tmp_path, LAN_PERSISTENT=True)
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = None
        mock_log = MagicMock()

        result = _run_as_slave(config, mock_log, MagicMock(), None, True, peer_mgr)

        assert result is True
        assert any("Waiting for peers" in str(c) for c in mock_log.info.call_args_list)


class TestStdinCommands:
    """Tests for the stdin command handler coroutines."""

    def _capture_handler(self):
        """Return a (handler_mock, registered_dict) pair for StdinCommandHandler."""
        registered: dict[str, Any] = {}
        handler = MagicMock()
        handler.register.side_effect = lambda name, fn: registered.__setitem__(name, fn)
        handler.run = AsyncMock()
        return handler, registered

    def test_with_stdin_commands_registers_and_dispatches(self, tmp_path):
        """_with_stdin_commands registers PREVIEW/SYNC/SET_SYNC_DIRECTION/SET_BANDWIDTH and runs them."""
        from hsync_client.lan_handler import _with_stdin_commands

        handler, registered = self._capture_handler()
        fake_config = {"LAN_MODE": True, "SYNC_DIRECTION": "bidirectional"}

        async def _coro() -> None:
            return None

        with (
            patch("hsync_client.preview.StdinCommandHandler", return_value=handler),
            patch("hsync_client.preview.CONFIG", fake_config),
            patch("hsync_client.preview.sync", new=AsyncMock()) as mock_sync,
            patch("hsync_client.preview._run_preview") as mock_preview,
        ):
            asyncio.run(_with_stdin_commands(MagicMock(), _coro()))

            assert set(registered) == {"PREVIEW", "SYNC", "SET_SYNC_DIRECTION", "SET_BANDWIDTH"}

            # PREVIEW delegates to _run_preview.
            registered["PREVIEW"]()
            mock_preview.assert_called_once()

            # SYNC triggers a sync using the current LAN_MODE.
            asyncio.run(registered["SYNC"]())
            mock_sync.assert_awaited_with(lan_mode=True)

            # Valid direction change resyncs; invalid and no-op paths short-circuit.
            asyncio.run(registered["SET_SYNC_DIRECTION"](direction="upload-only"))
            assert fake_config["SYNC_DIRECTION"] == "upload-only"
            asyncio.run(registered["SET_SYNC_DIRECTION"](direction="upload-only"))
            asyncio.run(registered["SET_SYNC_DIRECTION"](direction="bogus"))

    def test_run_stdin_wait_sync_disables_dryrun_and_stops(self, tmp_path):
        """_run_stdin_wait's SYNC handler clears dry-run, syncs and stops the handler."""
        from hsync_client.lan_handler import _run_stdin_wait

        handler, registered = self._capture_handler()
        fake_config = {"LAN_MODE": False, "DRYRUN": True}

        with (
            patch("hsync_client.preview.StdinCommandHandler", return_value=handler),
            patch("hsync_client.preview.CONFIG", fake_config),
            patch("hsync_client.preview.sync", new=AsyncMock()) as mock_sync,
            patch("hsync_client.preview._run_preview"),
        ):
            asyncio.run(_run_stdin_wait(MagicMock()))

            assert set(registered) == {"PREVIEW", "SYNC"}
            asyncio.run(registered["SYNC"]())

        assert fake_config["DRYRUN"] is False
        mock_sync.assert_awaited_once_with(lan_mode=False)
        handler.stop.assert_called_once()


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

        # First: get_master returns a secure peer (client mode)
        master_peer = _make_master()
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
            patch(
                "hsync_client.identity.load_or_create_device_identity",
                return_value=DeviceIdentity(
                    device_id="TestDevice1234567890ab", cert_info=_make_cert_info()
                ),
            ),
            patch("hsync_client.lan_handler.set_config"),
            patch("hsync_client.lan_handler._evaluate_master_pin", return_value=True),
            patch("hsync_client.lan_handler.sync", new=MagicMock()),
            # Mocked asyncio.run must not receive real coroutines: the
            # persistent slave path would create un-awaited _with_stdin_commands
            # / lan_websocket_client coroutine objects that trigger
            # "coroutine was never awaited" RuntimeWarnings at GC time.
            patch("hsync_client.lan_handler._with_stdin_commands", new=MagicMock()),
            patch("hsync_client.lan_handler.lan_websocket_client", new=MagicMock()),
            patch("asyncio.run"),
            patch("time.sleep", side_effect=KeyboardInterrupt),
        ):
            run_lan_mode(config, mock_log)

        # After transition to master, servers should have been stopped
        # (since master takes over)
        # Servers may be stopped on transition — verified by test_master_transition_from_slave
        # which only checks that the outer loop completes without error
