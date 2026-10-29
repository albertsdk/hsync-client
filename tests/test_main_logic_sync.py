"""
Tests for hsync_client.main_logic module - Sync functionality.

Contains TestGetStabilityDelayValue, TestCheckServerConnectivity, TestRunSync classes.
"""

import os
from typing import Unpack, cast
from unittest.mock import MagicMock, patch

import pytest
from hsync_common.protocol.events import parse_event_line
from hsync_common.runtime import ColorMode, ContainerInfo
from typing_extensions import TypedDict

from hsync_client.config import CONFIG, HsyncConfig, get_env_settings
from hsync_client.constants import (
    CLIENT_NAME,
    FILE_STABILITY_INTERVAL_DEFAULT,
    HEADER_CLIENT_NAME,
    HEADER_CLIENT_VERSION,
    HSYNC_VERSION,
    RENDEZVOUS_TIMEOUT_DEFAULT,
    RESYNC_DEFAULT,
)
from hsync_client.events import get_emitter, init_emitter
from hsync_client.identity import InstanceLockError
from hsync_client.main_logic import (
    _check_server_connectivity,
    _get_peer_rendezvous_timeout_value,
    _get_resync_value,
    _get_stability_delay_value,
    _safe_config_for_log,
    _warn_if_p2p_in_container,
    run_sync,
)
from hsync_client.sync import SyncCycleResult


def _cycle(
    *,
    transfers: int = 0,
    failed: int = 0,
    conflicts: int = 0,
    guard: bool = False,
) -> SyncCycleResult:
    """A completed cycle result reporting *transfers* downloads."""
    return SyncCycleResult(
        completed=True,
        downloaded=transfers,
        uploaded=0,
        deleted=0,
        renamed=0,
        skipped=0,
        failed=failed,
        conflicts=conflicts,
        guard_tripped=guard,
    )


class TestGetStabilityDelayValue:
    """Tests for _get_stability_delay_value function."""

    def test_cli_value_takes_priority(self):
        """CLI argument is used when provided."""
        assert _get_stability_delay_value(42) == 42

    def test_cli_zero(self):
        """CLI value of 0 is valid and used."""
        assert _get_stability_delay_value(0) == 0

    def test_env_value_used_when_no_cli(self):
        """Environment variable is used when CLI argument is None."""
        get_env_settings.cache_clear()
        old = os.environ.get("HSYNC_STABILITY_DELAY")
        os.environ["HSYNC_STABILITY_DELAY"] = "15"
        try:
            get_env_settings.cache_clear()
            result = _get_stability_delay_value(None)
            assert result == 15
        finally:
            if old is not None:
                os.environ["HSYNC_STABILITY_DELAY"] = old
            else:
                os.environ.pop("HSYNC_STABILITY_DELAY", None)
            get_env_settings.cache_clear()

    def test_default_when_no_cli_no_env(self):
        """Default constant is used when neither CLI nor env is set."""
        get_env_settings.cache_clear()
        old = os.environ.get("HSYNC_STABILITY_DELAY")
        os.environ.pop("HSYNC_STABILITY_DELAY", None)
        try:
            get_env_settings.cache_clear()
            result = _get_stability_delay_value(None)
            assert result == FILE_STABILITY_INTERVAL_DEFAULT
        finally:
            if old is not None:
                os.environ["HSYNC_STABILITY_DELAY"] = old
            get_env_settings.cache_clear()

    def test_cli_value_overrides_env(self):
        """CLI argument overrides environment variable."""
        get_env_settings.cache_clear()
        old = os.environ.get("HSYNC_STABILITY_DELAY")
        os.environ["HSYNC_STABILITY_DELAY"] = "99"
        try:
            get_env_settings.cache_clear()
            result = _get_stability_delay_value(5)
            assert result == 5
        finally:
            if old is not None:
                os.environ["HSYNC_STABILITY_DELAY"] = old
            else:
                os.environ.pop("HSYNC_STABILITY_DELAY", None)
            get_env_settings.cache_clear()


class TestGetRendezvousTimeoutValue:
    """Tests for _get_peer_rendezvous_timeout_value function."""

    def test_cli_value_takes_priority(self):
        """CLI argument is used when provided."""
        assert _get_peer_rendezvous_timeout_value(15) == 15

    def test_cli_zero_means_wait_indefinitely(self):
        """CLI value of 0 is valid and preserved."""
        assert _get_peer_rendezvous_timeout_value(0) == 0

    def test_env_value_used_when_no_cli(self):
        """Environment variable is used when CLI argument is None."""
        get_env_settings.cache_clear()
        old = os.environ.get("HSYNC_PEER_RENDEZVOUS_TIMEOUT")
        os.environ["HSYNC_PEER_RENDEZVOUS_TIMEOUT"] = "20"
        try:
            get_env_settings.cache_clear()
            assert _get_peer_rendezvous_timeout_value(None) == 20
        finally:
            if old is not None:
                os.environ["HSYNC_PEER_RENDEZVOUS_TIMEOUT"] = old
            else:
                os.environ.pop("HSYNC_PEER_RENDEZVOUS_TIMEOUT", None)
            get_env_settings.cache_clear()

    def test_env_zero_means_wait_indefinitely(self):
        """Environment value of 0 is valid and preserved."""
        get_env_settings.cache_clear()
        old = os.environ.get("HSYNC_PEER_RENDEZVOUS_TIMEOUT")
        os.environ["HSYNC_PEER_RENDEZVOUS_TIMEOUT"] = "0"
        try:
            get_env_settings.cache_clear()
            assert _get_peer_rendezvous_timeout_value(None) == 0
        finally:
            if old is not None:
                os.environ["HSYNC_PEER_RENDEZVOUS_TIMEOUT"] = old
            else:
                os.environ.pop("HSYNC_PEER_RENDEZVOUS_TIMEOUT", None)
            get_env_settings.cache_clear()

    def test_default_when_no_cli_no_env(self):
        """Default constant is used when neither CLI nor env is set."""
        get_env_settings.cache_clear()
        old = os.environ.get("HSYNC_PEER_RENDEZVOUS_TIMEOUT")
        os.environ.pop("HSYNC_PEER_RENDEZVOUS_TIMEOUT", None)
        try:
            get_env_settings.cache_clear()
            assert _get_peer_rendezvous_timeout_value(None) == RENDEZVOUS_TIMEOUT_DEFAULT
        finally:
            if old is not None:
                os.environ["HSYNC_PEER_RENDEZVOUS_TIMEOUT"] = old
            get_env_settings.cache_clear()

    def test_cli_overrides_env(self):
        """CLI argument overrides environment variable."""
        get_env_settings.cache_clear()
        old = os.environ.get("HSYNC_PEER_RENDEZVOUS_TIMEOUT")
        os.environ["HSYNC_PEER_RENDEZVOUS_TIMEOUT"] = "99"
        try:
            get_env_settings.cache_clear()
            assert _get_peer_rendezvous_timeout_value(5) == 5
        finally:
            if old is not None:
                os.environ["HSYNC_PEER_RENDEZVOUS_TIMEOUT"] = old
            else:
                os.environ.pop("HSYNC_PEER_RENDEZVOUS_TIMEOUT", None)
            get_env_settings.cache_clear()


