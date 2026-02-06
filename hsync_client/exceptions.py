# HSYNC Client - HTTP-based file synchronization client
# Copyright (C) 2025 Alberto Basso
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

from typing import Any, Optional


class HSyncClientError(Exception):
    """
    Base exception for all HSYNC Client errors.

    Attributes:
        message: Human-readable error message.
        error_code: Unique error code for debugging (e.g., "HSYNC-C1001").
        details: Optional additional error details.
    """

    error_code: str = "HSYNC-C5000"
    default_message: str = "An unexpected error occurred"

    def __init__(
        self,
        message: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        self.message = message or self.default_message
        self.details = details or {}
        super().__init__(self.message)


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

