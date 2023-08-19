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
CLI argument validators for HSYNC Client.

This module provides validation functions for command-line arguments,
used with argparse's 'type' parameter.
"""

from argparse import ArgumentTypeError
from typing import Callable

from hsync_client.constants import (
    RECONNECT_DELAY_MAX,
    RECONNECT_DELAY_MIN,
    RESYNC_MAX,
    RESYNC_MIN,
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
            raise ArgumentTypeError(
                f"The {name} must be between {min_value} and {max_value}."
            )

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

    Bucket names must be alphanumeric only.

    Args:
        value: Bucket name from command line.

    Returns:
        Validated bucket name.

    Raises:
        ArgumentTypeError: If name contains non-alphanumeric characters.
    """
    if not value.isalnum():
        raise ArgumentTypeError("The bucket name must be alphanumeric.")
    return value

