# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Custom exceptions for HSYNC Client.

This module defines a hierarchy of exceptions for better error handling
and more informative error messages. Each exception includes an error
code for debugging purposes.

Error Code Format: HSYNC-CXXXX (C = Client)
- C1XXX: Configuration/Validation errors
- C2XXX: Network/Connection errors
- C3XXX: Sync operation errors
- C4XXX: File operation errors
"""

from hsync_common.exceptions import HSyncError


class HSyncClientError(HSyncError):
    """
    Base exception for all HSYNC Client errors.

    Inherits the common construction contract (``message``, ``error_code``,
    ``details``, ``to_dict()``) from ``hsync_common.exceptions.HSyncError``
    and uses the client-specific default error code (``HSYNC-C5000``).
    """

    error_code: str = "HSYNC-C5000"
    default_message: str = "An unexpected error occurred"


# =============================================================================
# Configuration/Validation Errors (C1XXX)
# =============================================================================


class ConfigurationError(HSyncClientError):
    """Configuration is invalid or missing."""

    error_code = "HSYNC-C1000"
    default_message = "Configuration error"


class InvalidLocalPathError(ConfigurationError):
    """Local directory path is invalid or does not exist."""

    error_code = "HSYNC-C1001"
    default_message = "Invalid local path"


class InvalidServerUrlError(ConfigurationError):
    """Server URL is invalid."""

    error_code = "HSYNC-C1002"
    default_message = "Invalid server URL"


class InvalidBucketNameError(ConfigurationError):
    """Bucket name is invalid."""

    error_code = "HSYNC-C1003"
    default_message = "Invalid bucket name"


# =============================================================================
# Network/Connection Errors (C2XXX)
# =============================================================================


class NetworkError(HSyncClientError):
    """Network operation failed."""

    error_code = "HSYNC-C2000"
    default_message = "Network error"


class ConnectionError(NetworkError):
    """Failed to connect to server."""

    error_code = "HSYNC-C2001"
    default_message = "Connection failed"


class AuthenticationError(NetworkError):
    """Authentication with server failed."""

    error_code = "HSYNC-C2002"
    default_message = "Authentication failed"


class WebSocketError(NetworkError):
    """WebSocket connection error."""

    error_code = "HSYNC-C2003"
    default_message = "WebSocket error"


class ServerError(NetworkError):
    """Server returned an error response."""

    error_code = "HSYNC-C2004"
    default_message = "Server error"


# =============================================================================
# Sync Operation Errors (C3XXX)
# =============================================================================


class SyncError(HSyncClientError):
    """Synchronization operation failed."""

    error_code = "HSYNC-C3000"
    default_message = "Sync error"


class UploadError(SyncError):
    """File upload failed."""

    error_code = "HSYNC-C3001"
    default_message = "Upload failed"


class DownloadError(SyncError):
    """File download failed."""

    error_code = "HSYNC-C3002"
    default_message = "Download failed"


class PermanentDownloadError(DownloadError):
    """Permanent download failure (e.g. file exceeds the size cap).

    Raised instead of returning silently so the caller can add the filepath
    to a skip-set and stop retrying it on every resync cycle.
    """

    error_code = "HSYNC-C3004"
    default_message = "Download permanently failed"


class ChecksumError(SyncError):
    """Checksum verification failed."""

    error_code = "HSYNC-C3003"
    default_message = "Checksum mismatch"


# =============================================================================
# File Operation Errors (C4XXX)
# =============================================================================


class FileOperationError(HSyncClientError):
    """Local file operation failed."""

    error_code = "HSYNC-C4000"
    default_message = "File operation failed"


class FileReadError(FileOperationError):
    """Failed to read file."""

    error_code = "HSYNC-C4001"
    default_message = "File read failed"


class FileWriteError(FileOperationError):
    """Failed to write file."""

    error_code = "HSYNC-C4002"
    default_message = "File write failed"


class PermissionError(FileOperationError):
    """Permission denied for file operation."""

    error_code = "HSYNC-C4003"
    default_message = "Permission denied"


class InsufficientDiskSpaceError(FileOperationError):
    """Insufficient disk space for sync operation."""

    error_code = "HSYNC-C4004"
    default_message = "Insufficient disk space"


# =============================================================================
# Utility Functions
# =============================================================================
#
# The generic ``format_exception`` / ``log_exception`` helpers live in
# ``hsync_common.exceptions``; import them from there when needed.