class TestGetResyncValue:
    """Tests for _get_resync_value function."""

    def test_cli_value_takes_priority(self):
        """CLI argument is used when provided."""
        assert _get_resync_value(120) == 120

    def test_env_value_used_when_no_cli(self):
        """Environment variable is used when CLI argument is None."""
        get_env_settings.cache_clear()
        old = os.environ.get("HSYNC_RESYNC")
        os.environ["HSYNC_RESYNC"] = "600"
        try:
            get_env_settings.cache_clear()
            assert _get_resync_value(None) == 600
        finally:
            if old is not None:
                os.environ["HSYNC_RESYNC"] = old
            else:
                os.environ.pop("HSYNC_RESYNC", None)
            get_env_settings.cache_clear()

    def test_env_zero_disables_resync(self):
        """Environment value of 0 disables periodic resync (None)."""
        get_env_settings.cache_clear()
        old = os.environ.get("HSYNC_RESYNC")
        os.environ["HSYNC_RESYNC"] = "0"
        try:
            get_env_settings.cache_clear()
            assert _get_resync_value(None) is None
        finally:
            if old is not None:
                os.environ["HSYNC_RESYNC"] = old
            else:
                os.environ.pop("HSYNC_RESYNC", None)
            get_env_settings.cache_clear()

    def test_default_when_no_cli_no_env(self):
        """Default constant is used when neither CLI nor env is set."""
        get_env_settings.cache_clear()
        old = os.environ.get("HSYNC_RESYNC")
        os.environ.pop("HSYNC_RESYNC", None)
        try:
            get_env_settings.cache_clear()
            assert _get_resync_value(None) == RESYNC_DEFAULT
        finally:
            if old is not None:
                os.environ["HSYNC_RESYNC"] = old
            get_env_settings.cache_clear()

    def test_cli_overrides_env(self):
        """CLI argument overrides environment variable."""
        get_env_settings.cache_clear()
        old = os.environ.get("HSYNC_RESYNC")
        os.environ["HSYNC_RESYNC"] = "600"
        try:
            get_env_settings.cache_clear()
            assert _get_resync_value(30) == 30
        finally:
            if old is not None:
                os.environ["HSYNC_RESYNC"] = old
            else:
                os.environ.pop("HSYNC_RESYNC", None)
            get_env_settings.cache_clear()


class TestCheckServerConnectivity:
    """Tests for _check_server_connectivity function."""

    @pytest.fixture(autouse=True)
    def _fast_retry(self, monkeypatch):
        """Set CONFIG connectivity retry to 1 attempt so tests don't hang."""
        monkeypatch.setitem(CONFIG, "CONNECTIVITY_RETRY_ATTEMPTS", 1)
        monkeypatch.setitem(CONFIG, "CONNECTIVITY_RETRY_MIN_WAIT", 0.01)
        monkeypatch.setitem(CONFIG, "CONNECTIVITY_RETRY_MAX_WAIT", 0.01)

    def test_successful_connection(self):
        """Test successful server connection."""
        mock_logger = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("requests.get", return_value=mock_response):
            result = _check_server_connectivity(
                server_url="http://localhost:8000",
                timeout=10,
                verify_ssl=True,
                logger=mock_logger,
            )

            assert result is True
            mock_logger.debug.assert_called()

    def test_server_error_500(self):
        """Test server returning 500 error."""
        mock_logger = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 500

        with patch("requests.get", return_value=mock_response):
            result = _check_server_connectivity(
                server_url="http://localhost:8000",
                timeout=10,
                verify_ssl=True,
                logger=mock_logger,
            )

            assert result is False
            mock_logger.error.assert_called()

    def test_server_error_503(self):
        """Test server returning 503 error."""
        mock_logger = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 503

        with patch("requests.get", return_value=mock_response):
            result = _check_server_connectivity(
                server_url="http://localhost:8000",
                timeout=10,
                verify_ssl=True,
                logger=mock_logger,
            )

            assert result is False

    def test_connection_error(self):
        """Test connection error (network unreachable)."""
        from requests import RequestException

        mock_logger = MagicMock()

        with patch("requests.get", side_effect=RequestException("Connection refused")):
            result = _check_server_connectivity(
                server_url="http://localhost:8000",
                timeout=10,
                verify_ssl=True,
                logger=mock_logger,
            )

            assert result is False
            mock_logger.error.assert_called()

    def test_ssl_plaintext_mismatch_hint(self):
        """An HTTPS client hitting a plaintext server logs an actionable hint."""
        from requests.exceptions import SSLError

        mock_logger = MagicMock()
        err = SSLError("[SSL: WRONG_VERSION_NUMBER] wrong version number")

        with patch("requests.get", side_effect=err):
            result = _check_server_connectivity(
                server_url="https://localhost:8000",
                timeout=10,
                verify_ssl=True,
                logger=mock_logger,
            )

        assert result is False
        logged = mock_logger.error.call_args[0][0]
        assert "plaintext HTTP" in logged

    def test_successful_with_ssl_disabled(self):
        """Test successful connection with SSL verification disabled."""
        mock_logger = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("requests.get", return_value=mock_response) as mock_get:
            result = _check_server_connectivity(
                server_url="https://localhost:8000",
                timeout=5,
                verify_ssl=False,
                logger=mock_logger,
            )

            assert result is True
            # Verify that verify=False was passed
            mock_get.assert_called_once()
            call_kwargs = mock_get.call_args[1]
            assert call_kwargs["verify"] is False

    def test_status_code_404_is_ok(self):
        """Test that 404 is considered successful (server is reachable)."""
        mock_logger = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch("requests.get", return_value=mock_response):
            result = _check_server_connectivity(
                server_url="http://localhost:8000",
                timeout=10,
                verify_ssl=True,
                logger=mock_logger,
            )

            # 404 is < 500, so it's considered successful
            assert result is True


