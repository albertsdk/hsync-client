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
# Network/Connection Errors (C2XXX)
# =============================================================================


class NetworkError(HSyncClientError):
    """Network operation failed."""

    error_code = "HSYNC-C2000"
    default_message = "Network error"


class AuthenticationError(NetworkError):
    """Authentication with server failed."""

    error_code = "HSYNC-C2002"
    default_message = "Authentication failed"


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


class UploadError(SyncError):
    """File upload failed."""

    error_code = "HSYNC-C3003"
    default_message = "Upload failed"


class PermanentUploadError(UploadError):
    """Permanent upload failure (e.g. server rejected the payload with 4xx).

    Raised instead of ``HTTPError`` so the retry decorator does not retry a
    deterministic client-side rejection, and so the response body (which
    carries the server's diagnostic message) is preserved in the exception
    text for logging.
    """

    error_code = "HSYNC-C3005"
    default_message = "Upload permanently failed"


# =============================================================================
# File Operation Errors (C4XXX)
# =============================================================================


class FileOperationError(HSyncClientError):
    """Local file operation failed."""

    error_code = "HSYNC-C4000"
    default_message = "File operation failed"


class InsufficientDiskSpaceError(FileOperationError):
    """Insufficient disk space for sync operation."""

    error_code = "HSYNC-C4004"
    default_message = "Insufficient disk space"
