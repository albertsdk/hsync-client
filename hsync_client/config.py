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
from typing import Any, TypedDict, cast

from hsync_common.env import load_env_file as _load_env_file
from hsync_common.models.tombstone import TOMBSTONE_TTL_DAYS_DEFAULT
from loguru import logger as log
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# =============================================================================
# Environment File Loading
# =============================================================================


def load_env_file(file_path: str) -> None:
    """Load env vars from a file and clear the client settings cache.

    Thin wrapper around :func:`hsync_common.env.load_env_file` that binds
    the local cache invalidation callback.
    """
    _load_env_file(file_path, on_loaded=get_env_settings.cache_clear)


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
    server: str | None = Field(default=None, description="Server URL")
    api_key: SecretStr = Field(default=SecretStr(""), description="API key for authentication")

    # Sync Settings
    stability_delay: int | None = Field(default=None, description="File stability delay in seconds")

    # LAN Mode Settings
    lan_key: SecretStr | None = Field(default=None, description="LAN shared secret key")
    lan_group: str | None = Field(default=None, description="LAN group name for peer filtering")
    lan_https: bool = Field(default=True, description="Use HTTPS for LAN communication")
    lan_port: int | None = Field(default=None, ge=1, le=65535, description="LAN port")

    # LAN Discovery Settings
    discovery_timeout: int | None = Field(
        default=None,
        ge=0,
        description="Discovery timeout in seconds (0 = wait indefinitely)",
    )
    heartbeat_interval: int | None = Field(
        default=None, ge=1, description="Heartbeat interval in seconds"
    )
    peer_timeout: int | None = Field(default=None, ge=1, description="Peer timeout in seconds")
    lan_persistent: bool = Field(
        default=True, description="Stay active indefinitely waiting for peers"
    )
    never_master: bool = Field(
        default=False,
        description="Never become master in LAN mode (always remain a client/slave)",
    )

    # Sync Settings
    ignore_delete: bool = Field(
        default=False, description="Ignore file deletion events (backup mode)"
    )
    compression_enabled: bool = Field(
        default=True, description="Enable zstd compression for file transfers"
    )
    sync_hidden: bool = Field(
        default=False,
        description="Include hidden/system files in sync (disables built-in filters)",
    )
    ignore_patterns: str = Field(
        default="",
        description="Comma-separated extra gitignore-style patterns (layered on .hsyncignore)",
    )

    # Tombstone Configuration
    tombstone_ttl_days: int = Field(
        default=TOMBSTONE_TTL_DAYS_DEFAULT,
        ge=0,
        description="Days to retain deletion markers before garbage collection (0 = forever)",
    )

    @model_validator(mode="after")
    def _lan_key_requires_https(self) -> "EnvSettings":
        """Drop ``lan_key`` when HTTPS is disabled.

        ``lan_key`` is a shared secret sent as a Bearer token; transmitting
        it over plain HTTP offers no protection. A ``model_validator`` is
        required here (not a ``field_validator``) because ``lan_key`` is
        declared before ``lan_https`` and would not see it otherwise.
        """
        if self.lan_key is not None and not self.lan_https:
            log.warning(
                "HSYNC_LAN_KEY is set but HSYNC_LAN_HTTPS is disabled; "
                "ignoring the key (secrets are never sent over plain HTTP)."
            )
            self.lan_key = None
        return self


@lru_cache
def get_env_settings() -> EnvSettings:
    """Get cached environment settings instance."""
    return EnvSettings()


# =============================================================================
# Environment Variables Getters (for late binding after env file loading)
# =============================================================================


def get_server_url() -> str | None:
    """Get server URL from environment."""
    return get_env_settings().server


def get_api_key() -> str:
    """Get API key from environment."""
    return get_env_settings().api_key.get_secret_value()


def get_stability_delay() -> str | None:
    """Get stability delay from environment."""
    delay = get_env_settings().stability_delay
    return str(delay) if delay is not None else None


def get_lan_key() -> str | None:
    """Get LAN shared secret key from environment."""
    key = get_env_settings().lan_key
    return key.get_secret_value() if key is not None else None


def get_lan_group() -> str | None:
    """Get LAN group name from environment."""
    return get_env_settings().lan_group


def get_lan_https() -> bool:
    """Get LAN HTTPS setting from environment."""
    return get_env_settings().lan_https


def get_lan_port() -> int | None:
    """Get LAN port from environment."""
    return get_env_settings().lan_port


def get_discovery_timeout() -> int | None:
    """Get discovery timeout from environment."""
    return get_env_settings().discovery_timeout


def get_heartbeat_interval() -> int | None:
    """Get heartbeat interval from environment."""
    return get_env_settings().heartbeat_interval


