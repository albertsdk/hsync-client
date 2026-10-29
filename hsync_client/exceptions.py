# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

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


class DeclaredSizeMismatchError(PermanentDownloadError):
    """The remote copy outgrew the size its listing declared mid-transfer.

    Unlike the absolute ``MAX_INBOUND_SIZE`` cap (a genuinely permanent
    condition: no retry helps), this is *transient*: the file is still being
    written on the source, so the size the listing declared no longer matches
    the bytes it streams. Callers treat it as a deferral — the next
    ``modified`` event from the peer re-queues the download once the writer
    stabilises — instead of writing the path into the per-cycle skip-set. It
    stays under ``PermanentDownloadError`` so the download retry policy (which
    never retries permanent errors) still applies: a retry in the same moment
    would only hit the same growing file again.
    """

    error_code = "HSYNC-C3006"
    default_message = "The remote copy changed while downloading"


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


class WatcherStartError(FileOperationError):
    """The local file watcher (watchdog) could not start.

    Raised by :class:`hsync_client.watcher.WatcherManager` when both the inotify
    backend AND the :class:`PollingObserver` fallback failed (an inotify limit —
    ``Errno 24`` instance / ``Errno 28`` watch — or descriptor exhaustion). The
    message is the human-readable remediation hint and ``original`` keeps the
    raw :class:`OSError` raised by watchdog, so callers can special-case the
    retry policy (a kernel limit cannot clear by retrying fast) without
    re-parsing the kernel error string.
    """

    error_code = "HSYNC-C4100"
    default_message = "File watcher could not start"

    def __init__(self, message: str = "", original: "OSError | None" = None) -> None:
        super().__init__(message or self.default_message)
        self.original = original
