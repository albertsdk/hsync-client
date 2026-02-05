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
- Colored console output following best practices:
  - DEBUG: dim/gray for low-priority messages
  - INFO: default color for normal operations
  - WARNING: yellow for attention-needed situations
  - ERROR: red for failures
  - CRITICAL: bold red for severe issues
- Action symbol highlighting (↓↑→✕+⚙·)
- File output support
- Quiet mode
- Automatic color detection (disabled for non-TTY)

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


# =============================================================================
# Color Configuration (Best Practices)
# =============================================================================
#
# Following industry-standard log coloring conventions:
# - Timestamps: dim/muted to reduce visual noise
# - DEBUG: gray/dim - low priority, verbose information
# - INFO: default/white - normal operational messages
# - SUCCESS: green - positive outcomes
# - WARNING: yellow - attention needed but not critical
# - ERROR: red - failures that need investigation
# - CRITICAL: bold red - severe issues requiring immediate action
#
# Action symbols use semantic colors:
# - Download (↓): cyan - incoming data
# - Upload (↑): green - outgoing data (success-like)
# - Move (→): blue - neutral operation
# - Remove (✕): red - destructive action
# - Create (+): green - additive action
# - Permission (⚙): magenta - system/config change
# - Skip (·): gray - no action taken
# =============================================================================

# Loguru color tags for terminal output
# Using loguru's built-in color system for proper terminal compatibility
LEVEL_STYLES: dict[str, tuple[str, str]] = {
    "DEBUG": ("<dim>", "</dim>"),
    "INFO": ("", ""),
    "SUCCESS": ("<green>", "</green>"),
    "WARNING": ("<yellow>", "</yellow>"),
    "ERROR": ("<red>", "</red>"),
    "CRITICAL": ("<bold><red>", "</red></bold>"),
}

# Action symbol color mappings using loguru tags
SYMBOL_STYLES: dict[str, tuple[str, str]] = {
    "↓": ("<cyan>", "</cyan>"),       # Download
    "↑": ("<green>", "</green>"),     # Upload
    "→": ("<blue>", "</blue>"),       # Move
    "✕": ("<red>", "</red>"),         # Remove
    "+": ("<green>", "</green>"),     # Create
    "⚙": ("<magenta>", "</magenta>"), # Permission
    "·": ("<dim>", "</dim>"),         # Skip
}


def _colorize_symbols(message: str, use_colors: bool = True) -> str:
    """
    Add color tags to action symbols in message.

    Args:
        message: The log message to colorize.
        use_colors: Whether to apply colors (False for plain output).

    Returns:
        Message with color tags applied to symbols.
    """
    if not use_colors:
        return message

    for symbol, (start_tag, end_tag) in SYMBOL_STYLES.items():
        if symbol in message:
            message = message.replace(
                f"{symbol} ",
                f"{start_tag}{symbol}{end_tag} "
            )
    return message


def _format_record_colored(record: dict) -> str:
    """
    Custom format function for loguru with colors.

    Uses loguru's built-in color tags for proper terminal compatibility
    and automatic color stripping when output is not a TTY.

    Format: 2024-01-15T10:30:45.123Z - [12345] [cid-abc123] INFO: message
    """
    client_id = record["extra"].get("client_id", "-")
    timestamp = record["time"].strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    level = record["level"].name
    message = record["message"]

    # Get level style tags
    level_start, level_end = LEVEL_STYLES.get(level, ("", ""))

    # Colorize action symbols in message
    message = _colorize_symbols(message, use_colors=True)

    return (
        f"<dim>{timestamp}</dim> - [{record['process'].id}] [{client_id}] "
        f"{level_start}{level}{level_end}: {message}\n"
    )


def _format_record_plain(record: dict) -> str:
    """
    Custom format function for loguru without colors.

    Format: 2024-01-15T10:30:45.123Z - [12345] [cid-abc123] INFO: message
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
        # Colors are enabled only when:
        # 1. --no-color flag is not set
        # 2. Output is a TTY (terminal)
        use_colors = not no_color and sys.stdout.isatty()
        format_func = _format_record_colored if use_colors else _format_record_plain

        logger.add(
            sys.stdout,
            level=level,
            format=format_func,
            colorize=use_colors,  # Enable loguru's color tag processing
        )

    return logger


