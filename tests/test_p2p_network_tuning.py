"""
Tests for P2P post-rendezvous network tuning refresh.

Covers:
- ``p2p_handler._refresh_p2p_network_tuning`` — RTT re-derivation after master
  rendezvous, CLI/env precedence via ``AUTOTUNE_MANAGED_KEYS``, dual-write to
  the global ``CONFIG`` and the local config dict.
- ``_run_as_slave`` ordering: pin check -> tuning refresh -> clock offset.
- ``run_sync`` P2P-placeholder behaviour: startup RTT probe skipped and no
  placeholder ``server`` UI event.
"""

from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from hsync_client.config import CONFIG, HsyncConfig
from hsync_client.events import UiEventName
from hsync_client.main_logic import run_sync
from hsync_client.p2p_peers import Peer
from tests.test_main_logic_sync import _default_run_sync_kwargs

_ALL_TUNING_KEYS = [
    "HTTP_TIMEOUT",
    "RECONNECT_DELAY",
    "TRANSFER_RETRY_MIN_WAIT",
    "TRANSFER_RETRY_MAX_WAIT",
    "MAX_CONCURRENT_TRANSFERS",
]


@pytest.fixture(autouse=True)
def _preserve_config():
    """Restore the global CONFIG after each test (refresh mutates it)."""
    snapshot = dict(CONFIG)
    try:
        yield
    finally:
        CONFIG.clear()
        CONFIG.update(snapshot)


def _make_config(tmp_path, **overrides: Any) -> HsyncConfig:
    """Return a config dict suitable for slave-path and tuning-refresh tests."""
    defaults: dict[str, Any] = {
        "CID": "test-cid",
        "REMOTE_BUCKET": "test-bucket",
        "P2P_LISTEN_PORT": 9000,
        "P2P_GROUP": None,
        "P2P_PREFER_MASTER": False,
        "P2P_NEVER_MASTER": False,
        "SYNC_DIRECTION": "bidirectional",
        "LOCAL_BUCKET": str(tmp_path),
        "P2P_KEY": None,
        "PEER_RENDEZVOUS_TIMEOUT": 1,
        "DRYRUN": False,
        "ONE_SHOT": True,
        "REQUEST_HEADERS": {"X-Client-Id": "test-cid"},
        "API": {"buckets": "", "events": ""},
        "SERVER_COMPRESSION_ENABLED": False,
        "HTTP_TIMEOUT": 30,
        "RECONNECT_DELAY": 5,
        "TRANSFER_RETRY_MIN_WAIT": 5.0,
        "TRANSFER_RETRY_MAX_WAIT": 10.0,
        "MASTER_CERT_FINGERPRINT": None,
        "AUTOTUNE_ENABLED": True,
        "AUTOTUNE_MANAGED_KEYS": list(_ALL_TUNING_KEYS),
    }
    defaults.update(overrides)
    return cast(HsyncConfig, defaults)


def _make_master(**overrides: Any) -> MagicMock:
    """Build a secure master-peer mock (HTTPS, ready, with fingerprint)."""
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


