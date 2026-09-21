"""
Advanced tests for hsync_client.p2p_handler module.

Tests for:
- _run_as_master continuous mode
- _run_as_slave continuous/one-shot modes with WebSocket
- _with_stdin_commands handler
- _run_stdin_wait handler
- Master election scenarios
- Error handling and cleanup
"""

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from hsync_client.config import HsyncConfig
from hsync_client.constants import HEARTBEAT_INTERVAL
from hsync_client.identity import DeviceIdentity


def _make_p2p_config(tmp_path, **overrides: Any) -> HsyncConfig:
    """Return a minimal config dict suitable for p2p_handler functions."""
    defaults: dict[str, Any] = {
        "CID": "test-cid",
        "REMOTE_BUCKET": "test-bucket",
        "P2P_LISTEN_PORT": 9000,
        "P2P_GROUP": None,
        "P2P_FORCE_MASTER": False,
        "P2P_NEVER_MASTER": False,
        "SYNC_DIRECTION": "bidirectional",
        "LOCAL_BUCKET": str(tmp_path),
        "P2P_KEY": None,
        "DISCOVERY_TIMEOUT": 1,
        "DRYRUN": False,
        "ONE_SHOT": True,
        "REQUEST_HEADERS": {"X-Client-Id": "test-cid"},
        "API": {"buckets": "", "events": ""},
        "SERVER_COMPRESSION_ENABLED": False,
        "HTTP_TIMEOUT": 30,
        "MASTER_CERT_FINGERPRINT": None,
    }
    defaults.update(overrides)
    return cast(HsyncConfig, defaults)


