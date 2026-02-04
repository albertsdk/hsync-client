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
Constants for HSYNC Client.

This module contains all magic numbers and constant values used
throughout the application, making them easy to find and modify.
"""

# =============================================================================
# Application Info
# =============================================================================

# Application version
HSYNC_VERSION = "0.1.0"


# =============================================================================
# Client ID Generation
# =============================================================================

# Length of generated client IDs
CID_LENGTH = 10


# =============================================================================
# File Transfer Settings
# =============================================================================

# Default chunk size for MD5 computation (64KB)
MD5_CHUNK_SIZE = 65536

# Chunk sizes based on file size for optimal transfer
CHUNK_SIZE_SMALL = 8 * 1024      # 8KB for files < 1MB
CHUNK_SIZE_MEDIUM = 64 * 1024    # 64KB for files < 100MB
CHUNK_SIZE_LARGE = 256 * 1024    # 256KB for files >= 100MB

# File size thresholds for chunk size selection
FILE_SIZE_SMALL = 1_000_000      # 1MB
FILE_SIZE_MEDIUM = 100_000_000   # 100MB


# =============================================================================
# File Stability Check
# =============================================================================

# Default interval (seconds) to wait between file size checks
FILE_STABILITY_INTERVAL = 2


# =============================================================================
# Parallel Transfer Settings
# =============================================================================

# Maximum number of concurrent file transfers (downloads/uploads)
# Higher values increase throughput but also server load
# Can be overridden via HSYNC_MAX_CONCURRENT_TRANSFERS environment variable
MAX_CONCURRENT_TRANSFERS = int(
    __import__("os").environ.get("HSYNC_MAX_CONCURRENT_TRANSFERS", "5")
)


# =============================================================================
# CLI Validation Limits
# =============================================================================

# Reconnection delay limits (seconds)
RECONNECT_DELAY_MIN = 1
RECONNECT_DELAY_MAX = 60
RECONNECT_DELAY_DEFAULT = 5

# Resync interval limits (seconds)
RESYNC_MIN = 20
RESYNC_MAX = 3600


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


# =============================================================================
# Path Validation
# =============================================================================

# Forbidden characters in filenames (cross-platform)
FORBIDDEN_FILENAME_CHARS = {"<", ">", ":", '"', "/", "\\", "|", "?", "*"}

# Forbidden characters in path parts (Unix-style)
FORBIDDEN_PATH_CHARS = {"<", ">", ":", '"', "\\", "|", "?", "*"}

# Reserved filenames (Windows compatibility)
RESERVED_FILENAMES = {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1"}

