"""
Tests for hsync_client.logger module.

Tests the loguru-based logging configuration.
"""

import tempfile
import os
from unittest.mock import patch

import pytest

from hsync_client.logger import (
    Colors,
    LEVEL_COLORS,
    SYMBOL_COLORS,
    setup_logging,
    get_client_id,
    set_client_id,
    _colorize_symbols,
)


class TestColors:
    """Tests for the Colors class."""

    def test_reset_code(self):
        """Test RESET color code."""
        assert Colors.RESET == "\033[0m"

    def test_regular_colors(self):
        """Test regular color codes."""
        assert Colors.RED == "\033[31m"
        assert Colors.GREEN == "\033[32m"
        assert Colors.YELLOW == "\033[33m"
        assert Colors.BLUE == "\033[34m"
        assert Colors.MAGENTA == "\033[35m"
        assert Colors.CYAN == "\033[36m"
        assert Colors.WHITE == "\033[37m"
        assert Colors.GRAY == "\033[90m"

    def test_bold_colors(self):
        """Test bold color codes."""
        assert Colors.BOLD_RED == "\033[1;31m"
        assert Colors.BOLD_GREEN == "\033[1;32m"
        assert Colors.BOLD_YELLOW == "\033[1;33m"
        assert Colors.BOLD_BLUE == "\033[1;34m"
        assert Colors.BOLD_CYAN == "\033[1;36m"
        assert Colors.BOLD_WHITE == "\033[1;37m"

    def test_dim_code(self):
        """Test DIM color code."""
        assert Colors.DIM == "\033[2m"


class TestLevelColors:
    """Tests for LEVEL_COLORS mapping."""

    def test_debug_color(self):
        """Test DEBUG level color."""
        assert LEVEL_COLORS["DEBUG"] == Colors.GRAY

    def test_info_color(self):
        """Test INFO level color."""
        assert LEVEL_COLORS["INFO"] == Colors.RESET

    def test_warning_color(self):
        """Test WARNING level color."""
        assert LEVEL_COLORS["WARNING"] == Colors.YELLOW

    def test_error_color(self):
        """Test ERROR level color."""
        assert LEVEL_COLORS["ERROR"] == Colors.RED

    def test_critical_color(self):
        """Test CRITICAL level color."""
        assert LEVEL_COLORS["CRITICAL"] == Colors.BOLD_RED


class TestSymbolColors:
    """Tests for SYMBOL_COLORS mapping."""

    def test_download_symbol(self):
        """Test download symbol color."""
        assert SYMBOL_COLORS["↓"] == Colors.CYAN

    def test_upload_symbol(self):
        """Test upload symbol color."""
        assert SYMBOL_COLORS["↑"] == Colors.GREEN

    def test_move_symbol(self):
        """Test move symbol color."""
        assert SYMBOL_COLORS["→"] == Colors.BLUE

    def test_remove_symbol(self):
        """Test remove symbol color."""
        assert SYMBOL_COLORS["✕"] == Colors.RED

    def test_create_symbol(self):
        """Test create symbol color."""
        assert SYMBOL_COLORS["+"] == Colors.GREEN

    def test_permission_symbol(self):
        """Test permission symbol color."""
        assert SYMBOL_COLORS["⚙"] == Colors.MAGENTA

    def test_skip_symbol(self):
        """Test skip symbol color."""
        assert SYMBOL_COLORS["·"] == Colors.GRAY


class TestColorizeSymbols:
    """Tests for _colorize_symbols function."""

    def test_colorize_download_symbol(self):
        """Test download symbol gets colored."""
        result = _colorize_symbols("↓ file.txt")
        assert Colors.CYAN in result
        assert "↓" in result

    def test_colorize_upload_symbol(self):
        """Test upload symbol gets colored."""
        result = _colorize_symbols("↑ file.txt")
        assert Colors.GREEN in result
        assert "↑" in result

    def test_colorize_multiple_symbols(self):
        """Test multiple symbols get colored."""
        result = _colorize_symbols("↓ download and ↑ upload")
        assert Colors.CYAN in result
        assert Colors.GREEN in result

    def test_no_symbols_unchanged(self):
        """Test message without symbols is unchanged."""
        result = _colorize_symbols("Test message")
        assert result == "Test message"


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