def _make_cert_info(**overrides: Any) -> MagicMock:
    """Build a master cert-info mock (P2P transport is HTTPS-only)."""
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
        from hsync_client.p2p_handler import _run_as_master

        config = _make_p2p_config(tmp_path)
        mock_log = MagicMock()

        mock_p2p_sync = MagicMock()
        mock_p2p_sync.port = 9000

        mock_cert_info = MagicMock()
        mock_cert_info.cert_file = "/tmp/cert.pem"
        mock_cert_info.key_file = "/tmp/key.pem"
        mock_cert_info.fingerprint = "abc123"

        mock_p2p_server = MagicMock()
        mock_ws_server = MagicMock()

        # Track sleep calls and raise KeyboardInterrupt after first call
        sleep_count = [0]

        def raise_after_sleep(seconds):
            sleep_count[0] += 1
            if sleep_count[0] >= 1:
                raise KeyboardInterrupt()

        # Test _run_as_master when p2p_server and p2p_ws_server are None
        with (
            patch(
                "hsync_client.p2p_handler.P2PServer", return_value=mock_p2p_server
            ) as mock_server_class,
            patch(
                "hsync_client.p2p_handler.P2PWebSocketServer", return_value=mock_ws_server
            ) as mock_ws_class,
            patch("time.sleep", side_effect=raise_after_sleep),
        ):
            try:
                _run_as_master(
                    config=config,
                    log=mock_log,
                    p2p_sync=mock_p2p_sync,
                    cert_info=mock_cert_info,
                    p2p_server=None,
                    p2p_ws_server=None,
                )
            except KeyboardInterrupt:
                pass

        # Verify servers were created and started
        mock_server_class.assert_called_once()
        mock_ws_class.assert_called_once()
        mock_p2p_server.start.assert_called_once()
        mock_ws_server.start.assert_called_once_with(watch_path=str(tmp_path))
        mock_p2p_sync.update_server_ready.assert_called_once()

    def test_run_as_master_loops_until_keyboard_interrupt(self, tmp_path):
        """Master runs indefinitely until keyboard interrupt."""
        from hsync_client.p2p_handler import _run_as_master

        config = _make_p2p_config(tmp_path, ONE_SHOT=False)
        mock_log = MagicMock()

        mock_p2p_sync = MagicMock()
        mock_p2p_sync.port = 9000

        mock_cert_info = _make_cert_info()

        mock_p2p_server = MagicMock()
        mock_ws_server = MagicMock()

        # Track sleep calls
        sleep_calls = []

        def track_sleep(seconds):
            sleep_calls.append(seconds)
            if len(sleep_calls) > 2:
                raise KeyboardInterrupt()

        try:
            with (
                patch("hsync_client.p2p_handler.P2PServer", return_value=mock_p2p_server),
                patch("hsync_client.p2p_handler.P2PWebSocketServer", return_value=mock_ws_server),
                patch("time.sleep", side_effect=track_sleep),
            ):
                _run_as_master(
                    config=config,
                    log=mock_log,
                    p2p_sync=mock_p2p_sync,
                    cert_info=mock_cert_info,
                    p2p_server=None,
                    p2p_ws_server=None,
                )
        except KeyboardInterrupt:
            pass

        # Verify sleep was called (continuous mode runs loop)
        assert len(sleep_calls) > 0

    def test_run_as_master_with_auth_key(self, tmp_path):
        """Master logs authentication info when P2P_KEY is set."""
        from hsync_client.p2p_handler import _run_as_master

        config = _make_p2p_config(tmp_path, P2P_KEY="secret_key")
        mock_log = MagicMock()

        mock_p2p_sync = MagicMock()
        mock_p2p_sync.port = 9000

        mock_cert_info = _make_cert_info()
        mock_p2p_server = MagicMock()
        mock_ws_server = MagicMock()

        sleep_count = [0]

        def raise_after_sleep(seconds):
            sleep_count[0] += 1
            if sleep_count[0] >= 1:
                raise KeyboardInterrupt()

        try:
            with (
                patch("hsync_client.p2p_handler.P2PServer", return_value=mock_p2p_server),
                patch("hsync_client.p2p_handler.P2PWebSocketServer", return_value=mock_ws_server),
                patch("time.sleep", side_effect=raise_after_sleep),
            ):
                _run_as_master(
                    config=config,
                    log=mock_log,
                    p2p_sync=mock_p2p_sync,
                    cert_info=mock_cert_info,
                    p2p_server=None,
                    p2p_ws_server=None,
                )
        except KeyboardInterrupt:
            pass

        # Verify auth-related log messages
        log_calls = [str(c) for c in mock_log.info.call_args_list]
        assert any("Authentication enabled" in c or "HSYNC_P2P_KEY" in c for c in log_calls)

    def test_run_as_master_reuses_existing_servers(self, tmp_path):
        """Master reuses existing server instances instead of creating new ones."""
        from hsync_client.p2p_handler import _run_as_master

        config = _make_p2p_config(tmp_path)
        mock_log = MagicMock()

        mock_p2p_sync = MagicMock()
        mock_p2p_sync.port = 9000

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
                patch("hsync_client.p2p_handler.P2PServer", return_value=mock_server_class),
                patch("hsync_client.p2p_handler.P2PWebSocketServer", return_value=mock_ws_class),
                patch("time.sleep", side_effect=raise_after_sleep),
            ):
                _run_as_master(
                    config=config,
                    log=mock_log,
                    p2p_sync=mock_p2p_sync,
                    cert_info=None,
                    p2p_server=mock_existing_server,
                    p2p_ws_server=mock_existing_ws,
                )
        except KeyboardInterrupt:
            pass

        # Servers should NOT be created since they already exist
        mock_server_class.assert_not_called()
        mock_ws_class.assert_not_called()

    def test_run_as_master_returns_server_tuple(self, tmp_path):
        """_run_as_master returns the (p2p_server, p2p_ws_server) tuple.

        Regression test: previously the function returned None, so the caller
        lost the references and the next discovery-loop iteration tried to
        re-bind the same port, raising OSError(98) 'Address already in use'.
        """
        from hsync_client.p2p_handler import _run_as_master

        config = _make_p2p_config(tmp_path)
        mock_log = MagicMock()
        mock_p2p_sync = MagicMock()
        mock_p2p_sync.port = 9000

        mock_server_instance = MagicMock()
        mock_ws_instance = MagicMock()

        # In continuous mode the inner loop is `while True: sleep; break`,
        # so the function returns after a single sleep call.
        with (
            patch(
                "hsync_client.p2p_handler.P2PServer",
                return_value=mock_server_instance,
            ),
            patch(
                "hsync_client.p2p_handler.P2PWebSocketServer",
                return_value=mock_ws_instance,
            ),
            patch("time.sleep"),
        ):
            result = _run_as_master(
                config=config,
                log=mock_log,
                p2p_sync=mock_p2p_sync,
                cert_info=_make_cert_info(),
                p2p_server=None,
                p2p_ws_server=None,
            )

        assert result == (mock_server_instance, mock_ws_instance), (
            "_run_as_master must return the (p2p_server, p2p_ws_server) tuple "
            "so the outer discovery loop can persist references and avoid "
            "re-binding the same port on the next iteration"
        )

    def test_run_as_master_idempotent_across_iterations(self, tmp_path):
        """Two consecutive calls in continuous mode must not re-create servers.

        Simulates the discovery-loop behaviour: the tuple returned by the first
        call is fed back into the second call; no new server instance must be
        instantiated and therefore no second bind() on the same port.
        """
        from hsync_client.p2p_handler import _run_as_master

        config = _make_p2p_config(tmp_path)
        mock_log = MagicMock()
        mock_p2p_sync = MagicMock()
        mock_p2p_sync.port = 9000

        mock_server_instance = MagicMock()
        mock_ws_instance = MagicMock()
        server_factory = MagicMock(return_value=mock_server_instance)
        ws_factory = MagicMock(return_value=mock_ws_instance)

        with (
            patch("hsync_client.p2p_handler.P2PServer", server_factory),
            patch("hsync_client.p2p_handler.P2PWebSocketServer", ws_factory),
            patch("time.sleep"),
        ):
            first = _run_as_master(
                config=config,
                log=mock_log,
                p2p_sync=mock_p2p_sync,
                cert_info=_make_cert_info(),
                p2p_server=None,
                p2p_ws_server=None,
            )
            # Feed the returned tuple back, as the discovery loop now does.
            second = _run_as_master(
                config=config,
                log=mock_log,
                p2p_sync=mock_p2p_sync,
                cert_info=_make_cert_info(),
                p2p_server=first[0],
                p2p_ws_server=first[1],
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
        from hsync_client.p2p_handler import _run_as_master

        config = _make_p2p_config(tmp_path)
        mock_log = MagicMock()
        mock_p2p_sync = MagicMock()
        mock_p2p_sync.port = 9000

        with (
            patch("hsync_client.p2p_handler.P2PServer", return_value=MagicMock()),
            patch("hsync_client.p2p_handler.P2PWebSocketServer", return_value=MagicMock()),
            patch("time.sleep"),
        ):
            first = _run_as_master(
                config=config,
                log=mock_log,
                p2p_sync=mock_p2p_sync,
                cert_info=_make_cert_info(),
                p2p_server=None,
                p2p_ws_server=None,
            )
            _run_as_master(
                config=config,
                log=mock_log,
                p2p_sync=mock_p2p_sync,
                cert_info=_make_cert_info(),
                p2p_server=first[0],
                p2p_ws_server=first[1],
            )

        infos = [str(c.args[0]) for c in mock_log.info.call_args_list if c.args]
        role_msgs = [m for m in infos if "is the MASTER" in m]
        ctrlc_msgs = [m for m in infos if "Ctrl+C" in m]
        ready_msgs = [m for m in infos if "[READY]" in m]
        assert len(role_msgs) == 1, f"role message must log once, got {role_msgs!r}"
        assert len(ctrlc_msgs) == 1, f"Ctrl+C message must log once, got {ctrlc_msgs!r}"
        # The readiness threshold is one-shot too: it sits inside the
        # first-activation block and must never repeat across iterations.
        assert len(ready_msgs) == 1, f"[READY] must log once, got {ready_msgs!r}"


class TestRunAsMasterContinuousSemantics:
    """Regression tests for the consolidated master loop.

    The master role is intrinsically continuous: it must remain
    available to serve any peer that joins.  After the refactor that
    collapsed the duplicated continuous/one-shot branches in
    :func:`_run_as_master`, the function must:

    * Always sleep once and return (no infinite ``while True`` inside).
    * Be re-entered by the outer discovery loop while the role holds,
      without rebinding servers (verified by other tests above).
    """

    def test_master_returns_after_single_sleep(self, tmp_path):
        """Master must not block forever: it returns after one sleep."""
        from hsync_client.p2p_handler import _run_as_master

        config = _make_p2p_config(tmp_path, ONE_SHOT=True)
        mock_log = MagicMock()
        mock_p2p_sync = MagicMock()
        mock_p2p_sync.port = 9000

        sleep_calls: list[float] = []

        with (
            patch("hsync_client.p2p_handler.P2PServer", return_value=MagicMock()),
            patch("hsync_client.p2p_handler.P2PWebSocketServer", return_value=MagicMock()),
            patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        ):
            result = _run_as_master(
                config=config,
                log=mock_log,
                p2p_sync=mock_p2p_sync,
                cert_info=_make_cert_info(),
                p2p_server=None,
                p2p_ws_server=None,
            )

        # Refactored behaviour: exactly one sleep, then return.
        assert len(sleep_calls) == 1, (
            f"Master must sleep exactly once and return; got {len(sleep_calls)} sleeps"
        )
        assert result is not None
        assert result[0] is not None and result[1] is not None


class TestRunLanModeOuterLoopMasterPath:
    """Regression tests for the outer discovery loop on the master path.

    The master path must never exit the outer loop on its own: since the
    master is inherently continuous, the loop only exits via
    KeyboardInterrupt or by losing the master role.
    """

    def test_master_loop_does_not_exit_on_one_shot(self, tmp_path):
        """Outer loop on master path must keep iterating even with ONE_SHOT=True."""
        from hsync_client.p2p_handler import run_p2p_mode

        config = _make_p2p_config(tmp_path, ONE_SHOT=True)
        mock_log = MagicMock()

        mock_peer_mgr = MagicMock()
        # Stay master across iterations until KeyboardInterrupt is raised.
        mock_peer_mgr.is_master.return_value = True

        mock_p2p_sync = MagicMock()
        mock_p2p_sync.start.return_value = mock_peer_mgr
        mock_p2p_sync.port = 9000

        sleep_calls: list[float] = []

        def _sleep_then_interrupt_after_three(seconds: float) -> None:
            sleep_calls.append(seconds)
            if len(sleep_calls) >= 3:
                raise KeyboardInterrupt

        with (
            patch("hsync_client.p2p_discovery_mdns.MdnsDiscovery", return_value=mock_p2p_sync),
            patch("hsync_client.p2p_handler.P2PServer", return_value=MagicMock()),
            patch("hsync_client.p2p_handler.P2PWebSocketServer", return_value=MagicMock()),
            patch("time.sleep", side_effect=_sleep_then_interrupt_after_three),
        ):
            run_p2p_mode(config, mock_log)

        # If the outer loop had broken on the first one-shot master
        # iteration, only one sleep would have been recorded.  The fact
        # that we reached three sleeps proves the loop kept re-entering
        # _run_as_master.
        assert len(sleep_calls) >= 3, (
            "Outer loop must keep calling _run_as_master while the master role "
            "is held, regardless of ONE_SHOT"
        )


class TestRunLanModeNoMasterWarning:
    """Tests for the [NO MASTER] warning emitted when no master is elected.

    This happens, for example, when every discovered peer is configured
    with --never-master so the candidate list is empty.
    """

    def test_no_master_warning_after_timeout(self, tmp_path):
        """A parsable [NO MASTER] warning is logged once after the timeout."""
        from hsync_client import p2p_handler
        from hsync_client.p2p_handler import NO_MASTER_WARNING_TIMEOUT, run_p2p_mode

        config = _make_p2p_config(tmp_path, ONE_SHOT=False)
        mock_log = MagicMock()

        mock_peer_mgr = MagicMock()
        mock_peer_mgr.is_master.return_value = False
        mock_peer_mgr.get_master_cid.return_value = None

        mock_p2p_sync = MagicMock()
        mock_p2p_sync.start.return_value = mock_peer_mgr
        mock_p2p_sync.port = 9000

        # First slave iteration continues the loop; the second breaks it so
        # the test terminates deterministically.
        slave_calls: list[int] = []

        def _slave(**_kw: Any) -> tuple[bool, int]:
            slave_calls.append(1)
            return len(slave_calls) < 2, 0

        # time.time advances past the warning threshold between iterations.
        times = iter([100.0, 100.0 + NO_MASTER_WARNING_TIMEOUT + 1.0])

        with (
            patch("hsync_client.p2p_discovery_mdns.MdnsDiscovery", return_value=mock_p2p_sync),
            patch.object(p2p_handler, "_run_as_slave", side_effect=_slave),
            patch("time.time", side_effect=lambda: next(times)),
            # The startup NTP offset probe (ADR-0023) is orthogonal to the
            # [NO MASTER] warning timed by ``time.time``: stub the async
            # launcher so no probe thread is spawned (it would otherwise
            # consume the finite time iterator and touch the network).
            patch("hsync_client.timesync.start_ntp_clock_offset_probe", return_value=None),
        ):
            run_p2p_mode(config, mock_log)

        warnings = [str(c.args[0]) for c in mock_log.warning.call_args_list if c.args]
        no_master_warnings = [w for w in warnings if "[NO MASTER]" in w]
        assert len(no_master_warnings) == 1, (
            f"expected exactly one [NO MASTER] warning, got {warnings!r}"
        )


class TestRunLanModeDiscoveryTimeout:
    """Tests for discovery timeout sentinel semantics in the outer P2P loop."""

    def test_timeout_zero_uses_poll_interval_not_busy_spin(self, tmp_path):
        """DISCOVERY_TIMEOUT=0 keeps waiting but still sleeps in finite polling windows."""
        from hsync_client.p2p_handler import run_p2p_mode

        config = _make_p2p_config(tmp_path, DISCOVERY_TIMEOUT=0, ONE_SHOT=True)
        mock_log = MagicMock()

        mock_peer_mgr = MagicMock()
        mock_peer_mgr.is_master.return_value = False
        mock_peer_mgr.get_master_cid.return_value = None
        mock_peer_mgr.get_master.return_value = None

        mock_p2p_sync = MagicMock()
        mock_p2p_sync.start.return_value = mock_peer_mgr
        mock_p2p_sync.port = 9000
        mock_p2p_sync.wait_for_discovery.side_effect = [None, KeyboardInterrupt]

        with patch("hsync_client.p2p_discovery_mdns.MdnsDiscovery", return_value=mock_p2p_sync):
            run_p2p_mode(config, mock_log)

        first_call = mock_p2p_sync.wait_for_discovery.call_args_list[0]
        assert first_call.kwargs["timeout"] == HEARTBEAT_INTERVAL
        assert any("Discovery timeout disabled" in str(c) for c in mock_log.info.call_args_list)

    def test_one_shot_ignores_early_ineligible_peer_until_deadline(self, tmp_path):
        """A seeder wakeup must not end discovery before a master appears."""
        from hsync_client import p2p_handler

        config = _make_p2p_config(tmp_path, DISCOVERY_TIMEOUT=10, ONE_SHOT=True)
        mock_log = MagicMock()
        mock_peer_mgr = MagicMock()
        mock_peer_mgr.is_master.return_value = False
        mock_peer_mgr.get_master_cid.return_value = None
        mock_peer_mgr.get_master.return_value = None

        mock_p2p_sync = MagicMock()
        mock_p2p_sync.start.return_value = mock_peer_mgr
        mock_p2p_sync.port = 9000
        monotonic = iter([100.0, 100.0, 101.0, 111.0, 111.0])

        with (
            patch("hsync_client.p2p_discovery_mdns.MdnsDiscovery", return_value=mock_p2p_sync),
            patch.object(p2p_handler, "_run_as_slave", return_value=(False, 5)) as run_slave,
            patch("hsync_client.p2p_handler.time.monotonic", side_effect=lambda: next(monotonic)),
            patch("hsync_client.timesync.start_ntp_clock_offset_probe", return_value=None),
        ):
            assert p2p_handler.run_p2p_mode(config, mock_log) == 5

        assert mock_p2p_sync.wait_for_discovery.call_count == 2
        assert mock_p2p_sync.wait_for_discovery.call_args_list[0].kwargs["timeout"] == 10
        run_slave.assert_called_once()


def _make_master(**overrides: Any) -> MagicMock:
    """Build a master-peer mock with the attributes _run_as_slave reads.

    Defaults to a secure master (P2P transport is HTTPS-only); pass
    ``use_https=False``/``cert_fingerprint=None`` to model an insecure peer.
    """
    master = MagicMock()
    master.cid = "master-1"
    master.server_ready = True
    master.use_https = True
    master.cert_fingerprint = "a" * 64
    master.port = 9000
    master.address = "192.168.1.1"
    master.compression_enabled = False
    master.delta_enabled = False
    master.peer_download_enabled = True
    master.get_server_url.return_value = "https://192.168.1.1:9000"
    for key, value in overrides.items():
        setattr(master, key, value)
    return master


class TestRunAsSlave:
    """Direct tests for the _run_as_slave branches not exercised elsewhere."""

    def test_one_shot_uses_peer_requester_without_starting_publisher(self, tmp_path):
        """One-shot slaves can consume peers even though they never seed."""
        from hsync_client.p2p_handler import _run_as_slave

        config = _make_p2p_config(tmp_path, ONE_SHOT=True, P2P_PEER_DOWNLOAD=True)
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = _make_master()
        resolver = MagicMock()
        seen: dict[str, object] = {}

        async def fake_sync(**kwargs):
            seen.update(kwargs)
            return None

        with (
            patch("hsync_client.p2p_handler.set_config"),
            patch("hsync_client.p2p_handler._evaluate_master_pin", return_value=True),
            patch("hsync_client.p2p_handler._refresh_p2p_network_tuning"),
            patch(
                "hsync_client.p2p_handler.P2PSourceResolver",
                return_value=resolver,
            ) as resolver_cls,
            patch("hsync_client.p2p_handler.sync", new=fake_sync),
        ):
            result = _run_as_slave(
                config,
                MagicMock(),
                MagicMock(),
                None,
                True,
                peer_mgr,
            )

        assert result == (False, 0)
        assert seen == {"p2p_mode": True, "source_resolver": resolver}
        resolver_kwargs = resolver_cls.call_args.kwargs
        assert resolver_kwargs["enabled"] is True
        assert resolver_kwargs["master_capable"] is True
        assert resolver_kwargs["timeout"] == config["HTTP_TIMEOUT"]

    def test_https_sets_fingerprint_and_bearer(self, tmp_path, monkeypatch):
        """HTTPS master with P2P_KEY stores fingerprint and sends a Bearer token."""
        from hsync_client.p2p_handler import _run_as_slave

        monkeypatch.setenv("HSYNC_P2P_PIN_FILE", str(tmp_path / "pins.json"))
        config = _make_p2p_config(tmp_path, P2P_KEY="secret")
        master = _make_master(
            use_https=True,
            cert_fingerprint="f" * 64,
            compression_enabled=True,
            get_server_url=MagicMock(return_value="https://192.168.1.1:9000"),
        )
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = master

        with (
            patch("hsync_client.p2p_handler.set_config"),
            patch("hsync_client.p2p_handler.sync", new=MagicMock()),
            patch("asyncio.run"),
        ):
            result = _run_as_slave(config, MagicMock(), MagicMock(), None, True, peer_mgr)

        assert result == (False, 0)
        assert config["MASTER_CERT_FINGERPRINT"] == "f" * 64
        assert config["REQUEST_HEADERS"]["Authorization"] == "Bearer secret"
        assert config["SERVER_COMPRESSION_ENABLED"] is True
        assert config["API"]["events"]

    def test_first_https_sighting_pins_fingerprint(self, tmp_path, monkeypatch):
        """First time a master's HTTPS cert is seen it is pinned (TOFU)."""
        from hsync_client.p2p_handler import _run_as_slave
        from hsync_client.pinning import PinStore

        pin_file = tmp_path / "pins.json"
        monkeypatch.setenv("HSYNC_P2P_PIN_FILE", str(pin_file))
        config = _make_p2p_config(tmp_path, P2P_KEY="secret")
        master = _make_master(
            use_https=True,
            cert_fingerprint="a" * 64,
            get_server_url=MagicMock(return_value="https://192.168.1.1:9000"),
        )
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = master

        with (
            patch("hsync_client.p2p_handler.set_config"),
            patch("hsync_client.p2p_handler.sync", new=MagicMock()),
            patch("asyncio.run"),
        ):
            _run_as_slave(config, MagicMock(), MagicMock(), None, True, peer_mgr)

        assert PinStore(pin_file).get("master-1", "test-bucket") == "a" * 64

    def test_non_sha256_master_fingerprint_is_not_persisted(self, tmp_path, monkeypatch):
        """Malformed discovery fingerprint material fails closed before TOFU."""
        from hsync_client.p2p_handler import _evaluate_master_pin

        pin_file = tmp_path / "pins.json"
        monkeypatch.setenv("HSYNC_P2P_PIN_FILE", str(pin_file))
        master = _make_master(cert_fingerprint="a" * 40)
        log = MagicMock()

        assert (
            _evaluate_master_pin(
                {"REMOTE_BUCKET": "test-bucket", "P2P_REQUIRE_PAIRING": False},  # type: ignore[arg-type]
                master,
                log,
            )
            is False
        )

        assert not pin_file.exists()
        assert any("non-SHA-256" in str(call) for call in log.error.call_args_list)

    def test_changed_fingerprint_is_refused(self, tmp_path, monkeypatch):
        """A master whose cert no longer matches the pin is refused (no connect)."""
        from hsync_client.p2p_handler import _run_as_slave
        from hsync_client.pinning import PinStore

        pin_file = tmp_path / "pins.json"
        monkeypatch.setenv("HSYNC_P2P_PIN_FILE", str(pin_file))
        PinStore(pin_file).set("master-1", "test-bucket", "a" * 64)

        config = _make_p2p_config(tmp_path, P2P_KEY="secret")
        master = _make_master(
            use_https=True,
            cert_fingerprint="b" * 64,
            get_server_url=MagicMock(return_value="https://192.168.1.1:9000"),
        )
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = master
        mock_log = MagicMock()
        mock_sync = MagicMock()

        with (
            patch("hsync_client.p2p_handler.set_config"),
            patch("hsync_client.p2p_handler.sync", new=mock_sync),
            patch("asyncio.run") as mock_run,
        ):
            result = _run_as_slave(config, mock_log, MagicMock(), None, True, peer_mgr)

        assert result == (False, 0)
        assert config["MASTER_CERT_FINGERPRINT"] is None
        mock_run.assert_not_called()
        assert any("FINGERPRINT CHANGED" in str(c) for c in mock_log.error.call_args_list)

    def test_strict_pairing_refuses_unpinned_first_sighting(self, tmp_path, monkeypatch):
        """With strict pairing a first-seen cert is refused and never pinned."""
        from hsync_client.p2p_handler import _run_as_slave
        from hsync_client.pinning import PinStore

        pin_file = tmp_path / "pins.json"
        monkeypatch.setenv("HSYNC_P2P_PIN_FILE", str(pin_file))

        config = _make_p2p_config(tmp_path, P2P_KEY="secret", P2P_REQUIRE_PAIRING=True)
        master = _make_master(
            use_https=True,
            cert_fingerprint="a" * 64,
            get_server_url=MagicMock(return_value="https://192.168.1.1:9000"),
        )
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = master
        mock_log = MagicMock()

        with (
            patch("hsync_client.p2p_handler.set_config"),
            patch("hsync_client.p2p_handler.sync", new=MagicMock()),
            patch("asyncio.run") as mock_run,
        ):
            result = _run_as_slave(config, mock_log, MagicMock(), None, True, peer_mgr)

        assert result == (False, 0)
        assert config["MASTER_CERT_FINGERPRINT"] is None
        mock_run.assert_not_called()
        assert PinStore(pin_file).get("master-1", "test-bucket") is None
        assert any("PAIRING NEEDED" in str(c) for c in mock_log.error.call_args_list)

    def test_insecure_master_is_refused(self, tmp_path):
        """A plain-HTTP master is refused (secure-only P2P); the key is never sent."""
        from hsync_client.p2p_handler import _run_as_slave

        config = _make_p2p_config(tmp_path, P2P_KEY="secret")
        master = _make_master(use_https=False, cert_fingerprint=None)
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = master
        mock_log = MagicMock()

        with (
            patch("hsync_client.p2p_handler.set_config"),
            patch("hsync_client.p2p_handler.sync", new=MagicMock()),
            patch("asyncio.run") as mock_run,
        ):
            result = _run_as_slave(config, mock_log, MagicMock(), None, True, peer_mgr)

        assert result == (False, 0)
        assert "Authorization" not in config["REQUEST_HEADERS"]
        mock_run.assert_not_called()
        assert any("INSECURE MASTER" in str(c) for c in mock_log.warning.call_args_list)

    def test_no_master_one_shot_wait_indefinitely_keeps_looping(self, tmp_path):
        """One-shot mode can still wait indefinitely for the first peer."""
        from hsync_client.p2p_handler import _run_as_slave

        config = _make_p2p_config(tmp_path, ONE_SHOT=True)
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = None
        mock_log = MagicMock()

        result = _run_as_slave(
            config,
            mock_log,
            MagicMock(),
            None,
            True,
            peer_mgr,
            wait_indefinitely_for_peer=True,
        )

        assert result[0] is True
        assert any("Waiting for peers" in str(c) for c in mock_log.info.call_args_list)

    def test_continuous_connects_websocket(self, tmp_path):
        """Continuous slave connects via WebSocket and returns True to loop again."""
        from hsync_client.p2p_handler import _run_as_slave

        config = _make_p2p_config(tmp_path, ONE_SHOT=False)
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = _make_master()

        with (
            patch("hsync_client.p2p_handler.set_config"),
            patch("hsync_client.p2p_handler._evaluate_master_pin", return_value=True),
            patch("hsync_client.p2p_handler.sync", new=MagicMock()),
            patch("hsync_client.p2p_handler.p2p_websocket_client", new=MagicMock()),
            patch("hsync_client.p2p_handler._with_stdin_commands", new=MagicMock()) as mock_wsc,
            patch("asyncio.run"),
        ):
            result = _run_as_slave(config, MagicMock(), MagicMock(), None, False, peer_mgr)

        assert result[0] is True
        mock_wsc.assert_called_once()

    def test_continuous_websocket_error_is_swallowed(self, tmp_path):
        """A WebSocket failure is logged but still re-enters the discovery loop."""
        from hsync_client.p2p_handler import _run_as_slave

        config = _make_p2p_config(tmp_path, ONE_SHOT=False)
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = _make_master()
        mock_log = MagicMock()

        # First run() is the initial sync (ok); second is the WebSocket (boom).
        with (
            patch("hsync_client.p2p_handler.set_config"),
            patch("hsync_client.p2p_handler._evaluate_master_pin", return_value=True),
            patch("hsync_client.p2p_handler.sync", new=MagicMock()),
            patch("hsync_client.p2p_handler.p2p_websocket_client", new=MagicMock()),
            patch("hsync_client.p2p_handler._with_stdin_commands", new=MagicMock()),
            patch("asyncio.run", side_effect=[None, RuntimeError("boom")]),
        ):
            result = _run_as_slave(config, mock_log, MagicMock(), None, False, peer_mgr)

        assert result[0] is True
        assert any("WebSocket connection ended" in str(c) for c in mock_log.warning.call_args_list)

    def test_continuous_websocket_drop_reconciles_stale_peers(self, tmp_path):
        """A WebSocket drop triggers a stale-peer reconcile before re-entering.

        When the connection to the master ends, the slave must immediately
        expire a master whose heartbeat already stopped so the surviving peers
        elect the successor instead of waiting for the next discovery wake. A
        master whose record is still fresh is left untouched (no split-brain).
        """
        from hsync_client.p2p_handler import _run_as_slave

        config = _make_p2p_config(tmp_path, ONE_SHOT=False)
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = _make_master()

        with (
            patch("hsync_client.p2p_handler.set_config"),
            patch("hsync_client.p2p_handler._evaluate_master_pin", return_value=True),
            patch("hsync_client.p2p_handler.sync", new=MagicMock()),
            patch("hsync_client.p2p_handler.p2p_websocket_client", new=MagicMock()),
            patch("hsync_client.p2p_handler._with_stdin_commands", new=MagicMock()),
            patch("asyncio.run", side_effect=[None, RuntimeError("boom")]),
        ):
            result = _run_as_slave(config, MagicMock(), MagicMock(), None, False, peer_mgr)

        assert result[0] is True
        peer_mgr.reconcile_stale_peers.assert_called_once()

    def test_continuous_sync_exception_reenters_loop(self, tmp_path):
        """A transport failure in continuous mode must not terminate the run.

        When the master goes offline mid-sync, ``sync()`` raises a transport
        error; a continuous run must re-enter the discovery loop (return True)
        so the peer stays alive and re-detects the master (or a newly elected
        one) on the next iteration instead of exiting with "P2P mode stopped".
        """
        from hsync_client.exceptions import NetworkError
        from hsync_client.p2p_handler import _run_as_slave

        config = _make_p2p_config(tmp_path, ONE_SHOT=False)
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = _make_master()

        with (
            patch("hsync_client.p2p_handler.set_config"),
            patch("hsync_client.p2p_handler._evaluate_master_pin", return_value=True),
            patch("hsync_client.p2p_handler.sync", new=MagicMock()),
            patch("asyncio.run", side_effect=NetworkError("boom")),
        ):
            result = _run_as_slave(config, MagicMock(), MagicMock(), None, False, peer_mgr)

        assert result == (True, 0)

    def test_one_shot_sync_exception_is_fatal(self, tmp_path):
        """A transport failure in one-shot mode still maps to a fatal exit."""
        from hsync_client.exceptions import NetworkError
        from hsync_client.p2p_handler import _run_as_slave

        config = _make_p2p_config(tmp_path, ONE_SHOT=True)
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = _make_master()

        with (
            patch("hsync_client.p2p_handler.set_config"),
            patch("hsync_client.p2p_handler._evaluate_master_pin", return_value=True),
            patch("hsync_client.p2p_handler.sync", new=MagicMock()),
            patch("asyncio.run", side_effect=NetworkError("boom")),
        ):
            result = _run_as_slave(config, MagicMock(), MagicMock(), None, True, peer_mgr)

        assert result == (False, 1)

    def test_continuous_unexpected_exception_is_fatal(self, tmp_path):
        """An unexpected (non-transport) error stays fatal even in continuous mode.

        A genuine programming bug must surface as a fatal exit rather than be
        masked into an endless retry loop.
        """
        from hsync_client.p2p_handler import _run_as_slave

        config = _make_p2p_config(tmp_path, ONE_SHOT=False)
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = _make_master()

        with (
            patch("hsync_client.p2p_handler.set_config"),
            patch("hsync_client.p2p_handler._evaluate_master_pin", return_value=True),
            patch("hsync_client.p2p_handler.sync", new=MagicMock()),
            patch("asyncio.run", side_effect=RuntimeError("boom")),
        ):
            result = _run_as_slave(config, MagicMock(), MagicMock(), None, False, peer_mgr)

        assert result == (False, 1)

    def test_dryrun_continuous_runs_preview_and_stdin_wait(self, tmp_path):
        """Dry-run continuous slave shows a preview then waits on stdin."""
        from hsync_client.p2p_handler import _run_as_slave

        config = _make_p2p_config(tmp_path, ONE_SHOT=False, DRYRUN=True)
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = _make_master()

        with (
            patch("hsync_client.p2p_handler.set_config"),
            patch("hsync_client.p2p_handler._evaluate_master_pin", return_value=True),
            patch("hsync_client.p2p_handler._run_preview") as mock_preview,
            patch("hsync_client.p2p_handler._run_stdin_wait", new=MagicMock()),
            patch("hsync_client.p2p_handler.p2p_websocket_client", new=MagicMock()),
            patch("hsync_client.p2p_handler._with_stdin_commands", new=MagicMock()),
            patch("asyncio.run"),
        ):
            result = _run_as_slave(config, MagicMock(), MagicMock(), None, False, peer_mgr)

        assert result[0] is True
        mock_preview.assert_called_once()

    def test_no_master_continuous_waits(self, tmp_path):
        """No master and continuous: keep looping, logging a wait message."""
        from hsync_client.p2p_handler import _run_as_slave

        config = _make_p2p_config(tmp_path, ONE_SHOT=False)
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = None
        mock_log = MagicMock()

        result = _run_as_slave(config, mock_log, MagicMock(), None, False, peer_mgr)

        assert result[0] is True
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
        from hsync_client.p2p_handler import _with_stdin_commands

        handler, registered = self._capture_handler()
        fake_config = {
            "P2P_MODE": True,
            "P2P_KEY": "shared-key",
            "SYNC_DIRECTION": "bidirectional",
        }

        async def _coro() -> None:
            return None

        with (
            patch("hsync_client.preview.StdinCommandHandler", return_value=handler),
            patch("hsync_client.preview.CONFIG", fake_config),
            patch("hsync_client.preview.sync", new=AsyncMock()) as mock_sync,
            patch("hsync_client.preview._run_preview") as mock_preview,
        ):
            asyncio.run(_with_stdin_commands(MagicMock(), _coro()))

            assert set(registered) == {
                "PREVIEW",
                "PEERS",
                "SYNC",
                "SET_SYNC_DIRECTION",
                "SET_BANDWIDTH",
            }

            # PREVIEW delegates to _run_preview.
            registered["PREVIEW"]()
            mock_preview.assert_called_once()

            # SYNC triggers a sync using the current P2P_MODE.
            asyncio.run(registered["SYNC"]())
            mock_sync.assert_awaited_with(
                p2p_mode=True, inventory_observer=None, source_resolver=None
            )

            # Valid direction change resyncs; invalid and no-op paths short-circuit.
            asyncio.run(registered["SET_SYNC_DIRECTION"](direction="upload-only"))
            assert fake_config["SYNC_DIRECTION"] == "upload-only"
            asyncio.run(registered["SET_SYNC_DIRECTION"](direction="upload-only"))
            asyncio.run(registered["SET_SYNC_DIRECTION"](direction="bogus"))

    def test_run_stdin_wait_sync_disables_dryrun_and_stops(self, tmp_path):
        """_run_stdin_wait's SYNC handler clears dry-run, syncs and stops the handler."""
        from hsync_client.p2p_handler import _run_stdin_wait

        handler, registered = self._capture_handler()
        fake_config = {"P2P_MODE": False, "DRYRUN": True}

        with (
            patch("hsync_client.preview.StdinCommandHandler", return_value=handler),
            patch("hsync_client.preview.CONFIG", fake_config),
            patch("hsync_client.preview.sync", new=AsyncMock()) as mock_sync,
            patch("hsync_client.preview._run_preview"),
        ):
            asyncio.run(_run_stdin_wait(MagicMock()))

            assert set(registered) == {"PREVIEW", "PEERS", "SYNC"}
            asyncio.run(registered["SYNC"]())

        assert fake_config["DRYRUN"] is False
        mock_sync.assert_awaited_once_with(
            p2p_mode=False, inventory_observer=None, source_resolver=None
        )
        handler.stop.assert_called_once()

    def test_runtime_direction_refuses_unauthenticated_p2p_write(self):
        """A stdin direction change cannot bypass the startup P2P-key policy."""
        from hsync_client.p2p_handler import _with_stdin_commands

        handler, registered = self._capture_handler()
        fake_config = {
            "P2P_MODE": True,
            "P2P_KEY": None,
            "P2P_ALLOW_UNAUTHENTICATED_WRITES": False,
            "SYNC_DIRECTION": "download-only",
        }

        async def _coro() -> None:
            return None

        with (
            patch("hsync_client.preview.StdinCommandHandler", return_value=handler),
            patch("hsync_client.preview.CONFIG", fake_config),
            patch("hsync_client.preview.sync", new=AsyncMock()) as mock_sync,
            patch("hsync_client.preview.emit_event") as mock_emit,
        ):
            asyncio.run(_with_stdin_commands(MagicMock(), _coro()))
            asyncio.run(registered["SET_SYNC_DIRECTION"](direction="bidirectional"))

        assert fake_config["SYNC_DIRECTION"] == "download-only"
        mock_sync.assert_not_awaited()
        assert any(str(call.args[0]) == "auth_required" for call in mock_emit.call_args_list)

    def test_dryrun_sync_refuses_unauthenticated_p2p_write(self):
        """SYNC keeps dry-run active when the selected P2P direction would write."""
        from hsync_client.p2p_handler import _run_stdin_wait

        handler, registered = self._capture_handler()
        fake_config = {
            "P2P_MODE": True,
            "P2P_KEY": None,
            "P2P_ALLOW_UNAUTHENTICATED_WRITES": False,
            "SYNC_DIRECTION": "bidirectional",
            "DRYRUN": True,
        }
        with (
            patch("hsync_client.preview.StdinCommandHandler", return_value=handler),
            patch("hsync_client.preview.CONFIG", fake_config),
            patch("hsync_client.preview.sync", new=AsyncMock()) as mock_sync,
        ):
            asyncio.run(_run_stdin_wait(MagicMock()))
            asyncio.run(registered["SYNC"]())

        assert fake_config["DRYRUN"] is True
        mock_sync.assert_not_awaited()
        handler.stop.assert_not_called()


class TestSlaveSeederLifecycle:
    """The read-only seeder is owned strictly by a continuous slave role."""

    def test_continuous_slave_starts_seeder_then_publishes_endpoint_ready(self, tmp_path):
        from hsync_client.p2p_handler import _start_slave_seeder
        from hsync_client.p2p_server import P2PServerMode

        config = _make_p2p_config(
            tmp_path,
            ONE_SHOT=False,
            P2P_PEER_DOWNLOAD=True,
            P2P_PEER_READ_CAP=3,
        )
        p2p_sync = MagicMock(port=9443)
        server = MagicMock()
        publisher = MagicMock()
        with (
            patch("hsync_client.p2p_handler.P2PServer", return_value=server) as server_cls,
            patch(
                "hsync_client.p2p_handler.P2PInventoryPublisher",
                return_value=publisher,
            ) as publisher_cls,
            patch("hsync_client.p2p_handler.set_active_inventory_publisher") as set_active,
        ):
            result = _start_slave_seeder(config, p2p_sync, _make_cert_info(), MagicMock())

        assert result == (server, publisher)
        server_cls.assert_called_once_with(
            port=9443,
            use_https=True,
            cert_file="/tmp/cert.pem",
            key_file="/tmp/key.pem",
            mode=P2PServerMode.seeder,
            bucket="test-bucket",
            peer_read_cap=3,
        )
        server.start.assert_called_once_with()
        p2p_sync.update_endpoint_ready.assert_called_once_with(True)
        publisher_cls.assert_called_once_with(str(tmp_path))
        set_active.assert_called_once_with(publisher)

    def test_one_shot_and_dry_run_never_start_seeder(self, tmp_path):
        from hsync_client.p2p_handler import _start_slave_seeder

        for overrides in ({"ONE_SHOT": True}, {"ONE_SHOT": False, "DRYRUN": True}):
            config = _make_p2p_config(tmp_path, **overrides)
            with patch("hsync_client.p2p_handler.P2PServer") as server_cls:
                assert _start_slave_seeder(
                    config,
                    MagicMock(port=9443),
                    _make_cert_info(),
                    MagicMock(),
                ) == (None, None)
            server_cls.assert_not_called()

    def test_bind_failure_degrades_to_requester_only(self, tmp_path):
        from hsync_client.p2p_handler import _start_slave_seeder

        config = _make_p2p_config(tmp_path, ONE_SHOT=False)
        p2p_sync = MagicMock(port=9443)
        server = MagicMock()
        server.start.side_effect = OSError("address in use")
        with patch("hsync_client.p2p_handler.P2PServer", return_value=server):
            assert _start_slave_seeder(
                config,
                p2p_sync,
                _make_cert_info(),
                MagicMock(),
            ) == (None, None)

        server.stop.assert_called_once_with()
        p2p_sync.update_endpoint_ready.assert_not_called()

    def test_readiness_failure_stops_and_clears_endpoint_material(self, tmp_path):
        """A partial publication is explicitly withdrawn before fallback."""
        from hsync_client.p2p_handler import _start_slave_seeder

        config = _make_p2p_config(tmp_path, ONE_SHOT=False)
        p2p_sync = MagicMock(port=9443)
        p2p_sync.update_endpoint_ready.side_effect = [RuntimeError("mdns failed"), None]
        server = MagicMock()
        with patch("hsync_client.p2p_handler.P2PServer", return_value=server):
            assert _start_slave_seeder(
                config,
                p2p_sync,
                _make_cert_info(),
                MagicMock(),
            ) == (None, None)

        server.stop.assert_called_once_with()
        assert [call.args[0] for call in p2p_sync.update_endpoint_ready.call_args_list] == [
            True,
            False,
        ]

    def test_stop_withdraws_txt_and_stops_even_if_withdraw_fails(self):
        from hsync_client.p2p_handler import _stop_slave_seeder

        p2p_sync = MagicMock()
        server = MagicMock()
        publisher = MagicMock()
        order: list[str] = []
        server.stop.side_effect = lambda: order.append("stop")

        def fail_withdraw(_ready: bool) -> None:
            order.append("withdraw")
            raise RuntimeError("mdns unavailable")

        p2p_sync.update_endpoint_ready.side_effect = fail_withdraw

        with patch("hsync_client.p2p_handler.set_active_inventory_publisher") as set_active:
            _stop_slave_seeder(server, p2p_sync, publisher)

        p2p_sync.update_endpoint_ready.assert_called_once_with(False)
        server.stop.assert_called_once_with()
        set_active.assert_called_once_with(None)
        assert order == ["stop", "withdraw"]


class TestMasterElection:
    """Tests for master election and peer management."""

    def test_master_transition_from_slave(self, tmp_path):
        """Test scenario where client transitions from client to master."""
        from hsync_client.p2p_handler import run_p2p_mode

        config = _make_p2p_config(tmp_path, ONE_SHOT=False)
        mock_log = MagicMock()

        # First iteration: is_master = False (client)
        # Second iteration: is_master = True (master)
        mock_peer_mgr = MagicMock()
        mock_peer_mgr.is_master.side_effect = [False, True]

        # First: get_master returns a secure peer (client mode)
        master_peer = _make_master()
        mock_peer_mgr.get_master.return_value = master_peer

        mock_p2p_sync = MagicMock()
        mock_p2p_sync.start.return_value = mock_peer_mgr
        mock_p2p_sync.port = 9000

        mock_p2p_server = MagicMock()
        mock_ws_server = MagicMock()

        with (
            patch("hsync_client.p2p_discovery_mdns.MdnsDiscovery", return_value=mock_p2p_sync),
            patch("hsync_client.p2p_handler.P2PServer", return_value=mock_p2p_server),
            patch("hsync_client.p2p_handler.P2PWebSocketServer", return_value=mock_ws_server),
            patch(
                "hsync_client.identity.load_or_create_device_identity",
                return_value=DeviceIdentity(
                    device_id="TestDevice1234567890ab", cert_info=_make_cert_info()
                ),
            ),
            patch("hsync_client.p2p_handler.set_config"),
            patch("hsync_client.p2p_handler._evaluate_master_pin", return_value=True),
            patch("hsync_client.p2p_handler.sync", new=MagicMock()),
            # Mocked asyncio.run must not receive real coroutines: the
            # continuous slave path would create un-awaited _with_stdin_commands
            # / p2p_websocket_client coroutine objects that trigger
            # "coroutine was never awaited" RuntimeWarnings at GC time.
            patch("hsync_client.p2p_handler._with_stdin_commands", new=MagicMock()),
            patch("hsync_client.p2p_handler.p2p_websocket_client", new=MagicMock()),
            patch("asyncio.run"),
            patch("time.sleep", side_effect=KeyboardInterrupt),
        ):
            run_p2p_mode(config, mock_log)

        # After transition to master, servers should have been stopped
        # (since master takes over)
        # Servers may be stopped on transition — verified by test_master_transition_from_slave
        # which only checks that the outer loop completes without error
