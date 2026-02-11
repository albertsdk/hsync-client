"""
Tests for hsync_client.main_logic module.

Tests _get_stability_delay_value and other testable functions.
"""

import os
from unittest.mock import patch, MagicMock

import pytest

from hsync_client.config import get_env_settings
from hsync_client.constants import FILE_STABILITY_INTERVAL_DEFAULT
from hsync_client.main_logic import _get_stability_delay_value


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

