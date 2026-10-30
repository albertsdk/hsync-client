"""Typed startup options for :func:`hsync_client.main_logic.run_sync`.

Consumers: the ``p2p`` and ``server`` CLI commands (``hsync_client.cli``)
build a :class:`RunOptions` from the shared option set; ``run_sync`` consumes
it as its single argument. Grouping the 57-parameter funnel into seven option
groups means a new flag touches one group and ``run_sync`` only. Each field
keeps the name of the ``run_sync`` parameter it replaces so the mapping stays
mechanical.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hsync_common.protocol.pairing import FingerprintPins
from hsync_common.runtime import ColorMode

from hsync_client.constants import (
    MAX_INBOUND_SIZE_MB_DEFAULT,
    MIN_FREE_DISK_SPACE_DEFAULT,
)

__all__ = [
    "RunOptions",
    "SecretsOptions",
    "LoggingOptions",
    "EndpointOptions",
    "SyncOptions",
    "P2POptions",
    "TransferOptions",
    "SafetyOptions",
]


@dataclass(frozen=True, slots=True)
class SecretsOptions:
    """Secrets passed via FD pipes plus the ``--env-file`` location."""

    env_file: str | None
    api_key_fd: int | None = None
    p2p_key_fd: int | None = None
    p2p_passphrase_fd: int | None = None


@dataclass(frozen=True, slots=True)
class LoggingOptions:
    """Log/sink configuration (stderr, logfile, structured-event FD)."""

    debug: bool
    quiet: bool
    logfile: str | None
    color: ColorMode
    events_json_fd: int | None = None


@dataclass(frozen=True, slots=True)
class EndpointOptions:
    """Remote endpoints: the sync-server URL and the rendezvous servers."""

    bucket: str
    server_url: str | None
    rendezvous_server_endpoints: tuple[Any, ...] = ()
    map_data_port: bool | None = None


@dataclass(frozen=True, slots=True)
class SyncOptions:
    """Reconcile behaviour (direction, scope, dry-run, cadence)."""

    local_path: str
    continuous: bool
    resync: int | None
    stability_delay: int | None
    max_wait_timeout: int
    filter_path: str | None
    dryrun: bool
    deletion_policy: str | None
    sync_direction: str


@dataclass(frozen=True, slots=True)
class P2POptions:
    """Peer-to-peer mode, rendezvous and trust knobs."""

    p2p_mode: bool
    p2p_listen_port: int
    peer_rendezvous_timeout: int | None
    p2p_group: str | None
    prefer_master: bool
    never_master: bool
    p2p_trust: str | None = None
    p2p_allow_unauthenticated: bool = False
    p2p_allow_unauthenticated_writes: bool = False
    p2p_peer_download_enabled: bool = True
    rendezvous_server_pins: FingerprintPins | None = None
    rendezvous_server_tofu: bool = False
    rendezvous_server_trust: str | None = None


@dataclass(frozen=True, slots=True)
class TransferOptions:
    """Transfer/network tuning, caps and queue/autotune overrides."""

    http_timeout: int | None
    reconnect_delay: int | None
    compression_enabled: bool
    max_bandwidth_upload: int | None
    max_bandwidth_download: int | None
    max_concurrent_transfers: int | None = None
    no_autotune: bool = False
    delta_enabled: bool = True
    delta_min_file_size: int | None = None
    delta_min_file_size_p2p: int | None = None
    min_free_disk_space: int = MIN_FREE_DISK_SPACE_DEFAULT
    max_inbound_size: int = MAX_INBOUND_SIZE_MB_DEFAULT
    ignore_patterns: list[str] | None = None
    mass_delete_guard: bool | None = None
    mass_delete_threshold: int | None = None
    mass_delete_percent: int | None = None


@dataclass(frozen=True, slots=True)
class SafetyOptions:
    """TLS verification, CA trust and the server trust pins/TOFU."""

    verify_ssl: bool
    ca_cert: str | None
    allow_deletions: bool = False
    server_fingerprints: tuple[str, ...] = ()
    server_tofu: bool = False
    server_trust: str | None = None


@dataclass(frozen=True, slots=True)
class RunOptions:
    """The complete typed startup surface consumed by ``run_sync``."""

    secrets: SecretsOptions
    logging: LoggingOptions
    endpoints: EndpointOptions
    sync: SyncOptions
    p2p: P2POptions
    transfers: TransferOptions
    safety: SafetyOptions
