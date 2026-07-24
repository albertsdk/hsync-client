"""
Tests for hsync_client.config module.

Tests configuration management: set/get config, debug mode, env getters, load_env_file.
"""

import os
import tempfile

import pytest

from hsync_client.config import (
    EnvSettings,
    get_api_key,
    get_env_settings,
    get_resync,
    get_server_url,
    is_debug,
    load_env_file,
    set_debug,
)


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

    def test_lan_key_is_preserved(self):
        """lan_key is always preserved (the LAN transport is HTTPS-only)."""
        get_env_settings.cache_clear()
        os.environ["HSYNC_LAN_KEY"] = "shared-secret"
        try:
            settings = EnvSettings()
            assert settings.lan_key is not None
            assert settings.lan_key.get_secret_value() == "shared-secret"
        finally:
            del os.environ["HSYNC_LAN_KEY"]
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


class TestGetResync:
    """Tests for get_resync env getter (periodic resync)."""

    def test_returns_none_when_unset(self):
        """Unset HSYNC_RESYNC yields None so the caller applies the default."""
        get_env_settings.cache_clear()
        old = os.environ.pop("HSYNC_RESYNC", None)
        try:
            get_env_settings.cache_clear()
            assert get_resync() is None
        finally:
            if old is not None:
                os.environ["HSYNC_RESYNC"] = old
            get_env_settings.cache_clear()

    def test_returns_configured_interval(self):
        """A positive HSYNC_RESYNC is returned as an int."""
        get_env_settings.cache_clear()
        old = os.environ.get("HSYNC_RESYNC")
        os.environ["HSYNC_RESYNC"] = "120"
        try:
            get_env_settings.cache_clear()
            assert get_resync() == 120
        finally:
            if old is not None:
                os.environ["HSYNC_RESYNC"] = old
            else:
                os.environ.pop("HSYNC_RESYNC", None)
            get_env_settings.cache_clear()

    def test_zero_is_preserved_as_disabled(self):
        """HSYNC_RESYNC=0 is preserved (0 explicitly disables periodic resync)."""
        get_env_settings.cache_clear()
        old = os.environ.get("HSYNC_RESYNC")
        os.environ["HSYNC_RESYNC"] = "0"
        try:
            get_env_settings.cache_clear()
            assert get_resync() == 0
        finally:
            if old is not None:
                os.environ["HSYNC_RESYNC"] = old
            else:
                os.environ.pop("HSYNC_RESYNC", None)
            get_env_settings.cache_clear()
