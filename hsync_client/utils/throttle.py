# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Bandwidth parsing and token-bucket throttling."""

import time

# Track bandwidth tokens for rate limiting
_bandwidth_tokens: dict[str, float] = {}
_bandwidth_last_check: dict[str, float] = {}


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

    Should be called between sync sessions to avoid stale token
    bucket state carrying over from previous operations.
    """
    _bandwidth_tokens.clear()
    _bandwidth_last_check.clear()


def get_throttle_delay(
    bytes_transferred: int,
    bandwidth_limit: int | None,
    transfer_key: str,
) -> float:
    """
    Calculate delay needed to throttle transfer to bandwidth limit.

    Uses token bucket algorithm for smooth rate limiting.

    Args:
        bytes_transferred: Number of bytes just transferred.
        bandwidth_limit: Maximum bytes per second (None for unlimited).
        transfer_key: Unique key for this transfer (e.g., "upload" or "download").

    Returns:
        Delay in seconds to wait before next chunk (0 if no throttling needed).
    """
    if bandwidth_limit is None or bandwidth_limit <= 0:
        return 0.0

    current_time = time.monotonic()

    # Initialize with full bucket so the first transfer is not penalised
    if transfer_key not in _bandwidth_tokens:
        _bandwidth_tokens[transfer_key] = float(bandwidth_limit)
        _bandwidth_last_check[transfer_key] = current_time

    elapsed = current_time - _bandwidth_last_check[transfer_key]
    tokens_to_add = elapsed * bandwidth_limit
    _bandwidth_tokens[transfer_key] += tokens_to_add
    _bandwidth_last_check[transfer_key] = current_time

    # Cap tokens at 2x bandwidth to allow burst
    max_tokens = bandwidth_limit * 2
    _bandwidth_tokens[transfer_key] = min(_bandwidth_tokens[transfer_key], max_tokens)

    if _bandwidth_tokens[transfer_key] >= bytes_transferred:
        _bandwidth_tokens[transfer_key] -= bytes_transferred
        return 0.0
    tokens_needed = bytes_transferred - _bandwidth_tokens[transfer_key]
    delay = tokens_needed / bandwidth_limit
    # The caller waits for `delay`, so the drained bucket refills meanwhile.
    _bandwidth_tokens[transfer_key] = 0.0
    return delay
