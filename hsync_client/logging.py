"""
Logging configuration for HSYNC Client.

This module provides a standardized logging setup that matches the server's
format while supporting client-specific features like quiet mode and file output.
"""

import logging
import sys

from json import dumps
from typing import Optional


class JsonFormatter(logging.Formatter):
    """
    Custom formatter that supports JSON data in log messages.
    
    Extends the standard formatter to handle optional structured data
    that can be appended to log messages as formatted JSON.
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
) -> logging.Logger:
    """
    Configure and return the HSYNC client logger.

    Sets up logging with a format matching the server's output style.
    Supports console output, file output, and quiet mode.

    Args:
        debug: Enable DEBUG level logging.
        quiet: Suppress all output.
        logfile: Optional path to log file.

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
    
    # Create formatter matching server format
    # Format: 2024-01-15T10:30:45.123 - [12345] INFO: message
    formatter = JsonFormatter(
        fmt='%(asctime)s.%(msecs)03d - [%(process)d] %(levelname)s: %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%SZ'
    )
    
    if logfile:
        # File handler
        file_handler = logging.FileHandler(logfile, mode='a', encoding='utf-8')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    else:
        # Console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
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

