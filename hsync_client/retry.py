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
Retry utilities using tenacity for HSYNC Client.

This module provides retry decorators with exponential backoff for
handling transient network failures gracefully.
"""

import logging
from typing import Any, Callable

from requests import RequestException
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from hsync_client.exceptions import NetworkError


LOG = logging.getLogger(__name__)


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
        before_sleep=before_sleep_log(LOG, logging.WARNING),
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
        before_sleep=before_sleep_log(LOG, logging.WARNING),
        reraise=True,
    )

