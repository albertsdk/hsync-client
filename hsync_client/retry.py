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

from requests import RequestException
from tenacity import (
    before_sleep_log,
    retry,
    retry_base,
    retry_if_exception_type,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from hsync_client.constants import (
    TRANSFER_RETRY_ATTEMPTS_DEFAULT,
    TRANSFER_RETRY_MAX_WAIT_DEFAULT,
    TRANSFER_RETRY_MIN_WAIT_DEFAULT,
)
from hsync_client.exceptions import AuthenticationError, NetworkError, ServerError

# =============================================================================
# Retry Configuration
# =============================================================================

# Default retry settings (single source: hsync_client.constants).
DEFAULT_MAX_ATTEMPTS = TRANSFER_RETRY_ATTEMPTS_DEFAULT
DEFAULT_MIN_WAIT = TRANSFER_RETRY_MIN_WAIT_DEFAULT
DEFAULT_MAX_WAIT = TRANSFER_RETRY_MAX_WAIT_DEFAULT

# Route tenacity's before-sleep logs through the stdlib logger so they pass
# the InterceptHandler, which neutralizes loguru markup (tenacity messages
# embed qualified names containing ``<locals>`` that would otherwise be parsed
# as a color tag and raise ``ValueError``).
_retry_log = logging.getLogger("hsync_client.retry")


def _retry_on(
    exc_types: tuple[type[BaseException], ...] | type[BaseException],
    max_attempts: int,
    min_wait: float,
    max_wait: float,
    *,
    exclude: type[BaseException] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Build a tenacity retry decorator for *exc_types* with exponential backoff.

    ``exclude`` narrows the retry set: exceptions of that type (or a subclass)
    are never retried even when they also match ``exc_types``. This keeps
    permanent failures out of the retry loop while the broader type still
    covers the transient ones.
    """
    predicate: retry_base = retry_if_exception_type(exc_types)
    if exclude is not None:
        predicate = predicate & retry_if_not_exception_type(exclude)
    return retry(
        retry=predicate,
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
        before_sleep=before_sleep_log(_retry_log, logging.WARNING),
        reraise=True,
    )


def retry_on_network_error(
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    min_wait: float = DEFAULT_MIN_WAIT,
    max_wait: float = DEFAULT_MAX_WAIT,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    Create a retry decorator for network operations.

    Retries on ``RequestException`` and ``NetworkError`` with exponential
    backoff, but never on ``AuthenticationError``: an auth failure (401/403)
    is permanent and would not be resolved by retrying.

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
    return _retry_on(
        (RequestException, NetworkError),
        max_attempts,
        min_wait,
        max_wait,
        exclude=AuthenticationError,
    )


def retry_on_transient_transfer_error(
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    min_wait: float = DEFAULT_MIN_WAIT,
    max_wait: float = DEFAULT_MAX_WAIT,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    Create a retry decorator for per-file transfers.

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
    return _retry_on((RequestException, ServerError), max_attempts, min_wait, max_wait)
