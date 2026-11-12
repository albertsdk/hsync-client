# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""``HsyncConfig`` assembly from CLI options and resolved env settings.

The value getters, the default/provenance resolver, the resolved secrets/tuning
dataclasses and ``_build_config`` form the configuration layer; the runtime
wiring (resolvers that produce the inputs and ``run_sync``) stays in
``hsync_client.main_logic``.
"""

from dataclasses import dataclass
from typing import Any, Literal

from hsync_common.protocol.handshake import PROTOCOL_VERSION_REST
from hsync_common.protocol.pairing import FingerprintPins
from hsync_common.protocol.tls_trust import (
    resolve_self_signed_policy,
)

from hsync_client.config import (
    HsyncConfig,
    RequestHeaders,
    get_compression_enabled,
    get_delta_enabled,
    get_heartbeat_interval,
    get_ignore_patterns,
    get_map_data_port,
    get_mass_delete_guard,
    get_mass_delete_percent,
    get_mass_delete_threshold,
    get_max_concurrent_transfers,
    get_mdns_enabled,
    get_never_master,
    get_p2p_group,
    get_p2p_listen_port,
    get_p2p_peer_download,
    get_p2p_peer_read_cap,
    get_p2p_peer_sources_max,
    get_p2p_trust,
    get_peer_rendezvous_timeout,
    get_peer_timeout,
    get_rendezvous_server_extra_ports,
    get_rendezvous_server_pins,
    get_rendezvous_server_refresh_interval,
    get_rendezvous_server_tofu,
    get_rendezvous_transport,
    get_resync,
    get_server_fingerprints,
    get_server_tofu,
    get_stability_delay,
    resolve_deletion_policy,
)
from hsync_client.constants import (
    CLIENT_NAME,
    FILE_STABILITY_INTERVAL_DEFAULT,
    HEADER_CLIENT_ID,
    HEADER_CLIENT_NAME,
    HEADER_CLIENT_VERSION,
    HEADER_PROTOCOL_VERSION,
    HEARTBEAT_INTERVAL,
    HSYNC_VERSION,
    PEER_TIMEOUT,
    RENDEZVOUS_REFRESH_INTERVAL,
    RENDEZVOUS_TIMEOUT_DEFAULT,
    RESYNC_DEFAULT,
    TRANSFER_RETRY_ATTEMPTS_DEFAULT,
    TRANSFER_RETRY_MAX_WAIT_DEFAULT,
    TRANSFER_RETRY_MIN_WAIT_DEFAULT,
)
from hsync_client.run_options import RunOptions
from hsync_client.server_trust_resolution import (
    _flatten_pins,
    _resolve_rendezvous_server_trust,
    _resolve_server_trust,
)

# Provenance of a resolved tuning value; feeds AUTOTUNE_MANAGED_KEYS so the P2P
# post-rendezvous refresh re-derives only autotune/default-sourced keys and never
# clobbers explicit CLI/env overrides. Single encode site: the resolvers below.
TuningSource = Literal["cli", "env", "autotune", "default"]


def _first_defined[T](cli: T | None, env: T | None, default: T) -> T:
    """CLI > env > default."""
    if cli is not None:
        return cli
    if env is not None:
        return env
    return default


def _resolve_with_provenance[T](
    cli: T | None,
    env: T | None,
    default: T,
    autotune: T | None = None,
) -> tuple[T, TuningSource]:
    """CLI > env > autotune > default, with a provenance tag."""
    if cli is not None:
        return cli, "cli"
    if env is not None:
        return env, "env"
    if autotune is not None:
        return autotune, "autotune"
    return default, "default"


def _get_stability_delay_value(cli_value: int | None) -> int:
    """Priority: CLI argument > environment variable > default constant."""
    return _first_defined(cli_value, get_stability_delay(), FILE_STABILITY_INTERVAL_DEFAULT)


def _get_peer_rendezvous_timeout_value(cli_value: int | None) -> int:
    """Priority: CLI > env > default. ``0`` means wait indefinitely."""
    return _first_defined(cli_value, get_peer_rendezvous_timeout(), RENDEZVOUS_TIMEOUT_DEFAULT)


def _get_resync_value(cli_value: int | None) -> int | None:
    """Priority: CLI > env > default. Env ``0`` disables periodic resync."""
    if cli_value is not None:
        return cli_value
    env_value = get_resync()
    if env_value is not None:
        return env_value or None
    return RESYNC_DEFAULT


def _get_max_concurrent_transfers_value(
    cli_value: int | None, autotune: int | None = None
) -> tuple[int, TuningSource]:
    """Priority: CLI > env > autotune > default. Provenance feeds AUTOTUNE_MANAGED_KEYS."""
    from hsync_client.constants import MAX_CONCURRENT_TRANSFERS_DEFAULT

    return _resolve_with_provenance(
        cli_value, get_max_concurrent_transfers(), MAX_CONCURRENT_TRANSFERS_DEFAULT, autotune
    )


def _get_http_timeout_value(
    cli_value: int | None, autotune: int | None = None
) -> tuple[int, TuningSource]:
    """Priority: CLI > env > autotune > default."""
    from hsync_client.config import get_http_timeout
    from hsync_client.constants import HTTP_TIMEOUT_DEFAULT

    return _resolve_with_provenance(cli_value, get_http_timeout(), HTTP_TIMEOUT_DEFAULT, autotune)


def _get_reconnect_delay_value(
    cli_value: int | None, autotune: int | None = None
) -> tuple[int, TuningSource]:
    """Priority: CLI > env > autotune > default."""
    from hsync_client.config import get_reconnect_delay
    from hsync_client.constants import RECONNECT_DELAY_DEFAULT

    return _resolve_with_provenance(
        cli_value, get_reconnect_delay(), RECONNECT_DELAY_DEFAULT, autotune
    )


def _get_transfer_retry_min_wait_value(autotune: int | None = None) -> tuple[float, TuningSource]:
    """Priority: env > autotune > default (no CLI flag)."""
    from hsync_client.config import get_transfer_retry_min_wait

    env_value = get_transfer_retry_min_wait()
    value, source = _resolve_with_provenance(
        None, env_value, TRANSFER_RETRY_MIN_WAIT_DEFAULT, autotune
    )
    return float(value), source


def _get_transfer_retry_max_wait_value(autotune: int | None = None) -> tuple[float, TuningSource]:
    """Priority: env > autotune > default (no CLI flag)."""
    from hsync_client.config import get_transfer_retry_max_wait

    env_value = get_transfer_retry_max_wait()
    value, source = _resolve_with_provenance(
        None, env_value, TRANSFER_RETRY_MAX_WAIT_DEFAULT, autotune
    )
    return float(value), source


def _get_mass_delete_guard_value(cli_value: bool | None) -> bool:
    """Priority: CLI > env > default."""
    if cli_value is not None:
        return cli_value
    return get_mass_delete_guard()


def _get_mass_delete_threshold_value(cli_value: int | None) -> int:
    """Priority: CLI > env > default."""
    if cli_value is not None:
        return cli_value
    return get_mass_delete_threshold()


def _get_mass_delete_percent_value(cli_value: int | None) -> int:
    """Priority: CLI > env > default."""
    if cli_value is not None:
        return cli_value
    return get_mass_delete_percent()


@dataclass(frozen=True, slots=True)
class _ResolvedSecrets:
    """Key material and authorization postures resolved at startup."""

    api_key: str
    p2p_key: str | None
    allow_deletions: bool
    allow_unauthenticated_p2p: bool
    allow_unauthenticated_p2p_writes: bool


@dataclass(frozen=True, slots=True)
class _ResolvedTuning:
    """Autotune outcome and resolved tuning values (provenance-tagged)."""

    autotune_enabled: bool
    profile: Any | None
    tuning_keys: Any
    resolved_http_timeout: Any
    resolved_reconnect_delay: Any
    resolved_retry_min: Any
    resolved_retry_max: Any
    resolved_max_concurrent: Any
    conn_retry_attempts: int
    conn_retry_min: float
    conn_retry_max: float


def _build_config(
    options: RunOptions,
    resolved: _ResolvedSecrets,
    cid: str,
    effective_server_url: str,
    ws_url: str,
    _t: _ResolvedTuning,
) -> HsyncConfig:
    """Resolve the TLS pins and build the global CONFIG payload."""
    bucket = options.endpoints.bucket
    verify_ssl = options.safety.verify_ssl
    ca_cert = options.safety.ca_cert
    local_path = options.sync.local_path
    continuous = options.sync.continuous
    resync = options.sync.resync
    stability_delay = options.sync.stability_delay
    max_wait_timeout = options.sync.max_wait_timeout
    filter_path = options.sync.filter_path
    dryrun = options.sync.dryrun
    deletion_policy = options.sync.deletion_policy
    sync_direction = options.sync.sync_direction
    compression_enabled = options.transfers.compression_enabled
    max_bandwidth_upload = options.transfers.max_bandwidth_upload
    max_bandwidth_download = options.transfers.max_bandwidth_download
    p2p_mode = options.p2p.p2p_mode
    p2p_listen_port = options.p2p.p2p_listen_port
    peer_rendezvous_timeout = options.p2p.peer_rendezvous_timeout
    p2p_group = options.p2p.p2p_group
    prefer_master = options.p2p.prefer_master
    never_master = options.p2p.never_master
    quiet = options.logging.quiet
    logfile = options.logging.logfile
    p2p_trust = options.p2p.p2p_trust
    min_free_disk_space = options.transfers.min_free_disk_space
    max_inbound_size = options.transfers.max_inbound_size
    ignore_patterns = options.transfers.ignore_patterns
    mass_delete_guard = options.transfers.mass_delete_guard
    mass_delete_threshold = options.transfers.mass_delete_threshold
    mass_delete_percent = options.transfers.mass_delete_percent
    delta_enabled = options.transfers.delta_enabled
    delta_min_file_size = options.transfers.delta_min_file_size
    delta_min_file_size_p2p = options.transfers.delta_min_file_size_p2p
    p2p_peer_download_enabled = options.p2p.p2p_peer_download_enabled
    mdns_enabled = options.p2p.mdns_enabled
    rendezvous_server_endpoints = options.endpoints.rendezvous_server_endpoints
    map_data_port = options.endpoints.map_data_port
    rendezvous_server_pins = options.p2p.rendezvous_server_pins
    rendezvous_server_tofu = options.p2p.rendezvous_server_tofu
    rendezvous_server_trust = options.p2p.rendezvous_server_trust
    server_fingerprints = options.safety.server_fingerprints
    server_tofu = options.safety.server_tofu
    server_trust = options.safety.server_trust
    resolved_api_key = resolved.api_key
    resolved_p2p_key = resolved.p2p_key
    allow_deletions = resolved.allow_deletions
    allow_unauthenticated_p2p = resolved.allow_unauthenticated_p2p
    allow_unauthenticated_p2p_writes = resolved.allow_unauthenticated_p2p_writes
    autotune_enabled = _t.autotune_enabled
    tuning = _t.profile
    _tuning_keys = _t.tuning_keys
    _resolved_http_timeout = _t.resolved_http_timeout
    _resolved_reconnect_delay = _t.resolved_reconnect_delay
    _resolved_retry_min = _t.resolved_retry_min
    _resolved_retry_max = _t.resolved_retry_max
    _resolved_max_concurrent = _t.resolved_max_concurrent
    _conn_retry_attempts = _t.conn_retry_attempts
    _conn_retry_min = _t.conn_retry_min
    _conn_retry_max = _t.conn_retry_max

    # Pins are keyed by endpoint (CLI/config resolved); a missing map means "read
    # the environment", which is also the single-endpoint configuration path.
    pins: FingerprintPins = (
        rendezvous_server_pins
        if rendezvous_server_pins is not None
        else get_rendezvous_server_pins()
    )

    # The P2P master keys its per-peer admission buckets on the requester CID:
    # relayed peers all reach it from the loopback address, so a request without
    # the CID would share one bucket with every other relayed peer. Central mode
    # sends nothing extra (the server does not key on it).
    request_headers: RequestHeaders = {
        "accept": "application/json",
        "Authorization": f"Bearer {resolved_p2p_key or '' if p2p_mode else resolved_api_key}",
        HEADER_CLIENT_NAME: CLIENT_NAME,
        HEADER_CLIENT_VERSION: HSYNC_VERSION,
        HEADER_PROTOCOL_VERSION: PROTOCOL_VERSION_REST,
    }
    if p2p_mode and cid:
        request_headers[HEADER_CLIENT_ID] = cid

    config: HsyncConfig = {
        "API": {
            "buckets": f"{effective_server_url}/api/v1/buckets",
            "events": f"{ws_url}/api/v1/buckets",
        },
        "API_KEY": resolved_api_key,
        "CID": cid,
        "DRYRUN": dryrun,
        "LOCAL_BUCKET": local_path,
        "LOGFILE": logfile,
        "NO_VERIFY_SSL": not verify_ssl,
        "CA_CERT": ca_cert,
        "QUIET": quiet,
        "REMOTE_BUCKET": bucket,
        "REQUEST_HEADERS": request_headers,
        "RECONNECT_DELAY": _resolved_reconnect_delay,
        "RESYNC": _get_resync_value(resync),
        "FILTER_PATH": filter_path,
        "STABILITY_DELAY": _get_stability_delay_value(stability_delay),
        "HTTP_TIMEOUT": _resolved_http_timeout,
        "MAX_WAIT_TIMEOUT": max_wait_timeout,
        "P2P_MODE": p2p_mode,
        "P2P_LISTEN_PORT": p2p_listen_port or get_p2p_listen_port() or 0,
        "RENDEZVOUS_SERVER_ENDPOINTS": tuple(rendezvous_server_endpoints),
        "RENDEZVOUS_SERVER_EXTRA_PORTS": get_rendezvous_server_extra_ports(),
        # Rendezvous-server transport selector: CLI override (``--rendezvous-transport``)
        # > the env value (``HSYNC_RENDEZVOUS_TRANSPORT``) > the ``auto`` default.
        "RENDEZVOUS_TRANSPORT": options.endpoints.rendezvous_transport
        or get_rendezvous_transport(),
        # Rendezvous-server TLS trust (ADR-0034): explicit fingerprints or opt-in TOFU.
        # Pins are keyed by endpoint: a pin only ever describes a server the
        # client dials, and two independent servers keep their own identity.
        "RENDEZVOUS_SERVER_PINS": pins,
        "RENDEZVOUS_SERVER_TRUST": _resolve_rendezvous_server_trust(
            rendezvous_server_trust,
            _flatten_pins(pins),
            rendezvous_server_tofu or get_rendezvous_server_tofu(),
            verify_ssl,
        ),
        "MAP_DATA_PORT": get_map_data_port() if map_data_port is None else bool(map_data_port),
        # Discover peers over mDNS on the local network (False = discovery servers
        # only). Both the CLI flag/env and the RunOptions field can disable it.
        "MDNS_ENABLED": mdns_enabled and get_mdns_enabled(),
        "PEER_RENDEZVOUS_TIMEOUT": _get_peer_rendezvous_timeout_value(peer_rendezvous_timeout),
        "HEARTBEAT_INTERVAL": get_heartbeat_interval() or HEARTBEAT_INTERVAL,
        "RENDEZVOUS_SERVER_REFRESH_INTERVAL": get_rendezvous_server_refresh_interval()
        or RENDEZVOUS_REFRESH_INTERVAL,
        "PEER_TIMEOUT": get_peer_timeout() or PEER_TIMEOUT,
        "P2P_KEY": resolved_p2p_key,
        "P2P_GROUP": p2p_group or get_p2p_group(),
        # One-shot is derived solely from --once: continuous is the default
        # and no env/flag knob for "persistence" exists.
        "ONE_SHOT": not continuous,
        "P2P_PREFER_MASTER": prefer_master,
        "P2P_NEVER_MASTER": never_master or get_never_master(),
        "P2P_TRUST": resolve_self_signed_policy(p2p_trust, env_default=get_p2p_trust()),
        "P2P_PEER_DOWNLOAD": p2p_peer_download_enabled and get_p2p_peer_download(),
        "P2P_PEER_READ_CAP": get_p2p_peer_read_cap(),
        "P2P_PEER_SOURCES_MAX": get_p2p_peer_sources_max(),
        "P2P_ALLOW_UNAUTHENTICATED_WRITES": allow_unauthenticated_p2p_writes,
        "P2P_ALLOW_UNAUTHENTICATED": allow_unauthenticated_p2p,
        "ALLOW_DELETIONS": allow_deletions,
        "MASTER_CERT_FINGERPRINT": None,
        # Server TLS trust (ADR-0036): resolved from flags/env; the pin itself is
        # set at connect time by _evaluate_server_trust.
        "SERVER_TRUST": _resolve_server_trust(
            server_trust,
            server_fingerprints or get_server_fingerprints(),
            server_tofu or get_server_tofu(),
            verify_ssl,
        ),
        "SERVER_FINGERPRINTS": server_fingerprints or get_server_fingerprints(),
        "SERVER_CERT_FINGERPRINT": None,
        "DELETION_POLICY": resolve_deletion_policy(deletion_policy),
        "COMPRESSION_ENABLED": compression_enabled and get_compression_enabled(),
        "SERVER_COMPRESSION_ENABLED": False,  # Set during WebSocket handshake
        "DELTA_ENABLED": delta_enabled and get_delta_enabled(),
        "SERVER_DELTA_ENABLED": False,  # Set during WebSocket handshake / master election
        # CLI overrides for the delta size floor (None = use env settings)
        "DELTA_MIN_FILE_SIZE": delta_min_file_size,
        "DELTA_MIN_FILE_SIZE_P2P": delta_min_file_size_p2p,
        "SYNC_DIRECTION": sync_direction,
        "MAX_BANDWIDTH_UPLOAD": max_bandwidth_upload,
        "MAX_BANDWIDTH_DOWNLOAD": max_bandwidth_download,
        "MIN_FREE_DISK_SPACE": min_free_disk_space,
        # Absolute cap (bytes) on a single inbound file, applied on top of the
        # peer-declared size (anti decompression-bomb); 0 = disabled.
        "MAX_INBOUND_SIZE": max_inbound_size * 1024 * 1024,
        "IGNORE_PATTERNS": ignore_patterns
        if ignore_patterns is not None
        else get_ignore_patterns(),
        "MASS_DELETE_GUARD": _get_mass_delete_guard_value(mass_delete_guard),
        "MASS_DELETE_THRESHOLD": _get_mass_delete_threshold_value(mass_delete_threshold),
        "MASS_DELETE_PERCENT": _get_mass_delete_percent_value(mass_delete_percent),
        "MAX_CONCURRENT_TRANSFERS": _resolved_max_concurrent,
        "TRANSFER_RETRY_ATTEMPTS": TRANSFER_RETRY_ATTEMPTS_DEFAULT,
        "TRANSFER_RETRY_MIN_WAIT": _resolved_retry_min,
        "TRANSFER_RETRY_MAX_WAIT": _resolved_retry_max,
        "CONNECTIVITY_RETRY_ATTEMPTS": _conn_retry_attempts,
        "CONNECTIVITY_RETRY_MIN_WAIT": _conn_retry_min,
        "CONNECTIVITY_RETRY_MAX_WAIT": _conn_retry_max,
        # Auto-tuning runtime state (updated below after set_config)
        "AUTOTUNE_ENABLED": autotune_enabled,
        "AUTOTUNE_STORAGE_TYPE": tuning.storage_type.value if (autotune_enabled and tuning) else "",
        # Network class from the autotune RTT probe to the master (set at runtime
        # by ``_refresh_p2p_network_tuning``; "" until measured).
        "P2P_NET_CLASS": "",
        # Keys the P2P post-rendezvous refresh AND the per-cycle concurrency
        # refresh may re-derive: only those resolved from autotune/default
        # (CLI/env overrides are preserved). Derived from `_tuning_keys` so it
        # stays in sync with the user-override log above.
        "AUTOTUNE_MANAGED_KEYS": [
            key
            for key, _label, _value, src, _unit in _tuning_keys
            if src in ("autotune", "default")
        ],
    }
    return config
