"""
Tests for hsync_client.main_logic module.

Tests _get_stability_delay_value and other testable functions.
"""

import os
import time
from typing import Optional, cast
from unittest.mock import patch, MagicMock, AsyncMock

from typing_extensions import TypedDict, Unpack

import pytest

from hsync_client.config import HsyncConfig, get_env_settings
from hsync_client.constants import FILE_STABILITY_INTERVAL_DEFAULT
from hsync_client.main_logic import (
    _get_stability_delay_value,
    _check_server_connectivity,
    _measure_clock_offset,
    _run_preview,
    run_sync,
    _with_stdin_commands,
    _run_stdin_wait,
    _run_lan_mode,
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

    def test_invalid_env_value_falls_back_to_default(self):
        """Invalid (non-integer) env value falls back to default."""
        with patch("hsync_client.main_logic.get_stability_delay", return_value="not_a_number"):
            result = _get_stability_delay_value(None)
            assert result == FILE_STABILITY_INTERVAL_DEFAULT

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


class TestCheckServerConnectivity:
    """Tests for _check_server_connectivity function."""

    def test_successful_connection(self):
        """Test successful server connection."""
        mock_logger = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch('requests.get', return_value=mock_response):
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

        with patch('requests.get', return_value=mock_response):
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

        with patch('requests.get', return_value=mock_response):
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

        with patch('requests.get', side_effect=RequestException("Connection refused")):
            result = _check_server_connectivity(
                server_url="http://localhost:8000",
                timeout=10,
                verify_ssl=True,
                logger=mock_logger,
            )

            assert result is False
            mock_logger.error.assert_called()

    def test_successful_with_ssl_disabled(self):
        """Test successful connection with SSL verification disabled."""
        mock_logger = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch('requests.get', return_value=mock_response) as mock_get:
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
            assert call_kwargs['verify'] is False

    def test_status_code_404_is_ok(self):
        """Test that 404 is considered successful (server is reachable)."""
        mock_logger = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch('requests.get', return_value=mock_response):
            result = _check_server_connectivity(
                server_url="http://localhost:8000",
                timeout=10,
                verify_ssl=True,
                logger=mock_logger,
            )

            # 404 is < 500, so it's considered successful
            assert result is True


def _make_clock_config() -> HsyncConfig:
    """Return a minimal HsyncConfig for _measure_clock_offset tests."""
    return cast(HsyncConfig, {
        "REQUEST_HEADERS": {"Authorization": "Bearer test"},
        "HTTP_TIMEOUT": 30,
    })


class TestMeasureClockOffset:
    """Tests for _measure_clock_offset function."""

    def test_successful_clock_sync(self):
        """Test successful clock offset measurement."""
        mock_logger = MagicMock()
        mock_master = MagicMock()
        mock_master.get_server_url.return_value = "http://192.168.1.100:8765"
        mock_master.cid = "master-123"

        config = _make_clock_config()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"timestamp": time.time()}

        with patch('requests.get', return_value=mock_response), \
             patch('hsync_client.utils.get_ssl_verify', return_value=True):
            _measure_clock_offset(mock_master, config, mock_logger)

            # Should set clock_offset on master
            assert hasattr(mock_master, 'clock_offset')

    def test_clock_skew_warning(self):
        """Test warning when clock skew exceeds threshold."""
        mock_logger = MagicMock()
        mock_master = MagicMock()
        mock_master.get_server_url.return_value = "http://192.168.1.100:8765"
        mock_master.cid = "master-123"

        config = _make_clock_config()

        # Simulate large clock offset (>60 seconds)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"timestamp": time.time() + 100}

        with patch('requests.get', return_value=mock_response), \
             patch('hsync_client.utils.get_ssl_verify', return_value=True):
            _measure_clock_offset(mock_master, config, mock_logger)

            # Should log warning
            mock_logger.warning.assert_called()

    def test_non_200_response(self):
        """Test handling non-200 response."""
        mock_logger = MagicMock()
        mock_master = MagicMock()
        mock_master.get_server_url.return_value = "http://192.168.1.100:8765"

        config = _make_clock_config()

        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch('requests.get', return_value=mock_response), \
             patch('hsync_client.utils.get_ssl_verify', return_value=True):
            _measure_clock_offset(mock_master, config, mock_logger)

            # Should log debug message and return early
            mock_logger.debug.assert_called()

    def test_missing_timestamp_in_response(self):
        """Test handling response without timestamp."""
        mock_logger = MagicMock()
        mock_master = MagicMock()
        mock_master.get_server_url.return_value = "http://192.168.1.100:8765"

        config = _make_clock_config()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}  # No timestamp

        with patch('requests.get', return_value=mock_response), \
             patch('hsync_client.utils.get_ssl_verify', return_value=True):
            _measure_clock_offset(mock_master, config, mock_logger)

            # Should log debug and return
            mock_logger.debug.assert_called()

    def test_request_exception(self):
        """Test handling network error."""
        from requests import RequestException

        mock_logger = MagicMock()
        mock_master = MagicMock()
        mock_master.get_server_url.return_value = "http://192.168.1.100:8765"

        config = _make_clock_config()

        with patch('requests.get', side_effect=RequestException("Network error")), \
             patch('hsync_client.utils.get_ssl_verify', return_value=True):
            _measure_clock_offset(mock_master, config, mock_logger)

            # Should log debug message
            mock_logger.debug.assert_called()

    def test_invalid_json_response(self):
        """Test handling invalid JSON response."""
        mock_logger = MagicMock()
        mock_master = MagicMock()
        mock_master.get_server_url.return_value = "http://192.168.1.100:8765"

        config = _make_clock_config()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.side_effect = ValueError("Invalid JSON")

        with patch('requests.get', return_value=mock_response), \
             patch('hsync_client.utils.get_ssl_verify', return_value=True):
            _measure_clock_offset(mock_master, config, mock_logger)

            # Should log debug message
            mock_logger.debug.assert_called()