class TestDiagnoseConnectionError:
    """Tests for diagnose_connection_error TLS hint helper."""

    def test_plaintext_signature_returns_hint(self):
        from requests.exceptions import SSLError

        from hsync_client.utils import diagnose_connection_error

        err = SSLError("[SSL: RECORD_LAYER_FAILURE] record layer failure")
        hint = diagnose_connection_error(err, "https://host:8000")
        assert hint is not None
        assert "plaintext HTTP" in hint
        assert "https://host:8000" in hint

    def test_certificate_verify_failed_returns_ca_hint(self):
        from requests.exceptions import SSLError

        from hsync_client.utils import diagnose_connection_error

        err = SSLError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        hint = diagnose_connection_error(err, "https://host:8000")
        assert hint is not None
        assert "--ca-cert" in hint

    def test_non_ssl_error_returns_none(self):
        from requests import RequestException

        from hsync_client.utils import diagnose_connection_error

        assert diagnose_connection_error(RequestException("refused"), "http://h") is None

    def test_unrecognized_ssl_error_returns_none(self):
        from requests.exceptions import SSLError

        from hsync_client.utils import diagnose_connection_error

        assert diagnose_connection_error(SSLError("some other tls issue"), "https://h") is None


# ── Helper for run_sync tests ────────────────────────────────────────


class _RunSyncKwargs(TypedDict, total=False):
    bucket: str
    env_file: str | None
    server_url: str | None
    verify_ssl: bool
    ca_cert: str | None
    http_timeout: int
    local_path: str
    continuous: bool
    resync: int | None
    reconnect_delay: int
    stability_delay: int | None
    max_wait_timeout: int
    filter_path: str | None
    dryrun: bool
    deletion_policy: str | None
    sync_direction: str
    compression_enabled: bool
    max_bandwidth_upload: int | None
    max_bandwidth_download: int | None
    p2p_mode: bool
    p2p_listen_port: int
    peer_rendezvous_timeout: int | None
    p2p_group: str | None
    prefer_master: bool
    never_master: bool
    debug: bool
    quiet: bool
    logfile: str | None
    color: ColorMode
    api_key_fd: int | None
    p2p_key_fd: int | None
    p2p_allow_unauthenticated: bool
    events_json_fd: int | None


def _default_run_sync_kwargs(
    tmp_path: object, **overrides: Unpack[_RunSyncKwargs]
) -> _RunSyncKwargs:
    """Return a dict with all required run_sync keyword arguments."""
    defaults: _RunSyncKwargs = {
        "bucket": "test-bucket",
        "env_file": None,
        "server_url": "http://localhost:8000",
        "verify_ssl": True,
        "ca_cert": None,
        "http_timeout": 30,
        "local_path": str(tmp_path),
        "continuous": False,
        "resync": None,
        "reconnect_delay": 5,
        "stability_delay": None,
        "max_wait_timeout": 60,
        "filter_path": None,
        "dryrun": False,
        "deletion_policy": "propagate",
        "sync_direction": "bidirectional",
        "compression_enabled": True,
        "max_bandwidth_upload": None,
        "max_bandwidth_download": None,
        "p2p_mode": False,
        "p2p_listen_port": 0,
        "peer_rendezvous_timeout": 5,
        "p2p_group": None,
        "prefer_master": False,
        "never_master": False,
        "debug": False,
        "quiet": False,
        "logfile": None,
        "color": ColorMode.NEVER,
        "events_json_fd": None,
    }
    defaults.update(overrides)
    return defaults


