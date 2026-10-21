# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Bandwidth parsing and schedule-based rate limiting."""

import time

# Per-direction time by which the bytes already handed out finish draining at
# the configured rate. One entry per transfer direction ("upload"/"download"),
# so concurrent transfers of the same direction share the budget instead of
# each getting the full limit.
_bandwidth_due: dict[str, float] = {}


def parse_bandwidth(value: str | int | None) -> int | None:
    """
    Parse bandwidth value from string or integer.

    Accepts:
    - Integer bytes per second
    - String with suffix: "KB", "MB", "GB", "KB/s", "MB/s", "GB/s"
    - None or 0 for unlimited

    Args:
        value: Bandwidth value to parse.

    Returns:
        Bandwidth in bytes per second, or None for unlimited.

    Raises:
        ValueError: If the value is invalid.
    """
    if value is None or value == 0 or value == "0":
        return None

    if isinstance(value, int):
        if value < 0:
            raise ValueError("Bandwidth must be positive")
        return value

    if isinstance(value, str):
        value = value.strip().upper()
        if not value:
            return None

        # Parse value with suffix
        multipliers = {
            "KB/S": 1024,
            "KB": 1024,
            "MB/S": 1024**2,
            "MB": 1024**2,
            "GB/S": 1024**3,
            "GB": 1024**3,
            "B/S": 1,
            "B": 1,
        }

        for suffix, mult in multipliers.items():
            if value.endswith(suffix):
                num_str = value[: -len(suffix)].strip()
                try:
                    return int(float(num_str) * mult)
                except ValueError:
                    raise ValueError(f"Invalid bandwidth value: {value}")

        # Try parsing as plain number (bytes)
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"Invalid bandwidth value: {value}")

    raise ValueError(f"Invalid bandwidth type: {type(value)}")


def reset_bandwidth_throttle() -> None:
    """
    Reset bandwidth throttle state.

    Should be called between sync sessions so a stale schedule does not
    delay the first chunks of the next one.
    """
    _bandwidth_due.clear()


def get_throttle_delay(
    bytes_transferred: int,
    bandwidth_limit: int | None,
    transfer_key: str,
) -> float:
    """
    Calculate how long the caller must wait so the stream keeps to the rate.

    The limiter schedules each chunk on a per-direction timeline instead of
    banking credit in a token bucket: chunk *n* is released ``n / limit``
    seconds after chunk *n-1*, so the sustained rate is the configured one from
    the very first byte. Idle time grants no credit (``max(now, due)``), which
    is what removes the 2x burst the old bucket allowed after a pause or at the
    start of a transfer.

    Args:
        bytes_transferred: Number of bytes just transferred.
        bandwidth_limit: Maximum bytes per second (None for unlimited).
        transfer_key: Unique key for this transfer (e.g., "upload" or "download").

    Returns:
        Delay in seconds to wait before the next chunk (0 if no delay needed).
    """
    if bandwidth_limit is None or bandwidth_limit <= 0 or bytes_transferred <= 0:
        return 0.0

    now = time.monotonic()
    # A transfer that starts (or resumes) after an idle period starts from now:
    # the bytes already drained, so there is no backlog to pay off either.
    start = max(now, _bandwidth_due.get(transfer_key, now))
    budget = bytes_transferred / bandwidth_limit
    _bandwidth_due[transfer_key] = start + budget
    # Adding the two terms instead of subtracting the sums keeps the no-backlog
    # case exact (``now + budget - now`` loses precision).
    return (start - now) + budget
