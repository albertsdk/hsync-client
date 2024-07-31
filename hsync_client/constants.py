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

# Default chunk size for SHA-256 computation (64KB)
SHA256_CHUNK_SIZE = 65536


# =============================================================================
# File Stability Check
# =============================================================================

# Default interval (seconds) to wait between file size/mtime checks
# before considering a file stable and ready for upload.
# 5 seconds is a reasonable compromise.
FILE_STABILITY_INTERVAL_DEFAULT = 5

# Limits for stability delay configuration
STABILITY_DELAY_MIN = 1
STABILITY_DELAY_MAX = 60

# Max wait timeout for file stability (seconds)
# Maximum time to wait for a file to stop being written before giving up
MAX_WAIT_TIMEOUT_MIN = 10
MAX_WAIT_TIMEOUT_MAX = 3600
MAX_WAIT_TIMEOUT_DEFAULT = 120  # 2 minutes


# =============================================================================
# Parallel Transfer Settings
# =============================================================================

# Maximum number of concurrent file transfers (downloads/uploads)
# Higher values increase throughput but also server load
# Can be overridden via HSYNC_MAX_CONCURRENT_TRANSFERS environment variable
MAX_CONCURRENT_TRANSFERS = int(__import__("os").environ.get("HSYNC_MAX_CONCURRENT_TRANSFERS", "5"))

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


# HSYNCKEEP_FILENAME is re-exported from hsync_common.constants above


# =============================================================================
# Action Labels for Logging
# =============================================================================

# Action labels with column alignment (10 chars for alignment)
ACTION_LABEL = {
    "download": "[DOWNLOAD  ]",
    "upload": "[UPLOAD    ]",
    "move": "[MOVE      ]",
    "remove": "[REMOVE    ]",
    "create": "[CREATE    ]",
    "permission": "[PERMISSION]",
    "symlink": "[SYMLINK   ]",
    "skip": "[SKIP      ]",
    "nothing": "[IN SYNC   ]",
}
