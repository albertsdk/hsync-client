"""
Tests for hsync_client.main_logic module - Sync functionality.

Contains TestGetStabilityDelayValue, TestCheckServerConnectivity, TestRunSync classes.
"""

import os
from typing import Unpack, cast
from unittest.mock import MagicMock, patch

import pytest
from hsync_common.runtime import ContainerInfo
from typing_extensions import TypedDict

from hsync_client.config import CONFIG, HsyncConfig, get_env_settings
from hsync_client.constants import (
    CLIENT_NAME,
    DISCOVERY_TIMEOUT_DEFAULT,
    FILE_STABILITY_INTERVAL_DEFAULT,
    HEADER_CLIENT_NAME,
    HEADER_CLIENT_VERSION,
    HSYNC_VERSION,
    RESYNC_DEFAULT,
)
from hsync_client.main_logic import (
    _check_server_connectivity,
    _get_discovery_timeout_value,
    _get_resync_value,
    _get_stability_delay_value,
    _safe_config_for_log,
    _warn_if_lan_in_container,
    run_sync,
)
from hsync_client.sync import SyncCycleResult


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


class TestGetDiscoveryTimeoutValue:
    """Tests for _get_discovery_timeout_value function."""

    def test_cli_value_takes_priority(self):
        """CLI argument is used when provided."""
        assert _get_discovery_timeout_value(15) == 15

    def test_cli_zero_means_wait_indefinitely(self):
        """CLI value of 0 is valid and preserved."""
        assert _get_discovery_timeout_value(0) == 0

    def test_env_value_used_when_no_cli(self):
        """Environment variable is used when CLI argument is None."""
        get_env_settings.cache_clear()
        old = os.environ.get("HSYNC_DISCOVERY_TIMEOUT")
        os.environ["HSYNC_DISCOVERY_TIMEOUT"] = "20"
        try:
            get_env_settings.cache_clear()
            assert _get_discovery_timeout_value(None) == 20
        finally:
            if old is not None:
                os.environ["HSYNC_DISCOVERY_TIMEOUT"] = old
            else:
                os.environ.pop("HSYNC_DISCOVERY_TIMEOUT", None)
            get_env_settings.cache_clear()

    def test_env_zero_means_wait_indefinitely(self):
        """Environment value of 0 is valid and preserved."""
        get_env_settings.cache_clear()
        old = os.environ.get("HSYNC_DISCOVERY_TIMEOUT")
        os.environ["HSYNC_DISCOVERY_TIMEOUT"] = "0"
        try:
            get_env_settings.cache_clear()
            assert _get_discovery_timeout_value(None) == 0
        finally:
            if old is not None:
                os.environ["HSYNC_DISCOVERY_TIMEOUT"] = old
            else:
                os.environ.pop("HSYNC_DISCOVERY_TIMEOUT", None)
            get_env_settings.cache_clear()

    def test_default_when_no_cli_no_env(self):
        """Default constant is used when neither CLI nor env is set."""
        get_env_settings.cache_clear()
        old = os.environ.get("HSYNC_DISCOVERY_TIMEOUT")
        os.environ.pop("HSYNC_DISCOVERY_TIMEOUT", None)
        try:
            get_env_settings.cache_clear()
            assert _get_discovery_timeout_value(None) == DISCOVERY_TIMEOUT_DEFAULT
        finally:
            if old is not None:
                os.environ["HSYNC_DISCOVERY_TIMEOUT"] = old
            get_env_settings.cache_clear()

    def test_cli_overrides_env(self):
        """CLI argument overrides environment variable."""
        get_env_settings.cache_clear()
        old = os.environ.get("HSYNC_DISCOVERY_TIMEOUT")
        os.environ["HSYNC_DISCOVERY_TIMEOUT"] = "99"
        try:
            get_env_settings.cache_clear()
            assert _get_discovery_timeout_value(5) == 5
        finally:
            if old is not None:
                os.environ["HSYNC_DISCOVERY_TIMEOUT"] = old
            else:
                os.environ.pop("HSYNC_DISCOVERY_TIMEOUT", None)
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
    ignore_delete: bool
    sync_direction: str
    compression_enabled: bool
    max_bandwidth_upload: int | None
    max_bandwidth_download: int | None
    lan_mode: bool
    lan_port: int
    discovery_timeout: int | None
    lan_group: str | None
    lan_persistent: bool
    force_master: bool
    never_master: bool
    debug: bool
    quiet: bool
    logfile: str | None
    no_color: bool
    legend: bool
    api_key_fd: int | None
    lan_key_fd: int | None


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
        "ignore_delete": False,
        "sync_direction": "bidirectional",
        "compression_enabled": True,
        "max_bandwidth_upload": None,
        "max_bandwidth_download": None,
        "lan_mode": False,
        "lan_port": 0,
        "discovery_timeout": 5,
        "lan_group": None,
        "lan_persistent": False,
        "force_master": False,
        "never_master": False,
        "debug": False,
        "quiet": False,
        "logfile": None,
        "no_color": True,
        "legend": False,
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
        """run_sync exits non-zero when no server URL and not in LAN mode."""
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

    def test_lan_mode_uses_placeholder_url(self, tmp_path):
        """In LAN mode without server URL, a placeholder is used."""
        kwargs = _default_run_sync_kwargs(
            tmp_path,
            server_url=None,
            lan_mode=True,
        )

        with (
            patch("hsync_client.main_logic.setup_logging") as mock_log_setup,
            patch("hsync_client.main_logic.get_server_url", return_value=None),
            patch("hsync_client.main_logic.run_lan_mode") as mock_lan,
            patch("hsync_client.main_logic.set_config"),
            patch("hsync_client.main_logic.set_debug"),
            patch("hsync_client.main_logic.is_tty_interactive", return_value=True),
        ):
            mock_lan.return_value = 0
            mock_logger = MagicMock()
            mock_log_setup.return_value = mock_logger
            with pytest.raises(SystemExit) as exc_info:
                run_sync(**kwargs)
            assert exc_info.value.code == 0
            # run_lan_mode should have been called
            mock_lan.assert_called_once()

    def test_lan_mode_uses_default_discovery_timeout_when_unset(self, tmp_path):
        """LAN discovery timeout defaults to 10 when CLI and env are unset."""
        kwargs = _default_run_sync_kwargs(
            tmp_path,
            server_url=None,
            lan_mode=True,
            discovery_timeout=None,
        )

        with (
            patch("hsync_client.main_logic.setup_logging") as mock_log_setup,
            patch("hsync_client.main_logic.get_server_url", return_value=None),
            patch("hsync_client.main_logic.get_discovery_timeout", return_value=None),
            patch("hsync_client.main_logic.run_lan_mode") as mock_lan,
            patch("hsync_client.main_logic.set_config"),
            patch("hsync_client.main_logic.set_debug"),
            patch("hsync_client.main_logic.is_tty_interactive", return_value=True),
        ):
            mock_lan.return_value = 0
            mock_log_setup.return_value = MagicMock()
            with pytest.raises(SystemExit) as exc_info:
                run_sync(**kwargs)
            assert exc_info.value.code == 0

        config_arg = mock_lan.call_args.args[0]
        assert config_arg["DISCOVERY_TIMEOUT"] == DISCOVERY_TIMEOUT_DEFAULT

    def test_lan_mode_preserves_zero_discovery_timeout(self, tmp_path):
        """LAN discovery timeout of 0 is preserved as wait-indefinitely."""
        kwargs = _default_run_sync_kwargs(
            tmp_path,
            server_url=None,
            lan_mode=True,
            discovery_timeout=0,
        )

        with (
            patch("hsync_client.main_logic.setup_logging") as mock_log_setup,
            patch("hsync_client.main_logic.get_server_url", return_value=None),
            patch("hsync_client.main_logic.run_lan_mode") as mock_lan,
            patch("hsync_client.main_logic.set_config"),
            patch("hsync_client.main_logic.set_debug"),
            patch("hsync_client.main_logic.is_tty_interactive", return_value=True),
        ):
            mock_lan.return_value = 0
            mock_log_setup.return_value = MagicMock()
            with pytest.raises(SystemExit) as exc_info:
                run_sync(**kwargs)
            assert exc_info.value.code == 0

        config_arg = mock_lan.call_args.args[0]
        assert config_arg["DISCOVERY_TIMEOUT"] == 0

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
            mock_run.return_value = SyncCycleResult(
                completed=True, transfers=1, failed=0, conflicts=0, guard_tripped=False
            )
            with pytest.raises(SystemExit) as exc_info:
                run_sync(**kwargs)
            assert exc_info.value.code == 0
            mock_run.assert_called_once()

    def test_legend_display(self, tmp_path):
        """run_sync displays legend when requested."""
        kwargs = _default_run_sync_kwargs(
            tmp_path, legend=True, server_url="https://localhost:8000"
        )

        with (
            patch("hsync_client.main_logic.setup_logging") as mock_log_setup,
            patch("hsync_client.main_logic.get_api_key", return_value="test-key"),
            patch("hsync_client.main_logic._check_server_connectivity", return_value=True),
            patch("hsync_client.main_logic.set_config"),
            patch("hsync_client.main_logic.set_debug"),
            patch("hsync_client.main_logic.sync", new=MagicMock()),
            patch(
                "hsync_client.main_logic.run",
                return_value=SyncCycleResult(
                    completed=True, transfers=0, failed=0, conflicts=0, guard_tripped=False
                ),
            ),
        ):
            mock_logger = MagicMock()
            mock_log_setup.return_value = mock_logger
            with pytest.raises(SystemExit) as exc_info:
                run_sync(**kwargs)
            assert exc_info.value.code == 0
            assert any("Log legend" in str(c) for c in mock_logger.info.call_args_list)

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
                return_value=SyncCycleResult(
                    completed=True, transfers=1, failed=0, conflicts=0, guard_tripped=False
                ),
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

    def test_env_file_loading_failure(self, tmp_path):
        """run_sync exits when env file is not found."""
        kwargs = _default_run_sync_kwargs(tmp_path, env_file="/nonexistent/.env")

        with pytest.raises(SystemExit):
            run_sync(**kwargs)


