"""
Tests for hsync_client.logger module.

Tests the loguru-based logging configuration.
"""

import logging
import os
import tempfile
from datetime import UTC
from typing import Any, cast
from unittest.mock import MagicMock

from hsync_common.logger import (
    DEFAULT_LOG_COMPRESSION,
    DEFAULT_LOG_RETENTION,
    DEFAULT_LOG_ROTATION,
    LEVEL_STYLES,
)
from hsync_common.logger import (
    get_rotation_config as _get_rotation_config,
)

from hsync_client.logger import (
    LABEL_STYLES,
    InterceptHandler,
    _colorize_labels,
    _format_record_colored,
    _format_record_plain,
    _get_json_data,
    get_client_id,
    set_client_id,
    setup_logging,
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


class TestLabelStyles:
    """Tests for LABEL_STYLES mapping with loguru color tags."""

    def test_download_label(self):
        """Test download label uses cyan tag."""
        assert LABEL_STYLES["[DOWNLOAD]"] == ("<cyan>", "</cyan>")

    def test_upload_label(self):
        """Test upload label uses green tag."""
        assert LABEL_STYLES["[UPLOAD]"] == ("<green>", "</green>")

    def test_resume_label(self):
        """Test resume label uses cyan tag."""
        assert LABEL_STYLES["[RESUME]"] == ("<cyan>", "</cyan>")

    def test_move_label(self):
        """Test move label uses blue tag."""
        assert LABEL_STYLES["[MOVE]"] == ("<blue>", "</blue>")

    def test_remove_label(self):
        """Test remove label uses red tag."""
        assert LABEL_STYLES["[REMOVE]"] == ("<red>", "</red>")

    def test_delete_label(self):
        """Test delete label uses red tag."""
        assert LABEL_STYLES["[DELETE]"] == ("<red>", "</red>")

    def test_create_label(self):
        """Test create label uses green tag."""
        assert LABEL_STYLES["[CREATE]"] == ("<green>", "</green>")

    def test_permission_label(self):
        """Test permission label uses magenta tag."""
        assert LABEL_STYLES["[PERMISSION]"] == ("<magenta>", "</magenta>")

    def test_symlink_label(self):
        """Test symlink label uses blue tag."""
        assert LABEL_STYLES["[SYMLINK]"] == ("<blue>", "</blue>")

    def test_in_sync_label(self):
        """Test in-sync label uses dim tag."""
        assert LABEL_STYLES["[IN SYNC]"] == ("<dim>", "</dim>")

    def test_skip_label(self):
        """Test skip label uses dim tag."""
        assert LABEL_STYLES["[SKIP]"] == ("<dim>", "</dim>")

    def test_ignore_del_label(self):
        """Test ignore-del label uses yellow tag."""
        assert LABEL_STYLES["[IGNORE DEL]"] == ("<yellow>", "</yellow>")

    def test_slave_up_label(self):
        """Test client upload label uses green tag."""
        assert LABEL_STYLES["[CLIENT UP]"] == ("<green>", "</green>")

    def test_slave_del_label(self):
        """Test client delete label uses red tag."""
        assert LABEL_STYLES["[CLIENT DEL]"] == ("<red>", "</red>")

    def test_slave_ren_label(self):
        """Test client rename label uses blue tag."""
        assert LABEL_STYLES["[CLIENT REN]"] == ("<blue>", "</blue>")

    def test_slave_dir_label(self):
        """Test client dir label uses green tag."""
        assert LABEL_STYLES["[CLIENT DIR]"] == ("<green>", "</green>")

    def test_slave_keep_label(self):
        """Test client keep label uses dim tag."""
        assert LABEL_STYLES["[CLIENT KEEP]"] == ("<dim>", "</dim>")

    def test_slave_ign_label(self):
        """Test client ignore label uses yellow tag."""
        assert LABEL_STYLES["[CLIENT IGN]"] == ("<yellow>", "</yellow>")


class TestColorizeLabels:
    """Tests for _colorize_labels function."""

    def test_colorize_download_label(self):
        """Test download label gets colored with loguru tags."""
        result = _colorize_labels('[DOWNLOAD] "file.txt" 1.2MB')
        assert "<cyan>[DOWNLOAD]</cyan>" in result

    def test_colorize_upload_label(self):
        """Test upload label gets colored with loguru tags."""
        result = _colorize_labels('[UPLOAD] "file.txt" 1.2MB')
        assert "<green>[UPLOAD]</green>" in result

    def test_colorize_remove_label(self):
        """Test remove label gets colored red."""
        result = _colorize_labels('[REMOVE] "/path/to/file"')
        assert "<red>[REMOVE]</red>" in result

    def test_colorize_multiple_labels(self):
        """Test message is unchanged when it has no labels."""
        result = _colorize_labels("[DOWNLOAD] then [UPLOAD]")
        assert "<cyan>" in result
        assert "<green>" in result

    def test_no_labels_unchanged(self):
        """Test message without labels is unchanged."""
        result = _colorize_labels("Test message")
        assert result == "Test message"

    def test_colorize_disabled(self):
        """Test colorization can be disabled."""
        result = _colorize_labels('[DOWNLOAD] "file.txt"', use_colors=False)
        assert "<cyan>" not in result
        assert result == '[DOWNLOAD] "file.txt"'

    def test_colorize_slave_label(self):
        """Test LAN client label gets colored."""
        result = _colorize_labels('[CLIENT DEL] "/path/to/file"')
        assert "<red>[CLIENT DEL]</red>" in result


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
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            logfile = f.name

        try:
            log = setup_logging(logfile=logfile)
            # Test that logging works
            log.info("Test message")

            # Verify file was written
            with open(logfile) as f:
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


class TestInterceptHandler:
    """Tests for InterceptHandler class."""

    def test_is_logging_handler(self):
        """InterceptHandler is a logging.Handler subclass."""
        handler = InterceptHandler()
        assert isinstance(handler, logging.Handler)

    def test_emit_redirects_to_loguru(self):
        """emit() redirects standard log records to loguru."""
        handler = InterceptHandler()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="test message",
            args=(),
            exc_info=None,
        )
        # Should not raise
        handler.emit(record)


