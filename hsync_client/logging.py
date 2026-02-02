"""
Logging configuration for HSYNC Client.

This module provides a standardized logging setup that matches the server's
format while supporting client-specific features like quiet mode, file output,
and colored console output.
"""

import logging
import sys

from json import dumps
from typing import Optional


# ANSI color codes for terminal output
class Colors:
    """ANSI escape codes for colored terminal output."""

    # Reset
    RESET = "\033[0m"

    # Regular colors
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    GRAY = "\033[90m"

    # Bold colors
    BOLD_RED = "\033[1;31m"
    BOLD_GREEN = "\033[1;32m"
    BOLD_YELLOW = "\033[1;33m"
    BOLD_BLUE = "\033[1;34m"
    BOLD_CYAN = "\033[1;36m"
    BOLD_WHITE = "\033[1;37m"

    # Dim
    DIM = "\033[2m"


# Mapping of log levels to colors
LEVEL_COLORS = {
    logging.DEBUG: Colors.GRAY,
    logging.INFO: Colors.RESET,
    logging.WARNING: Colors.YELLOW,
    logging.ERROR: Colors.RED,
    logging.CRITICAL: Colors.BOLD_RED,
}

# Mapping of action symbols to colors
SYMBOL_COLORS = {
    "↓": Colors.CYAN,      # Download
    "↑": Colors.GREEN,     # Upload
    "→": Colors.BLUE,      # Move
    "✕": Colors.RED,       # Remove
    "+": Colors.GREEN,     # Create
    "⚙": Colors.MAGENTA,   # Permission
    "·": Colors.GRAY,      # Skip
}


class ColoredFormatter(logging.Formatter):
    """
    Custom formatter that adds colors to log messages.

    Extends the standard formatter to:
    - Color log levels (ERROR=red, WARNING=yellow, etc.)
    - Color action symbols (↓=cyan, ↑=green, etc.)
    - Support optional JSON data appended to messages
    """

    def __init__(
        self,
        fmt: Optional[str] = None,
        datefmt: Optional[str] = None,
        use_colors: bool = True,
    ):
        """Initialize the formatter with optional color support."""
        super().__init__(fmt, datefmt)
        self.use_colors = use_colors and sys.stdout.isatty()

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record with colors and optional JSON data."""
        # Get the base formatted message
        message = super().format(record)

        if self.use_colors:
            # Color the log level
            level_color = LEVEL_COLORS.get(record.levelno, Colors.RESET)

            # Color action symbols in the message
            for symbol, color in SYMBOL_COLORS.items():
                if symbol in message:
                    message = message.replace(
                        f": {symbol} ",
                        f": {color}{symbol}{Colors.RESET} "
                    )

            # Color the level name in the message
            levelname = record.levelname
            colored_level = f"{level_color}{levelname}{Colors.RESET}"
            message = message.replace(f" {levelname}:", f" {colored_level}:")

            # Dim the timestamp
            if message.startswith("20"):  # ISO timestamp starts with year
                parts = message.split(" - ", 1)
                if len(parts) == 2:
                    message = f"{Colors.DIM}{parts[0]}{Colors.RESET} - {parts[1]}"

        # Append JSON data if present
        if hasattr(record, 'json_data') and record.json_data is not None:
            json_str = dumps(record.json_data, indent=4, sort_keys=True, default=str)
            if self.use_colors:
                json_str = f"{Colors.DIM}{json_str}{Colors.RESET}"
            message = f"{message}\n{json_str}"

        return message


class JsonFormatter(logging.Formatter):
    """
    Custom formatter that supports JSON data in log messages (no colors).

    Used for file output where ANSI codes should not be written.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record, appending JSON data if present."""
        message = super().format(record)

        # Check if record has extra data to format as JSON
        if hasattr(record, 'json_data') and record.json_data is not None:
            json_str = dumps(record.json_data, indent=4, sort_keys=True, default=str)
            message = f"{message}\n{json_str}"

        return message


def setup_logging(
    debug: bool = False,
    quiet: bool = False,
    logfile: Optional[str] = None,
    no_color: bool = False,
) -> logging.Logger:
    """
    Configure and return the HSYNC client logger.

    Sets up logging with a format matching the server's output style.
    Supports console output with colors, file output, and quiet mode.

    Args:
        debug: Enable DEBUG level logging.
        quiet: Suppress all output.
        logfile: Optional path to log file.
        no_color: Disable colored output.

    Returns:
        Configured logger instance.
    """
    logger = logging.getLogger("hsync")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)

    # Clear any existing handlers
    logger.handlers.clear()

    if quiet:
        # Add null handler for quiet mode
        logger.addHandler(logging.NullHandler())
        return logger

    # Log format matching server style
    # Format: 2024-01-15T10:30:45.123 - [12345] INFO: message
    log_format = '%(asctime)s.%(msecs)03d - [%(process)d] %(levelname)s: %(message)s'
    date_format = '%Y-%m-%dT%H:%M:%SZ'

    if logfile:
        # File handler - no colors
        file_formatter = JsonFormatter(fmt=log_format, datefmt=date_format)
        file_handler = logging.FileHandler(logfile, mode='a', encoding='utf-8')
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)
    else:
        # Console handler - with colors (unless disabled)
        console_formatter = ColoredFormatter(
            fmt=log_format,
            datefmt=date_format,
            use_colors=not no_color,
        )
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)

    # Prevent propagation to root logger
    logger.propagate = False

    return logger


def log_with_data(
    logger: logging.Logger,
    level: int,
    message: str,
    data: Optional[object] = None,
) -> None:
    """
    Log a message with optional JSON data.

    Args:
        logger: Logger instance to use.
        level: Logging level (e.g., logging.INFO).
        message: Log message.
        data: Optional data to format as JSON.
    """
    extra = {'json_data': data} if data else {'json_data': None}
    logger.log(level, message, extra=extra)