class TestRefreshLanNetworkTuning:
    """Unit tests for p2p_handler._refresh_p2p_network_tuning."""

    def test_derives_values_and_dual_writes_both_dicts(self, tmp_path):
        """RTT 80ms (BROADBAND) re-derives timeout/retry/reconnect on CONFIG and config."""
        from hsync_client.p2p_handler import _refresh_p2p_network_tuning

        config = _make_config(tmp_path)
        CONFIG.clear()
        CONFIG.update(config)
        log = MagicMock()

        with patch("hsync_client.autotune.measure_rtt", return_value=80.0):
            _refresh_p2p_network_tuning(_make_master(), config, log)

        # factor = 1 + 80/100 = 1.8 -> min_wait 9, max_wait 18, reconnect 9;
        # RTT > 50 -> timeout 45 (see autotune.derive_timeout/derive_retry).
        for cfg in (config, CONFIG):
            assert cfg["HTTP_TIMEOUT"] == 45
            assert cfg["TRANSFER_RETRY_MIN_WAIT"] == 9.0
            assert cfg["TRANSFER_RETRY_MAX_WAIT"] == 18.0
            assert cfg["RECONNECT_DELAY"] == 9
        assert isinstance(config["TRANSFER_RETRY_MIN_WAIT"], float)
        assert isinstance(config["TRANSFER_RETRY_MAX_WAIT"], float)
        assert any("autotune (P2P)" in str(c) for c in log.info.call_args_list)

    def test_only_managed_keys_are_updated(self, tmp_path):
        """Keys excluded from AUTOTUNE_MANAGED_KEYS (CLI/env overrides) are kept."""
        from hsync_client.p2p_handler import _refresh_p2p_network_tuning

        config = _make_config(tmp_path, AUTOTUNE_MANAGED_KEYS=["HTTP_TIMEOUT"])
        CONFIG.clear()
        CONFIG.update(config)

        with patch("hsync_client.autotune.measure_rtt", return_value=80.0):
            _refresh_p2p_network_tuning(_make_master(), config, MagicMock())

        assert config["HTTP_TIMEOUT"] == 45
        assert CONFIG["HTTP_TIMEOUT"] == 45
        # Explicitly overridden keys keep their startup values.
        for cfg in (config, CONFIG):
            assert cfg["RECONNECT_DELAY"] == 5
            assert cfg["TRANSFER_RETRY_MIN_WAIT"] == 5.0
            assert cfg["TRANSFER_RETRY_MAX_WAIT"] == 10.0

    def test_unmeasurable_rtt_keeps_startup_values(self, tmp_path):
        """measure_rtt -> None: no changes, no exception, debug log."""
        from hsync_client.p2p_handler import _refresh_p2p_network_tuning

        config = _make_config(tmp_path)
        CONFIG.clear()
        CONFIG.update(config)
        log = MagicMock()

        with patch("hsync_client.autotune.measure_rtt", return_value=None):
            _refresh_p2p_network_tuning(_make_master(), config, log)

        for cfg in (config, CONFIG):
            assert cfg["HTTP_TIMEOUT"] == 30
            assert cfg["RECONNECT_DELAY"] == 5
        assert log.debug.call_count >= 1

    def test_autotune_disabled_is_noop(self, tmp_path):
        """--no-autotune (AUTOTUNE_ENABLED False): RTT is never measured."""
        from hsync_client.p2p_handler import _refresh_p2p_network_tuning

        config = _make_config(tmp_path, AUTOTUNE_ENABLED=False)

        with patch("hsync_client.autotune.measure_rtt") as mock_rtt:
            _refresh_p2p_network_tuning(_make_master(), config, MagicMock())

        mock_rtt.assert_not_called()
        assert config["HTTP_TIMEOUT"] == 30

    def test_missing_autotune_keys_is_noop(self, tmp_path):
        """Regression: legacy configs without AUTOTUNE_* keys must not break.

        Existing p2p_handler tests build configs without the autotune runtime
        keys; the refresh must no-op (via ``.get``) instead of raising.
        """
        from hsync_client.p2p_handler import _refresh_p2p_network_tuning

        config = cast(HsyncConfig, {"HTTP_TIMEOUT": 30})

        with patch("hsync_client.autotune.measure_rtt") as mock_rtt:
            _refresh_p2p_network_tuning(_make_master(), config, MagicMock())

        mock_rtt.assert_not_called()
        assert config["HTTP_TIMEOUT"] == 30

    def test_autotune_log_does_not_raise_loguru_keyerror(self, tmp_path):
        """Regression: dict braces in the autotune log must not crash loguru.

        Before the fix, interpolating ``{updates}`` via f-string produced a
        message containing ``{'HTTP_TIMEOUT': 30, ...}``; loguru then parsed
        the ``{...}`` as a format placeholder and raised
        ``KeyError: "'HTTP_TIMEOUT'"`` during ``Handler.emit``.

        This uses the project's real callable formatter
        (``_format_record_plain``) with ``catch=False`` so the exception
        propagates instead of being silently captured by loguru's default
        error handler. The test fails if the autotune summary message cannot
        be formatted.
        """
        from loguru import logger

        from hsync_client.logger import _format_record_plain
        from hsync_client.p2p_handler import _refresh_p2p_network_tuning

        config = _make_config(tmp_path)
        CONFIG.clear()
        CONFIG.update(config)

        captured: list[str] = []

        def sink(message):
            captured.append(str(message))

        handler_id = logger.add(sink, level="INFO", format=_format_record_plain, catch=False)
        try:
            with patch("hsync_client.autotune.measure_rtt", return_value=0.1):
                # Pass the real loguru logger so emit() runs the formatter.
                _refresh_p2p_network_tuning(_make_master(), config, logger)
        finally:
            logger.remove(handler_id)

        # If we got here, no KeyError was raised by loguru's formatter.
        assert any("autotune (P2P)" in m for m in captured)
        # The dict is JSON-serialized with escaped braces so it renders verbatim.
        assert any("HTTP_TIMEOUT" in m and "30" in m for m in captured)