class TestRunSync:
    """Tests for run_sync function."""

    def test_returns_early_if_local_path_missing(self, tmp_path):
        """run_sync exits non-zero when the local directory doesn't exist."""
        bad_path = str(tmp_path / "nonexistent")
        kwargs = _default_run_sync_kwargs(tmp_path, local_path=bad_path)

        with patch("hsync_client.main_logic.setup_logging") as mock_log_setup:
            mock_logger = MagicMock()
            mock_log_setup.return_value = mock_logger
            with pytest.raises(SystemExit) as exc_info:
                run_sync(**kwargs)
            assert exc_info.value.code != 0
            mock_logger.error.assert_called()
            assert any("does not exist" in str(c) for c in mock_logger.error.call_args_list)

    def test_returns_early_if_no_server_url(self, tmp_path):
        """run_sync exits non-zero when no server URL and not in P2P mode."""
        kwargs = _default_run_sync_kwargs(tmp_path, server_url=None)

        with (
            patch("hsync_client.main_logic.setup_logging") as mock_log_setup,
            patch("hsync_client.main_logic.get_server_url", return_value=None),
        ):
            mock_logger = MagicMock()
            mock_log_setup.return_value = mock_logger
            with pytest.raises(SystemExit) as exc_info:
                run_sync(**kwargs)
            assert exc_info.value.code != 0
            mock_logger.error.assert_called()
            assert any("Server URL is required" in str(c) for c in mock_logger.error.call_args_list)

    def test_missing_server_url_error_is_markup_safe(self, tmp_path):
        """The missing-server error must not crash the colorized loguru sink.

        Regression: the previous message included ``…'hsync server <hostname>'…``,
        an unescaped ``<…>`` that loguru's colorizer parsed as a color directive
        (``ValueError: Tag "<hostname>" does not correspond to any known color
        directive``), swallowing the real error. With a colorized, non-catching
        sink the error must render the message verbatim.
        """
        from loguru import logger

        from hsync_client.logger import _format_record_colored

        captured: list[str] = []

        def sink(message):
            captured.append(str(message))

        handler_id = logger.add(
            sink, level="ERROR", format=_format_record_colored, colorize=True, catch=False
        )
        try:
            kwargs = _default_run_sync_kwargs(tmp_path, server_url=None)
            with (
                patch("hsync_client.main_logic.setup_logging", return_value=logger),
                patch("hsync_client.main_logic.get_server_url", return_value=None),
            ):
                with pytest.raises(SystemExit) as exc_info:
                    run_sync(**kwargs)
                assert exc_info.value.code == 1
        finally:
            logger.remove(handler_id)

        assert any("Server URL is required" in m for m in captured)

    def test_p2p_mode_uses_placeholder_url(self, tmp_path):
        """In P2P mode without server URL, a placeholder is used."""
        kwargs = _default_run_sync_kwargs(
            tmp_path,
            server_url=None,
            p2p_mode=True,
            p2p_allow_unauthenticated=True,
            sync_direction="download-only",
        )

        with (
            patch("hsync_client.main_logic.setup_logging") as mock_log_setup,
            patch("hsync_client.main_logic.get_server_url", return_value=None),
            patch("hsync_client.p2p_handler.run_p2p_mode") as mock_p2p,
            patch("hsync_client.main_logic.set_config"),
            patch("hsync_client.main_logic.set_debug"),
            patch("hsync_client.main_logic.is_tty_interactive", return_value=True),
        ):
            mock_p2p.return_value = 0
            mock_logger = MagicMock()
            mock_log_setup.return_value = mock_logger
            with pytest.raises(SystemExit) as exc_info:
                run_sync(**kwargs)
            assert exc_info.value.code == 0
            # run_p2p_mode should have been called
            mock_p2p.assert_called_once()

    def test_p2p_mode_uses_default_rendezvous_timeout_when_unset(self, tmp_path):
        """P2P rendezvous timeout defaults to 10 when CLI and env are unset."""
        kwargs = _default_run_sync_kwargs(
            tmp_path,
            server_url=None,
            p2p_mode=True,
            p2p_allow_unauthenticated=True,
            peer_rendezvous_timeout=None,
            sync_direction="download-only",
        )

        with (
            patch("hsync_client.main_logic.setup_logging") as mock_log_setup,
            patch("hsync_client.main_logic.get_server_url", return_value=None),
            patch("hsync_client.main_logic.get_peer_rendezvous_timeout", return_value=None),
            patch("hsync_client.p2p_handler.run_p2p_mode") as mock_p2p,
            patch("hsync_client.main_logic.set_config"),
            patch("hsync_client.main_logic.set_debug"),
            patch("hsync_client.main_logic.is_tty_interactive", return_value=True),
        ):
            mock_p2p.return_value = 0
            mock_log_setup.return_value = MagicMock()
            with pytest.raises(SystemExit) as exc_info:
                run_sync(**kwargs)
            assert exc_info.value.code == 0

        config_arg = mock_p2p.call_args.args[0]
        assert config_arg["PEER_RENDEZVOUS_TIMEOUT"] == RENDEZVOUS_TIMEOUT_DEFAULT

    def test_p2p_mode_derives_one_shot_from_continuous(self, tmp_path):
        """ONE_SHOT mirrors --once: continuous=False -> True, True -> False."""
        for continuous_value, expected in ((False, True), (True, False)):
            kwargs = _default_run_sync_kwargs(
                tmp_path,
                server_url=None,
                p2p_mode=True,
                p2p_allow_unauthenticated=True,
                continuous=continuous_value,
                sync_direction="download-only",
            )
            with (
                patch("hsync_client.main_logic.setup_logging") as mock_log_setup,
                patch("hsync_client.main_logic.get_server_url", return_value=None),
                patch("hsync_client.p2p_handler.run_p2p_mode") as mock_p2p,
                patch("hsync_client.main_logic.set_config"),
                patch("hsync_client.main_logic.set_debug"),
                patch("hsync_client.main_logic.is_tty_interactive", return_value=True),
            ):
                mock_p2p.return_value = 0
                mock_log_setup.return_value = MagicMock()
                with pytest.raises(SystemExit) as exc_info:
                    run_sync(**kwargs)
                assert exc_info.value.code == 0
            config_arg = mock_p2p.call_args.args[0]
            assert config_arg["ONE_SHOT"] is expected

    def test_p2p_mode_preserves_zero_rendezvous_timeout(self, tmp_path):
        """P2P rendezvous timeout of 0 is preserved as wait-indefinitely."""
        kwargs = _default_run_sync_kwargs(
            tmp_path,
            server_url=None,
            p2p_mode=True,
            p2p_allow_unauthenticated=True,
            peer_rendezvous_timeout=0,
            sync_direction="download-only",
        )

        with (
            patch("hsync_client.main_logic.setup_logging") as mock_log_setup,
            patch("hsync_client.main_logic.get_server_url", return_value=None),
            patch("hsync_client.p2p_handler.run_p2p_mode") as mock_p2p,
            patch("hsync_client.main_logic.set_config"),
            patch("hsync_client.main_logic.set_debug"),
            patch("hsync_client.main_logic.is_tty_interactive", return_value=True),
        ):
            mock_p2p.return_value = 0
            mock_log_setup.return_value = MagicMock()
            with pytest.raises(SystemExit) as exc_info:
                run_sync(**kwargs)
            assert exc_info.value.code == 0

        config_arg = mock_p2p.call_args.args[0]
        assert config_arg["PEER_RENDEZVOUS_TIMEOUT"] == 0

    def test_invalid_logfile_returns_early(self, tmp_path):
        """run_sync exits non-zero when the logfile path is invalid."""
        kwargs = _default_run_sync_kwargs(tmp_path, logfile="CON")

        with patch("hsync_client.main_logic.setup_logging") as mock_log_setup:
            mock_logger = MagicMock()
            mock_log_setup.return_value = mock_logger
            with pytest.raises(SystemExit) as exc_info:
                run_sync(**kwargs)
            assert exc_info.value.code != 0
            mock_logger.error.assert_called()
            assert any("Invalid output file" in str(c) for c in mock_logger.error.call_args_list)

    def test_server_connectivity_failure_returns(self, tmp_path):
        """run_sync exits non-zero when the server connectivity check fails."""
        kwargs = _default_run_sync_kwargs(tmp_path, server_url="https://localhost:8000")

        with (
            patch("hsync_client.main_logic.setup_logging") as mock_log_setup,
            patch("hsync_client.main_logic.get_api_key", return_value="test-key"),
            patch("hsync_client.main_logic._check_server_connectivity", return_value=False),
            patch("hsync_client.main_logic.set_config"),
            patch("hsync_client.main_logic.set_debug"),
        ):
            mock_logger = MagicMock()
            mock_log_setup.return_value = mock_logger
            with pytest.raises(SystemExit) as exc_info:
                run_sync(**kwargs)
            assert exc_info.value.code != 0
            # Should not call sync since connectivity failed

    def test_continuous_connectivity_failure_does_not_exit(self, tmp_path):
        """Continuous mode (default) keeps running when the server is unreachable."""
        kwargs = _default_run_sync_kwargs(
            tmp_path, continuous=True, dryrun=False, server_url="https://localhost:8000"
        )

        with (
            patch("hsync_client.main_logic.setup_logging") as mock_log_setup,
            patch("hsync_client.main_logic.get_api_key", return_value="test-key"),
            patch("hsync_client.main_logic._check_server_connectivity", return_value=False),
            patch("hsync_client.main_logic.set_config"),
            patch("hsync_client.main_logic.set_debug"),
            patch("hsync_client.main_logic._run_with_stdin_commands", new=lambda log: None),
            patch("hsync_client.main_logic.run"),
        ):
            mock_logger = MagicMock()
            mock_log_setup.return_value = mock_logger
            # Must NOT raise SystemExit: the reconnect loop takes over.
            run_sync(**kwargs)
            assert any(
                "keeping the client alive" in str(c) for c in mock_logger.warning.call_args_list
            )

    def test_single_sync_mode(self, tmp_path):
        """run_sync runs a single sync when not continuous and not dryrun."""
        kwargs = _default_run_sync_kwargs(
            tmp_path, continuous=False, dryrun=False, server_url="https://localhost:8000"
        )

        with (
            patch("hsync_client.main_logic.setup_logging") as mock_log_setup,
            patch("hsync_client.main_logic.get_api_key", return_value="test-key"),
            patch("hsync_client.main_logic._check_server_connectivity", return_value=True),
            patch("hsync_client.main_logic.set_config"),
            patch("hsync_client.main_logic.set_debug"),
            patch("hsync_client.main_logic.sync", new=MagicMock()),
            patch("hsync_client.main_logic.run") as mock_run,
        ):
            mock_logger = MagicMock()
            mock_log_setup.return_value = mock_logger
            mock_run.return_value = _cycle(transfers=1, failed=0, conflicts=0, guard=False)
            with pytest.raises(SystemExit) as exc_info:
                run_sync(**kwargs)
            assert exc_info.value.code == 0
            mock_run.assert_called_once()

    def test_api_key_from_fd(self, tmp_path):
        """run_sync reads API key from file descriptor."""
        r_fd, w_fd = os.pipe()
        os.write(w_fd, b"my-secret-key\n")
        os.close(w_fd)

        # An API key is only transmitted over HTTPS, so use an https:// URL.
        kwargs = _default_run_sync_kwargs(
            tmp_path, api_key_fd=r_fd, server_url="https://localhost:8000"
        )

        with (
            patch("hsync_client.main_logic.setup_logging") as mock_log_setup,
            patch("hsync_client.main_logic._check_server_connectivity", return_value=True),
            patch("hsync_client.main_logic.set_config") as mock_set_cfg,
            patch("hsync_client.main_logic.set_debug"),
            patch("hsync_client.main_logic.sync", new=MagicMock()),
            patch(
                "hsync_client.main_logic.run",
                return_value=_cycle(transfers=1, failed=0, conflicts=0, guard=False),
            ),
        ):
            mock_logger = MagicMock()
            mock_log_setup.return_value = mock_logger
            with pytest.raises(SystemExit) as exc_info:
                run_sync(**kwargs)
            assert exc_info.value.code == 0
            # set_config should have been called with config containing the key
            config_arg = mock_set_cfg.call_args[0][0]
            assert "my-secret-key" in config_arg["REQUEST_HEADERS"]["Authorization"]
            # The client-name header identifies the product, never an internal
            # module (regression: it used to leak Path(__file__).name).
            assert config_arg["REQUEST_HEADERS"][HEADER_CLIENT_NAME] == CLIENT_NAME
            assert config_arg["REQUEST_HEADERS"][HEADER_CLIENT_VERSION] == HSYNC_VERSION

    def test_api_key_over_http_aborts(self, tmp_path):
        """run_sync refuses to send an API key over an unencrypted http:// URL."""
        r_fd, w_fd = os.pipe()
        os.write(w_fd, b"my-secret-key\n")
        os.close(w_fd)

        kwargs = _default_run_sync_kwargs(
            tmp_path, api_key_fd=r_fd, server_url="http://localhost:8000"
        )

        with (
            patch("hsync_client.main_logic.setup_logging") as mock_log_setup,
            patch("hsync_client.main_logic._check_server_connectivity", return_value=True),
            patch("hsync_client.main_logic.set_config") as mock_set_cfg,
            patch("hsync_client.main_logic.set_debug"),
            patch("hsync_client.main_logic.sync", new=MagicMock()),
            patch("hsync_client.main_logic.run"),
        ):
            mock_logger = MagicMock()
            mock_log_setup.return_value = mock_logger
            with pytest.raises(SystemExit) as exc_info:
                run_sync(**kwargs)
            assert exc_info.value.code != 0
            # Aborted before configuring: set_config is never called
            mock_set_cfg.assert_not_called()
            assert any("not HTTPS" in str(c) for c in mock_logger.error.call_args_list)

    def test_missing_api_key_aborts(self, tmp_path):
        """run_sync fails fast with a clear message when no API key is configured."""
        kwargs = _default_run_sync_kwargs(tmp_path, server_url="https://localhost:8000")

        with (
            patch("hsync_client.main_logic.setup_logging") as mock_log_setup,
            patch("hsync_client.main_logic.get_api_key", return_value=""),
            patch("hsync_client.main_logic._check_server_connectivity") as mock_check,
            patch("hsync_client.main_logic.set_config") as mock_set_cfg,
            patch("hsync_client.main_logic.set_debug"),
        ):
            mock_logger = MagicMock()
            mock_log_setup.return_value = mock_logger
            with pytest.raises(SystemExit) as exc_info:
                run_sync(**kwargs)
            assert exc_info.value.code != 0
            # Aborted before configuring or contacting the server.
            mock_set_cfg.assert_not_called()
            mock_check.assert_not_called()
            assert any("No API key configured" in str(c) for c in mock_logger.error.call_args_list)

    def test_weak_p2p_key_aborts(self, tmp_path):
        """A group key below the shared strength floor fails fast with a clear message."""
        kwargs = _default_run_sync_kwargs(tmp_path, server_url="https://localhost:8000")

        with (
            patch("hsync_client.main_logic.setup_logging") as mock_log_setup,
            patch("hsync_client.main_logic.get_api_key", return_value="api-key"),
            patch("hsync_client.main_logic.get_p2p_key", return_value="tooshort"),
            patch("hsync_client.main_logic._check_server_connectivity") as mock_check,
            patch("hsync_client.main_logic.set_config") as mock_set_cfg,
            patch("hsync_client.main_logic.set_debug"),
        ):
            mock_logger = MagicMock()
            mock_log_setup.return_value = mock_logger
            with pytest.raises(SystemExit) as exc_info:
                run_sync(**kwargs)
            assert exc_info.value.code != 0
            # Aborted before configuring or contacting the server.
            mock_set_cfg.assert_not_called()
            mock_check.assert_not_called()
            assert any(
                "HSYNC_P2P_KEY is not acceptable" in str(c)
                for c in mock_logger.error.call_args_list
            )

    def test_short_p2p_passphrase_aborts(self, tmp_path):
        """A shared password below the 12-char floor fails fast."""
        kwargs = _default_run_sync_kwargs(tmp_path, server_url="https://localhost:8000")

        with (
            patch("hsync_client.main_logic.setup_logging") as mock_log_setup,
            patch("hsync_client.main_logic.get_api_key", return_value="api-key"),
            patch("hsync_client.main_logic.get_p2p_key", return_value=None),
            patch("hsync_client.main_logic.get_p2p_passphrase", return_value="short"),
            patch("hsync_client.main_logic._check_server_connectivity") as mock_check,
            patch("hsync_client.main_logic.set_config") as mock_set_cfg,
            patch("hsync_client.main_logic.set_debug"),
        ):
            mock_logger = MagicMock()
            mock_log_setup.return_value = mock_logger
            with pytest.raises(SystemExit) as exc_info:
                run_sync(**kwargs)
            assert exc_info.value.code != 0
            mock_set_cfg.assert_not_called()
            mock_check.assert_not_called()
            assert any(
                "HSYNC_P2P_PASSPHRASE is not acceptable" in str(c)
                for c in mock_logger.error.call_args_list
            )

    def test_p2p_key_and_passphrase_are_mutually_exclusive(self, tmp_path):
        """The raw key and the passphrase are two forms of the same secret."""
        kwargs = _default_run_sync_kwargs(tmp_path, server_url="https://localhost:8000")

        with (
            patch("hsync_client.main_logic.setup_logging") as mock_log_setup,
            patch("hsync_client.main_logic.get_api_key", return_value="api-key"),
            patch("hsync_client.main_logic.get_p2p_key", return_value="k" * 32),
            patch(
                "hsync_client.main_logic.get_p2p_passphrase",
                return_value="abcdwxyz1234",
            ),
            patch("hsync_client.main_logic._check_server_connectivity") as mock_check,
            patch("hsync_client.main_logic.set_config") as mock_set_cfg,
            patch("hsync_client.main_logic.set_debug"),
        ):
            mock_logger = MagicMock()
            mock_log_setup.return_value = mock_logger
            with pytest.raises(SystemExit) as exc_info:
                run_sync(**kwargs)
            assert exc_info.value.code != 0
            mock_set_cfg.assert_not_called()
            mock_check.assert_not_called()
            assert any("not both" in str(c) for c in mock_logger.error.call_args_list)

    def test_p2p_passphrase_is_derived_into_the_config(self, tmp_path):
        """A valid password is scrypt-derived to the 64-hex group key."""
        from hsync_common.protocol.rdv_tokens import derive_group_key

        kwargs = _default_run_sync_kwargs(tmp_path, server_url="https://localhost:8000")

        with (
            patch("hsync_client.main_logic.setup_logging") as mock_log_setup,
            patch("hsync_client.main_logic.get_api_key", return_value="api-key"),
            patch("hsync_client.main_logic.get_p2p_key", return_value=None),
            patch(
                "hsync_client.main_logic.get_p2p_passphrase",
                return_value="abcdwxyz1234",
            ),
            patch("hsync_client.main_logic._check_server_connectivity", return_value=True),
            patch("hsync_client.main_logic.set_config") as mock_set_cfg,
            patch("hsync_client.main_logic.set_debug"),
            patch("hsync_client.main_logic.sync", new=MagicMock()),
            patch(
                "hsync_client.main_logic.run",
                return_value=_cycle(transfers=0, failed=0, conflicts=0, guard=False),
            ),
        ):
            mock_log_setup.return_value = MagicMock()
            with pytest.raises(SystemExit) as exc_info:
                run_sync(**kwargs)
            assert exc_info.value.code == 0
            config_arg = mock_set_cfg.call_args[0][0]
            assert config_arg["P2P_KEY"] == derive_group_key("abcdwxyz1234")

    def test_env_file_loading_failure(self, tmp_path):
        """run_sync exits when env file is not found."""
        kwargs = _default_run_sync_kwargs(tmp_path, env_file="/nonexistent/.env")

        with pytest.raises(SystemExit):
            run_sync(**kwargs)


