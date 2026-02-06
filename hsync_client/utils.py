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

import stat

from hashlib import md5
from os import chmod, stat as os_stat
from os.path import abspath, exists, normpath
from pathlib import Path
from random import choices
from string import ascii_letters, digits
from typing import Optional

from loguru import logger as LOG

from hsync_client.config import CONFIG
from hsync_client.constants import (
    CID_LENGTH,
    CHUNK_SIZE_LARGE,
    CHUNK_SIZE_MEDIUM,
    CHUNK_SIZE_SMALL,
    CONTROL_CHARS,
    FILE_SIZE_MEDIUM,
    FILE_SIZE_SMALL,
    FORBIDDEN_FILENAME_CHARS,
    FORBIDDEN_PATH_CHARS,
    MD5_CHUNK_SIZE,
    PROBLEMATIC_TRAILING_CHARS,
    RESERVED_FILENAMES,
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
        LOG.warning("Cannot verify fingerprint: no host in URL")
        return False

    actual_fingerprint = get_server_cert_fingerprint(host, port)
    if not actual_fingerprint:
        LOG.warning(f"Cannot get certificate from {host}:{port}")
        return False

    if actual_fingerprint.lower() != expected_fingerprint.lower():
        LOG.error(
            f"Certificate fingerprint mismatch!\n"
            f"  Expected: {expected_fingerprint}\n"
            f"  Actual:   {actual_fingerprint}"
        )
        return False

    LOG.debug(f"Certificate fingerprint verified: {actual_fingerprint[:16]}...")
    return True


# =============================================================================
# Path Utilities
# =============================================================================


def _has_forbidden_chars(name: str, chars: frozenset[str]) -> bool:
    """Check if name contains any forbidden characters."""
    return any(char in name for char in chars)


def _has_control_chars(name: str) -> bool:
    """Check if name contains any control characters (ASCII 1-31)."""
    return any(char in name for char in CONTROL_CHARS)


def _is_reserved_name(name: str) -> bool:
    """
    Check if name is a Windows reserved filename.

    Handles names with or without extensions (e.g., NUL.txt is also reserved).
    """
    # Get base name without extension for checking reserved names
    base_name = name.split(".")[0].upper() if "." in name else name.upper()
    return base_name in RESERVED_FILENAMES


def _has_problematic_trailing(name: str) -> bool:
    """Check if name ends with problematic characters (space or period)."""
    return bool(name) and name[-1] in PROBLEMATIC_TRAILING_CHARS


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

    if _has_forbidden_chars(filename, FORBIDDEN_FILENAME_CHARS):
        return False

    if _has_control_chars(filename):
        return False

    if _is_reserved_name(filename):
        return False

    if _has_problematic_trailing(filename):
        return False

    return True


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
            if _has_forbidden_chars(name, FORBIDDEN_FILENAME_CHARS):
                return False

            # Check for control characters
            if _has_control_chars(name):
                return False

            # Check for Windows reserved names (including with extensions)
            if _is_reserved_name(name):
                return False

            # Check for problematic trailing characters
            if _has_problematic_trailing(name):
                return False

        else:
            # Check for absolute path if a directory is present
            if not path.is_absolute():
                return False

            # Check each part of the path for forbidden and control characters
            for part in path.parts:
                if _has_forbidden_chars(part, FORBIDDEN_PATH_CHARS):
                    return False
                if _has_control_chars(part):
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
        return path.decode('utf-8')
    return str(path)


# =============================================================================
# Client ID Generation
# =============================================================================


def get_cid(length: int = CID_LENGTH) -> str:
    """
    Generate a random client ID.

    Creates a unique identifier for this client session used to
    identify the source of events and prevent echo.

    Args:
        length: Length of the ID string (default: CID_LENGTH).

    Returns:
        Random alphanumeric string.
    """
    characters = ascii_letters + digits
    random_string = "".join(choices(characters, k=length))
    return random_string


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


def compute_md5(filepath: str, chunk_size: int = MD5_CHUNK_SIZE) -> str:
    """
    Compute the MD5 checksum of a file.

    Reads the file in chunks to avoid loading large files entirely
    into memory.

    Args:
        filepath: Path to the file.
        chunk_size: Size of chunks to read (default: MD5_CHUNK_SIZE).

    Returns:
        MD5 hash as hexadecimal string.
    """
    hash_md5 = md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


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


def get_chunk_size(file_size: Optional[int]) -> int:
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

    if file_size < FILE_SIZE_SMALL:      # < 1MB
        return CHUNK_SIZE_SMALL          # 8KB
    elif file_size < FILE_SIZE_MEDIUM:   # < 100MB
        return CHUNK_SIZE_MEDIUM         # 64KB
    else:
        return CHUNK_SIZE_LARGE          # 256KB


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
    for i, section in enumerate(
        [permissions[1:4], permissions[4:7], permissions[7:10]]
    ):
        for char in section:
            if char in permission_types:
                mode |= permission_types[char] << (6 - (i * 3))

    LOG.debug(f'[PERMISSION] Set permission "{permissions}" to {file_path}')
    chmod(file_path, mode)

