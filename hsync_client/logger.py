# HSYNC Client - HTTP-based file synchronization client
# Copyright (C) 2025 Alberto Basso <alberto.sdk [at] gmail [dot] com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
Logging configuration for HSYNC Client using Loguru.

This module provides a standardized logging setup with enterprise-grade features:
- ISO 8601 timestamps with milliseconds
- Client ID (CID) for session correlation and distributed tracing
- Colored console output with action symbol highlighting
- File output support
- Quiet mode

Log format: 2024-01-15T10:30:45.123Z - [12345] [cid-abc123] INFO: message
"""

import sys
from contextvars import ContextVar
from typing import Optional

from loguru import logger


# Context variable for client ID - thread-safe storage for per-session data
client_id_var: ContextVar[Optional[str]] = ContextVar("client_id", default=None)


def get_client_id() -> Optional[str]:
    """Get the current client ID from context."""
    return client_id_var.get()


def set_client_id(client_id: Optional[str]) -> None:
    """Set the client ID in context for the current session."""
    client_id_var.set(client_id)


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
LEVEL_COLORS: dict[str, str] = {
    "DEBUG": Colors.GRAY,
    "INFO": Colors.RESET,
    "WARNING": Colors.YELLOW,
    "ERROR": Colors.RED,
    "CRITICAL": Colors.BOLD_RED,
}

# Mapping of action symbols to colors
SYMBOL_COLORS: dict[str, str] = {
    "↓": Colors.CYAN,      # Download
    "↑": Colors.GREEN,     # Upload
    "→": Colors.BLUE,      # Move
    "✕": Colors.RED,       # Remove
    "+": Colors.GREEN,     # Create
    "⚙": Colors.MAGENTA,   # Permission
    "·": Colors.GRAY,      # Skip
}


def _colorize_symbols(message: str) -> str:
    """Add color codes to action symbols in message."""
    for symbol, color in SYMBOL_COLORS.items():
        if symbol in message:
            message = message.replace(
                f"{symbol} ",
                f"{color}{symbol}{Colors.RESET} "
            )
    return message


def _format_record_colored(record: dict) -> str:
    """
    Custom format function for loguru with colors.

    Produces: 2024-01-15T10:30:45.123Z - [12345] [cid-abc123] INFO: message
    """
    client_id = record["extra"].get("client_id", "-")
    timestamp = record["time"].strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    level = record["level"].name
    message = record["message"]

    # Get level color
    level_color = LEVEL_COLORS.get(level, Colors.RESET)

    # Colorize action symbols in message
    message = _colorize_symbols(message)

    return (
        f"{Colors.DIM}{timestamp}{Colors.RESET} - [{record['process'].id}] [{client_id}] "
        f"{level_color}{level}{Colors.RESET}: {message}\n"
    )


def _format_record_plain(record: dict) -> str:
    """
    Custom format function for loguru without colors.

    Produces: 2024-01-15T10:30:45.123Z - [12345] [cid-abc123] INFO: message
    """
    client_id = record["extra"].get("client_id", "-")
    timestamp = record["time"].strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    level = record["level"].name
    message = record["message"]

    return (
        f"{timestamp} - [{record['process'].id}] [{client_id}] "
        f"{level}: {message}\n"
    )


def setup_logging(
    debug: bool = False,
    quiet: bool = False,
    logfile: Optional[str] = None,
    no_color: bool = False,
) -> "logger":  # type: ignore[valid-type]
    """
    Configure and return the HSYNC client logger.

    Sets up logging with enterprise-grade features including client ID
    correlation for session tracing.

    Args:
        debug: Enable DEBUG level logging.
        quiet: Suppress all output.
        logfile: Optional path to log file.
        no_color: Disable colored output.

    Returns:
        Configured loguru logger instance.
    """
    # Remove default loguru handler
    logger.remove()

    if quiet:
        # No handlers for quiet mode
        return logger

    level = "DEBUG" if debug else "INFO"

    if logfile:
        # File handler - no colors
        logger.add(
            logfile,
            level=level,
            format=_format_record_plain,
            colorize=False,
            encoding="utf-8",
        )
    else:
        # Console handler - with colors (unless disabled)
        use_colors = not no_color and sys.stdout.isatty()
        format_func = _format_record_colored if use_colors else _format_record_plain

        logger.add(
            sys.stdout,
            level=level,
            format=format_func,
            colorize=False,  # We handle colors manually
        )

    return logger


