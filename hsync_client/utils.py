# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Utility functions for HSYNC Client.

This module provides common utility functions for path handling,
file operations, and data formatting.
"""

import secrets
import stat
from hashlib import sha256
from os import chmod
from os import stat as os_stat
from os.path import abspath, exists, normpath
from pathlib import Path

from hsync_common.path_validation import (
    FORBIDDEN_FILENAME_CHARS,
    FORBIDDEN_PATH_CHARS,
    has_control_chars,
    has_forbidden_chars,
    has_problematic_trailing,
    is_reserved_name,
)
from loguru import logger as log

from hsync_client.config import CONFIG
from hsync_client.constants import (
    CHUNK_SIZE_LARGE,
    CHUNK_SIZE_MEDIUM,
    CHUNK_SIZE_SMALL,
    CID_ALPHABET,
    CID_LENGTH,
    FILE_SIZE_MEDIUM,
    FILE_SIZE_SMALL,
    SHA256_CHUNK_SIZE,
)

# =============================================================================
# SSL Utilities
# =============================================================================

# Track if we've already suppressed the InsecureRequestWarning
_insecure_warning_suppressed = False


def _suppress_insecure_warning() -> None:
    """Suppress urllib3 InsecureRequestWarning when using fingerprint verification."""
    global _insecure_warning_suppressed
    if not _insecure_warning_suppressed:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        _insecure_warning_suppressed = True


def get_ssl_verify() -> bool | str:
    """
    Get the SSL verification setting for requests library.

    For LAN mode with MASTER_CERT_FINGERPRINT, returns False to disable
    standard SSL verification (fingerprint verification is done separately).

    Returns:
        - False if SSL verification is disabled (--insecure or LAN fingerprint mode)
        - Path to CA certificate if --ca-cert is provided
        - True for default SSL verification
    """
    if CONFIG.get("NO_VERIFY_SSL"):
        return False
    # In LAN mode with fingerprint, we disable standard SSL verification
    # and verify the fingerprint manually before each request
    if CONFIG.get("MASTER_CERT_FINGERPRINT"):
        # Suppress the InsecureRequestWarning since we verify via fingerprint
        _suppress_insecure_warning()
        return False
    if CONFIG.get("CA_CERT"):
        return str(CONFIG["CA_CERT"])
    return True


def verify_master_fingerprint(url: str) -> bool:
    """
    Verify the master's SSL certificate fingerprint before making a request.

    This should be called before making HTTPS requests to the LAN master
    when MASTER_CERT_FINGERPRINT is set.

    Args:
        url: The URL to verify (used to extract host and port).

    Returns:
        True if fingerprint matches or no fingerprint verification needed.
        False if fingerprint doesn't match.
    """
    expected_fingerprint = CONFIG.get("MASTER_CERT_FINGERPRINT")
    if not expected_fingerprint:
        return True  # No fingerprint verification needed

    from urllib.parse import urlparse

    from hsync_client.ssl_utils import get_server_cert_fingerprint

    parsed = urlparse(url)
    if parsed.scheme != "https":
        return True  # Not HTTPS, no fingerprint to verify

    host = parsed.hostname
    port = parsed.port or 443

    if not host:
        log.warning("Cannot verify fingerprint: no host in URL")
        return False

    actual_fingerprint = get_server_cert_fingerprint(host, port)
    if not actual_fingerprint:
        log.warning(f"Cannot get certificate from {host}:{port}")
        return False

    if actual_fingerprint.lower() != expected_fingerprint.lower():
        log.error(
            f"Certificate fingerprint mismatch!\n"
            f"  Expected: {expected_fingerprint}\n"
            f"  Actual:   {actual_fingerprint}"
        )
        return False

    log.debug(f"Certificate fingerprint verified: {actual_fingerprint[:16]}...")
    return True


# =============================================================================
# Path Utilities
# =============================================================================


def is_valid_filename(filename: str) -> bool:
    """
    Validate a filename for cross-platform compatibility.

    Checks for:
    - Empty filename
    - Forbidden characters (cross-platform)
    - Control characters (ASCII 1-31)
    - Reserved names on Windows (including with extensions, e.g., NUL.txt)
    - Problematic trailing characters (space, period)

    Args:
        filename: The filename to validate (without path).

    Returns:
        True if the filename is valid, False otherwise.
    """
    if not filename:
        return False

    if has_forbidden_chars(filename, FORBIDDEN_FILENAME_CHARS):
        return False

    if has_control_chars(filename):
        return False

    if is_reserved_name(filename):
        return False

    return not has_problematic_trailing(filename)


def is_valid_logfile(path_string: str) -> bool:
    """
    Validate a log file path for correctness.

    Checks for:
    - Forbidden characters (cross-platform)
    - Control characters (ASCII 1-31)
    - Reserved names on Windows (including with extensions, e.g., NUL.txt)
    - Problematic trailing characters (space, period)
    - Absolute path requirement when directory is specified

    Args:
        path_string: Path to validate.

    Returns:
        True if the path is valid, False otherwise.
    """
    try:
        path = Path(path_string)

        # If there's no parent directory, assume it's just a filename
        if path.parent == Path("."):
            name = path.name

            # Check for forbidden characters in the filename
            if has_forbidden_chars(name, FORBIDDEN_FILENAME_CHARS):
                return False

            # Check for control characters
            if has_control_chars(name):
                return False

            # Check for Windows reserved names (including with extensions)
            if is_reserved_name(name):
                return False

            # Check for problematic trailing characters
            if has_problematic_trailing(name):
                return False

        else:
            # Check for absolute path if a directory is present
            if not path.is_absolute():
                return False

            # Check each part of the path for forbidden and control characters
            for part in path.parts:
                if has_forbidden_chars(part, FORBIDDEN_PATH_CHARS):
                    return False
                if has_control_chars(part):
                    return False

        return True
    except Exception:
        return False


def is_same_paths(path1: str, path2: str) -> bool:
    """
    Check if two paths refer to the same location.

    Normalizes and resolves paths to absolute form before comparison.

    Args:
        path1: First path to compare.
        path2: Second path to compare.

    Returns:
        True if paths are equivalent, False otherwise.
    """
    return normpath(abspath(path1)) == normpath(abspath(path2))


def safe_rmdir_if_empty(dir_path: str, bucket_root: str | None = None) -> bool:
    """
    Remove *dir_path* only if it is empty and strictly inside *bucket_root*.

    Acts as a safety wrapper around ``os.rmdir`` to prevent accidental
    removal of the LOCAL_BUCKET root (which would destroy the sync target).

    Args:
        dir_path: Directory to remove if empty.
        bucket_root: Path that must NOT be removed. Defaults to
            ``CONFIG["LOCAL_BUCKET"]`` when not provided.

    Returns:
        True if the directory was removed, False otherwise.
    """
    from os import listdir, rmdir

    if not dir_path or not exists(dir_path):
        return False
    if bucket_root is None:
        bucket_root = CONFIG.get("LOCAL_BUCKET", "")
    if bucket_root and is_same_paths(dir_path, bucket_root):
        return False
    try:
        if listdir(dir_path):
            return False
        rmdir(dir_path)
        return True
    except OSError:
        return False


def ensure_str_path(path: str | bytes | None) -> str:
    """
    Convert a path to string if it's bytes.

    Watchdog events can return paths as bytes or str depending on the platform.
    This function ensures we always work with strings.

    Args:
        path: Path as string, bytes, or None.

    Returns:
        Path as string (empty string if None).
    """
    if path is None:
        return ""
    if isinstance(path, bytes):
        return path.decode("utf-8")
    return str(path)


# =============================================================================
# Client ID Generation
# =============================================================================


def get_cid(length: int = CID_LENGTH) -> str:
    """
    Generate a cryptographically secure client ID (NanoID-style).

    Creates a unique identifier for this client session used to
    identify the source of events and prevent echo. Uses the secrets
    module for cryptographic security.

    The default configuration provides 126 bits of entropy:
    - 21 characters from a 64-character alphabet (URL-safe)
    - Probability of collision: ~1 in 2^126

    Args:
        length: Length of the ID string (default: CID_LENGTH = 21).

    Returns:
        Cryptographically secure random string (URL-safe).
    """
    return "".join(secrets.choice(CID_ALPHABET) for _ in range(length))


# =============================================================================
# Data Formatting
# =============================================================================


def bytes_to_human_readable(size_bytes: float) -> str:
    """
    Convert bytes to human-readable format.

    Automatically selects the appropriate unit (B, KB, MB, GB, etc.)
    and formats with 2 decimal places.

    Args:
        size_bytes: Number of bytes to convert.

    Returns:
        Formatted string like "1.50MB" or "256.00KB".
    """
    units = ["B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB"]
    index = 0
    while size_bytes >= 1024 and index < len(units) - 1:
        size_bytes /= 1024.0
        index += 1
    return f"{size_bytes:.2f}{units[index]}"


# =============================================================================
# File Operations
# =============================================================================


def compute_sha256(filepath: str, chunk_size: int = SHA256_CHUNK_SIZE) -> str:
    """
    Compute the SHA-256 checksum of a file.

    Reads the file in chunks to avoid loading large files entirely
    into memory.

    Args:
        filepath: Path to the file.
        chunk_size: Size of chunks to read (default: SHA256_CHUNK_SIZE).

    Returns:
        SHA-256 hash as hexadecimal string.
    """
    hash_sha256 = sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            hash_sha256.update(chunk)
    return hash_sha256.hexdigest()


def get_permissions(obj_path: str) -> str:
    """
    Get file permissions in symbolic notation.

    Args:
        obj_path: Path to the file or directory.

    Returns:
        Permission string like '-rw-r--r--'.
    """
    obj_stat = os_stat(obj_path)
    permissions = obj_stat.st_mode
    return stat.filemode(permissions)


def get_chunk_size(file_size: int | None) -> int:
    """
    Calculate optimal chunk size based on file size.

    Uses larger chunks for bigger files to improve transfer efficiency
    while keeping memory usage reasonable.

    Args:
        file_size: Size of the file in bytes, or None if unknown.

    Returns:
        Optimal chunk size in bytes (8KB to 256KB).
    """
    if file_size is None:
        return CHUNK_SIZE_MEDIUM  # Default 64KB for unknown size

    if file_size < FILE_SIZE_SMALL:  # < 1MB
        return CHUNK_SIZE_SMALL  # 8KB
    elif file_size < FILE_SIZE_MEDIUM:  # < 100MB
        return CHUNK_SIZE_MEDIUM  # 64KB
    else:
        return CHUNK_SIZE_LARGE  # 256KB


def set_permissions(file_path: str, permissions: str) -> None:
    """
    Set file permissions from symbolic notation.

    Converts symbolic permission string (e.g., '-rw-r--r--') to
    octal mode and applies it to the file.

    Args:
        file_path: Path to the file.
        permissions: Symbolic permission string (10 characters).
    """
    # Skip if permissions already match
    if exists(file_path) and permissions == get_permissions(file_path):
        return

    # Parse symbolic permissions to octal mode
    mode = 0
    permission_types = {"r": 4, "w": 2, "x": 1}
    for i, section in enumerate([permissions[1:4], permissions[4:7], permissions[7:10]]):
        for char in section:
            if char in permission_types:
                mode |= permission_types[char] << (6 - (i * 3))

    log.debug(f'[PERMISSION] Set permission "{permissions}" to {file_path}')
    chmod(file_path, mode)


# =============================================================================
# Bandwidth Limiting
# =============================================================================


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


# Track bandwidth tokens for rate limiting
_bandwidth_tokens: dict[str, float] = {}
_bandwidth_last_check: dict[str, float] = {}


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
    import time

    if bandwidth_limit is None or bandwidth_limit <= 0:
        return 0.0

    current_time = time.monotonic()

    # Initialize with full bucket so the first transfer is not penalised
    if transfer_key not in _bandwidth_tokens:
        _bandwidth_tokens[transfer_key] = float(bandwidth_limit)
        _bandwidth_last_check[transfer_key] = current_time

    # Add tokens based on elapsed time
    elapsed = current_time - _bandwidth_last_check[transfer_key]
    tokens_to_add = elapsed * bandwidth_limit
    _bandwidth_tokens[transfer_key] += tokens_to_add
    _bandwidth_last_check[transfer_key] = current_time

    # Cap tokens at 2x bandwidth to allow burst
    max_tokens = bandwidth_limit * 2
    if _bandwidth_tokens[transfer_key] > max_tokens:
        _bandwidth_tokens[transfer_key] = max_tokens

    # Check if we have enough tokens
    if _bandwidth_tokens[transfer_key] >= bytes_transferred:
        # Consume tokens
        _bandwidth_tokens[transfer_key] -= bytes_transferred
        return 0.0
    else:
        # Calculate delay needed to regenerate tokens
        tokens_needed = bytes_transferred - _bandwidth_tokens[transfer_key]
        delay = tokens_needed / bandwidth_limit
        # Consume all tokens (we'll wait)
        _bandwidth_tokens[transfer_key] = 0.0
        return delay
