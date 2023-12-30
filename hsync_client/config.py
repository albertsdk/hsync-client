# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Configuration management for HSYNC Client.

This module uses pydantic-settings for type-safe environment variable handling
and provides typed configuration structures for the application.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any, Optional, TypedDict, cast

from dotenv import load_dotenv
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


# =============================================================================
# Environment File Loading
# =============================================================================


def load_env_file(file_path: str) -> None:
    """
    Load environment variables from a file using python-dotenv.

    The file format supports:
    - KEY=value pairs (one per line)
    - Comments starting with #
    - Quoted values (single or double quotes)
    - export KEY=value syntax
    - Multiline values with quotes

    Environment variables already set (e.g., from CLI or shell) take
    precedence and are NOT overwritten by the file.

    Args:
        file_path: Path to the environment file.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Environment file not found: {file_path}")

    # override=False ensures CLI/shell variables take precedence
    load_dotenv(dotenv_path=path, override=False)
    # Clear cached settings to reload from new env
    get_env_settings.cache_clear()


# =============================================================================
# Pydantic Settings for Environment Variables
# =============================================================================


class EnvSettings(BaseSettings):
    """
    Environment-based settings using pydantic-settings.

    All settings are loaded from environment variables with HSYNC_ prefix.
    """

    model_config = SettingsConfigDict(
        env_prefix="HSYNC_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server Connection
    server: Optional[str] = Field(default=None, description="Server URL")
    api_key: SecretStr = Field(default=SecretStr(""), description="API key for authentication")

    # Sync Settings
    stability_delay: Optional[int] = Field(default=None, description="File stability delay in seconds")

    # LAN Mode Settings
    lan_key: Optional[SecretStr] = Field(default=None, description="LAN shared secret key")
    lan_group: Optional[str] = Field(default=None, description="LAN group name for peer filtering")
    lan_https: bool = Field(default=True, description="Use HTTPS for LAN communication")
    lan_port: Optional[int] = Field(default=None, ge=1, le=65535, description="LAN port")

    # LAN Discovery Settings
    discovery_timeout: Optional[int] = Field(default=None, ge=1, description="Discovery timeout in seconds")
    heartbeat_interval: Optional[int] = Field(default=None, ge=1, description="Heartbeat interval in seconds")
    peer_timeout: Optional[int] = Field(default=None, ge=1, description="Peer timeout in seconds")
    lan_persistent: bool = Field(default=True, description="Stay active indefinitely waiting for peers")

    # Sync Settings
    ignore_delete: bool = Field(default=False, description="Ignore file deletion events (backup mode)")
    compression_enabled: bool = Field(default=True, description="Enable zstd compression for file transfers")


@lru_cache
def get_env_settings() -> EnvSettings:
    """Get cached environment settings instance."""
    return EnvSettings()


# =============================================================================
# Environment Variables Getters (for late binding after env file loading)
# =============================================================================


def get_server_url() -> Optional[str]:
    """Get server URL from environment."""
    return get_env_settings().server


def get_api_key() -> str:
    """Get API key from environment."""
    return get_env_settings().api_key.get_secret_value()


def get_stability_delay() -> Optional[str]:
    """Get stability delay from environment."""
    delay = get_env_settings().stability_delay
    return str(delay) if delay is not None else None


def get_lan_key() -> Optional[str]:
    """Get LAN shared secret key from environment."""
    key = get_env_settings().lan_key
    return key.get_secret_value() if key is not None else None


def get_lan_group() -> Optional[str]:
    """Get LAN group name from environment."""
    return get_env_settings().lan_group


def get_lan_https() -> bool:
    """Get LAN HTTPS setting from environment."""
    return get_env_settings().lan_https


def get_lan_port() -> Optional[int]:
    """Get LAN port from environment."""
    return get_env_settings().lan_port


def get_discovery_timeout() -> Optional[int]:
    """Get discovery timeout from environment."""
    return get_env_settings().discovery_timeout


def get_heartbeat_interval() -> Optional[int]:
    """Get heartbeat interval from environment."""
    return get_env_settings().heartbeat_interval


def get_peer_timeout() -> Optional[int]:
    """Get peer timeout from environment."""
    return get_env_settings().peer_timeout


def get_lan_persistent() -> bool:
    """Get LAN persistent mode from environment."""
    return get_env_settings().lan_persistent


def get_ignore_delete() -> bool:
    """Get ignore delete setting from environment."""
    return get_env_settings().ignore_delete


def get_compression_enabled() -> bool:
    """Get compression enabled setting from environment."""
    return get_env_settings().compression_enabled


# Legacy exports for backward compatibility (read at import time)
def _get_env() -> EnvSettings:
    """Get current settings (uncached, for backward compatibility)."""
    return EnvSettings()


HSYNC_SERVER_URL: Optional[str] = _get_env().server
HSYNC_API_KEY: str = _get_env().api_key.get_secret_value()
HSYNC_STABILITY_DELAY: Optional[str] = str(_get_env().stability_delay) if _get_env().stability_delay else None


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
    # Filter path for subdirectory sync (optional)
    FILTER_PATH: Optional[str]
    # File stability delay in seconds
    STABILITY_DELAY: int
    # HTTP request timeout in seconds
    HTTP_TIMEOUT: int
    # Maximum wait timeout for file stability in seconds
    MAX_WAIT_TIMEOUT: int
    # LAN P2P mode flag
    LAN_MODE: bool
    # LAN port for master server
    LAN_PORT: int
    # Discovery timeout in seconds for LAN mode
    DISCOVERY_TIMEOUT: int
    # Heartbeat interval in seconds for LAN mode
    HEARTBEAT_INTERVAL: int
    # Peer timeout in seconds for LAN mode
    PEER_TIMEOUT: int
    # LAN shared secret key for authentication (None = no auth)
    LAN_KEY: Optional[str]
    # LAN group name for peer filtering (None = no filtering)
    LAN_GROUP: Optional[str]
    # LAN HTTPS mode (self-signed certificate per session)
    LAN_HTTPS: bool
    # LAN persistent mode (stay active indefinitely waiting for peers)
    LAN_PERSISTENT: bool
    # Force this client to be master in LAN mode
    LAN_FORCE_MASTER: bool
    # Master certificate fingerprint for HTTPS verification (set at runtime)
    MASTER_CERT_FINGERPRINT: Optional[str]
    # Ignore file deletion events (backup mode)
    IGNORE_DELETE: bool
    # Enable zstd compression for file transfers
    COMPRESSION_ENABLED: bool
    # Server compression capability (set at runtime after handshake)
    SERVER_COMPRESSION_ENABLED: bool


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

    Updates the global CONFIG dictionary in-place to ensure all modules
    that imported CONFIG directly will see the updated values.

    Args:
        config: The configuration dictionary to use.
    """
    cast(dict[str, Any], CONFIG).clear()
    CONFIG.update(config)


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

