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

import re
from functools import lru_cache
from typing import Any, TypedDict, cast

from hsync_common.constants import DELTA_MIN_FILE_SIZE, DELTA_MIN_FILE_SIZE_LAN
from hsync_common.env import load_env_file as _load_env_file
from hsync_common.models.tombstone import TOMBSTONE_TTL_DAYS_DEFAULT
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from hsync_client.constants import (
    HEARTBEAT_INTERVAL,
    HEARTBEAT_INTERVAL_MAX,
    HEARTBEAT_INTERVAL_MIN,
    HOSTNAME_PATTERN,
    LAN_PEER_DOWNLOAD_DEFAULT,
    LAN_PEER_DOWNLOAD_MIN_SIZE_DEFAULT,
    LAN_PEER_READ_CAP_DEFAULT,
    LAN_PEER_READ_CAP_MAX,
    LAN_PEER_READ_CAP_MIN,
    LAN_PEER_SOURCES_MAX_DEFAULT,
    LAN_PEER_SOURCES_MAX_MAX,
    LAN_PEER_SOURCES_MAX_MIN,
    MASS_DELETE_GUARD_DEFAULT,
    MASS_DELETE_PERCENT_DEFAULT,
    MASS_DELETE_THRESHOLD_DEFAULT,
    PEER_TIMEOUT,
    PEER_TIMEOUT_MAX,
    PEER_TIMEOUT_MIN,
)

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

    @field_validator("server", mode="before")
    @classmethod
    def _validate_server_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = str(v).strip()
        if not v:
            return None
        if not v.startswith("https://"):
            raise ValueError("Server URL must start with 'https://'")
        if not re.match(rf"^https://{HOSTNAME_PATTERN}(\:\d+)?(/.*)?$", v):
            raise ValueError(f"Invalid server URL format: {v!r}")
        return v

    api_key: SecretStr = Field(default=SecretStr(""), description="API key for authentication")

    # Sync Settings
    stability_delay: int | None = Field(default=None, description="File stability delay in seconds")
    resync: int | None = Field(
        default=None,
        ge=0,
        description="Periodic resync interval in seconds (0 = disabled, unset = default)",
    )

    # LAN Mode Settings
    lan_key: SecretStr | None = Field(default=None, description="LAN shared secret key")
    lan_group: str | None = Field(default=None, description="LAN group name for peer filtering")
    lan_port: int | None = Field(default=None, ge=1, le=65535, description="LAN port")

    # LAN Discovery Settings
    discovery_timeout: int | None = Field(
        default=None,
        ge=0,
        description="Discovery timeout in seconds (0 = wait indefinitely)",
    )
    heartbeat_interval: int | None = Field(
        default=None,
        ge=HEARTBEAT_INTERVAL_MIN,
        le=HEARTBEAT_INTERVAL_MAX,
        description="Heartbeat interval in seconds",
    )
    peer_timeout: int | None = Field(
        default=None,
        ge=PEER_TIMEOUT_MIN,
        le=PEER_TIMEOUT_MAX,
        description="Peer timeout in seconds",
    )
    never_master: bool = Field(
        default=False,
        description="Never become master in LAN mode (always remain a client/slave)",
    )
    lan_require_pairing: bool = Field(
        default=False,
        description=(
            "Strict pairing: refuse a first-seen LAN master certificate until it is "
            "confirmed out of band (disables Trust-On-First-Use auto-pinning)"
        ),
    )
    lan_peer_download: bool = Field(
        default=LAN_PEER_DOWNLOAD_DEFAULT,
        description="Enable peer-assisted downloads in LAN mode",
    )
    lan_peer_download_min_size: int = Field(
        default=LAN_PEER_DOWNLOAD_MIN_SIZE_DEFAULT,
        ge=0,
        le=2**63 - 1,
        description="Minimum file size in bytes eligible for peer-assisted download",
    )
    lan_peer_read_cap: int = Field(
        default=LAN_PEER_READ_CAP_DEFAULT,
        ge=LAN_PEER_READ_CAP_MIN,
        le=LAN_PEER_READ_CAP_MAX,
        description="Maximum concurrent peer-seeder reads",
    )
    lan_peer_sources_max: int = Field(
        default=LAN_PEER_SOURCES_MAX_DEFAULT,
        ge=LAN_PEER_SOURCES_MAX_MIN,
        le=LAN_PEER_SOURCES_MAX_MAX,
        description="Maximum unique peer sources attempted per file",
    )
    lan_allow_unauthenticated_writes: bool = Field(
        default=False,
        description="Explicitly allow dangerous no-key LAN mutation requests",
    )

    # Sync Settings
    ignore_delete: bool = Field(
        default=False, description="Ignore file deletion events (backup mode)"
    )
    compression_enabled: bool = Field(
        default=True, description="Enable zstd compression for file transfers"
    )
    delta_enabled: bool = Field(
        default=True, description="Enable rsync-style delta transfer (block-level sync)"
    )
    delta_min_file_size: int = Field(
        default=DELTA_MIN_FILE_SIZE,
        ge=0,
        description="Minimum file size in bytes for delta transfer (remote mode)",
    )
    delta_min_file_size_lan: int = Field(
        default=DELTA_MIN_FILE_SIZE_LAN,
        ge=0,
        description="Minimum file size in bytes for delta transfer in LAN mode",
    )
    ignore_patterns: str = Field(
        default="",
        description="Comma-separated extra gitignore-style patterns (layered on .hsyncignore)",
    )

    # Transfer Settings
    max_concurrent_transfers: int | None = Field(
        default=None,
        ge=1,
        le=20,
        description="Maximum number of concurrent file transfers (1-20)",
    )

    # Tombstone Configuration
    tombstone_ttl_days: int = Field(
        default=TOMBSTONE_TTL_DAYS_DEFAULT,
        ge=0,
        description="Days to retain deletion markers before garbage collection (0 = forever)",
    )

    # Mass-Deletion Guard
    mass_delete_guard: bool = Field(
        default=MASS_DELETE_GUARD_DEFAULT,
        description="Pause deletions when a cycle would remove too many local files",
    )
    mass_delete_threshold: int = Field(
        default=MASS_DELETE_THRESHOLD_DEFAULT,
        ge=0,
        description="Absolute number of deletions per cycle that trips the guard (0 = disabled)",
    )
    mass_delete_percent: int = Field(
        default=MASS_DELETE_PERCENT_DEFAULT,
        ge=0,
        le=100,
        description="Percentage of local files whose deletion trips the guard (0 = disabled)",
    )

    # Auto-Tuning
    autotune: bool = Field(
        default=True,
        description="Enable auto-tuning of timeout, retry and concurrency based on environment",
    )
    http_timeout: int | None = Field(
        default=None, ge=1, le=300, description="HTTP request timeout in seconds"
    )
    reconnect_delay: int | None = Field(
        default=None, ge=1, le=60, description="Reconnection delay in seconds"
    )
    transfer_retry_min_wait: float | None = Field(
        default=None, ge=0.1, description="Minimum per-file retry wait in seconds"
    )
    transfer_retry_max_wait: float | None = Field(
        default=None, ge=0.1, description="Maximum per-file retry wait in seconds"
    )

    # Connectivity Check Retry
    connectivity_retry_attempts: int | None = Field(
        default=None,
        ge=1,
        le=20,
        description="Server connectivity check retry attempts (1-20)",
    )
    connectivity_retry_min_wait: float | None = Field(
        default=None,
        ge=0.1,
        description="Minimum connectivity retry wait in seconds",
    )
    connectivity_retry_max_wait: float | None = Field(
        default=None,
        ge=0.1,
        description="Maximum connectivity retry wait in seconds",
    )

    @model_validator(mode="after")
    def _validate_peer_timeout_relationship(self) -> "EnvSettings":
        """Enforce the anti-false-positive relationship between the two knobs.

        A peer is marked stale only after ``peer_timeout`` seconds without a
        heartbeat, so the timeout must stay at least twice the heartbeat
        cadence: a single missed announce (packet loss, transient congestion)
        must never demote a live peer and trigger a needless re-election.
        """
        heartbeat = (
            self.heartbeat_interval if self.heartbeat_interval is not None else HEARTBEAT_INTERVAL
        )
        peer_timeout = self.peer_timeout if self.peer_timeout is not None else PEER_TIMEOUT
        if peer_timeout < 2 * heartbeat:
            raise ValueError(
                f"HSYNC_PEER_TIMEOUT ({peer_timeout}s) must be at least 2x "
                f"HSYNC_HEARTBEAT_INTERVAL ({heartbeat}s)"
            )
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


