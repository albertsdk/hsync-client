# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
CLI argument validators for HSYNC Client.

This module provides validation functions for command-line arguments,
used with argparse's 'type' parameter.
"""

from argparse import ArgumentTypeError
from collections.abc import Callable
from pathlib import Path

from hsync_common.path_validation import has_dotdot_component

from hsync_client.constants import (
    HTTP_TIMEOUT_MAX,
    HTTP_TIMEOUT_MIN,
    MAX_WAIT_TIMEOUT_MAX,
    MAX_WAIT_TIMEOUT_MIN,
    RECONNECT_DELAY_MAX,
    RECONNECT_DELAY_MIN,
    RESYNC_MAX,
    RESYNC_MIN,
    STABILITY_DELAY_MAX,
    STABILITY_DELAY_MIN,
)


def create_int_range_validator(
    min_value: int,
    max_value: int,
    name: str = "value",
) -> Callable[[str], int]:
    """
    Create a validator function for integer values within a range.

    Factory function that generates argparse type validators for
    integer values that must fall within specified bounds.

    Args:
        min_value: Minimum allowed value (exclusive).
        max_value: Maximum allowed value (exclusive).
        name: Name of the value for error messages.

    Returns:
        A validator function suitable for argparse type parameter.
    """

    def validator(value: str) -> int:
        try:
            ivalue = int(value)
        except ValueError:
            raise ArgumentTypeError(f"The {name} '{value}' must be an integer.")

        if ivalue <= min_value or ivalue >= max_value:
            raise ArgumentTypeError(f"The {name} must be between {min_value} and {max_value}.")

        return ivalue

    return validator


# =============================================================================
# Pre-configured Validators
# =============================================================================


def valid_reconnect_delay(value: str) -> int:
    """
    Validate reconnection delay argument.

    Used as an argparse type validator. Value must be between
    RECONNECT_DELAY_MIN and RECONNECT_DELAY_MAX seconds.

    Args:
        value: String value from command line.

    Returns:
        Validated integer value.

    Raises:
        ArgumentTypeError: If value is not within valid range.
    """
    validator = create_int_range_validator(
        RECONNECT_DELAY_MIN,
        RECONNECT_DELAY_MAX,
        "reconnect delay",
    )
    return validator(value)


def valid_resync(value: str) -> int:
    """
    Validate resync interval argument.

    Used as an argparse type validator. Value must be between
    RESYNC_MIN and RESYNC_MAX seconds.

    Args:
        value: String value from command line.

    Returns:
        Validated integer value.

    Raises:
        ArgumentTypeError: If value is not within valid range.
    """
    validator = create_int_range_validator(
        RESYNC_MIN,
        RESYNC_MAX,
        "resync interval",
    )
    return validator(value)


def valid_bucket(value: str) -> str:
    """
    Validate bucket name argument.

    Bucket names must contain only alphanumeric characters, hyphens
    and underscores. They cannot start or end with a hyphen or underscore.

    Args:
        value: Bucket name from command line.

    Returns:
        Validated bucket name.

    Raises:
        ArgumentTypeError: If name contains invalid characters.
    """
    from re import match as re_match

    if not re_match(r"^[a-zA-Z0-9]([a-zA-Z0-9_-]*[a-zA-Z0-9])?$", value):
        raise ArgumentTypeError(
            "The bucket name must be alphanumeric (hyphens and underscores "
            "allowed, but not at the beginning or end)."
        )
    return value


def valid_stability_delay(value: str) -> int:
    """
    Validate file stability delay argument.

    Used as an argparse type validator. Value must be between
    STABILITY_DELAY_MIN and STABILITY_DELAY_MAX seconds.

    Args:
        value: String value from command line.

    Returns:
        Validated integer value.

    Raises:
        ArgumentTypeError: If value is not within valid range.
    """
    validator = create_int_range_validator(
        STABILITY_DELAY_MIN - 1,  # -1 because validator uses exclusive bounds
        STABILITY_DELAY_MAX + 1,
        "stability delay",
    )
    return validator(value)


def valid_http_timeout(value: str) -> int:
    """
    Validate HTTP timeout argument.

    Used as an argparse type validator. Value must be between
    HTTP_TIMEOUT_MIN and HTTP_TIMEOUT_MAX seconds.

    Args:
        value: String value from command line.

    Returns:
        Validated integer value.

    Raises:
        ArgumentTypeError: If value is not within valid range.
    """
    validator = create_int_range_validator(
        HTTP_TIMEOUT_MIN - 1,  # -1 because validator uses exclusive bounds
        HTTP_TIMEOUT_MAX + 1,
        "HTTP timeout",
    )
    return validator(value)


def valid_max_wait_timeout(value: str) -> int:
    """
    Validate max wait timeout argument.

    Used as an argparse type validator. Value must be between
    MAX_WAIT_TIMEOUT_MIN and MAX_WAIT_TIMEOUT_MAX seconds.

    Args:
        value: String value from command line.

    Returns:
        Validated integer value.

    Raises:
        ArgumentTypeError: If value is not within valid range.
    """
    validator = create_int_range_validator(
        MAX_WAIT_TIMEOUT_MIN - 1,  # -1 because validator uses exclusive bounds
        MAX_WAIT_TIMEOUT_MAX + 1,
        "max wait timeout",
    )
    return validator(value)


def valid_bucket_path(value: str) -> str:
    """
    Validate bucket path argument for path traversal attacks.

    Checks that the provided path:
    - Is a valid directory path
    - Does not contain path traversal sequences (..)
    - Resolves to a path within the expected base directory

    Args:
        value: Path to the local bucket directory.

    Returns:
        Validated and normalized path.

    Raises:
        ArgumentTypeError: If path is invalid or contains traversal sequences.
    """
    from os.path import normpath

    # Normalize the path to resolve any .. or . components
    normalized = normpath(value)

    # Check for path traversal attempts (".." as a component, not substring,
    # so legitimate names like "my..backup" are not rejected)
    if has_dotdot_component(normalized):
        raise ArgumentTypeError(
            "Path contains invalid traversal sequence (..). "
            "Paths outside the bucket directory are not allowed."
        )

    # Convert to Path object for validation
    try:
        Path(normalized).resolve()
    except (ValueError, OSError) as e:
        raise ArgumentTypeError(f"Invalid path: {e}")

    return value


__all__ = [
    "create_int_range_validator",
    "valid_reconnect_delay",
    "valid_resync",
    "valid_bucket",
    "valid_stability_delay",
    "valid_http_timeout",
    "valid_max_wait_timeout",
    "valid_bucket_path",
]
