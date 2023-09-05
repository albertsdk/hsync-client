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

# Default interval (seconds) to wait between file size/mtime checks
# before considering a file stable and ready for upload.
# 5 seconds is a reasonable compromise.
FILE_STABILITY_INTERVAL_DEFAULT = 5

# Limits for stability delay configuration
STABILITY_DELAY_MIN = 1
STABILITY_DELAY_MAX = 60


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
# Based on Microsoft documentation:
# https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file
#
# Windows: < > : " / \ | ? * and characters 0-31
# Linux/macOS: / and NUL (\x00)
# For cross-platform compatibility, we use the union of all restrictions.
FORBIDDEN_FILENAME_CHARS: frozenset[str] = frozenset({
    "<", ">", ":", '"', "/", "\\", "|", "?", "*",
    "\x00",  # NUL - forbidden on all systems
})

# Control characters (ASCII 1-31) - forbidden on Windows
# These are checked separately for efficiency
CONTROL_CHARS: frozenset[str] = frozenset(chr(i) for i in range(1, 32))

# Forbidden characters in path parts (allows / for path separator)
# Same as FORBIDDEN_FILENAME_CHARS but without forward slash
FORBIDDEN_PATH_CHARS: frozenset[str] = frozenset({
    "<", ">", ":", '"', "\\", "|", "?", "*",
    "\x00",  # NUL - forbidden on all systems
})

# Reserved filenames (Windows compatibility)
# These names are reserved in every directory and cannot be used as filenames.
# Also reserved when followed by an extension (e.g., NUL.txt, COM1.log)
# Source: Microsoft documentation
#
# Note: Windows also recognizes superscript digits ¹²³ (ISO 8859-1) as valid
# parts of COM# and LPT# device names (e.g., COM¹ is reserved).
RESERVED_FILENAMES: frozenset[str] = frozenset({
    # Standard device names
    "CON", "PRN", "AUX", "NUL",
    # Serial ports COM1-COM9 and superscript variants
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "COM\u00b9", "COM\u00b2", "COM\u00b3",  # COM¹, COM², COM³
    # Parallel ports LPT1-LPT9 and superscript variants
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    "LPT\u00b9", "LPT\u00b2", "LPT\u00b3",  # LPT¹, LPT², LPT³
})

# Characters that are problematic at the end of filenames (Windows)
# Windows shell ignores trailing spaces and periods
PROBLEMATIC_TRAILING_CHARS: frozenset[str] = frozenset({" ", "."})