def get_stability_delay() -> int | None:
    """Get stability delay from environment."""
    return get_env_settings().stability_delay


def get_resync() -> int | None:
    """Get the periodic resync interval from environment.

    Returns ``None`` when unset (caller applies the default) and ``0``
    when explicitly disabled.
    """
    return get_env_settings().resync


def get_lan_key() -> str | None:
    """Get LAN shared secret key from environment."""
    key = get_env_settings().lan_key
    return key.get_secret_value() if key is not None else None


def get_lan_group() -> str | None:
    """Get LAN group name from environment."""
    return get_env_settings().lan_group


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


def get_never_master() -> bool:
    """Get never-master mode from environment."""
    return get_env_settings().never_master


def get_lan_require_pairing() -> bool:
    """Get strict-pairing mode (out-of-band cert confirmation) from environment."""
    return get_env_settings().lan_require_pairing


def get_lan_peer_download() -> bool:
    """Get whether LAN peer-assisted download is enabled."""
    return get_env_settings().lan_peer_download


def get_lan_peer_download_min_size() -> int:
    """Get the minimum peer-assisted download size in bytes."""
    return get_env_settings().lan_peer_download_min_size


def get_lan_peer_read_cap() -> int:
    """Get the peer seeder's concurrent read cap."""
    return get_env_settings().lan_peer_read_cap