class TestRunPreview:
    """Tests for _run_preview function."""

    def test_preview_with_no_files(self):
        """Test preview with no files to sync."""
        mock_logger = MagicMock()

        config = {
            "LAN_MODE": False,
        }

        with patch('hsync_client.main_logic.CONFIG', config), \
             patch('hsync_client.main_logic.ensure_hsynckeep_files'), \
             patch('hsync_client.main_logic.get_files_local', return_value=[]), \
             patch('hsync_client.main_logic.get_files_remote', return_value=[]), \
             patch('hsync_client.main_logic.get_sync_actions', return_value=[]):
            _run_preview(mock_logger)

            # Should log preview start and end
            assert any("Preview" in str(call) for call in mock_logger.info.call_args_list)

    def test_preview_with_downloads(self, tmp_path):
        """Test preview showing files to download."""
        from hsync_client.models.file import File
        from hsync_client.models.sync_action import SyncAction, SyncActionName

        mock_logger = MagicMock()

        config = {
            "LAN_MODE": False,
        }

        # Create a download action
        file_obj = File(str(tmp_path / "test.txt"))
        file_obj.size = 1024
        download_action = SyncAction(file_obj, SyncActionName.download)

        with patch('hsync_client.main_logic.CONFIG', config), \
             patch('hsync_client.main_logic.ensure_hsynckeep_files'), \
             patch('hsync_client.main_logic.get_files_local', return_value=[]), \
             patch('hsync_client.main_logic.get_files_remote', return_value=[]), \
             patch('hsync_client.main_logic.get_sync_actions', return_value=[download_action]):
            _run_preview(mock_logger)

            # Should log download action
            assert any("TO_DOWNLOAD" in str(call) for call in mock_logger.info.call_args_list)

    def test_preview_with_uploads(self, tmp_path):
        """Test preview showing files to upload."""
        from hsync_client.models.file import File
        from hsync_client.models.sync_action import SyncAction, SyncActionName

        mock_logger = MagicMock()

        config = {
            "LAN_MODE": False,
        }

        # Create an upload action
        file_obj = File(str(tmp_path / "upload.txt"))
        file_obj.size = 2048
        upload_action = SyncAction(file_obj, SyncActionName.upload)

        with patch('hsync_client.main_logic.CONFIG', config), \
             patch('hsync_client.main_logic.ensure_hsynckeep_files'), \
             patch('hsync_client.main_logic.get_files_local', return_value=[]), \
             patch('hsync_client.main_logic.get_files_remote', return_value=[]), \
             patch('hsync_client.main_logic.get_sync_actions', return_value=[upload_action]):
            _run_preview(mock_logger)

            # Should log upload action
            assert any("TO_UPLOAD" in str(call) for call in mock_logger.info.call_args_list)

    def test_preview_with_deletes(self, tmp_path):
        """Test preview showing files to delete."""
        from hsync_client.models.file import File
        from hsync_client.models.sync_action import SyncAction, SyncActionName

        mock_logger = MagicMock()

        config = {
            "LAN_MODE": False,
        }

        # Create a delete action
        file_obj = File(str(tmp_path / "delete.txt"))
        file_obj.size = 512
        delete_action = SyncAction(file_obj, SyncActionName.delete)

        with patch('hsync_client.main_logic.CONFIG', config), \
             patch('hsync_client.main_logic.ensure_hsynckeep_files'), \
             patch('hsync_client.main_logic.get_files_local', return_value=[]), \
             patch('hsync_client.main_logic.get_files_remote', return_value=[]), \
             patch('hsync_client.main_logic.get_sync_actions', return_value=[delete_action]):
            _run_preview(mock_logger)

            # Should log delete action
            assert any("TO_DELETE" in str(call) for call in mock_logger.info.call_args_list)

    def test_preview_with_synced_files(self, tmp_path):
        """Test preview showing files already in sync."""
        from hsync_client.models.file import File
        from hsync_client.models.sync_action import SyncAction, SyncActionName

        mock_logger = MagicMock()

        config = {
            "LAN_MODE": False,
        }

        # Create a nothing action (already synced)
        file_obj = File(str(tmp_path / "synced.txt"))
        file_obj.size = 256
        synced_action = SyncAction(file_obj, SyncActionName.nothing)

        with patch('hsync_client.main_logic.CONFIG', config), \
             patch('hsync_client.main_logic.ensure_hsynckeep_files'), \
             patch('hsync_client.main_logic.get_files_local', return_value=[]), \
             patch('hsync_client.main_logic.get_files_remote', return_value=[]), \
             patch('hsync_client.main_logic.get_sync_actions', return_value=[synced_action]):
            _run_preview(mock_logger)

            # Should log synced file
            assert any("SYNCED" in str(call) for call in mock_logger.info.call_args_list)

    def test_preview_detects_renames(self, tmp_path):
        """Test preview detecting rename operations."""
        from hsync_client.models.file import File
        from hsync_client.models.sync_action import SyncAction, SyncActionName

        mock_logger = MagicMock()

        config = {
            "LAN_MODE": False,
        }

        # Create delete and download actions with same md5sum (rename)
        old_file = File(str(tmp_path / "old.txt"))
        old_file.size = 1024
        old_file.md5sum = "abc123"
        delete_action = SyncAction(old_file, SyncActionName.delete)

        new_file = File(str(tmp_path / "new.txt"))
        new_file.size = 1024
        new_file.md5sum = "abc123"
        download_action = SyncAction(new_file, SyncActionName.download)

        with patch('hsync_client.main_logic.CONFIG', config), \
             patch('hsync_client.main_logic.ensure_hsynckeep_files'), \
             patch('hsync_client.main_logic.get_files_local', return_value=[]), \
             patch('hsync_client.main_logic.get_files_remote', return_value=[]), \
             patch('hsync_client.main_logic.get_sync_actions', return_value=[delete_action, download_action]):
            _run_preview(mock_logger)

            # Should log rename action
            assert any("TO_RENAME" in str(call) for call in mock_logger.info.call_args_list)

    def test_preview_skips_sentinel_files(self, tmp_path):
        """Test preview skipping .hsynckeep sentinel files."""
        from hsync_client.models.file import File
        from hsync_client.models.sync_action import SyncAction, SyncActionName

        mock_logger = MagicMock()

        config = {
            "LAN_MODE": False,
        }

        # Create action for sentinel file
        sentinel_file = File(str(tmp_path / ".hsynckeep"))
        sentinel_file.size = 0
        sentinel_action = SyncAction(sentinel_file, SyncActionName.upload)

        with patch('hsync_client.main_logic.CONFIG', config), \
             patch('hsync_client.main_logic.ensure_hsynckeep_files'), \
             patch('hsync_client.main_logic.get_files_local', return_value=[]), \
             patch('hsync_client.main_logic.get_files_remote', return_value=[]), \
             patch('hsync_client.main_logic.get_sync_actions', return_value=[sentinel_action]):
            _run_preview(mock_logger)

            # Should not log sentinel file in upload list
            upload_calls = [str(call) for call in mock_logger.info.call_args_list if "TO_UPLOAD" in str(call)]
            assert len(upload_calls) == 0

    def test_preview_handles_exception(self):
        """Test preview handling exceptions gracefully."""
        mock_logger = MagicMock()

        config = {
            "LAN_MODE": False,
        }

        with patch('hsync_client.main_logic.CONFIG', config), \
             patch('hsync_client.main_logic.ensure_hsynckeep_files', side_effect=Exception("Test error")):
            _run_preview(mock_logger)

            # Should log error
            mock_logger.error.assert_called()

    def test_preview_summary(self, tmp_path):
        """Test preview showing summary statistics."""
        from hsync_client.models.file import File
        from hsync_client.models.sync_action import SyncAction, SyncActionName

        mock_logger = MagicMock()

        config = {
            "LAN_MODE": False,
        }

        # Create various actions
        actions = [
            SyncAction(File(str(tmp_path / "dl.txt")), SyncActionName.download),
            SyncAction(File(str(tmp_path / "ul.txt")), SyncActionName.upload),
            SyncAction(File(str(tmp_path / "sync.txt")), SyncActionName.nothing),
        ]

        for action in actions:
            action.file.size = 100

        with patch('hsync_client.main_logic.CONFIG', config), \
             patch('hsync_client.main_logic.ensure_hsynckeep_files'), \
             patch('hsync_client.main_logic.get_files_local', return_value=[]), \
             patch('hsync_client.main_logic.get_files_remote', return_value=[]), \
             patch('hsync_client.main_logic.get_sync_actions', return_value=actions):
            _run_preview(mock_logger)

            # Should log summary with counts
            summary_calls = [str(call) for call in mock_logger.info.call_args_list if "Preview:" in str(call)]
            assert len(summary_calls) > 0