class TestGetJsonData:
    """Tests for _get_json_data function."""

    def test_returns_none_without_extra(self):
        """Returns None when no extra data."""
        record = cast(Any, {"extra": {}})
        assert _get_json_data(record) is None

    def test_returns_json_data_from_direct_extra(self):
        """Returns json_data from record extra (bind/opt pattern)."""
        record = cast(Any, {"extra": {"json_data": {"key": "value"}}})
        assert _get_json_data(record) == {"key": "value"}

    def test_returns_json_data_from_nested_extra(self):
        """Returns json_data from nested extra dict."""
        record = cast(Any, {"extra": {"extra": {"json_data": {"nested": True}}}})
        assert _get_json_data(record) == {"nested": True}

    def test_returns_none_for_missing_json_data(self):
        """Returns None when extra exists but no json_data."""
        record = cast(Any, {"extra": {"other_key": 123}})
        assert _get_json_data(record) is None

    def test_returns_none_for_empty_record(self):
        """Returns None for record with no extra at all."""
        record = cast(Any, {})
        assert _get_json_data(record) is None


class TestFormatRecordColored:
    """Tests for _format_record_colored function."""

    def _make_record(self, message: str = "test", level: str = "INFO", extra: Any = None) -> Any:
        """Create a mock loguru record."""
        from datetime import datetime

        level_record = MagicMock()
        level_record.name = level
        record = {
            "time": datetime(2024, 1, 15, 10, 30, 45, 123000, tzinfo=UTC),
            "level": level_record,
            "message": message,
            "process": MagicMock(id=12345),
            "extra": extra or {},
        }
        return record

    def test_basic_format(self):
        """Formatted output contains timestamp, PID, level, and message."""
        record = self._make_record("hello world")
        result = _format_record_colored(record)
        assert "2024-01-15T10:30:45.123Z" in result
        assert "12345" in result
        assert "INFO" in result
        assert "hello world" in result

    def test_includes_json_data(self):
        """JSON data from extra is appended to message."""
        record = self._make_record("msg", extra={"json_data": {"k": "v"}})
        result = _format_record_colored(record)
        assert '"k"' in result
        assert '"v"' in result


class TestFormatRecordPlain:
    """Tests for _format_record_plain function."""

    def _make_record(self, message: str = "test", level: str = "INFO", extra: Any = None) -> Any:
        """Create a mock loguru record."""
        from datetime import datetime

        level_record = MagicMock()
        level_record.name = level
        record = {
            "time": datetime(2024, 1, 15, 10, 30, 45, 123000, tzinfo=UTC),
            "level": level_record,
            "message": message,
            "process": MagicMock(id=12345),
            "extra": extra or {},
        }
        return record

    def test_no_color_tags(self):
        """Plain format has no loguru color tags."""
        record = self._make_record("↓ file.txt")
        result = _format_record_plain(record)
        assert "<dim>" not in result
        assert "<cyan>" not in result

    def test_basic_format(self):
        """Plain format contains timestamp, PID, level, message."""
        record = self._make_record("plain message")
        result = _format_record_plain(record)
        assert "2024-01-15T10:30:45.123Z" in result
        assert "12345" in result
        assert "INFO" in result
        assert "plain message" in result


class TestGetRotationConfig:
    """Tests for _get_rotation_config function."""

    def test_default_values(self):
        """Returns defaults when env vars are not set."""
        # Remove any existing env vars
        for key in ("HSYNC_LOG_ROTATION", "HSYNC_LOG_RETENTION", "HSYNC_LOG_COMPRESSION"):
            os.environ.pop(key, None)

        config = _get_rotation_config()
        assert config["rotation"] == DEFAULT_LOG_ROTATION
        assert config["retention"] == DEFAULT_LOG_RETENTION
        assert config["compression"] == DEFAULT_LOG_COMPRESSION

    def test_env_overrides(self):
        """Environment variables override defaults."""
        os.environ["HSYNC_LOG_ROTATION"] = "1 day"
        os.environ["HSYNC_LOG_RETENTION"] = "30 days"
        os.environ["HSYNC_LOG_COMPRESSION"] = "zip"

        try:
            config = _get_rotation_config()
            assert config["rotation"] == "1 day"
            assert config["retention"] == "30 days"
            assert config["compression"] == "zip"
        finally:
            del os.environ["HSYNC_LOG_ROTATION"]
            del os.environ["HSYNC_LOG_RETENTION"]
            del os.environ["HSYNC_LOG_COMPRESSION"]
