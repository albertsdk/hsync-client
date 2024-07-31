"""
Tests for hsync_client.config module.

Tests configuration management: set/get config, debug mode, env getters, load_env_file.
"""

import os
import tempfile
from typing import cast

import pytest

from hsync_client.config import (
    CONFIG,
    EnvSettings,
    HsyncConfig,
    get_api_key,
    get_config,
    get_env_settings,
    get_server_url,
    is_debug,
    load_env_file,
    set_config,
    set_debug,
)


class TestSetGetConfig:
    """Tests for set_config and get_config."""

    def test_set_and_get_config(self):
        """set_config updates global CONFIG, get_config retrieves it."""
        original = CONFIG.copy()
        try:
            test_config = cast(HsyncConfig, {"CID": "test123", "DRYRUN": True})
            set_config(test_config)
            result = get_config()
            assert result["CID"] == "test123"
            assert result["DRYRUN"] is True
        finally:
            set_config(original)

    def test_set_config_clears_previous(self):
        """set_config replaces previous values entirely."""
        original = CONFIG.copy()
        try:
            set_config(cast(HsyncConfig, {"CID": "first", "DRYRUN": False}))
            set_config(cast(HsyncConfig, {"CID": "second"}))
            result = get_config()
            assert result["CID"] == "second"
            assert "DRYRUN" not in result
        finally:
            set_config(original)

    def test_get_config_returns_same_object(self):
        """get_config returns the global CONFIG (same reference)."""
        result = get_config()
        assert result is CONFIG


class TestSetDebug:
    """Tests for set_debug and is_debug."""

    def test_set_debug_true(self):
        """set_debug(True) enables debug mode."""
        original = is_debug()
        try:
            set_debug(True)
            assert is_debug() is True
        finally:
            set_debug(original)

    def test_set_debug_false(self):
        """set_debug(False) disables debug mode."""
        original = is_debug()
        try:
            set_debug(False)
            assert is_debug() is False
        finally:
            set_debug(original)


class TestLoadEnvFile:
    """Tests for load_env_file."""

    def test_file_not_found_raises(self):
        """Loading non-existent file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_env_file("/nonexistent/path/to/.env")

    def test_loads_env_vars(self):
        """Environment variables are loaded from file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("HSYNC_TEST_VAR_12345=hello\n")
            f.flush()
            temp_path = f.name

        try:
            # Clear any cached settings
            get_env_settings.cache_clear()
            load_env_file(temp_path)
            # The variable should now be in the environment
            # (load_dotenv with override=False won't overwrite existing)
            val = os.environ.get("HSYNC_TEST_VAR_12345")
            assert val == "hello"
        finally:
            os.unlink(temp_path)
            os.environ.pop("HSYNC_TEST_VAR_12345", None)
            get_env_settings.cache_clear()


class TestEnvSettings:
    """Tests for EnvSettings pydantic model."""

    def test_defaults(self):
        """Default values are correct."""
        get_env_settings.cache_clear()
        settings = EnvSettings()
        assert settings.server is None
        assert settings.api_key.get_secret_value() == ""
        assert settings.stability_delay is None
        assert settings.lan_key is None
        assert settings.lan_group is None
        assert settings.lan_https is True
        assert settings.lan_port is None
        assert settings.discovery_timeout is None
        assert settings.lan_persistent is True
        assert settings.ignore_delete is False
        assert settings.compression_enabled is True

    def test_discovery_timeout_zero_is_valid(self):
        """HSYNC_DISCOVERY_TIMEOUT=0 means wait indefinitely and is valid."""
        get_env_settings.cache_clear()
        old = os.environ.get("HSYNC_DISCOVERY_TIMEOUT")
        os.environ["HSYNC_DISCOVERY_TIMEOUT"] = "0"
        try:
            settings = EnvSettings()
            assert settings.discovery_timeout == 0
        finally:
            if old is not None:
                os.environ["HSYNC_DISCOVERY_TIMEOUT"] = old
            else:
                os.environ.pop("HSYNC_DISCOVERY_TIMEOUT", None)
            get_env_settings.cache_clear()

    def test_reads_from_env(self):
        """Settings are read from HSYNC_ prefixed env vars."""
        get_env_settings.cache_clear()
        os.environ["HSYNC_SERVER"] = "http://test:8000"
        try:
            settings = EnvSettings()
            assert settings.server == "http://test:8000"
        finally:
            del os.environ["HSYNC_SERVER"]
            get_env_settings.cache_clear()

    def test_lan_key_dropped_without_https(self):
        """lan_key is ignored when HTTPS is disabled (no secrets over HTTP)."""
        get_env_settings.cache_clear()
        os.environ["HSYNC_LAN_KEY"] = "shared-secret"
        os.environ["HSYNC_LAN_HTTPS"] = "false"
        try:
            settings = EnvSettings()
            assert settings.lan_key is None
        finally:
            del os.environ["HSYNC_LAN_KEY"]
            del os.environ["HSYNC_LAN_HTTPS"]
            get_env_settings.cache_clear()

    def test_lan_key_kept_with_https(self):
        """lan_key is preserved when HTTPS is enabled."""
        get_env_settings.cache_clear()
        os.environ["HSYNC_LAN_KEY"] = "shared-secret"
        os.environ["HSYNC_LAN_HTTPS"] = "true"
        try:
            settings = EnvSettings()
            assert settings.lan_key is not None
            assert settings.lan_key.get_secret_value() == "shared-secret"
        finally:
            del os.environ["HSYNC_LAN_KEY"]
            del os.environ["HSYNC_LAN_HTTPS"]
            get_env_settings.cache_clear()


class TestEnvGetters:
    """Tests for individual env getter functions."""

    def test_get_server_url_default(self):
        """get_server_url returns None when not set."""
        get_env_settings.cache_clear()
        # Ensure HSYNC_SERVER is not set
        old = os.environ.pop("HSYNC_SERVER", None)
        try:
            get_env_settings.cache_clear()
            assert get_server_url() is None
        finally:
            if old is not None:
                os.environ["HSYNC_SERVER"] = old
            get_env_settings.cache_clear()

    def test_get_api_key_default(self):
        """get_api_key returns empty string when not set."""
        get_env_settings.cache_clear()
        old = os.environ.pop("HSYNC_API_KEY", None)
        try:
            get_env_settings.cache_clear()
            assert get_api_key() == ""
        finally:
            if old is not None:
                os.environ["HSYNC_API_KEY"] = old
            get_env_settings.cache_clear()