class TestSlaveTuningRefreshOrdering:
    """_run_as_slave must refresh tuning after the pin check."""

    def test_refresh_runs_after_pin(self, tmp_path, monkeypatch):
        from hsync_client.p2p_handler import _run_as_slave

        monkeypatch.setenv("HSYNC_P2P_PIN_FILE", str(tmp_path / "pins.json"))
        config = _make_config(tmp_path, P2P_KEY="secret")
        master = _make_master()
        master.connected = 0  # presence arithmetic reads an int, not a mock
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = master
        peer_mgr.get_alive_peers.return_value = [master]
        peer_mgr.is_wss_connected.return_value = True

        calls: list[str] = []

        with (
            patch("hsync_client.p2p_handler.set_config"),
            patch(
                "hsync_client.p2p_handler._evaluate_master_pin",
                side_effect=lambda *_a, **_k: calls.append("pin") or True,
            ),
            patch(
                "hsync_client.p2p_handler._refresh_p2p_network_tuning",
                side_effect=lambda *_a, **_k: calls.append("refresh"),
            ),
            patch("hsync_client.p2p_handler._master_endpoint_reachable", return_value=True),
            patch("hsync_client.p2p_handler.sync", new=MagicMock()),
            patch("asyncio.run"),
        ):
            result = _run_as_slave(config, MagicMock(), True, peer_mgr)

        assert result == (False, 0)
        assert calls == ["pin", "refresh"]


class TestRefreshCheckOrdering:
    """The managed-keys check must short-circuit before any RTT probing."""

    def test_no_probe_when_all_keys_overridden(self, tmp_path):
        """Empty AUTOTUNE_MANAGED_KEYS: measure_rtt is never called."""
        from hsync_client.p2p_handler import _refresh_p2p_network_tuning

        config = _make_config(tmp_path, AUTOTUNE_MANAGED_KEYS=[])

        with patch("hsync_client.autotune.measure_rtt") as mock_rtt:
            _refresh_p2p_network_tuning(_make_master(), config, MagicMock())

        mock_rtt.assert_not_called()
        assert config["HTTP_TIMEOUT"] == 30


