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
Utility functions for HSYNC Client.

This module provides common utility functions for path handling,
file operations, and data formatting.
"""

import logging
import stat

from hashlib import md5
from os import chmod, stat as os_stat
from os.path import abspath, exists, normpath
from pathlib import Path
from random import choices
from string import ascii_letters, digits
from typing import Optional

from hsync_client.config import CONFIG
from hsync_client.constants import (
    CID_LENGTH,
    CHUNK_SIZE_LARGE,
    CHUNK_SIZE_MEDIUM,
    CHUNK_SIZE_SMALL,
    FILE_SIZE_MEDIUM,
    FILE_SIZE_SMALL,
    FORBIDDEN_FILENAME_CHARS,
    FORBIDDEN_PATH_CHARS,
    MD5_CHUNK_SIZE,
    RESERVED_FILENAMES,
)


# Logger instance for this module
LOG = logging.getLogger(__name__)


# =============================================================================
# SSL Utilities
# =============================================================================


def get_ssl_verify() -> bool | str:
    """
    Get the SSL verification setting for requests library.

    Returns:
        - False if SSL verification is disabled (--insecure)
        - Path to CA certificate if --ca-cert is provided
        - True for default SSL verification
    """
    if CONFIG.get("NO_VERIFY_SSL"):
        return False
    if CONFIG.get("CA_CERT"):
        return str(CONFIG["CA_CERT"])
    return True


# =============================================================================
# Path Utilities
# =============================================================================


def is_valid_logfile(path_string: str) -> bool:
    """
    Validate a log file path for correctness.

    Checks for forbidden characters and reserved names on Windows.
    If a directory is specified, the path must be absolute.

    Args:
        path_string: Path to validate.

    Returns:
        True if the path is valid, False otherwise.
    """
    try:
        path = Path(path_string)

        # If there's no parent directory, assume it's just a filename
        if path.parent == Path("."):
            # Basic check for forbidden characters in the filename
            if any(char in path.name for char in FORBIDDEN_FILENAME_CHARS):
                return False

            # Verify the filename doesn't use reserved names (Windows check)
            if path.name.upper() in RESERVED_FILENAMES:
                return False

        else:
            # Check for absolute path if a directory is present
            if not path.is_absolute():
                return False

            # Check each part of the path for forbidden characters
            for part in path.parts:
                if any(char in part for char in FORBIDDEN_PATH_CHARS):
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

