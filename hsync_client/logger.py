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
- HSYNC_LOG_COMPRESSION: Compression format (default "xz"; also "gz", "zip", "bz2")

Log format: 2024-01-15T10:30:45.123Z - [12345] INFO: message
"""

import logging
import sys
from contextvars import ContextVar
from typing import TYPE_CHECKING

from hsync_common.logger import (
    BaseInterceptHandler,
    build_record_formatter,
    get_rotation_config,
)
from hsync_common.text import escape_loguru_markup
from loguru import logger

from hsync_client.constants import ActionLabel as AL

if TYPE_CHECKING:
    from loguru import Logger, Record


# =============================================================================
# Intercept Handler for stdlib logging
# =============================================================================


class InterceptHandler(BaseInterceptHandler):
    """
    Handler that intercepts standard logging and redirects to loguru.

    Escapes loguru markup in messages so braces (e.g. JSON logged by the
    websockets library) and angle brackets (e.g. ``<locals>`` in tenacity
    retry messages) are rendered verbatim instead of being parsed.
    """

    def __init__(self) -> None:
        super().__init__(escape_markup=True)


# Context variable for client ID - thread-safe storage for per-session data
# ContextVar is used for async/thread-local context, but we also need a global
# fallback for threads that don't inherit the context (watchdog, HTTP server, etc.)
client_id_var: ContextVar[str | None] = ContextVar("client_id", default=None)

# Global fallback for threads that don't inherit ContextVar
# This is safe because HSYNC client is a single-client application (one CID per process)
_global_client_id: str | None = None


def get_client_id() -> str | None:
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


def set_client_id(client_id: str | None) -> None:
    """
    Set the client ID for the current session.

    Sets both the ContextVar (for async context) and the global
    variable (for child threads like watchdog, HTTP server, etc.).
    """
    global _global_client_id
    client_id_var.set(client_id)
    _global_client_id = client_id


# =============================================================================
# Action Label Color Configuration
# =============================================================================
#
# LEVEL_STYLES is shared via hsync_common.logger. Action symbols use
# semantic colors:
# - Download (↓): cyan - incoming data
# - Upload (↑): green - outgoing data (success-like)
# - Move (→): blue - neutral operation
# - Remove (✕): red - destructive action
# - Create (+): green - additive action
# - Permission (⚙): magenta - system/config change
# - Skip (·): gray - no action taken
# =============================================================================

# Action label color mappings using loguru tags. Keyed by the centralized
# ActionLabel values (hsync_client.constants) so the colorized text always
# matches what the emit sites produce.
LABEL_STYLES: dict[str, tuple[str, str]] = {
    # Sync operations
    AL.DOWNLOAD: ("<cyan>", "</cyan>"),
    AL.UPLOAD: ("<green>", "</green>"),
    AL.RESUME: ("<cyan>", "</cyan>"),
    AL.MOVE: ("<blue>", "</blue>"),
    AL.REMOVE: ("<red>", "</red>"),
    AL.DELETE: ("<red>", "</red>"),
    AL.CREATE: ("<green>", "</green>"),
    AL.PERMISSION: ("<magenta>", "</magenta>"),
    AL.SYMLINK: ("<blue>", "</blue>"),
    AL.IN_SYNC: ("<dim>", "</dim>"),
    AL.SKIP: ("<dim>", "</dim>"),
    AL.IGNORE_DEL: ("<yellow>", "</yellow>"),
    # LAN client operations
    AL.CLIENT_UP: ("<green>", "</green>"),
    AL.CLIENT_DEL: ("<red>", "</red>"),
    AL.CLIENT_REN: ("<blue>", "</blue>"),
    AL.CLIENT_DIR: ("<green>", "</green>"),
    AL.CLIENT_KEEP: ("<dim>", "</dim>"),
    AL.CLIENT_IGN: ("<yellow>", "</yellow>"),
}


def _colorize_labels(message: str, use_colors: bool = True) -> str:
    """
    Add color tags to action labels in message.

    Args:
        message: The log message to colorize.
        use_colors: Whether to apply colors (False for plain output).

    Returns:
        Message with color tags applied to action labels.
    """
    if not use_colors:
        return message

    for label, (start_tag, end_tag) in LABEL_STYLES.items():
        if label in message:
            message = message.replace(label, f"{start_tag}{label}{end_tag}")
    return message


def _get_json_data(record: "Record") -> dict | None:
    """
    Extract json_data from record extra, handling different loguru API patterns.

    Loguru handles extra data differently depending on how it's passed:
    - log.bind(json_data=...).debug(...) -> record["extra"]["json_data"]
    - log.debug(..., json_data=...) via opt() -> record["extra"]["json_data"]
    - log.debug(..., extra={...}) -> record["extra"]["extra"]["json_data"]

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


def _append_json_data(record: "Record", message: str) -> str:
    """Append a pretty-printed, markup-escaped json_data payload to a message."""
    import json

    json_data = _get_json_data(record)
    if json_data:
        json_str = escape_loguru_markup(json.dumps(json_data, indent=2, default=str))
        message = f"{message}\n{json_str}"
    return message


def _transform_colored(record: "Record", message: str) -> str:
    """Colorize action labels then append any json_data payload."""
    return _append_json_data(record, _colorize_labels(message, use_colors=True))


def _transform_plain(record: "Record", message: str) -> str:
    """Append any json_data payload (no label colorizing)."""
    return _append_json_data(record, message)


_format_record_colored = build_record_formatter(colored=True, message_transform=_transform_colored)
_format_record_plain = build_record_formatter(colored=False, message_transform=_transform_plain)


def setup_logging(
    debug: bool = False,
    quiet: bool = False,
    logfile: str | None = None,
    no_color: bool = False,
) -> "Logger":
    """
    Configure and return the HSYNC client logger.

    Sets up logging with enterprise-grade features including client ID
    correlation for session tracing and log rotation for file output.

    Log rotation is configured via environment variables:
    - HSYNC_LOG_ROTATION: When to rotate (default: "10 MB")
      Examples: "10 MB", "1 day", "00:00" (midnight), "1 week"
    - HSYNC_LOG_RETENTION: How long to keep old logs (default: "7 days")
      Examples: "7 days", "1 month", "5" (keep 5 files)
    - HSYNC_LOG_COMPRESSION: Compression format (default: "xz")
      Options: "xz", "gz", "bz2", "lzma", "tar", "tar.gz", "tar.bz2", "tar.xz", "zip"

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
        rotation_config = get_rotation_config()
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
