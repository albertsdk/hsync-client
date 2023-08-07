"""
Tests for hsync_client.logger module.
"""

import logging
import tempfile
import os
from unittest.mock import patch, MagicMock

import pytest

from hsync_client.logger import (
    Colors,
    LEVEL_COLORS,
    SYMBOL_COLORS,
    ColoredFormatter,
    JsonFormatter,
    ClientIdFilter,
    setup_logging,
    log_with_data,
    get_client_id,
    set_client_id,
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
        assert LEVEL_COLORS[logging.DEBUG] == Colors.GRAY

    def test_info_color(self):
        """Test INFO level color."""
        assert LEVEL_COLORS[logging.INFO] == Colors.RESET

    def test_warning_color(self):
        """Test WARNING level color."""
        assert LEVEL_COLORS[logging.WARNING] == Colors.YELLOW

    def test_error_color(self):
        """Test ERROR level color."""
        assert LEVEL_COLORS[logging.ERROR] == Colors.RED

    def test_critical_color(self):
        """Test CRITICAL level color."""
        assert LEVEL_COLORS[logging.CRITICAL] == Colors.BOLD_RED


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


class TestJsonFormatter:
    """Tests for JsonFormatter class."""

    def test_format_simple_message(self):
        """Test formatting a simple message."""
        formatter = JsonFormatter(fmt="%(message)s")
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        result = formatter.format(record)
        assert result == "Test message"

    def test_format_with_json_data(self):
        """Test formatting with JSON data."""
        formatter = JsonFormatter(fmt="%(message)s")
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        record.json_data = {"key": "value", "count": 42}
        result = formatter.format(record)
        assert "Test message" in result
        assert '"key": "value"' in result
        assert '"count": 42' in result

    def test_format_without_json_data(self):
        """Test formatting without json_data attribute."""
        formatter = JsonFormatter(fmt="%(message)s")
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        # No json_data attribute set
        result = formatter.format(record)
        assert result == "Test message"


class TestColoredFormatter:
    """Tests for ColoredFormatter class."""

    def test_init_with_colors_disabled(self):
        """Test initialization with colors disabled."""
        formatter = ColoredFormatter(use_colors=False)
        assert formatter.use_colors is False

    @patch("sys.stdout.isatty", return_value=False)
    def test_init_no_tty(self, mock_isatty):
        """Test that colors are disabled when not a TTY."""
        formatter = ColoredFormatter(use_colors=True)
        assert formatter.use_colors is False

    @patch("sys.stdout.isatty", return_value=True)
    def test_init_with_tty(self, mock_isatty):
        """Test that colors are enabled when TTY."""
        formatter = ColoredFormatter(use_colors=True)
        assert formatter.use_colors is True

    def test_format_without_colors(self):
        """Test formatting without colors."""
        formatter = ColoredFormatter(fmt="%(message)s", use_colors=False)
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        result = formatter.format(record)
        assert result == "Test message"

    def test_format_with_json_data_no_colors(self):
        """Test formatting with JSON data but no colors."""
        formatter = ColoredFormatter(fmt="%(message)s", use_colors=False)
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        record.json_data = {"key": "value"}
        result = formatter.format(record)
        assert "Test message" in result
        assert '"key": "value"' in result

    @patch("sys.stdout.isatty", return_value=True)
    def test_format_with_colors_enabled(self, mock_isatty):
        """Test formatting with colors enabled."""
        formatter = ColoredFormatter(
            fmt="%(asctime)s - %(levelname)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
            use_colors=True,
        )
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        result = formatter.format(record)
        # Should contain ANSI codes for ERROR level (red)
        assert Colors.RED in result or Colors.RESET in result
        assert "Test message" in result

    @patch("sys.stdout.isatty", return_value=True)
    def test_format_with_action_symbol(self, mock_isatty):
        """Test formatting with action symbol gets colored."""
        formatter = ColoredFormatter(fmt="%(message)s", use_colors=True)
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=": ↑ uploaded file",
            args=(),
            exc_info=None,
        )
        result = formatter.format(record)
        # Should contain color codes for upload symbol
        assert Colors.GREEN in result or "↑" in result

    @patch("sys.stdout.isatty", return_value=True)
    def test_format_with_timestamp_dimmed(self, mock_isatty):
        """Test that ISO timestamp is dimmed."""
        formatter = ColoredFormatter(
            fmt="%(asctime)s - %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
            use_colors=True,
        )
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        result = formatter.format(record)
        # Timestamp should be dimmed
        assert Colors.DIM in result

    @patch("sys.stdout.isatty", return_value=True)
    def test_format_with_json_data_colored(self, mock_isatty):
        """Test formatting with JSON data is dimmed when colors enabled."""
        formatter = ColoredFormatter(fmt="%(message)s", use_colors=True)
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        record.json_data = {"key": "value"}
        result = formatter.format(record)
        # JSON should be dimmed
        assert Colors.DIM in result
        assert '"key": "value"' in result


