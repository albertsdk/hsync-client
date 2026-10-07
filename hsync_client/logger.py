# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

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
- Automatic color detection (disabled for non-TTY or when NO_COLOR is set)

Log rotation configuration (via environment variables):
- HSYNC_LOG_ROTATION: When to rotate (e.g., "10 MB", "1 day", "00:00")
- HSYNC_LOG_RETENTION: How long to keep old logs (e.g., "7 days", "5")
- HSYNC_LOG_COMPRESSION: Compression format (default "xz"; also "gz", "zip", "bz2")

Log format: 2024-01-15T10:30:45.123Z - [12345] [<cid>] INFO: message
"""

import logging
import sys
from contextvars import ContextVar
from typing import TYPE_CHECKING

from hsync_common.labels import colorize_labels
from hsync_common.logger import (
    BaseInterceptHandler,
    build_record_formatter,
    get_rotation_config,
)
from hsync_common.runtime import color_output_enabled
from hsync_common.text import escape_loguru_markup
from loguru import logger

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


def set_client_id(client_id: str | None) -> None:
    """
    Set the client ID for the current session.

    Sets both the ContextVar (for async context) and the global
    variable (for child threads like watchdog, HTTP server, etc.).
    """
    global _global_client_id
    client_id_var.set(client_id)
    _global_client_id = client_id


def get_client_id() -> str | None:
    """Return the current client ID (ContextVar first, global fallback)."""
    return client_id_var.get() or _global_client_id


def _patch_client_id(record: "Record") -> None:
    """Inject the current CID into every record's ``extra`` for formatting."""
    record["extra"]["client_id"] = get_client_id() or "N/A"


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
#
# The label -> color mapping lives in hsync_common.labels (shared with
# hsync-server) so both projects colorize the same operations identically.
# =============================================================================


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
    return _append_json_data(record, colorize_labels(message, use_colors=True))


def _transform_plain(record: "Record", message: str) -> str:
    """Append any json_data payload (no label colorizing)."""
    return _append_json_data(record, message)


_format_record_colored = build_record_formatter(
    colored=True, extra_fields=("client_id",), message_transform=_transform_colored
)
_format_record_plain = build_record_formatter(
    colored=False, extra_fields=("client_id",), message_transform=_transform_plain
)


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

    # Inject the session CID into every record's ``extra`` so the shared
    # formatter renders it as a correlation field (process-global value).
    logger.configure(patcher=_patch_client_id)

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
            # Asynchronous sink: formatting and the write syscall move to a
            # dedicated loguru thread, so high-frequency log lines (e.g. one
            # [LOCAL CREATE] per file during a burst — measured to cost ~2.5x
            # on the watcher thread when synchronous) never throttle the
            # engine. Callers that must read the file right after a log call
            # wait for the queue via ``logger.complete()``.
            enqueue=True,
        )
    else:
        # Console handler - with colors (unless disabled)
        # Colors are enabled only when:
        # 1. --no-color flag is not set and NO_COLOR env is unset
        # 2. Output is a TTY (terminal)
        use_colors = color_output_enabled(no_color=no_color)
        format_func = _format_record_colored if use_colors else _format_record_plain

        logger.add(
            sys.stdout,
            level=level,
            format=format_func,
            colorize=use_colors,  # Enable loguru's color tag processing
            # Async here too: when stdout is redirected to a file (service /
            # test harness) every line pays the same write+flush as the file
            # sink and must not throttle the engine (see above).
            enqueue=True,
        )

    # Intercept standard logging (for modules using logging.getLogger).
    #
    # The level is the resolved one, not loguru's usual `level=0` recipe: a
    # NOTSET root logger answers True to `isEnabledFor(DEBUG)` (0 <= 10), so
    # every DEBUG line a library emits builds a LogRecord and walks the stack in
    # `findCaller` before crossing into loguru — which then drops the record,
    # because the sinks above are configured at this same level. Measured on the
    # P2P burst: 14-26 such records per transferred file, 6.5 us each, against
    # 0.08 us for the level check that avoids them. Output is unchanged: with
    # `--debug` the level is DEBUG and everything still flows.
    logging.basicConfig(handlers=[InterceptHandler()], level=level, force=True)

    # aioquic logs every QUIC connection lifecycle at INFO ("Retrying with
    # token", "Negotiated protocol version", "ALPN negotiated protocol",
    # "Connection close sent") — several lines per boot/1 exchange. The bootnode
    # client already logs one concise line per exchange, so keep the library at
    # WARNING (still surfaced) unless the user asked for the full detail with
    # ``--debug``, where its diagnostics remain useful.
    aioquic_level = logging.DEBUG if debug else logging.WARNING
    for noisy_logger in ("quic", "http3", "aioquic"):
        logging.getLogger(noisy_logger).setLevel(aioquic_level)

    # Configure specific loggers to use intercept handler
    for logger_name in (
        "hsync",
        "hsync_client",
        "hsync.p2p",
        "hsync.p2p_server",
        "hsync.p2p_websocket",
    ):
        logging.getLogger(logger_name).handlers = [InterceptHandler()]
        logging.getLogger(logger_name).propagate = False

    return logger


def flush_logs() -> None:
    """Block until every enqueued log record has been written (best-effort).

    Both sinks are asynchronous (``enqueue=True``), so a process that logs and
    then terminates immediately can lose the last records: the writer thread
    races the exit and the stream may already be closed when it flushes
    (observed as ``ValueError: I/O operation on closed file`` from loguru's
    queued writer, with the message — typically the *reason* for a fatal exit —
    silently missing from the user's output).

    Every fatal exit path must therefore flush before leaving. Best-effort by
    contract: a failure here must never replace the error being reported.

    Consumers: ``main_logic`` (instance-lock refusal and the run's exit paths)
    and ``cli.run`` (the process entry point, which drains only when the logging
    stack was actually imported, so the ``--version`` / ``--help`` fast paths
    keep their minimal import surface).
    """
    try:
        logger.complete()
    except Exception:  # pragma: no cover - flushing must never break the exit path
        pass
