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
Configuration management for HSYNC Client.

This module provides typed configuration structures and global
configuration access for the application.
"""

from os import environ
from pathlib import Path
from typing import Optional, TypedDict


# =============================================================================
# Environment File Loading
# =============================================================================


def load_env_file(file_path: str) -> None:
    """
    Load environment variables from a file.

    The file format is simple key=value pairs, one per line.
    Lines starting with # are comments and are ignored.
    Empty lines are ignored.

    Args:
        file_path: Path to the environment file.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If a line has invalid format.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Environment file not found: {file_path}")

    with path.open("r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()

            # Skip empty lines and comments
            if not line or line.startswith("#"):
                continue

            # Parse key=value
            if "=" not in line:
                raise ValueError(
                    f"Invalid format at line {line_num}: expected KEY=value"
                )

            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()

            # Remove surrounding quotes if present
            if (value.startswith('"') and value.endswith('"')) or \
               (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]

            # Only set if not already in environment (CLI/shell takes precedence)
            if key not in environ:
                environ[key] = value


# =============================================================================
# Environment Variables (read via getters for late binding)
# =============================================================================


def get_server_url() -> Optional[str]:
    """Get server URL from environment."""
    return environ.get("HSYNC_SERVER")


def get_api_key() -> str:
    """Get API key from environment."""
    return environ.get("HSYNC_API_KEY", "")


def get_stability_delay() -> Optional[str]:
    """Get stability delay from environment."""
    return environ.get("HSYNC_STABILITY_DELAY")


# Legacy exports for backward compatibility (read at import time)
# These are used where early binding is acceptable
HSYNC_SERVER_URL: Optional[str] = environ.get("HSYNC_SERVER")
HSYNC_API_KEY: str = environ.get("HSYNC_API_KEY", "")
HSYNC_STABILITY_DELAY: Optional[str] = environ.get("HSYNC_STABILITY_DELAY")


# =============================================================================
# Configuration TypedDict
# =============================================================================


class ApiEndpoints(TypedDict):
    """API endpoint URLs."""

    buckets: str  # REST API base URL for bucket operations
    events: str   # WebSocket URL for real-time events


# RequestHeaders uses functional syntax because keys contain hyphens
RequestHeaders = TypedDict('RequestHeaders', {
    'accept': str,
    'Authorization': str,
    'X-Client-Name': str,
    'X-Client-Version': str,
})


class HsyncConfig(TypedDict):
    """
    Complete configuration structure for HSYNC Client.

    This TypedDict documents the expected structure of the CONFIG
    dictionary used throughout the application.
    """

    # API endpoints
    API: ApiEndpoints
    # Unique client identifier for this session
    CID: str
    # Dry-run mode flag
    DRYRUN: bool
    # Local directory path to sync
    LOCAL_BUCKET: str
    # Log file path (optional)
    LOGFILE: Optional[str]
    # Skip SSL verification flag
    NO_VERIFY_SSL: bool
    # Custom CA certificate path (optional)
    CA_CERT: Optional[str]
    # Quiet mode flag
    QUIET: bool
    # Remote bucket name on server
    REMOTE_BUCKET: str
    # HTTP request headers
    REQUEST_HEADERS: RequestHeaders
    # Reconnection delay in seconds
    RECONNECT_DELAY: int
    # Periodic resync interval in seconds (optional)
    RESYNC: Optional[int]
    # Sync ordering field (mtime or ctime)
    SYNC_ORDER: str
    # Filter path for subdirectory sync (optional)
    FILTER_PATH: Optional[str]
    # File stability delay in seconds
    STABILITY_DELAY: int


# =============================================================================
# Global Configuration
# =============================================================================

# Global configuration dictionary (populated at runtime by main())
CONFIG: HsyncConfig = {}  # type: ignore[assignment]

# Global debug flag
DEBUG: bool = False


def set_config(config: HsyncConfig) -> None:
    """
    Set the global configuration.

    Args:
        config: The configuration dictionary to use.
    """
    global CONFIG
    CONFIG = config


def set_debug(debug: bool) -> None:
    """
    Set the global debug flag.

    Args:
        debug: Whether debug mode is enabled.
    """
    global DEBUG
    DEBUG = debug


def get_config() -> HsyncConfig:
    """
    Get the current global configuration.

    Returns:
        The current configuration dictionary.
    """
    return CONFIG


def is_debug() -> bool:
    """
    Check if debug mode is enabled.

    Returns:
        True if debug mode is enabled.
    """
    return DEBUG

