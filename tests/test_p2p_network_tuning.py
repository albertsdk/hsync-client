"""
Tests for P2P post-discovery network tuning refresh.

Covers:
- ``p2p_handler._refresh_p2p_network_tuning`` — RTT re-derivation after master
  discovery, CLI/env precedence via ``AUTOTUNE_MANAGED_KEYS``, dual-write to
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

        with patch("hsync_client.p2p_handler.measure_rtt", return_value=80.0):
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

        with patch("hsync_client.p2p_handler.measure_rtt", return_value=80.0):
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

        with patch("hsync_client.p2p_handler.measure_rtt", return_value=None):
            _refresh_p2p_network_tuning(_make_master(), config, log)

        for cfg in (config, CONFIG):
            assert cfg["HTTP_TIMEOUT"] == 30
            assert cfg["RECONNECT_DELAY"] == 5
        assert log.debug.call_count >= 1

    def test_autotune_disabled_is_noop(self, tmp_path):
        """--no-autotune (AUTOTUNE_ENABLED False): RTT is never measured."""
        from hsync_client.p2p_handler import _refresh_p2p_network_tuning

        config = _make_config(tmp_path, AUTOTUNE_ENABLED=False)

        with patch("hsync_client.p2p_handler.measure_rtt") as mock_rtt:
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

        with patch("hsync_client.p2p_handler.measure_rtt") as mock_rtt:
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
            with patch("hsync_client.p2p_handler.measure_rtt", return_value=0.1):
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
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = _make_master()

        calls: list[str] = []

        with (
            patch("hsync_client.p2p_handler.set_config"),
            patch(
                "hsync_client.p2p_handler._evaluate_master_pin",
                side_effect=lambda *_a: calls.append("pin") or True,
            ),
            patch(
                "hsync_client.p2p_handler._refresh_p2p_network_tuning",
                side_effect=lambda *_a: calls.append("refresh"),
            ),
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

        with patch("hsync_client.p2p_handler.measure_rtt") as mock_rtt:
            _refresh_p2p_network_tuning(_make_master(), config, MagicMock())

        mock_rtt.assert_not_called()
        assert config["HTTP_TIMEOUT"] == 30


class TestRunSyncLanPlaceholder:
    """run_sync in P2P mode with the placeholder URL must defer RTT probing."""

    def _run_p2p(self, tmp_path, **kwargs_overrides: Any):
        kwargs_overrides.setdefault("sync_direction", "download-only")
        kwargs = _default_run_sync_kwargs(tmp_path, **kwargs_overrides)
        with (
            patch("hsync_client.main_logic.setup_logging", return_value=MagicMock()),
            patch("hsync_client.p2p_handler.run_p2p_mode") as mock_p2p,
            patch("hsync_client.main_logic.set_config"),
            patch("hsync_client.main_logic.set_debug"),
            patch("hsync_client.main_logic.is_tty_interactive", return_value=True),
            patch("hsync_client.main_logic.emit_event") as mock_emit,
            patch("hsync_client.autotune.measure_rtt") as mock_rtt,
        ):
            mock_p2p.return_value = 0
            with pytest.raises(SystemExit) as exc_info:
                run_sync(**kwargs)
            assert exc_info.value.code == 0
        return mock_p2p, mock_emit, mock_rtt

    def test_placeholder_skips_rtt_probe_and_server_event(self, tmp_path):
        """No server URL: probe deferred, no placeholder URL leaks to the UI."""
        with patch("hsync_client.main_logic.get_server_url", return_value=None):
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
        with patch("hsync_client.main_logic.get_server_url", return_value=None):
            mock_p2p, _, _ = self._run_p2p(
                tmp_path,
                server_url=None,
                p2p_mode=True,
                http_timeout=None,
                reconnect_delay=None,
            )

        config_arg = mock_p2p.call_args.args[0]
        assert set(config_arg["AUTOTUNE_MANAGED_KEYS"]) == set(_ALL_TUNING_KEYS)

    def test_config_carries_peer_download_environment_policy(self, tmp_path):
        """Read cap and dangerous opt-in come from HSYNC_ settings; the peer path
        itself has no size threshold, so eligibility is a runtime, capacity-driven
        decision (ADR-0030)."""
        with (
            patch("hsync_client.main_logic.get_server_url", return_value=None),
            patch("hsync_client.main_logic.get_p2p_peer_download", return_value=True),
            patch("hsync_client.main_logic.get_p2p_peer_read_cap", return_value=7),
            patch(
                "hsync_client.main_logic.get_p2p_allow_unauthenticated_writes",
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
            patch("hsync_client.main_logic.get_server_url", return_value=None),
            patch("hsync_client.main_logic.get_p2p_peer_download", return_value=True),
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
        with patch("hsync_client.main_logic.get_server_url", return_value=None):
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
            patch("hsync_client.main_logic.get_server_url", return_value=None),
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
