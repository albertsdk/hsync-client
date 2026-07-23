# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Constants for HSYNC Client.

This module contains all magic numbers and constant values used
throughout the application, making them easy to find and modify.
"""

from enum import StrEnum

# =============================================================================
# Application Info
# =============================================================================
# Re-export shared constants from hsync-common
from hsync_common.constants import (  # noqa: E402, F401
    CHUNK_SIZE_LARGE,
    CHUNK_SIZE_MEDIUM,
    CHUNK_SIZE_SMALL,
    FILE_SIZE_MEDIUM,
    FILE_SIZE_SMALL,
    HSYNCKEEP_FILENAME,
    PARTIAL_DOWNLOAD_SUFFIX,
    VERSION,
)

# Application version (re-exported from hsync-common)
HSYNC_VERSION = VERSION


# =============================================================================
# Client ID Generation
# =============================================================================

# Client ID Generation (NanoID-style)
# 21 characters with 64-char alphabet = 126 bits of entropy
CID_LENGTH = 21
CID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


# =============================================================================
# File Transfer Settings (chunk sizes re-exported from hsync-common above)
# =============================================================================


# =============================================================================
# File Stability Check
# =============================================================================

# Default interval (seconds) to wait between file size/mtime checks
# before considering a file stable and ready for upload.
# 5 seconds is a reasonable compromise.
FILE_STABILITY_INTERVAL_DEFAULT = 5

# Fast re-check interval (seconds) used when an upload is triggered by an
# on_closed (IN_CLOSE_WRITE) event: the close already signals write completion,
# so a single short re-check is enough to absorb edge cases (NFS chunked writes,
# rapid multi-close, non-atomic saves) before trusting the close and uploading.
STABILITY_DELAY_ON_CLOSED = 0.3

# Limits for stability delay configuration
STABILITY_DELAY_MIN = 1
STABILITY_DELAY_MAX = 60

# Max wait timeout for file stability (seconds)
# Maximum time to wait for a file to stop being written before giving up
MAX_WAIT_TIMEOUT_MIN = 10
MAX_WAIT_TIMEOUT_MAX = 3600
MAX_WAIT_TIMEOUT_DEFAULT = 120  # 2 minutes

# When a file never stabilizes within MAX_WAIT_TIMEOUT the upload is rescheduled
# for a later attempt instead of being forced, avoiding a "torn" upload of
# a file still being written. After UPLOAD_MAX_RESCHEDULES deferrals the file is
# skipped and left to the next file-system event (on_closed) or periodic resync.
UPLOAD_RESCHEDULE_DELAY_DEFAULT = 30  # seconds between deferred attempts
UPLOAD_MAX_RESCHEDULES_DEFAULT = 5


# =============================================================================
# Parallel Transfer Settings
# =============================================================================

# Maximum number of concurrent file transfers (downloads/uploads)
# Higher values increase throughput but also server load.
# Can be overridden via HSYNC_MAX_CONCURRENT_TRANSFERS environment variable
# or --max-concurrent-transfers CLI option.
MAX_CONCURRENT_TRANSFERS_DEFAULT = 5
MAX_CONCURRENT_TRANSFERS_MIN = 1
MAX_CONCURRENT_TRANSFERS_MAX = 20

# =============================================================================
# Per-File Transfer Retry/Backoff
# =============================================================================

# A single transient network blip (timeout, connection reset, 5xx) must not
# leave a file unsynced until the next full sync: each download/upload is
# retried with exponential backoff. Permanent errors (4xx) are never retried.
TRANSFER_RETRY_ATTEMPTS_DEFAULT = 3
TRANSFER_RETRY_MIN_WAIT_DEFAULT = 1  # seconds
TRANSFER_RETRY_MAX_WAIT_DEFAULT = 10  # seconds

# Maximum accepted WebSocket message size. Events carry file metadata only
# (never bulk content), so a small explicit bound is safe and prevents a
# hostile peer from flooding memory with giant frames.
WS_MAX_MESSAGE_BYTES = 4 * 1024 * 1024  # 4 MiB

# =============================================================================
# Mass-Deletion Guard
# =============================================================================

# A master that (temporarily) exposes an empty or wrong bucket must not be able
# to wipe valid local data in a single reconciliation cycle. When a cycle would
# delete more than an absolute count or a percentage of the local files, the
# deletions are paused (skipped) and surfaced for confirmation instead. A master
# with 0 files is always treated as a suspect state, never as a delete command.
MASS_DELETE_GUARD_DEFAULT = True
MASS_DELETE_THRESHOLD_DEFAULT = 50  # absolute deletions per cycle (0 = disabled)
MASS_DELETE_PERCENT_DEFAULT = 50  # percentage of total local files (0 = disabled)

# =============================================================================
# CLI Validation Limits
# =============================================================================

# Bandwidth limits for CLI validation (bytes per second)
BANDWIDTH_MIN = 1024  # 1 KB/s minimum
BANDWIDTH_MAX = 100 * 1024 * 1024  # 100 MB/s maximum

# Reconnection delay limits (seconds)
RECONNECT_DELAY_MIN = 1
RECONNECT_DELAY_MAX = 60
RECONNECT_DELAY_DEFAULT = 5

# Resync interval limits (seconds)
RESYNC_MIN = 20
RESYNC_MAX = 3600
# Periodic resync is enabled by default as a safety net against silently
# missed file-system events (e.g. inotify queue overflow). Set HSYNC_RESYNC=0
# to disable it.
RESYNC_DEFAULT = 300

# HTTP timeout limits (seconds)
HTTP_TIMEOUT_MIN = 1
HTTP_TIMEOUT_MAX = 300
HTTP_TIMEOUT_DEFAULT = 30

# LAN discovery timeout (seconds)
# 0 is reserved as the CLI/env sentinel for "wait indefinitely".
DISCOVERY_TIMEOUT_DEFAULT = 10
# Polling window used when discovery waits indefinitely (timeout == 0): how often
# the LAN loop wakes up to re-evaluate peer/master state. Matches the LAN
# heartbeat cadence (HEARTBEAT_INTERVAL) so we react within one announce cycle
# without busy-spinning while no peer is present.
DISCOVERY_POLL_INTERVAL = 5

# Minimum free disk space limits (MB)
MIN_FREE_DISK_SPACE_MB_MIN = 0  # 0 = disabled
MIN_FREE_DISK_SPACE_MB_MAX = 100_000  # 100 GB
MIN_FREE_DISK_SPACE_MB_DEFAULT = 500  # 500 MB

# Maximum size of a single inbound file (MB), applied to downloads and to
# received LAN uploads as an absolute cap on top of the peer-declared size:
# the effective anti-bomb cap is min(declared, MAX_DOWNLOAD_SIZE). Without an
# absolute cap a hostile peer could declare an arbitrary size and expand a
# decompression bomb up to it on disk before the SHA-256 check discards it.
# The default mirrors the server's max_upload_size (10 GB): larger files
# require raising both.
MAX_DOWNLOAD_SIZE_MB_MIN = 0  # 0 = disabled (cap = declared size only)
MAX_DOWNLOAD_SIZE_MB_MAX = 10_000_000  # 10 TB
MAX_DOWNLOAD_SIZE_MB_DEFAULT = 10_240  # 10 GB

# =============================================================================
# inotify Watch-Limit Diagnostics (Linux only)
# =============================================================================
# The recursive watcher schedules one inotify watch per sub-directory. When the
# tree approaches fs.inotify.max_user_watches, the kernel drops events silently
# (watchdog does not surface IN_Q_OVERFLOW), so we proactively warn the user.
# Periodic resync (RESYNC) remains the safety net that recovers missed events.
INOTIFY_MAX_USER_WATCHES_PATH = "/proc/sys/fs/inotify/max_user_watches"
# Fraction of the limit at which we emit the warning.
INOTIFY_WATCH_WARN_RATIO = 0.9


# HSYNCKEEP_FILENAME is re-exported from hsync_common.constants above


# =============================================================================
# Action Labels for Logging
# =============================================================================


class ActionLabel(StrEnum):
    """Single source of truth for the bracketed action labels in log lines.

    Labels are minimalist (no padding): the log line already carries a
    variable-width level token before the message, so column alignment is not
    achievable and dynamic-width tags keep the output clean and maintainable.
    """

    # Sync operations
    DOWNLOAD = "[DOWNLOAD]"
    UPLOAD = "[UPLOAD]"
    RESUME = "[RESUME]"
    MOVE = "[MOVE]"
    REMOVE = "[REMOVE]"
    DELETE = "[DELETE]"
    CREATE = "[CREATE]"
    PERMISSION = "[PERMISSION]"
    SYMLINK = "[SYMLINK]"
    IN_SYNC = "[IN SYNC]"
    SKIP = "[SKIP]"
    IGNORE_DEL = "[IGNORE DEL]"
    CONFLICT = "[CONFLICT]"
    GUARD = "[GUARD]"
    TOMBSTONE = "[TOMBSTONE]"
    # Preview (dry-run)
    SYNCED = "[SYNCED]"
    TO_DOWNLOAD = "[TO_DOWNLOAD]"
    TO_UPLOAD = "[TO_UPLOAD]"
    TO_DELETE = "[TO_DELETE]"
    TO_RENAME = "[TO_RENAME]"
    # LAN client operations
    CLIENT_UP = "[CLIENT UP]"
    CLIENT_DEL = "[CLIENT DEL]"
    CLIENT_REN = "[CLIENT REN]"
    CLIENT_DIR = "[CLIENT DIR]"
    CLIENT_NEW = "[CLIENT NEW]"
    CLIENT_KEEP = "[CLIENT KEEP]"
    CLIENT_IGN = "[CLIENT IGN]"
    # LAN master broadcast
    LAN_FILE = "[LAN FILE]"
