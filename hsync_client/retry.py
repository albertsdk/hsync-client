# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Retry utilities using tenacity for HSYNC Client.

This module provides retry decorators with exponential backoff for
handling transient network failures gracefully.
"""

import logging
from collections.abc import Callable
from typing import Any

from loguru import logger as log
from requests import RequestException
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from hsync_client.exceptions import NetworkError, ServerError

# =============================================================================
# Retry Configuration
# =============================================================================

# Default retry settings
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MIN_WAIT = 1  # seconds
DEFAULT_MAX_WAIT = 10  # seconds


def retry_on_network_error(
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    min_wait: float = DEFAULT_MIN_WAIT,
    max_wait: float = DEFAULT_MAX_WAIT,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    Create a retry decorator for network operations.

    Retries on RequestException and NetworkError with exponential backoff.

    Args:
        max_attempts: Maximum number of retry attempts.
        min_wait: Minimum wait time between retries in seconds.
        max_wait: Maximum wait time between retries in seconds.

    Returns:
        A retry decorator configured with the specified parameters.

    Example:
        @retry_on_network_error(max_attempts=3)
        def fetch_data():
            response = requests.get(url)
            return response.json()
    """
    return retry(
        retry=retry_if_exception_type((RequestException, NetworkError)),
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=True,
    )


def retry_on_connection_error(
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    min_wait: float = DEFAULT_MIN_WAIT,
    max_wait: float = DEFAULT_MAX_WAIT,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    Create a retry decorator specifically for connection errors.

    Retries only on RequestException (connection-level errors).

    Args:
        max_attempts: Maximum number of retry attempts.
        min_wait: Minimum wait time between retries in seconds.
        max_wait: Maximum wait time between retries in seconds.

    Returns:
        A retry decorator configured with the specified parameters.
    """
    return retry(
        retry=retry_if_exception_type(RequestException),
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=True,
    )


def retry_on_transient_transfer_error(
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    min_wait: float = DEFAULT_MIN_WAIT,
    max_wait: float = DEFAULT_MAX_WAIT,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    Create a retry decorator for per-file transfers (§11).

    Retries only on transient failures: connection-level errors
    (``RequestException``) and server-side 5xx responses (raised as
    ``ServerError``). Permanent errors (4xx) are signalled differently by the
    caller and are therefore never retried. The last exception is re-raised
    after the final attempt so the caller can surface ``transfer_failed``.

    Args:
        max_attempts: Maximum number of attempts (1 disables retrying).
        min_wait: Minimum wait time between retries in seconds.
        max_wait: Maximum wait time between retries in seconds.

    Returns:
        A retry decorator configured with the specified parameters.
    """
    return retry(
        retry=retry_if_exception_type((RequestException, ServerError)),
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=True,
    )
