# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Configuration management for HSYNC Client.

This module uses pydantic-settings for type-safe environment variable handling
and provides typed configuration structures for the application.
"""

from functools import lru_cache
from typing import Any, Literal, TypedDict, cast

from hsync_common.constants import DELTA_MIN_FILE_SIZE, DELTA_MIN_FILE_SIZE_P2P
from hsync_common.endpoint import (
    HostPort,
    build_https_url,
    format_authority,
    parse_host_port,
    parse_https_url,
    parse_port_list,
)
from hsync_common.env import load_env_file as _load_env_file
from hsync_common.models.tombstone import TOMBSTONE_TTL_DAYS_DEFAULT
from hsync_common.protocol.pairing import parse_fingerprint
from hsync_common.protocol.tls_trust import TrustPolicy
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from hsync_client.constants import (
    BOOTNODE_REFRESH_INTERVAL_MAX,
    BOOTNODE_REFRESH_INTERVAL_MIN,
    HEARTBEAT_INTERVAL,
    HEARTBEAT_INTERVAL_MAX,
    HEARTBEAT_INTERVAL_MIN,
    MASS_DELETE_GUARD_DEFAULT,
    MASS_DELETE_PERCENT_DEFAULT,
    MASS_DELETE_THRESHOLD_DEFAULT,
    P2P_PEER_DOWNLOAD_DEFAULT,
    P2P_PEER_READ_CAP_DEFAULT,
    P2P_PEER_READ_CAP_MAX,
    P2P_PEER_READ_CAP_MIN,
    P2P_PEER_SOURCES_MAX_DEFAULT,
    P2P_PEER_SOURCES_MAX_MAX,
    P2P_PEER_SOURCES_MAX_MIN,
    PEER_TIMEOUT,
    PEER_TIMEOUT_MAX,
    PEER_TIMEOUT_MIN,
    SERVER_DEFAULT_PORT,
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


def _normalize_fingerprint_list(value: object, subject: str) -> str | None:
    """Normalize a comma-separated pin list (up to two) and reject invalid entries.

    Shared by the bootnode and server ``*_FINGERPRINT`` fields: at most two
    fingerprints are accepted (a rotation window); each must be a valid
    ``sha256:<hex>`` / bare-hex digest. Fail-fast keeps an operator from silently
    pinning nothing.
    """
    if value is None:
        return None
    parts = [p.strip() for p in str(value).split(",") if p.strip()]
    if not parts:
        return None
    if len(parts) > 2:
        raise ValueError(f"at most two {subject} fingerprints are allowed (rotation window)")
    for part in parts:
        if parse_fingerprint(part) is None:
            raise ValueError(f"invalid {subject} fingerprint: {part!r}")
    return ",".join(parts)


class EnvSettings(BaseSettings):
    """
    Environment-based settings using pydantic-settings.

    All settings are loaded from environment variables with HSYNC_ prefix.
    """

    model_config = SettingsConfigDict(
        env_prefix="HSYNC_",
        env_file_encoding="utf-8",
    )

    # Server Connection
    server: str | None = Field(default=None, description="Server URL")

    @field_validator("server", mode="before")
    @classmethod
    def _validate_server_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        raw = str(v).strip()
        if not raw:
            return None
        if raw.lower().startswith("https://"):
            return parse_https_url(raw)
        if "://" in raw or raw.lower().startswith("http://"):
            raise ValueError("Server URL must use HTTPS")
        endpoint = parse_host_port(raw)
        return build_https_url(endpoint, SERVER_DEFAULT_PORT)

    @field_validator("bootnode", mode="before")
    @classmethod
    def _validate_bootnode_host(cls, v: str | None) -> str | None:
        """Accept a ``HOST[:PORT]`` endpoint: no scheme, no path.

        The bootnode channel is QUIC/UDP (not HTTPS), so the ``https://``
        validator used for ``HSYNC_SERVER`` does not apply; the host may be a
        bare hostname/IPv4, a ``HOST[:PORT]`` pair or a bracketed IPv6 literal.
        The value is normalized to the unbracketed host, with the composite
        ``[IPv6]:port`` when a port is present.
        """
        if v is None:
            return None
        raw = str(v).strip()
        if not raw:
            return None
        endpoint = parse_host_port(raw)
        if endpoint.port is None:
            return endpoint.host
        return format_authority(endpoint.host, endpoint.port)

    @field_validator("bootnode_fingerprint", mode="before")
    @classmethod
    def _validate_bootnode_fingerprints(cls, v: object) -> str | None:
        """Normalize the bootnode comma-separated pin list (see ADR-0034)."""
        return _normalize_fingerprint_list(v, "bootnode")

    @field_validator("server_fingerprint", mode="before")
    @classmethod
    def _validate_server_fingerprints(cls, v: object) -> str | None:
        """Normalize the server comma-separated pin list (see ADR-0036)."""
        return _normalize_fingerprint_list(v, "server")

    api_key: SecretStr = Field(default=SecretStr(""), description="API key for authentication")

    # Sync Settings
    stability_delay: int | None = Field(default=None, description="File stability delay in seconds")
    resync: int | None = Field(
        default=None,
        ge=0,
        description="Periodic resync interval in seconds (0 = disabled, unset = default)",
    )

    # P2P Mode Settings
    p2p_key: SecretStr | None = Field(default=None, description="P2P shared secret key")
    p2p_group: str | None = Field(default=None, description="P2P group name for peer filtering")
    p2p_listen_port: int | None = Field(
        default=None, ge=1, le=65535, description="P2P master TCP listen port"
    )
    bootnode: str | None = Field(
        default=None,
        description=(
            "Bare bootnode host for cross-network discovery (QUIC/UDP, no scheme). "
            "Unset = mDNS-only discovery"
        ),
    )
    bootnode_ports: str | None = Field(
        default=None,
        description=(
            "Comma-separated extra UDP ports to try for the SAME bootnode (multi-port "
            "dial fallback; e.g. '53' to also try DNS when UDP 443 is blocked by the "
            "client egress firewall). Tried after the bootnode/default port"
        ),
    )
    bootnode_fingerprint: str | None = Field(
        default=None,
        description=(
            "Bootnode certificate fingerprint(s) to pin (comma-separated, up to two "
            "for a rotation window); each accepts 'sha256:<hex>' or bare hex"
        ),
    )
    bootnode_tofu: bool = Field(
        default=False,
        description=(
            "Trust the bootnode certificate on first use (TOFU); a later mismatch "
            "is refused as a possible man-in-the-middle"
        ),
    )
    upnp_enabled: bool = Field(
        default=False,
        description=(
            "Map the master TCP port with UPnP (opt-in; off by default). Only "
            "meaningful with a bootnode."
        ),
    )

    # Server TLS Trust (ADR-0036): chain (default) or SSH-style pinning policies
    server_trust: Literal["chain", "ask", "accept-new", "strict", "none"] | None = Field(
        default=None,
        description="Sync-server TLS trust policy (default: chain)",
    )
    server_fingerprint: str | None = Field(
        default=None,
        description=(
            "Sync-server certificate fingerprint(s) to pin (comma-separated, up to two "
            "for a rotation window); each accepts 'sha256:<hex>' or bare hex"
        ),
    )
    server_tofu: bool = Field(
        default=False,
        description=(
            "Trust the sync-server certificate on first use (TOFU); a later mismatch "
            "is refused as a possible man-in-the-middle"
        ),
    )

    # P2P Discovery Settings
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
    bootnode_refresh_interval: int | None = Field(
        default=None,
        ge=BOOTNODE_REFRESH_INTERVAL_MIN,
        le=BOOTNODE_REFRESH_INTERVAL_MAX,
        description="boot/1 lease/query cadence in seconds (default 30)",
    )
    bootnode_trust: Literal["ask", "accept-new", "strict", "none"] | None = Field(
        default=None,
        description="Bootnode TLS trust policy (default: ask)",
    )
    never_master: bool = Field(
        default=False,
        description="Never become master in P2P mode (always remain a client/slave)",
    )
    p2p_require_pairing: bool = Field(
        default=False,
        description=(
            "Strict pairing: refuse a first-seen P2P master certificate until it is "
            "confirmed out of band (disables Trust-On-First-Use auto-pinning)"
        ),
    )
    p2p_peer_download: bool = Field(
        default=P2P_PEER_DOWNLOAD_DEFAULT,
        description="Enable peer-assisted downloads in P2P mode",
    )
    p2p_peer_read_cap: int = Field(
        default=P2P_PEER_READ_CAP_DEFAULT,
        ge=P2P_PEER_READ_CAP_MIN,
        le=P2P_PEER_READ_CAP_MAX,
        description="Maximum concurrent peer-seeder reads",
    )
    p2p_peer_sources_max: int = Field(
        default=P2P_PEER_SOURCES_MAX_DEFAULT,
        ge=P2P_PEER_SOURCES_MAX_MIN,
        le=P2P_PEER_SOURCES_MAX_MAX,
        description="Maximum unique peer sources attempted per file",
    )
    p2p_allow_unauthenticated_writes: bool = Field(
        default=False,
        description="Explicitly allow dangerous no-key P2P mutation requests",
    )
    p2p_allow_unauthenticated: bool = Field(
        default=False,
        description="Explicitly acknowledge running P2P without a shared key",
    )
    allow_deletions: bool = Field(
        default=False,
        description="Explicitly accept the deletion-risk confirmation",
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
    delta_min_file_size_p2p: int = Field(
        default=DELTA_MIN_FILE_SIZE_P2P,
        ge=0,
        description="Minimum file size in bytes for delta transfer in P2P mode",
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


def get_p2p_key() -> str | None:
    """Get P2P shared secret key from environment."""
    key = get_env_settings().p2p_key
    return key.get_secret_value() if key is not None else None


def get_p2p_group() -> str | None:
    """Get P2P group name from environment."""
    return get_env_settings().p2p_group


def get_p2p_listen_port() -> int | None:
    """Get P2P port from environment."""
    return get_env_settings().p2p_listen_port


def get_bootnode_endpoint() -> HostPort | None:
    """Get the parsed bootnode endpoint from ``HSYNC_BOOTNODE`` (None = mDNS plan)."""
    raw = get_env_settings().bootnode
    if not raw:
        return None
    return parse_host_port(raw)


def get_bootnode_ports() -> tuple[int, ...]:
    """Extra UDP ports to try for the same bootnode (multi-port dial fallback).

    Parsed from ``HSYNC_BOOTNODE_PORTS`` with the shared
    ``hsync_common.endpoint.parse_port_list`` SSOT (comma-separated, order
    preserved, invalid/duplicate entries dropped — the lenient env policy).
    Consumed by the bootnode client so a client whose egress firewall blocks UDP
    443 can still reach the node on another port (e.g. 53) — the same node, one
    routing table.
    """
    raw = get_env_settings().bootnode_ports
    if not raw:
        return ()
    return parse_port_list(raw)


def _parse_fingerprint_list(raw: str | None) -> tuple[str, ...]:
    """Normalize a comma-separated pinned-fingerprint list, dropping invalid parts."""
    if not raw:
        return ()
    pins: list[str] = []
    for part in raw.split(","):
        normalized = parse_fingerprint(part.strip())
        if normalized is not None:
            pins.append(normalized)
    return tuple(pins)


def get_bootnode_fingerprints() -> tuple[str, ...]:
    """Get the pinned bootnode fingerprints (validated, up to two, normalized)."""
    return _parse_fingerprint_list(get_env_settings().bootnode_fingerprint)


def get_bootnode_tofu() -> bool:
    """Get whether the bootnode certificate is trusted on first use."""
    return get_env_settings().bootnode_tofu


def get_discovery_timeout() -> int | None:
    """Get discovery timeout from environment."""
    return get_env_settings().discovery_timeout


def get_heartbeat_interval() -> int | None:
    """Get heartbeat interval from environment."""
    return get_env_settings().heartbeat_interval


def get_peer_timeout() -> int | None:
    """Get peer timeout from environment."""
    return get_env_settings().peer_timeout


def get_bootnode_refresh_interval() -> int | None:
    """Get the boot/1 lease/query cadence from the environment."""
    return get_env_settings().bootnode_refresh_interval


def get_bootnode_trust() -> str | None:
    """Get the bootnode TLS trust policy from the environment (see ADR-0035)."""
    return get_env_settings().bootnode_trust


def get_server_trust() -> str | None:
    """Get the sync-server TLS trust policy from the environment (see ADR-0036)."""
    return get_env_settings().server_trust


def get_server_fingerprints() -> tuple[str, ...]:
    """Get the pinned server fingerprints (validated, up to two, normalized)."""
    return _parse_fingerprint_list(get_env_settings().server_fingerprint)


def get_server_tofu() -> bool:
    """Get whether the server certificate is trusted on first use."""
    return get_env_settings().server_tofu


def get_tombstone_ttl_days() -> int:
    """Get tombstone retention window in days from environment (late binding)."""
    return get_env_settings().tombstone_ttl_days


def get_never_master() -> bool:
    """Get never-master mode from environment."""
    return get_env_settings().never_master


def get_p2p_require_pairing() -> bool:
    """Get strict-pairing mode (out-of-band cert confirmation) from environment."""
    return get_env_settings().p2p_require_pairing


def get_p2p_peer_download() -> bool:
    """Get whether P2P peer-assisted download is enabled."""
    return get_env_settings().p2p_peer_download


def get_p2p_peer_read_cap() -> int:
    """Get the peer seeder's concurrent read cap."""
    return get_env_settings().p2p_peer_read_cap