# ── Helper for run_sync tests ────────────────────────────────────────


class _RunSyncKwargs(TypedDict, total=False):
    bucket: str
    env_file: Optional[str]
    server_url: Optional[str]
    verify_ssl: bool
    ca_cert: Optional[str]
    http_timeout: int
    local_path: str
    continuous: bool
    resync: Optional[int]
    reconnect_delay: int
    stability_delay: Optional[int]
    max_wait_timeout: int
    filter_path: Optional[str]
    dryrun: bool
    ignore_delete: bool
    sync_direction: str
    compression_enabled: bool
    lan_mode: bool
    lan_port: int
    discovery_timeout: int
    lan_group: Optional[str]
    lan_https: bool
    lan_persistent: bool
    force_master: bool
    debug: bool
    quiet: bool
    logfile: Optional[str]
    no_color: bool
    legend: bool
    api_key_fd: Optional[int]
    lan_key_fd: Optional[int]


def _default_run_sync_kwargs(tmp_path: object, **overrides: Unpack[_RunSyncKwargs]) -> _RunSyncKwargs:
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
        "lan_mode": False,
        "lan_port": 0,
        "discovery_timeout": 5,
        "lan_group": None,
        "lan_https": False,
        "lan_persistent": False,
        "force_master": False,
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
        """run_sync returns immediately when the local directory doesn't exist."""
        bad_path = str(tmp_path / "nonexistent")
        kwargs = _default_run_sync_kwargs(tmp_path, local_path=bad_path)

        with patch('hsync_client.main_logic.setup_logging') as mock_log_setup:
            mock_logger = MagicMock()
            mock_log_setup.return_value = mock_logger
            run_sync(**kwargs)
            mock_logger.error.assert_called()
            assert any("does not exist" in str(c) for c in mock_logger.error.call_args_list)

    def test_returns_early_if_no_server_url(self, tmp_path):
        """run_sync returns when no server URL and not in LAN mode."""
        kwargs = _default_run_sync_kwargs(tmp_path, server_url=None)

        with patch('hsync_client.main_logic.setup_logging') as mock_log_setup, \
             patch('hsync_client.main_logic.get_server_url', return_value=None):
            mock_logger = MagicMock()
            mock_log_setup.return_value = mock_logger
            run_sync(**kwargs)
            mock_logger.error.assert_called()
            assert any("Server URL is required" in str(c) for c in mock_logger.error.call_args_list)

    def test_lan_mode_uses_placeholder_url(self, tmp_path):
        """In LAN mode without server URL, a placeholder is used."""
        kwargs = _default_run_sync_kwargs(
            tmp_path, server_url=None, lan_mode=True,
        )

        with patch('hsync_client.main_logic.setup_logging') as mock_log_setup, \
             patch('hsync_client.main_logic.get_server_url', return_value=None), \
             patch('hsync_client.main_logic._run_lan_mode') as mock_lan, \
             patch('hsync_client.main_logic.set_config'), \
             patch('hsync_client.main_logic.set_debug'):
            mock_logger = MagicMock()
            mock_log_setup.return_value = mock_logger
            run_sync(**kwargs)
            # _run_lan_mode should have been called
            mock_lan.assert_called_once()

    def test_invalid_logfile_returns_early(self, tmp_path):
        """run_sync returns when logfile path is invalid."""
        kwargs = _default_run_sync_kwargs(tmp_path, logfile="CON")

        with patch('hsync_client.main_logic.setup_logging') as mock_log_setup:
            mock_logger = MagicMock()
            mock_log_setup.return_value = mock_logger
            run_sync(**kwargs)
            mock_logger.error.assert_called()
            assert any("Invalid output file" in str(c) for c in mock_logger.error.call_args_list)

    def test_server_connectivity_failure_returns(self, tmp_path):
        """run_sync returns when server connectivity check fails."""
        kwargs = _default_run_sync_kwargs(tmp_path)

        with patch('hsync_client.main_logic.setup_logging') as mock_log_setup, \
             patch('hsync_client.main_logic._check_server_connectivity', return_value=False), \
             patch('hsync_client.main_logic.set_config'), \
             patch('hsync_client.main_logic.set_debug'):
            mock_logger = MagicMock()
            mock_log_setup.return_value = mock_logger
            run_sync(**kwargs)
            # Should not call sync since connectivity failed

    def test_single_sync_mode(self, tmp_path):
        """run_sync runs a single sync when not continuous and not dryrun."""
        kwargs = _default_run_sync_kwargs(tmp_path, continuous=False, dryrun=False)

        with patch('hsync_client.main_logic.setup_logging') as mock_log_setup, \
             patch('hsync_client.main_logic._check_server_connectivity', return_value=True), \
             patch('hsync_client.main_logic.set_config'), \
             patch('hsync_client.main_logic.set_debug'), \
             patch('hsync_client.main_logic.sync', new=MagicMock()), \
             patch('hsync_client.main_logic.run') as mock_run:
            mock_logger = MagicMock()
            mock_log_setup.return_value = mock_logger
            run_sync(**kwargs)
            mock_run.assert_called_once()

    def test_legend_display(self, tmp_path):
        """run_sync displays legend when requested."""
        kwargs = _default_run_sync_kwargs(tmp_path, legend=True)

        with patch('hsync_client.main_logic.setup_logging') as mock_log_setup, \
             patch('hsync_client.main_logic._check_server_connectivity', return_value=True), \
             patch('hsync_client.main_logic.set_config'), \
             patch('hsync_client.main_logic.set_debug'), \
             patch('hsync_client.main_logic.sync', new=MagicMock()), \
             patch('hsync_client.main_logic.run'):
            mock_logger = MagicMock()
            mock_log_setup.return_value = mock_logger
            run_sync(**kwargs)
            assert any("Log legend" in str(c) for c in mock_logger.info.call_args_list)

    def test_api_key_from_fd(self, tmp_path):
        """run_sync reads API key from file descriptor."""
        r_fd, w_fd = os.pipe()
        os.write(w_fd, b"my-secret-key\n")
        os.close(w_fd)

        kwargs = _default_run_sync_kwargs(tmp_path, api_key_fd=r_fd)

        with patch('hsync_client.main_logic.setup_logging') as mock_log_setup, \
             patch('hsync_client.main_logic._check_server_connectivity', return_value=True), \
             patch('hsync_client.main_logic.set_config') as mock_set_cfg, \
             patch('hsync_client.main_logic.set_debug'), \
             patch('hsync_client.main_logic.sync', new=MagicMock()), \
             patch('hsync_client.main_logic.run'):
            mock_logger = MagicMock()
            mock_log_setup.return_value = mock_logger
            run_sync(**kwargs)
            # set_config should have been called with config containing the key
            config_arg = mock_set_cfg.call_args[0][0]
            assert "my-secret-key" in config_arg["REQUEST_HEADERS"]["Authorization"]

    def test_env_file_loading_failure(self, tmp_path):
        """run_sync exits when env file is not found."""
        kwargs = _default_run_sync_kwargs(tmp_path, env_file="/nonexistent/.env")

        with pytest.raises(SystemExit):
            run_sync(**kwargs)