def get_peer_timeout() -> int | None:
    """Get peer timeout from environment."""
    return get_env_settings().peer_timeout


def get_tombstone_ttl_days() -> int:
    """Get tombstone retention window in days from environment (late binding)."""
    return get_env_settings().tombstone_ttl_days


def get_lan_persistent() -> bool:
    """Get LAN persistent mode from environment."""
    return get_env_settings().lan_persistent


def get_never_master() -> bool:
    """Get never-master mode from environment."""
    return get_env_settings().never_master


def get_ignore_delete() -> bool:
    """Get ignore delete setting from environment."""
    return get_env_settings().ignore_delete


def get_compression_enabled() -> bool:
    """Get compression enabled setting from environment."""
    return get_env_settings().compression_enabled


def get_sync_hidden() -> bool:
    """Get sync-hidden setting from environment."""
    return get_env_settings().sync_hidden


def get_ignore_patterns() -> list[str]:
    """Return extra gitignore patterns parsed from the env variable.

    The env value is a comma-separated string; empty entries are
    dropped and surrounding whitespace is stripped.
    """
    raw = get_env_settings().ignore_patterns or ""
    return [p.strip() for p in raw.split(",") if p.strip()]


# =============================================================================
# Configuration TypedDict
# =============================================================================


class ApiEndpoints(TypedDict):
    """API endpoint URLs."""

    buckets: str  # REST API base URL for bucket operations
    events: str  # WebSocket URL for real-time events


# RequestHeaders uses functional syntax because keys contain hyphens
RequestHeaders = TypedDict(
    "RequestHeaders",
    {
        "accept": str,
        "Authorization": str,
        "X-Client-Name": str,
        "X-Client-Version": str,
    },
)


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
    LOGFILE: str | None
    # Skip SSL verification flag
    NO_VERIFY_SSL: bool
    # Custom CA certificate path (optional)
    CA_CERT: str | None
    # Quiet mode flag
    QUIET: bool
    # Remote bucket name on server
    REMOTE_BUCKET: str
    # API key resolved at startup (from --api-key-fd pipe or HSYNC_API_KEY env)
    API_KEY: str
    # HTTP request headers
    REQUEST_HEADERS: RequestHeaders
    # Reconnection delay in seconds
    RECONNECT_DELAY: int
    # Periodic resync interval in seconds (optional)
    RESYNC: int | None
    # Filter path for subdirectory sync (optional)
    FILTER_PATH: str | None
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
    # Discovery timeout in seconds for LAN mode (0 = wait indefinitely)
    DISCOVERY_TIMEOUT: int
    # Heartbeat interval in seconds for LAN mode
    HEARTBEAT_INTERVAL: int
    # Peer timeout in seconds for LAN mode
    PEER_TIMEOUT: int
    # LAN shared secret key for authentication (None = no auth)
    LAN_KEY: str | None
    # LAN group name for peer filtering (None = no filtering)
    LAN_GROUP: str | None
    # LAN HTTPS mode (self-signed certificate per session)
    LAN_HTTPS: bool
    # LAN persistent mode (stay active indefinitely waiting for peers)
    LAN_PERSISTENT: bool
    # Force this client to be master in LAN mode
    LAN_FORCE_MASTER: bool
    # Never let this client become master in LAN mode (always client/slave)
    LAN_NEVER_MASTER: bool
    # Master certificate fingerprint for HTTPS verification (set at runtime)
    MASTER_CERT_FINGERPRINT: str | None
    # Ignore file deletion events (backup mode)
    IGNORE_DELETE: bool
    # Enable zstd compression for file transfers
    COMPRESSION_ENABLED: bool
    # Server compression capability (set at runtime after handshake)
    SERVER_COMPRESSION_ENABLED: bool
    # Sync direction: "bidirectional", "upload-only", or "download-only"
    SYNC_DIRECTION: str
    # Maximum upload bandwidth in bytes per second (None = unlimited)
    MAX_BANDWIDTH_UPLOAD: int | None
    # Maximum download bandwidth in bytes per second (None = unlimited)
    MAX_BANDWIDTH_DOWNLOAD: int | None
    # Minimum free disk space in MB before sync (0 = disabled)
    MIN_FREE_DISK_SPACE: int
    # Include hidden/system files in sync (disables built-in filters)
    SYNC_HIDDEN: bool
    # Extra gitignore-style patterns layered on top of .hsyncignore
    IGNORE_PATTERNS: list[str]


# =============================================================================
# Global Configuration
# =============================================================================

# Global configuration dictionary (populated at runtime by main())
CONFIG: HsyncConfig = cast(HsyncConfig, {})

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