class TestWarnIfLanInContainer:
    """Tests for _warn_if_p2p_in_container (container networking caveat)."""

    def test_no_warning_outside_container(self):
        """On bare metal no container warning is emitted."""
        mock_logger = MagicMock()
        with patch(
            "hsync_client.main_logic.detect_container",
            return_value=ContainerInfo(False),
        ):
            _warn_if_p2p_in_container(mock_logger)
        mock_logger.warning.assert_not_called()

    def test_warning_inside_container(self):
        """Inside a container the mDNS/bridge warning suggests host networking."""
        mock_logger = MagicMock()
        with patch(
            "hsync_client.main_logic.detect_container",
            return_value=ContainerInfo(True, "docker", "/.dockerenv"),
        ):
            _warn_if_p2p_in_container(mock_logger)
        warning_texts = " ".join(str(c) for c in mock_logger.warning.call_args_list)
        assert "docker" in warning_texts
        assert "--network host" in warning_texts


class TestSafeConfigForLog:
    """Tests for _safe_config_for_log (verbose 'Local configuration' dump)."""

    @staticmethod
    def _make_config() -> HsyncConfig:
        return cast(
            HsyncConfig,
            {
                "API_KEY": "super-secret-api-key",
                "P2P_KEY": "super-secret-lan-key",
                "REQUEST_HEADERS": {
                    "accept": "application/json",
                    "Authorization": "Bearer super-secret-api-key",
                    "X-Client-Name": CLIENT_NAME,
                    "X-Client-Version": HSYNC_VERSION,
                    "X-Protocol-Version": "1",
                },
                "LOCAL_BUCKET": "/tmp/bucket",
            },
        )

    def test_masks_secrets(self):
        """API_KEY, P2P_KEY and the Authorization header are redacted."""
        safe = _safe_config_for_log(self._make_config(), None)
        assert safe["API_KEY"] == "****"
        assert safe["P2P_KEY"] == "****"
        assert safe["REQUEST_HEADERS"]["Authorization"] == "Bearer ****"

    def test_paths_section_resolves_env_overrides(self, tmp_path, monkeypatch):
        """PATHS reflects HSYNC_* overrides and the env file argument."""
        device = tmp_path / "device"
        state = tmp_path / "state"
        locks = tmp_path / "locks"
        monkeypatch.setenv("HSYNC_DEVICE_DIR", str(device))
        monkeypatch.setenv("HSYNC_STATE_DIR", str(state))
        monkeypatch.setenv("HSYNC_LOCKS_DIR", str(locks))

        safe = _safe_config_for_log(self._make_config(), "/tmp/.env")

        assert safe["PATHS"]["ENV_FILE"] == "/tmp/.env"
        assert safe["PATHS"]["DEVICE_DIR"] == str(device)
        assert safe["PATHS"]["STATE_DIR"] == str(state)
        # No HSYNC_STATE_DB override: the per-folder layout has no single path.
        assert safe["PATHS"]["STATE_DB"] == ""
        assert safe["PATHS"]["LOCKS_DIR"] == str(locks)

    def test_env_file_none_when_omitted(self):
        """ENV_FILE is None when no --env-file was passed."""
        safe = _safe_config_for_log(self._make_config(), None)
        assert safe["PATHS"]["ENV_FILE"] is None