def get_lan_peer_sources_max() -> int:
    """Get the maximum peer CIDs attempted for one file."""
    return get_env_settings().lan_peer_sources_max


def get_lan_allow_unauthenticated_writes() -> bool:
    """Get the explicit dangerous opt-in for keyless LAN writes."""
    return get_env_settings().lan_allow_unauthenticated_writes


def get_ignore_delete() -> bool:
    """Get ignore delete setting from environment."""
    return get_env_settings().ignore_delete


def get_compression_enabled() -> bool:
    """Get compression enabled setting from environment."""
    return get_env_settings().compression_enabled


def get_delta_enabled() -> bool:
    """Get delta transfer enabled setting from environment."""
    return get_env_settings().delta_enabled


def get_delta_min_file_size() -> int:
    """Get the minimum file size for delta transfer (remote mode)."""
    return get_env_settings().delta_min_file_size


def get_delta_min_file_size_lan() -> int:
    """Get the minimum file size for delta transfer in LAN mode."""
    return get_env_settings().delta_min_file_size_lan


def get_ignore_patterns() -> list[str]:
    """Return extra gitignore patterns parsed from the env variable.

    The env value is a comma-separated string; empty entries are
    dropped and surrounding whitespace is stripped.
    """
    raw = get_env_settings().ignore_patterns or ""
    return [p.strip() for p in raw.split(",") if p.strip()]


def get_mass_delete_guard() -> bool:
    """Get the mass-deletion guard on/off setting from environment."""
    return get_env_settings().mass_delete_guard


def get_mass_delete_threshold() -> int:
    """Get the absolute mass-deletion threshold from environment."""
    return get_env_settings().mass_delete_threshold


def get_mass_delete_percent() -> int:
    """Get the percentage mass-deletion threshold from environment."""
    return get_env_settings().mass_delete_percent


def get_max_concurrent_transfers() -> int | None:
    """Get max concurrent transfers from environment (None = use default)."""
    return get_env_settings().max_concurrent_transfers


def get_autotune_enabled() -> bool:
    """Get auto-tuning on/off setting from environment."""
    return get_env_settings().autotune


def get_http_timeout() -> int | None:
    """Get HTTP timeout from environment (None = use default)."""
    return get_env_settings().http_timeout


def get_reconnect_delay() -> int | None:
    """Get reconnection delay from environment (None = use default)."""
    return get_env_settings().reconnect_delay


def get_transfer_retry_min_wait() -> float | None:
    """Get minimum per-file retry wait from environment (None = use default)."""
    return get_env_settings().transfer_retry_min_wait


def get_transfer_retry_max_wait() -> float | None:
    """Get maximum per-file retry wait from environment (None = use default)."""
    return get_env_settings().transfer_retry_max_wait


def get_connectivity_retry_attempts() -> int | None:
    """Get connectivity check retry attempts from environment (None = use default)."""
    return get_env_settings().connectivity_retry_attempts


def get_connectivity_retry_min_wait() -> float | None:
    """Get minimum connectivity retry wait from environment (None = use default)."""
    return get_env_settings().connectivity_retry_min_wait