class TestRunSyncLanPlaceholder:
    """run_sync in P2P mode with the placeholder URL must defer RTT probing."""

    def _run_p2p(self, tmp_path, **kwargs_overrides: Any):
        kwargs_overrides.setdefault("sync_direction", "download-only")
        # The keyless P2P gate is exercised in test_p2p_keyless_policy; here the
        # run must reach run_p2p_mode, so acknowledge the posture explicitly.
        kwargs_overrides.setdefault("p2p_allow_unauthenticated", True)
        kwargs = _default_run_sync_kwargs(tmp_path, **kwargs_overrides)
        mock_emit = MagicMock()
        with (
            patch("hsync_client.main_logic.setup_logging", return_value=MagicMock()),
            patch("hsync_client.p2p_handler.run_p2p_mode") as mock_p2p,
            patch("hsync_client.main_logic.set_config"),
            patch("hsync_client.main_logic.set_debug"),
            patch("hsync_client.startup_checks.is_tty_interactive", return_value=True),
            patch("hsync_client.main_logic.emit_event", mock_emit),
            patch("hsync_client.runtime_resolvers.emit_event", mock_emit),
            patch("hsync_client.autotune.measure_rtt") as mock_rtt,
        ):
            mock_p2p.return_value = 0
            with pytest.raises(SystemExit) as exc_info:
                run_sync(kwargs)
            assert exc_info.value.code == 0
        return mock_p2p, mock_emit, mock_rtt

    def test_placeholder_skips_rtt_probe_and_server_event(self, tmp_path):
        """No server URL: probe deferred, no placeholder URL leaks to the UI."""
        with patch("hsync_client.runtime_resolvers.get_server_url", return_value=None):
            mock_p2p, mock_emit, mock_rtt = self._run_p2p(tmp_path, server_url=None, p2p_mode=True)

        mock_p2p.assert_called_once()
        mock_rtt.assert_not_called()
        server_calls = [
            c for c in mock_emit.call_args_list if c.args and c.args[0] == UiEventName.server
        ]
        assert server_calls == []

    def test_real_server_url_probes_rtt_and_emits(self, tmp_path):
        """A configured server URL keeps the startup probe and the server event."""
        _, mock_emit, mock_rtt = self._run_p2p(
            tmp_path, server_url="https://master.lan:8443", p2p_mode=True
        )

        mock_rtt.assert_called_once_with("master.lan", 8443)
        server_calls = [
            c for c in mock_emit.call_args_list if c.args and c.args[0] == UiEventName.server
        ]
        assert len(server_calls) == 1
        assert server_calls[0].kwargs["url"] == "https://master.lan:8443"

    def test_config_carries_autotune_managed_keys(self, tmp_path):
        """The P2P config handed to run_p2p_mode records autotune-managed keys."""
        with patch("hsync_client.runtime_resolvers.get_server_url", return_value=None):
            mock_p2p, _, _ = self._run_p2p(
                tmp_path,
                server_url=None,
                p2p_mode=True,
                http_timeout=None,
                reconnect_delay=None,
            )

        config_arg = mock_p2p.call_args.args[0]
        assert set(config_arg["AUTOTUNE_MANAGED_KEYS"]) == set(_ALL_TUNING_KEYS)

    def test_config_carries_the_client_id_header_in_p2p_mode(self, tmp_path):
        """The P2P data plane sends the requester CID so the master can bucket it.

        Relayed peers all arrive from loopback; the header lets the master's
        admission control separate them even for requests without a ``cid``
        query parameter.
        """
        with patch("hsync_client.runtime_resolvers.get_server_url", return_value=None):
            mock_p2p, _, _ = self._run_p2p(tmp_path, server_url=None, p2p_mode=True)

        config_arg = mock_p2p.call_args.args[0]
        assert config_arg["REQUEST_HEADERS"]["X-Client-Id"] == config_arg["CID"]

    def test_config_carries_peer_download_environment_policy(self, tmp_path):
        """Read cap and dangerous opt-in come from HSYNC_ settings; the peer path
        itself has no size threshold, so eligibility is a runtime, capacity-driven
        decision (ADR-0030)."""
        with (
            patch("hsync_client.runtime_resolvers.get_server_url", return_value=None),
            patch("hsync_client.config_build.get_p2p_peer_download", return_value=True),
            patch("hsync_client.config_build.get_p2p_peer_read_cap", return_value=7),
            patch(
                "hsync_client.runtime_resolvers.get_p2p_allow_unauthenticated_writes",
                return_value=True,
            ),
        ):
            mock_p2p, _, _ = self._run_p2p(
                tmp_path,
                server_url=None,
                p2p_mode=True,
                sync_direction="bidirectional",
            )

        config_arg = mock_p2p.call_args.args[0]
        assert config_arg["P2P_PEER_DOWNLOAD"] is True
        assert config_arg["P2P_PEER_READ_CAP"] == 7
        assert config_arg["P2P_ALLOW_UNAUTHENTICATED_WRITES"] is True

    def test_cli_peer_download_opt_out_wins_over_environment(self, tmp_path):
        """--no-peer-download remains final even when the environment enables peers."""
        with (
            patch("hsync_client.runtime_resolvers.get_server_url", return_value=None),
            patch("hsync_client.config_build.get_p2p_peer_download", return_value=True),
        ):
            mock_p2p, _, _ = self._run_p2p(
                tmp_path,
                server_url=None,
                p2p_mode=True,
                p2p_peer_download_enabled=False,
            )

        assert mock_p2p.call_args.args[0]["P2P_PEER_DOWNLOAD"] is False

    def test_cli_override_excluded_from_managed_keys(self, tmp_path):
        """--http-timeout 30 must survive the P2P refresh: not autotune-managed."""
        with patch("hsync_client.runtime_resolvers.get_server_url", return_value=None):
            mock_p2p, _, _ = self._run_p2p(
                tmp_path,
                server_url=None,
                p2p_mode=True,
                http_timeout=30,
                reconnect_delay=None,
            )

        config_arg = mock_p2p.call_args.args[0]
        assert "HTTP_TIMEOUT" not in config_arg["AUTOTUNE_MANAGED_KEYS"]
        assert "RECONNECT_DELAY" in config_arg["AUTOTUNE_MANAGED_KEYS"]

    def test_env_override_excluded_from_managed_keys(self, tmp_path):
        """HSYNC_HTTP_TIMEOUT must survive the P2P refresh: not autotune-managed."""
        with (
            patch("hsync_client.runtime_resolvers.get_server_url", return_value=None),
            patch("hsync_client.config.get_http_timeout", return_value=45),
        ):
            mock_p2p, _, _ = self._run_p2p(
                tmp_path,
                server_url=None,
                p2p_mode=True,
                http_timeout=None,
                reconnect_delay=None,
            )

        config_arg = mock_p2p.call_args.args[0]
        assert config_arg["HTTP_TIMEOUT"] == 45
        assert "HTTP_TIMEOUT" not in config_arg["AUTOTUNE_MANAGED_KEYS"]
        assert set(config_arg["AUTOTUNE_MANAGED_KEYS"]) == set(_ALL_TUNING_KEYS) - {"HTTP_TIMEOUT"}