class TestEvaluateServerTrust:
    """Server TLS trust resolution at connect time (ADR-0036)."""

    @staticmethod
    def _config(policy, *, fingerprints=()):
        from hsync_common.protocol.tls_trust import TrustPolicy

        assert isinstance(policy, TrustPolicy)
        return {
            "SERVER_TRUST": policy,
            "SERVER_FINGERPRINTS": tuple(fingerprints),
            "HTTP_TIMEOUT": 5,
        }

    def test_chain_returns_no_pin_without_probing(self, monkeypatch):
        from hsync_common.protocol.tls_trust import TrustPolicy

        import hsync_client.main_logic as ml

        monkeypatch.setattr(
            ml, "fetch_server_fingerprint", lambda *a, **k: pytest.fail("must not probe")
        )
        assert (
            ml._evaluate_server_trust(self._config(TrustPolicy.chain), MagicMock(), "h", 443)
            is None
        )

    def test_accept_new_pins_the_first_sighting(self, tmp_path, monkeypatch):
        import hsync_client.main_logic as ml
        from hsync_client.server_trust import ServerTrustStore

        store = ServerTrustStore(tmp_path / "servers.json")
        monkeypatch.setattr(ml, "default_server_trust_store", lambda: store)
        monkeypatch.setattr(ml, "fetch_server_fingerprint", lambda *a, **k: "a" * 64)
        from hsync_common.protocol.tls_trust import TrustPolicy

        assert (
            ml._evaluate_server_trust(self._config(TrustPolicy.accept_new), MagicMock(), "h", 443)
            == "a" * 64
        )
        assert store.get("h", 443) == "a" * 64

    def test_matched_pin_proceeds(self, tmp_path, monkeypatch):
        import hsync_client.main_logic as ml
        from hsync_client.server_trust import ServerTrustStore

        store = ServerTrustStore(tmp_path / "servers.json")
        store.set("h", 443, "a" * 64, source="cli")
        monkeypatch.setattr(ml, "default_server_trust_store", lambda: store)
        monkeypatch.setattr(ml, "fetch_server_fingerprint", lambda *a, **k: "a" * 64)
        from hsync_common.protocol.tls_trust import TrustPolicy

        assert (
            ml._evaluate_server_trust(self._config(TrustPolicy.ask), MagicMock(), "h", 443)
            == "a" * 64
        )

    def test_changed_pin_is_refused_and_emits_fingerprint_changed(self, tmp_path, monkeypatch):
        import hsync_client.main_logic as ml
        from hsync_client.server_trust import ServerTrustStore

        store = ServerTrustStore(tmp_path / "servers.json")
        store.set("h", 443, "b" * 64, source="cli")
        monkeypatch.setattr(ml, "default_server_trust_store", lambda: store)
        monkeypatch.setattr(ml, "fetch_server_fingerprint", lambda *a, **k: "a" * 64)
        emitted: list[dict] = []
        monkeypatch.setattr(
            ml, "emit_event", lambda name, **kw: emitted.append({"name": name, **kw})
        )
        from hsync_common.protocol.tls_trust import TrustPolicy

        with pytest.raises(ml.ServerTrustError):
            ml._evaluate_server_trust(self._config(TrustPolicy.ask), MagicMock(), "h", 443)
        assert emitted and emitted[0]["scope"] == "server"
        assert emitted[0]["name"] == "fingerprint_changed"

    def test_strict_unknown_is_refused_and_emits_pairing_needed(self, tmp_path, monkeypatch):
        import hsync_client.main_logic as ml
        from hsync_client.server_trust import ServerTrustStore

        monkeypatch.setattr(
            ml, "default_server_trust_store", lambda: ServerTrustStore(tmp_path / "s.json")
        )
        monkeypatch.setattr(ml, "fetch_server_fingerprint", lambda *a, **k: "a" * 64)
        emitted: list[dict] = []
        monkeypatch.setattr(
            ml, "emit_event", lambda name, **kw: emitted.append({"name": name, **kw})
        )
        from hsync_common.protocol.tls_trust import TrustPolicy

        with pytest.raises(ml.ServerTrustError):
            ml._evaluate_server_trust(self._config(TrustPolicy.strict), MagicMock(), "h", 443)
        assert emitted[0]["name"] == "pairing_needed"

    def test_ask_without_a_terminal_fails_closed(self, tmp_path, monkeypatch):
        import hsync_client.main_logic as ml
        from hsync_client.server_trust import ServerTrustStore

        monkeypatch.setattr(
            ml, "default_server_trust_store", lambda: ServerTrustStore(tmp_path / "s.json")
        )
        monkeypatch.setattr(ml, "fetch_server_fingerprint", lambda *a, **k: "a" * 64)
        monkeypatch.setattr(ml, "build_trust_prompt", lambda *a, **k: None)
        from hsync_common.protocol.tls_trust import TrustPolicy

        with pytest.raises(ml.ServerTrustError):
            ml._evaluate_server_trust(self._config(TrustPolicy.ask), MagicMock(), "h", 443)

    def test_ask_prompt_acceptance_pins(self, tmp_path, monkeypatch):
        import hsync_client.main_logic as ml
        from hsync_client.server_trust import ServerTrustStore

        store = ServerTrustStore(tmp_path / "servers.json")
        monkeypatch.setattr(ml, "default_server_trust_store", lambda: store)
        monkeypatch.setattr(ml, "fetch_server_fingerprint", lambda *a, **k: "a" * 64)
        monkeypatch.setattr(ml, "build_trust_prompt", lambda *a, **k: lambda h, p, f: True)
        from hsync_common.protocol.tls_trust import TrustPolicy

        assert (
            ml._evaluate_server_trust(self._config(TrustPolicy.ask), MagicMock(), "h", 443)
            == "a" * 64
        )
        assert store.entries()[0]["source"] == "prompt"

    def test_explicit_pin_match_is_recorded(self, tmp_path, monkeypatch):
        import hsync_client.main_logic as ml
        from hsync_client.server_trust import ServerTrustStore

        store = ServerTrustStore(tmp_path / "servers.json")
        monkeypatch.setattr(ml, "default_server_trust_store", lambda: store)
        monkeypatch.setattr(ml, "fetch_server_fingerprint", lambda *a, **k: "a" * 64)
        from hsync_common.protocol.tls_trust import TrustPolicy

        assert (
            ml._evaluate_server_trust(
                self._config(TrustPolicy.strict, fingerprints=("a" * 64,)), MagicMock(), "h", 443
            )
            == "a" * 64
        )
        assert store.get("h", 443) == "a" * 64

    def test_explicit_pin_mismatch_is_refused(self, tmp_path, monkeypatch):
        import hsync_client.main_logic as ml
        from hsync_client.server_trust import ServerTrustStore

        monkeypatch.setattr(
            ml, "default_server_trust_store", lambda: ServerTrustStore(tmp_path / "s.json")
        )
        monkeypatch.setattr(ml, "fetch_server_fingerprint", lambda *a, **k: "a" * 64)
        from hsync_common.protocol.tls_trust import TrustPolicy

        with pytest.raises(ml.ServerTrustError):
            ml._evaluate_server_trust(
                self._config(TrustPolicy.strict, fingerprints=("b" * 64,)), MagicMock(), "h", 443
            )