class TestWithStdinCommands:
    """Tests for _with_stdin_commands async function."""

    @pytest.mark.asyncio
    async def test_cancels_stdin_task_when_coro_completes(self):
        """stdin reader task is cancelled after the main coroutine finishes."""
        mock_log = MagicMock()

        async def quick_coro():
            pass

        with patch('hsync_client.main_logic.StdinCommandHandler') as MockHandler:
            handler_inst = MagicMock()
            handler_inst.run = AsyncMock()
            MockHandler.return_value = handler_inst

            await _with_stdin_commands(mock_log, quick_coro())

            # Handler was instantiated and commands registered
            MockHandler.assert_called_once_with(mock_log)
            assert handler_inst.register.call_count == 3  # PREVIEW, SYNC, SET_SYNC_DIRECTION

    @pytest.mark.asyncio
    async def test_cancels_stdin_task_when_coro_raises(self):
        """stdin reader task is cancelled even when the coroutine raises."""
        mock_log = MagicMock()

        async def failing_coro():
            raise RuntimeError("boom")

        with patch('hsync_client.main_logic.StdinCommandHandler') as MockHandler:
            handler_inst = MagicMock()
            handler_inst.run = AsyncMock()
            MockHandler.return_value = handler_inst

            with pytest.raises(RuntimeError, match="boom"):
                await _with_stdin_commands(mock_log, failing_coro())

    @pytest.mark.asyncio
    async def test_registers_preview_sync_and_direction_commands(self):
        """All three commands are registered: PREVIEW, SYNC, SET_SYNC_DIRECTION."""
        mock_log = MagicMock()

        async def noop():
            pass

        with patch('hsync_client.main_logic.StdinCommandHandler') as MockHandler:
            handler_inst = MagicMock()
            handler_inst.run = AsyncMock()
            MockHandler.return_value = handler_inst

            await _with_stdin_commands(mock_log, noop())

            registered_cmds = [c[0][0] for c in handler_inst.register.call_args_list]
            assert "PREVIEW" in registered_cmds
            assert "SYNC" in registered_cmds
            assert "SET_SYNC_DIRECTION" in registered_cmds