def _master_with_endpoints(endpoints, *, address=None, port=None):
    return Peer(
        cid="abc123",
        bucket="shared",
        address=address or (endpoints[0][0] if endpoints else "192.168.1.1"),
        port=port or (endpoints[0][1] if endpoints else 9000),
        use_https=True,
        cert_fingerprint="a" * 64,
        endpoints=tuple(endpoints),
    )


class TestSelectMasterEndpoint:
    """The slave picks the first reachable candidate, host/LAN first (ICE-lite)."""

    def test_a_multi_homed_mdns_peer_is_probed_for_the_reachable_address(self):
        """Several advertised interfaces: probe, do not guess.

        ``same_subnet`` is only a /24 heuristic, so on a wider LAN the promoted
        address can be the wrong interface; the probe (pinned fingerprint) picks
        the reachable one instead of dialing an unreachable address.
        """
        from hsync_client.p2p_handler import _select_master_endpoint

        master = _master_with_endpoints([("10.145.201.139", 9000), ("192.168.1.42", 9000)])
        with (
            # No /24 match with either candidate: the advertised order is kept.
            patch("hsync_client.p2p_master_connect.get_local_ips", return_value=["10.99.0.1"]),
            patch(
                "hsync_client.p2p_master_connect._probe_master_endpoint",
                side_effect=lambda host, port, fingerprint, timeout: host == "192.168.1.42",
            ),
        ):
            assert _select_master_endpoint(master, MagicMock()) == ("192.168.1.42", 9000)

    def test_an_mdns_peer_with_one_address_skips_the_probe(self):
        """A LAN-only peer (mDNS, no advertised candidate list) keeps its single
        address without a probe: the data-plane connection re-checks it."""
        from hsync_client.p2p_handler import _select_master_endpoint

        master = _master_with_endpoints([])  # mDNS: no advertised candidates
        with (
            patch("hsync_client.p2p_master_connect.get_local_ips", return_value=["10.0.0.5"]),
            patch("hsync_client.p2p_master_connect._probe_master_endpoint") as probe,
        ):
            assert _select_master_endpoint(master, MagicMock()) == ("192.168.1.1", 9000)
        probe.assert_not_called()

    def test_single_rendezvous_candidate_unreachable_returns_none_for_the_relay(self):
        """A rendezvous peer that advertised only an unreachable private IP must
        fall back to the data relay instead of being returned as-is (the slave
        would then dial the private IP forever and never try ``--relay``)."""
        from hsync_client.p2p_handler import _select_master_endpoint

        master = _master_with_endpoints([("192.168.1.1", 9000)])
        with (
            patch("hsync_client.p2p_master_connect.get_local_ips", return_value=["10.0.0.5"]),
            patch("hsync_client.p2p_master_connect._probe_master_endpoint", return_value=False),
        ):
            assert _select_master_endpoint(master, MagicMock()) is None

    def test_the_lan_candidate_is_preferred_when_both_answer(self):
        """Both reachable: the LAN candidate wins, whatever the probe timing."""
        from hsync_client.p2p_handler import _select_master_endpoint

        master = _master_with_endpoints([("203.0.113.10", 9000), ("192.168.1.42", 9000)])
        probed: list[tuple[str, int]] = []

        def reachable(host, port, fingerprint, timeout):
            probed.append((host, port))
            return True

        with (
            patch("hsync_client.p2p_master_connect.get_local_ips", return_value=["192.168.1.50"]),
            patch("hsync_client.p2p_master_connect._probe_master_endpoint", side_effect=reachable),
        ):
            assert _select_master_endpoint(master, MagicMock()) == ("192.168.1.42", 9000)
        # Every candidate of the round is probed; the *order* is the preference,
        # so the probe that comes back first does not decide the endpoint.
        assert set(probed) == {("192.168.1.42", 9000), ("203.0.113.10", 9000)}

    def test_a_round_costs_one_timeout_not_the_sum_of_them(self):
        """The CGNAT case: several silent candidates must not add up."""
        import time

        from hsync_client.p2p_master_connect import _probe_round

        def probe(host, port, fingerprint, timeout):
            time.sleep(0.3 if host == "10.0.0.1" else 0.6)
            return host == "203.0.113.7"

        started = time.monotonic()
        with patch("hsync_client.p2p_master_connect._probe_master_endpoint", side_effect=probe):
            selected = _probe_round((("10.0.0.1", 9000), ("203.0.113.7", 9000)), "aa", 3.0)
        elapsed = time.monotonic() - started

        assert selected == ("203.0.113.7", 9000)
        assert elapsed < 0.8, "the candidates were probed one after the other"

    def test_a_candidate_needing_longer_than_the_short_round_is_still_found(self):
        """The full budget applies to whatever the short round could not answer."""
        from hsync_client.constants import MASTER_PROBE_FAST_TIMEOUT_S
        from hsync_client.p2p_master_connect import (
            MASTER_CANDIDATE_PROBE_TIMEOUT_S,
            _select_master_endpoint,
        )

        master = _master_with_endpoints([("203.0.113.10", 9000), ("192.168.1.42", 9000)])
        rounds: list[float] = []

        def slow(host, port, fingerprint, timeout):
            rounds.append(timeout)
            return timeout > MASTER_PROBE_FAST_TIMEOUT_S

        with (
            patch("hsync_client.p2p_master_connect.get_local_ips", return_value=["192.168.1.50"]),
            patch("hsync_client.p2p_master_connect._probe_master_endpoint", side_effect=slow),
        ):
            assert _select_master_endpoint(master, MagicMock()) == ("192.168.1.42", 9000)

        assert rounds == [MASTER_PROBE_FAST_TIMEOUT_S] * 2 + [MASTER_CANDIDATE_PROBE_TIMEOUT_S] * 2

    def test_no_reachable_candidate_returns_none(self):
        from hsync_client.p2p_handler import _select_master_endpoint

        master = _master_with_endpoints([("203.0.113.10", 9000), ("192.168.1.42", 9000)])
        with (
            patch("hsync_client.p2p_master_connect.get_local_ips", return_value=["192.168.1.50"]),
            patch("hsync_client.p2p_master_connect._probe_master_endpoint", return_value=False),
        ):
            assert _select_master_endpoint(master, MagicMock()) is None