class TestStartupErrorVisibility:
    """Startup failures must reach the Desktop as structured events.

    A config error exits *normally* with code 1; the Desktop cannot tell that
    apart from a clean stop by the exit status alone, so it relies on an
    ``error`` event with ``reason="startup"`` (and on a ``ready`` marker for the
    successful path).
    """

    @staticmethod
    def _drain(read_fd: int) -> list[dict]:
        data = b""
        while True:
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            data += chunk
        return [
            event
            for event in (parse_event_line(line) for line in data.decode().splitlines())
            if event is not None
        ]

    @staticmethod
    def _close(read_fd: int, write_fd: int) -> None:
        os.close(read_fd)
        init_emitter(None)
        try:
            os.close(write_fd)
        except OSError:
            pass

    def test_missing_server_url_emits_startup_error(self, tmp_path):
        """A missing server URL is reported as an ``error(reason=startup)`` event."""
        read_fd, write_fd = os.pipe()
        kwargs = _default_run_sync_kwargs(tmp_path, server_url=None, events_json_fd=write_fd)
        try:
            with (
                patch("hsync_client.main_logic.setup_logging") as mock_log_setup,
                patch("hsync_client.main_logic.get_server_url", return_value=None),
            ):
                mock_log_setup.return_value = MagicMock()
                with pytest.raises(SystemExit) as exc_info:
                    run_sync(**kwargs)
            get_emitter().close()
            captured = self._drain(read_fd)
        finally:
            self._close(read_fd, write_fd)

        assert exc_info.value.code == 1
        errors = [event for event in captured if event["event"] == "error"]
        assert errors and errors[-1]["reason"] == "startup"
        assert "Server URL is required" in errors[-1]["message"]
        assert not any(event["event"] == "ready" for event in captured)

    def test_missing_api_key_emits_startup_error(self, tmp_path):
        """A missing API key in server mode is reported as a startup error."""
        read_fd, write_fd = os.pipe()
        kwargs = _default_run_sync_kwargs(
            tmp_path, server_url="https://sync.example.test", events_json_fd=write_fd
        )
        try:
            with (
                patch("hsync_client.main_logic.setup_logging") as mock_log_setup,
                patch("hsync_client.main_logic.get_api_key", return_value=""),
            ):
                mock_log_setup.return_value = MagicMock()
                with pytest.raises(SystemExit) as exc_info:
                    run_sync(**kwargs)
            get_emitter().close()
            captured = self._drain(read_fd)
        finally:
            self._close(read_fd, write_fd)

        assert exc_info.value.code == 1
        errors = [event for event in captured if event["event"] == "error"]
        assert errors and errors[-1]["reason"] == "startup"
        assert "No API key configured" in errors[-1]["message"]

    def test_ready_is_emitted_on_a_successful_startup(self, tmp_path):
        """The explicit readiness marker is emitted once validation passes."""
        read_fd, write_fd = os.pipe()
        kwargs = _default_run_sync_kwargs(
            tmp_path,
            server_url=None,
            p2p_mode=True,
            p2p_allow_unauthenticated=True,
            peer_rendezvous_timeout=None,
            sync_direction="download-only",
            events_json_fd=write_fd,
        )
        try:
            with (
                patch("hsync_client.main_logic.setup_logging") as mock_log_setup,
                patch("hsync_client.main_logic.get_server_url", return_value=None),
                patch("hsync_client.main_logic.get_peer_rendezvous_timeout", return_value=None),
                patch("hsync_client.p2p_handler.run_p2p_mode") as mock_p2p,
                patch("hsync_client.main_logic.set_config"),
                patch("hsync_client.main_logic.set_debug"),
                patch("hsync_client.main_logic.is_tty_interactive", return_value=True),
            ):
                mock_p2p.return_value = 0
                mock_log_setup.return_value = MagicMock()
                with pytest.raises(SystemExit) as exc_info:
                    run_sync(**kwargs)
            get_emitter().close()
            captured = self._drain(read_fd)
        finally:
            self._close(read_fd, write_fd)

        assert exc_info.value.code == 0
        names = [event["event"] for event in captured]
        assert "ready" in names
        assert not any(event.get("reason") == "startup" for event in captured)

    def test_instance_lock_refusal_is_a_startup_error_before_ready(self, tmp_path):
        """A second process on the same folder is a startup failure, not a stop.

        The instance lock is the last gate before the run loop, so its refusal
        must reach the Desktop as ``error(reason=startup)`` *and* happen before
        ``ready``: the GUI treats an exit after the marker as an ordinary stop
        and would leave the reason in the log only.
        """
        read_fd, write_fd = os.pipe()
        kwargs = _default_run_sync_kwargs(
            tmp_path,
            server_url=None,
            p2p_mode=True,
            p2p_allow_unauthenticated=True,
            peer_rendezvous_timeout=None,
            sync_direction="download-only",
            events_json_fd=write_fd,
        )
        refusal = (
            f"Another hsync process is already syncing '{tmp_path}' (pid 42). "
            "Refusing to start: two processes on the same directory would "
            "corrupt each other's sync state."
        )
        try:
            with (
                patch("hsync_client.main_logic.setup_logging") as mock_log_setup,
                patch("hsync_client.main_logic.get_server_url", return_value=None),
                patch("hsync_client.main_logic.get_peer_rendezvous_timeout", return_value=None),
                patch("hsync_client.main_logic.set_config"),
                patch("hsync_client.main_logic.set_debug"),
                patch("hsync_client.main_logic.is_tty_interactive", return_value=True),
                patch(
                    "hsync_client.identity.InstanceLock.acquire",
                    side_effect=InstanceLockError(refusal),
                ),
            ):
                mock_log_setup.return_value = MagicMock()
                with pytest.raises(SystemExit) as exc_info:
                    run_sync(**kwargs)
            get_emitter().close()
            captured = self._drain(read_fd)
        finally:
            self._close(read_fd, write_fd)

        assert exc_info.value.code == 1
        errors = [event for event in captured if event["event"] == "error"]
        assert errors and errors[-1]["reason"] == "startup"
        assert errors[-1]["message"] == refusal
        # The readiness marker must never precede the refusal: the client would
        # look "running" and its exit like a clean stop.
        assert not any(event["event"] == "ready" for event in captured)
