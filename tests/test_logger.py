"""
Tests for hsync_client.logger module.

Tests the loguru-based logging configuration.
"""

import tempfile
import os
from unittest.mock import patch

import pytest

from hsync_client.logger import (
    LEVEL_STYLES,
    SYMBOL_STYLES,
    setup_logging,
    get_client_id,
    set_client_id,
    _colorize_symbols,
)


class TestLevelStyles:
    """Tests for LEVEL_STYLES mapping with loguru color tags."""

    def test_debug_style(self):
        """Test DEBUG level style uses dim tag."""
        assert LEVEL_STYLES["DEBUG"] == ("<dim>", "</dim>")

    def test_info_style(self):
        """Test INFO level style has no color (default)."""
        assert LEVEL_STYLES["INFO"] == ("", "")

    def test_success_style(self):
        """Test SUCCESS level style uses green tag."""
        assert LEVEL_STYLES["SUCCESS"] == ("<green>", "</green>")

    def test_warning_style(self):
        """Test WARNING level style uses yellow tag."""
        assert LEVEL_STYLES["WARNING"] == ("<yellow>", "</yellow>")

    def test_error_style(self):
        """Test ERROR level style uses red tag."""
        assert LEVEL_STYLES["ERROR"] == ("<red>", "</red>")

    def test_critical_style(self):
        """Test CRITICAL level style uses bold red tag."""
        assert LEVEL_STYLES["CRITICAL"] == ("<bold><red>", "</red></bold>")


class TestSymbolStyles:
    """Tests for SYMBOL_STYLES mapping with loguru color tags."""

    def test_download_symbol(self):
        """Test download symbol uses cyan tag."""
        assert SYMBOL_STYLES["↓"] == ("<cyan>", "</cyan>")

    def test_upload_symbol(self):
        """Test upload symbol uses green tag."""
        assert SYMBOL_STYLES["↑"] == ("<green>", "</green>")

    def test_move_symbol(self):
        """Test move symbol uses blue tag."""
        assert SYMBOL_STYLES["→"] == ("<blue>", "</blue>")

    def test_remove_symbol(self):
        """Test remove symbol uses red tag."""
        assert SYMBOL_STYLES["✕"] == ("<red>", "</red>")

    def test_create_symbol(self):
        """Test create symbol uses green tag."""
        assert SYMBOL_STYLES["+"] == ("<green>", "</green>")

    def test_permission_symbol(self):
        """Test permission symbol uses magenta tag."""
        assert SYMBOL_STYLES["⚙"] == ("<magenta>", "</magenta>")

    def test_skip_symbol(self):
        """Test skip symbol uses dim tag."""
        assert SYMBOL_STYLES["·"] == ("<dim>", "</dim>")


class TestColorizeSymbols:
    """Tests for _colorize_symbols function."""

    def test_colorize_download_symbol(self):
        """Test download symbol gets colored with loguru tags."""
        result = _colorize_symbols("↓ file.txt")
        assert "<cyan>" in result
        assert "</cyan>" in result
        assert "↓" in result

    def test_colorize_upload_symbol(self):
        """Test upload symbol gets colored with loguru tags."""
        result = _colorize_symbols("↑ file.txt")
        assert "<green>" in result
        assert "</green>" in result
        assert "↑" in result

    def test_colorize_multiple_symbols(self):
        """Test multiple symbols get colored."""
        result = _colorize_symbols("↓ download and ↑ upload")
        assert "<cyan>" in result
        assert "<green>" in result

    def test_no_symbols_unchanged(self):
        """Test message without symbols is unchanged."""
        result = _colorize_symbols("Test message")
        assert result == "Test message"

    def test_colorize_disabled(self):
        """Test colorization can be disabled."""
        result = _colorize_symbols("↓ file.txt", use_colors=False)
        assert "<cyan>" not in result
        assert result == "↓ file.txt"


class TestSetupLogging:
    """Tests for setup_logging function."""

    def test_setup_returns_loguru_logger(self):
        """Test setup_logging returns loguru logger."""
        result = setup_logging()
        # Loguru logger has specific attributes
        assert hasattr(result, "info")
        assert hasattr(result, "debug")
        assert hasattr(result, "warning")
        assert hasattr(result, "error")
        assert hasattr(result, "bind")

    def test_setup_debug_mode(self):
        """Test debug mode logging setup."""
        # Should not raise
        setup_logging(debug=True)

    def test_setup_quiet_mode(self):
        """Test quiet mode logging setup."""
        # Should not raise
        setup_logging(quiet=True)

    def test_setup_with_logfile(self):
        """Test logging setup with file output."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.log', delete=False) as f:
            logfile = f.name

        try:
            log = setup_logging(logfile=logfile)
            # Test that logging works
            log.info("Test message")

            # Verify file was written
            with open(logfile, 'r') as f:
                content = f.read()
                assert "Test message" in content
        finally:
            os.unlink(logfile)

    def test_setup_no_color(self):
        """Test logging setup with colors disabled."""
        # Should not raise
        setup_logging(no_color=True)


class TestClientIdContext:
    """Tests for client ID context functions."""

    def test_get_client_id_default_none(self):
        """Test get_client_id returns None by default."""
        set_client_id(None)
        assert get_client_id() is None

    def test_set_and_get_client_id(self):
        """Test setting and getting client ID."""
        set_client_id("test-client-123")
        assert get_client_id() == "test-client-123"
        set_client_id(None)  # cleanup

    def test_client_id_can_be_cleared(self):
        """Test client ID can be cleared."""
        set_client_id("test-id")
        set_client_id(None)
        assert get_client_id() is None