def get_p2p_peer_sources_max() -> int:
    """Get the maximum peer CIDs attempted for one file."""
    return get_env_settings().p2p_peer_sources_max


def get_p2p_allow_unauthenticated_writes() -> bool:
    """Get the explicit dangerous opt-in for keyless P2P writes."""
    return get_env_settings().p2p_allow_unauthenticated_writes


def get_p2p_allow_unauthenticated() -> bool:
    """Get the explicit acknowledgement for keyless P2P operation."""
    return get_env_settings().p2p_allow_unauthenticated


def get_allow_deletions() -> bool:
    """Get the explicit acceptance of the deletion-risk confirmation."""
    return get_env_settings().allow_deletions


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


def get_delta_min_file_size_p2p() -> int:
    """Get the minimum file size for delta transfer in P2P mode."""
    return get_env_settings().delta_min_file_size_p2p


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
    # P2P mode flag
    P2P_MODE: bool
    # P2P port for master server
    P2P_LISTEN_PORT: int
    # Discovery timeout in seconds for P2P mode (0 = wait indefinitely)
    DISCOVERY_TIMEOUT: int
    # Heartbeat interval in seconds for P2P mode
    HEARTBEAT_INTERVAL: int
    # boot/1 lease/query cadence in seconds (default 30; bootnode plan only)
    BOOTNODE_REFRESH_INTERVAL: int
    # Peer timeout in seconds for P2P mode
    PEER_TIMEOUT: int
    # P2P shared secret key for authentication (None = no auth)
    P2P_KEY: str | None
    # P2P group name for peer filtering (None = no filtering)
    P2P_GROUP: str | None
    # One-shot run: a single sync cycle then exit (False = continuous, the default)
    ONE_SHOT: bool
    # Force this client to be master in P2P mode
    P2P_FORCE_MASTER: bool
    # Never let this client become master in P2P mode (always client/slave)
    P2P_NEVER_MASTER: bool
    # Strict pairing: refuse a first-seen master cert until confirmed out of band
    P2P_REQUIRE_PAIRING: bool
    # Peer-assisted download requester/seeder controls
    P2P_PEER_DOWNLOAD: bool
    P2P_PEER_READ_CAP: int
    P2P_PEER_SOURCES_MAX: int
    # Bootnode host for cross-network discovery (None = mDNS-only plan)
    BOOTNODE: str | None
    # UDP (QUIC) port of the remote bootnode
    BOOTNODE_PORT: int
    # Extra UDP ports to try for the same bootnode (multi-port dial fallback)
    BOOTNODE_PORTS: tuple[int, ...]
    # Bootnode certificate fingerprint(s) to pin (up to two for a rotation window)
    BOOTNODE_FINGERPRINT: tuple[str, ...]
    # Bootnode TLS trust policy (ADR-0035; default ask)
    BOOTNODE_TRUST: TrustPolicy
    # Dangerous explicit opt-in: permit keyless mutation requests
    P2P_ALLOW_UNAUTHENTICATED_WRITES: bool
    # Explicit acknowledgement: run P2P without a shared key
    P2P_ALLOW_UNAUTHENTICATED: bool
    # Explicit acceptance: bypass the deletion-risk confirmation
    ALLOW_DELETIONS: bool
    # Map the master TCP port with UPnP (plan default: on only with a bootnode)
    UPNP_ENABLED: bool
    # Master certificate fingerprint for HTTPS verification (set at runtime)
    MASTER_CERT_FINGERPRINT: str | None
    # Server TLS trust policy (ADR-0036; default chain) and explicit pins
    SERVER_TRUST: TrustPolicy
    SERVER_FINGERPRINT: tuple[str, ...]
    # Server certificate fingerprint for HTTPS verification (set at runtime)
    SERVER_CERT_FINGERPRINT: str | None
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
    # CLI override for the minimum file size for delta transfer, P2P mode
    # (None = use HSYNC_DELTA_MIN_FILE_SIZE_P2P / built-in default)
    DELTA_MIN_FILE_SIZE_P2P: int | None
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
    # the P2P post-discovery refresh may re-derive only these
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