class TestRelayTransferConcurrencyRestore:
    """The relay's concurrency cap must not outlive the relay.

    ``_apply_relay_transfer_tuning`` forces ``MAX_CONCURRENT_TRANSFERS`` down for
    the relayed data plane. Autotune re-derives that key only when it manages it,
    so without an explicit restore a CLI/env override (or ``--no-autotune``) left
    a direct LAN path throttled to the relay value for the rest of the process.
    """

    @pytest.fixture(autouse=True)
    def _reset_saved(self):
        import hsync_client.p2p_serving as handler

        handler._relay_saved_max_concurrent_transfers = None
        yield
        handler._relay_saved_max_concurrent_transfers = None

    def test_the_cap_is_lifted_when_a_direct_path_is_used_again(self, tmp_path):
        from hsync_client.constants import MAX_CONCURRENT_TRANSFERS_RELAY
        from hsync_client.p2p_handler import (
            _apply_relay_transfer_tuning,
            _restore_relay_transfer_tuning,
        )

        config = _make_config(tmp_path, AUTOTUNE_ENABLED=False)
        config["MAX_CONCURRENT_TRANSFERS"] = 12
        CONFIG.clear()
        CONFIG.update(config)

        _apply_relay_transfer_tuning(config, MagicMock())
        assert config["MAX_CONCURRENT_TRANSFERS"] == MAX_CONCURRENT_TRANSFERS_RELAY
        assert CONFIG["MAX_CONCURRENT_TRANSFERS"] == MAX_CONCURRENT_TRANSFERS_RELAY

        _restore_relay_transfer_tuning(config, MagicMock())
        assert config["MAX_CONCURRENT_TRANSFERS"] == 12
        assert CONFIG["MAX_CONCURRENT_TRANSFERS"] == 12

    def test_restore_without_a_prior_cap_is_a_noop(self, tmp_path):
        from hsync_client.p2p_handler import _restore_relay_transfer_tuning

        config = _make_config(tmp_path)
        config["MAX_CONCURRENT_TRANSFERS"] = 7
        CONFIG.clear()
        CONFIG.update(config)

        _restore_relay_transfer_tuning(config, MagicMock())
        assert config["MAX_CONCURRENT_TRANSFERS"] == 7

    def test_a_second_cap_keeps_the_original_to_restore(self, tmp_path):
        """Re-entering the relay must not overwrite the value saved the first time."""
        from hsync_client.p2p_handler import (
            _apply_relay_transfer_tuning,
            _restore_relay_transfer_tuning,
        )

        config = _make_config(tmp_path)
        config["MAX_CONCURRENT_TRANSFERS"] = 12
        CONFIG.clear()
        CONFIG.update(config)

        _apply_relay_transfer_tuning(config, MagicMock())
        _apply_relay_transfer_tuning(config, MagicMock())  # already capped: no re-save
        _restore_relay_transfer_tuning(config, MagicMock())

        assert config["MAX_CONCURRENT_TRANSFERS"] == 12