class TestRunStdinWait:
    """Tests for _run_stdin_wait async function."""

    @pytest.mark.asyncio
    async def test_registers_preview_and_sync(self):
        """_run_stdin_wait registers PREVIEW and SYNC commands."""
        mock_log = MagicMock()

        with patch('hsync_client.main_logic.StdinCommandHandler') as MockHandler:
            handler_inst = MagicMock()
            handler_inst.run = AsyncMock()
            MockHandler.return_value = handler_inst

            await _run_stdin_wait(mock_log)

            registered_cmds = [c[0][0] for c in handler_inst.register.call_args_list]
            assert "PREVIEW" in registered_cmds
            assert "SYNC" in registered_cmds

    @pytest.mark.asyncio
    async def test_calls_handler_run(self):
        """_run_stdin_wait calls handler.run() to read stdin."""
        mock_log = MagicMock()

        with patch('hsync_client.main_logic.StdinCommandHandler') as MockHandler:
            handler_inst = MagicMock()
            handler_inst.run = AsyncMock()
            MockHandler.return_value = handler_inst

            await _run_stdin_wait(mock_log)

            handler_inst.run.assert_awaited_once()


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

        with patch('hsync_client.main_logic.LANSync', return_value=mock_lan_sync):
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

        with patch('hsync_client.main_logic.LANSync', return_value=mock_lan_sync), \
             patch('hsync_client.main_logic.LANServer', return_value=mock_lan_server), \
             patch('hsync_client.main_logic.LANWebSocketServer', return_value=mock_ws_server), \
             patch('time.sleep', side_effect=KeyboardInterrupt):
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

        with patch('hsync_client.main_logic.LANSync', return_value=mock_lan_sync), \
             patch('hsync_client.main_logic.set_config'), \
             patch('hsync_client.main_logic._measure_clock_offset'), \
             patch('hsync_client.main_logic.sync', new=MagicMock()), \
             patch('hsync_client.main_logic.run') as mock_run:
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

        with patch('hsync_client.main_logic.LANSync', return_value=mock_lan_sync), \
             patch('hsync_client.main_logic.set_config'), \
             patch('hsync_client.main_logic._measure_clock_offset'), \
             patch('hsync_client.main_logic._run_preview') as mock_preview:
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

        with patch('hsync_client.main_logic.LANSync', return_value=mock_lan_sync):
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

        with patch('hsync_client.main_logic.LANSync', return_value=mock_lan_sync), \
             patch('hsync_client.ssl_utils.generate_self_signed_cert', return_value=mock_cert_info) as mock_gen, \
             patch('hsync_client.ssl_utils.save_cert_to_temp_files', return_value=mock_cert_info) as mock_save:
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

        with patch('hsync_client.main_logic.LANSync', return_value=mock_lan_sync), \
             patch('hsync_client.main_logic.set_config'), \
             patch('hsync_client.main_logic._measure_clock_offset'), \
             patch('hsync_client.main_logic.sync', new=MagicMock()), \
             patch('hsync_client.main_logic.run'):
            _run_lan_mode(config, mock_log)

        # wait_for_discovery should be called twice (first loop: not ready, second: ready)
        assert mock_lan_sync.wait_for_discovery.call_count == 2