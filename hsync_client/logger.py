# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

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
- File output support with log rotation
- Quiet mode
- Automatic color detection (disabled for non-TTY)

Log rotation configuration (via environment variables):
- HSYNC_LOG_ROTATION: When to rotate (e.g., "10 MB", "1 day", "00:00")
- HSYNC_LOG_RETENTION: How long to keep old logs (e.g., "7 days", "5")
- HSYNC_LOG_COMPRESSION: Compression format (e.g., "gz", "zip", "bz2")

Log format: 2024-01-15T10:30:45.123Z - [12345] [cid-abc123] INFO: message
"""

import logging
import os
import sys
from contextvars import ContextVar
from typing import Final, Optional

from loguru import logger


# =============================================================================
# Intercept Handler for stdlib logging
# =============================================================================


class InterceptHandler(logging.Handler):
    """
    Handler that intercepts standard logging and redirects to loguru.

    This allows modules using standard logging (logging.getLogger)
    to have their logs processed through loguru with consistent formatting.
    """

    def emit(self, record: logging.LogRecord) -> None:
        """Redirect log record to loguru."""
        # Get corresponding loguru level
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where originated the logged message
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        # Get the message and escape curly braces to prevent loguru from
        # interpreting them as format placeholders (e.g., websockets library logs JSON)
        message = record.getMessage()
        message = message.replace("{", "{{").replace("}", "}}")

        # Bind client_id to the log context
        client_id = get_client_id()
        log = logger.bind(client_id=client_id or "-")
        log.opt(depth=depth, exception=record.exc_info).log(level, message)


# =============================================================================
# Log Rotation Configuration Defaults
# =============================================================================

# Default rotation: rotate when file reaches 10 MB
DEFAULT_LOG_ROTATION: Final[str] = "10 MB"

# Default retention: keep logs for 7 days
DEFAULT_LOG_RETENTION: Final[str] = "7 days"

# Default compression: gzip for space efficiency
DEFAULT_LOG_COMPRESSION: Final[str] = "gz"


# Context variable for client ID - thread-safe storage for per-session data
# ContextVar is used for async/thread-local context, but we also need a global
# fallback for threads that don't inherit the context (watchdog, HTTP server, etc.)
client_id_var: ContextVar[Optional[str]] = ContextVar("client_id", default=None)

# Global fallback for threads that don't inherit ContextVar
# This is safe because HSYNC client is a single-client application (one CID per process)
_global_client_id: Optional[str] = None


def get_client_id() -> Optional[str]:
    """
    Get the current client ID.

    First tries the ContextVar (for async/thread-local context),
    then falls back to the global variable (for child threads).
    """
    # Try ContextVar first (works in same thread/async context)
    cid = client_id_var.get()
    if cid:
        return cid
    # Fall back to global (works across all threads)
    return _global_client_id


def set_client_id(client_id: Optional[str]) -> None:
    """
    Set the client ID for the current session.

    Sets both the ContextVar (for async context) and the global
    variable (for child threads like watchdog, HTTP server, etc.).
    """
    global _global_client_id
    client_id_var.set(client_id)
    _global_client_id = client_id


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


def _get_json_data(record: dict) -> dict | None:
    """
    Extract json_data from record extra, handling different loguru API patterns.

    Loguru handles extra data differently depending on how it's passed:
    - LOG.bind(json_data=...).debug(...) -> record["extra"]["json_data"]
    - LOG.debug(..., json_data=...) via opt() -> record["extra"]["json_data"]
    - LOG.debug(..., extra={...}) -> record["extra"]["extra"]["json_data"]

    This function normalizes access to json_data regardless of how it was passed.
    """
    extra = record.get("extra", {})

    # Check direct access (from bind() or opt())
    if "json_data" in extra:
        return extra["json_data"]

    # Check nested access (from extra={...} parameter)
    if "extra" in extra and isinstance(extra["extra"], dict):
        return extra["extra"].get("json_data")

    return None


def _format_record_colored(record: dict) -> str:
    """
    Custom format function for loguru with colors.

    Uses loguru's built-in color tags for proper terminal compatibility
    and automatic color stripping when output is not a TTY.

    Format: 2024-01-15T10:30:45.123Z - [12345] [cid-abc123] INFO: message
    """
    import json

    # Get client_id from record extra (if bound) or from context variable
    client_id = record["extra"].get("client_id") or get_client_id() or "-"
    timestamp = record["time"].strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    level = record["level"].name
    message = record["message"]

    # Get level style tags
    level_start, level_end = LEVEL_STYLES.get(level, ("", ""))

    # Colorize action symbols in message
    message = _colorize_symbols(message, use_colors=True)

    # Check for json_data in extra
    json_data = _get_json_data(record)
    if json_data:
        json_str = json.dumps(json_data, indent=2, default=str)
        # Escape curly braces to prevent loguru from interpreting them as format placeholders
        json_str = json_str.replace("{", "{{").replace("}", "}}")
        message = f"{message}\n{json_str}"

    return (
        f"<dim>{timestamp}</dim> - [{record['process'].id}] [{client_id}] "
        f"{level_start}{level}{level_end}: {message}\n"
    )


def _format_record_plain(record: dict) -> str:
    """
    Custom format function for loguru without colors.

    Format: 2024-01-15T10:30:45.123Z - [12345] [cid-abc123] INFO: message
    """
    import json

    # Get client_id from record extra (if bound) or from context variable
    client_id = record["extra"].get("client_id") or get_client_id() or "-"
    timestamp = record["time"].strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    level = record["level"].name
    message = record["message"]

    # Check for json_data in extra
    json_data = _get_json_data(record)
    if json_data:
        json_str = json.dumps(json_data, indent=2, default=str)
        # Escape curly braces to prevent loguru from interpreting them as format placeholders
        json_str = json_str.replace("{", "{{").replace("}", "}}")
        message = f"{message}\n{json_str}"

    return (
        f"{timestamp} - [{record['process'].id}] [{client_id}] "
        f"{level}: {message}\n"
    )


def _get_rotation_config() -> dict[str, Optional[str]]:
    """
    Get log rotation configuration from environment variables.

    Returns:
        Dictionary with rotation, retention, and compression settings.
    """
    return {
        "rotation": os.environ.get("HSYNC_LOG_ROTATION", DEFAULT_LOG_ROTATION),
        "retention": os.environ.get("HSYNC_LOG_RETENTION", DEFAULT_LOG_RETENTION),
        "compression": os.environ.get("HSYNC_LOG_COMPRESSION", DEFAULT_LOG_COMPRESSION),
    }


def setup_logging(
    debug: bool = False,
    quiet: bool = False,
    logfile: Optional[str] = None,
    no_color: bool = False,
) -> "logger":  # type: ignore[valid-type]
    """
    Configure and return the HSYNC client logger.

    Sets up logging with enterprise-grade features including client ID
    correlation for session tracing and log rotation for file output.

    Log rotation is configured via environment variables:
    - HSYNC_LOG_ROTATION: When to rotate (default: "10 MB")
      Examples: "10 MB", "1 day", "00:00" (midnight), "1 week"
    - HSYNC_LOG_RETENTION: How long to keep old logs (default: "7 days")
      Examples: "7 days", "1 month", "5" (keep 5 files)
    - HSYNC_LOG_COMPRESSION: Compression format (default: "gz")
      Options: "gz", "bz2", "xz", "lzma", "tar", "tar.gz", "tar.bz2", "tar.xz", "zip"

    Args:
        debug: Enable DEBUG level logging.
        quiet: Suppress all output.
        logfile: Optional path to log file (enables rotation).
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
        # File handler with rotation - no colors
        rotation_config = _get_rotation_config()
        logger.add(
            logfile,
            level=level,
            format=_format_record_plain,
            colorize=False,
            encoding="utf-8",
            rotation=rotation_config["rotation"],
            retention=rotation_config["retention"],
            compression=rotation_config["compression"],
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

    # Intercept standard logging (for modules using logging.getLogger)
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

    # Configure specific loggers to use intercept handler
    for logger_name in (
        "hsync",
        "hsync_client",
        "hsync.lan",
        "hsync.lan_server",
        "hsync.lan_websocket",
    ):
        logging.getLogger(logger_name).handlers = [InterceptHandler()]
        logging.getLogger(logger_name).propagate = False

    return logger