class TestWarnIfLanInContainer:
    """Tests for _warn_if_lan_in_container (container networking caveat)."""

    def test_no_warning_outside_container(self):
        """On bare metal no container warning is emitted."""
        mock_logger = MagicMock()
        with patch(
            "hsync_client.main_logic.detect_container",
            return_value=ContainerInfo(False),
        ):
            _warn_if_lan_in_container(mock_logger)
        mock_logger.warning.assert_not_called()

    def test_warning_inside_container(self):
        """Inside a container the mDNS/bridge warning suggests host networking."""
        mock_logger = MagicMock()
        with patch(
            "hsync_client.main_logic.detect_container",
            return_value=ContainerInfo(True, "docker", "/.dockerenv"),
        ):
            _warn_if_lan_in_container(mock_logger)
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
                "LAN_KEY": "super-secret-lan-key",
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
        """API_KEY, LAN_KEY and the Authorization header are redacted."""
        safe = _safe_config_for_log(self._make_config(), None)
        assert safe["API_KEY"] == "****"
        assert safe["LAN_KEY"] == "****"
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
        assert safe["PATHS"]["STATE_DB"] == str(state / "syncstate.db")
        assert safe["PATHS"]["LOCKS_DIR"] == str(locks)

    def test_env_file_none_when_omitted(self):
        """ENV_FILE is None when no --env-file was passed."""
        safe = _safe_config_for_log(self._make_config(), None)
        assert safe["PATHS"]["ENV_FILE"] is None