def get_connectivity_retry_max_wait() -> float | None:
    """Get maximum connectivity retry wait from environment (None = use default)."""
    return get_env_settings().connectivity_retry_max_wait


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
        "X-Protocol-Version": str,
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
    # One-shot run: a single sync cycle then exit (False = continuous, the default)
    ONE_SHOT: bool
    # Force this client to be master in LAN mode
    LAN_FORCE_MASTER: bool
    # Never let this client become master in LAN mode (always client/slave)
    LAN_NEVER_MASTER: bool
    # Strict pairing: refuse a first-seen master cert until confirmed out of band
    LAN_REQUIRE_PAIRING: bool
    # Peer-assisted download requester/seeder controls
    LAN_PEER_DOWNLOAD: bool
    LAN_PEER_DOWNLOAD_MIN_SIZE: int
    LAN_PEER_READ_CAP: int
    LAN_PEER_SOURCES_MAX: int
    # Dangerous explicit opt-in: permit keyless mutation requests
    LAN_ALLOW_UNAUTHENTICATED_WRITES: bool
    # Master certificate fingerprint for HTTPS verification (set at runtime)
    MASTER_CERT_FINGERPRINT: str | None
    # Ignore file deletion events (backup mode)
    IGNORE_DELETE: bool
    # Enable zstd compression for file transfers
    COMPRESSION_ENABLED: bool
    # Server compression capability (set at runtime after handshake)
    SERVER_COMPRESSION_ENABLED: bool
    # Enable rsync-style delta transfer (block-level sync)
    DELTA_ENABLED: bool
    # Peer delta capability (set at runtime after handshake / master election)
    SERVER_DELTA_ENABLED: bool
    # CLI override for the minimum file size for delta transfer, remote mode
    # (None = use HSYNC_DELTA_MIN_FILE_SIZE / built-in default)
    DELTA_MIN_FILE_SIZE: int | None
    # CLI override for the minimum file size for delta transfer, LAN mode
    # (None = use HSYNC_DELTA_MIN_FILE_SIZE_LAN / built-in default)
    DELTA_MIN_FILE_SIZE_LAN: int | None
    # Sync direction: "bidirectional", "upload-only", or "download-only"
    SYNC_DIRECTION: str
    # Maximum upload bandwidth in bytes per second (None = unlimited)
    MAX_BANDWIDTH_UPLOAD: int | None
    # Maximum download bandwidth in bytes per second (None = unlimited)
    MAX_BANDWIDTH_DOWNLOAD: int | None
    # Minimum free disk space in MB before sync (0 = disabled)
    MIN_FREE_DISK_SPACE: int
    # Absolute cap in bytes on a single inbound file, applied on top of the
    # peer-declared size as min(declared, MAX_INBOUND_SIZE) — anti
    # decompression-bomb (0 = disabled, cap = declared size only)
    MAX_INBOUND_SIZE: int
    # Extra gitignore-style patterns layered on top of .hsyncignore
    IGNORE_PATTERNS: list[str]
    # Mass-deletion guard: pause deletions over a count/percentage threshold
    MASS_DELETE_GUARD: bool
    # Absolute number of deletions per cycle that trips the guard (0 = disabled)
    MASS_DELETE_THRESHOLD: int
    # Percentage of local files whose deletion trips the guard (0 = disabled)
    MASS_DELETE_PERCENT: int
    # Maximum number of concurrent file transfers (downloads/uploads)
    MAX_CONCURRENT_TRANSFERS: int
    # Per-file transfer retry attempts (default 3, no CLI override in v1)
    TRANSFER_RETRY_ATTEMPTS: int
    # Minimum per-file retry wait in seconds (exponential backoff floor)
    TRANSFER_RETRY_MIN_WAIT: float
    # Maximum per-file retry wait in seconds (exponential backoff ceiling)
    TRANSFER_RETRY_MAX_WAIT: float
    # Server connectivity check retry attempts
    CONNECTIVITY_RETRY_ATTEMPTS: int
    # Minimum connectivity retry wait in seconds (exponential backoff floor)
    CONNECTIVITY_RETRY_MIN_WAIT: float
    # Maximum connectivity retry wait in seconds (exponential backoff ceiling)
    CONNECTIVITY_RETRY_MAX_WAIT: float
    # Auto-tuning enabled flag (set at runtime, not from env/CLI directly)
    AUTOTUNE_ENABLED: bool
    # Cached storage type from autotune probes (set at runtime for per-cycle refresh)
    AUTOTUNE_STORAGE_TYPE: str
    # Tuning keys resolved from autotune/default at startup (not CLI/env) —
    # the LAN post-discovery refresh may re-derive only these
    AUTOTUNE_MANAGED_KEYS: list[str]


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


def is_debug() -> bool:
    """
    Check if debug mode is enabled.

    Returns:
        True if debug mode is enabled.
    """
    return DEBUG