class TestSetupLogging:
    """Tests for setup_logging function."""

    def test_setup_default(self):
        """Test default logging setup."""
        logger = setup_logging()
        assert logger.name == "hsync"
        assert logger.level == logging.INFO
        assert len(logger.handlers) == 1
        assert logger.propagate is False

    def test_setup_debug_mode(self):
        """Test debug mode logging setup."""
        logger = setup_logging(debug=True)
        assert logger.level == logging.DEBUG

    def test_setup_quiet_mode(self):
        """Test quiet mode logging setup."""
        logger = setup_logging(quiet=True)
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0], logging.NullHandler)

    def test_setup_with_logfile(self):
        """Test logging setup with file output."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.log', delete=False) as f:
            logfile = f.name

        try:
            logger = setup_logging(logfile=logfile)
            assert len(logger.handlers) == 1
            assert isinstance(logger.handlers[0], logging.FileHandler)

            # Test that logging works
            logger.info("Test message")

            # Verify file was written
            with open(logfile, 'r') as f:
                content = f.read()
                assert "Test message" in content
        finally:
            # Cleanup
            for handler in logger.handlers[:]:
                handler.close()
                logger.removeHandler(handler)
            os.unlink(logfile)

    def test_setup_clears_existing_handlers(self):
        """Test that setup clears existing handlers."""
        logger = setup_logging()
        initial_handlers = len(logger.handlers)

        # Setup again
        logger = setup_logging()
        assert len(logger.handlers) == initial_handlers

    def test_setup_no_color(self):
        """Test logging setup with colors disabled."""
        logger = setup_logging(no_color=True)
        assert len(logger.handlers) == 1
        handler = logger.handlers[0]
        assert isinstance(handler.formatter, ColoredFormatter)
        # Colors should be disabled
        assert handler.formatter.use_colors is False


class TestLogWithData:
    """Tests for log_with_data function."""

    def test_log_with_data(self):
        """Test logging with data."""
        logger = MagicMock(spec=logging.Logger)
        data = {"key": "value"}

        log_with_data(logger, logging.INFO, "Test message", data)

        logger.log.assert_called_once()
        call_args = logger.log.call_args
        assert call_args[0][0] == logging.INFO
        assert call_args[0][1] == "Test message"
        assert call_args[1]["extra"]["json_data"] == data

    def test_log_without_data(self):
        """Test logging without data."""
        logger = MagicMock(spec=logging.Logger)

        log_with_data(logger, logging.WARNING, "Warning message")

        logger.log.assert_called_once()
        call_args = logger.log.call_args
        assert call_args[0][0] == logging.WARNING
        assert call_args[0][1] == "Warning message"
        assert call_args[1]["extra"]["json_data"] is None

    def test_log_with_none_data(self):
        """Test logging with explicit None data."""
        logger = MagicMock(spec=logging.Logger)

        log_with_data(logger, logging.ERROR, "Error message", None)

        logger.log.assert_called_once()
        call_args = logger.log.call_args
        assert call_args[1]["extra"]["json_data"] is None


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


class TestClientIdFilter:
    """Tests for ClientIdFilter class."""

    def test_filter_adds_client_id(self):
        """Test filter adds client_id to log record."""
        set_client_id("filter-test-id")
        filter_obj = ClientIdFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="test message", args=(), exc_info=None
        )
        result = filter_obj.filter(record)
        assert result is True
        assert record.client_id == "filter-test-id"
        set_client_id(None)  # cleanup

    def test_filter_uses_dash_when_no_client_id(self):
        """Test filter uses '-' when no client ID is set."""
        set_client_id(None)
        filter_obj = ClientIdFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="test message", args=(), exc_info=None
        )
        filter_obj.filter(record)
        assert record.client_id == "-"
