# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""CLI option parsing, validation and ``RunOptions`` assembly.

Pure helpers shared by the ``hsync`` commands. The Typer app and the commands
themselves live in ``hsync_client.cli``; these helpers only read/validate the
parsed CLI values and never register anything.
"""

import sys
from collections.abc import Mapping
from typing import Annotated, Any, TypedDict, cast

import typer
import typer.main
from hsync_common.endpoint import (
    HostPort,
    build_https_url,
    parse_endpoint_list,
    parse_host_port,
    parse_https_url,
)
from hsync_common.path_validation import is_valid_bucket_name
from hsync_common.protocol.pairing import (
    FingerprintPins,
    parse_pin_map,
)
from hsync_common.runtime import ColorMode

from hsync_client.constants import (
    BANDWIDTH_MAX,
    BANDWIDTH_MIN,
    HTTP_TIMEOUT_MAX,
    HTTP_TIMEOUT_MIN,
    MAX_CONCURRENT_TRANSFERS_MAX,
    MAX_CONCURRENT_TRANSFERS_MIN,
    MAX_INBOUND_SIZE_MB_MAX,
    MAX_INBOUND_SIZE_MB_MIN,
    MAX_WAIT_TIMEOUT_MAX,
    MAX_WAIT_TIMEOUT_MIN,
    MIN_FREE_DISK_SPACE_DEFAULT,
    MIN_FREE_DISK_SPACE_MAX,
    MIN_FREE_DISK_SPACE_MIN,
    RECONNECT_DELAY_MAX,
    RECONNECT_DELAY_MIN,
    RENDEZVOUS_DEFAULT_PORT,
    RENDEZVOUS_TIMEOUT_DEFAULT,
    RESYNC_MAX,
    RESYNC_MIN,
    SERVER_DEFAULT_PORT,
    STABILITY_DELAY_MAX,
    STABILITY_DELAY_MIN,
    SYNC_DIRECTIONS,
)
from hsync_client.run_options import (
    EndpointOptions,
    LoggingOptions,
    P2POptions,
    RunOptions,
    SafetyOptions,
    SecretsOptions,
    SyncOptions,
    TransferOptions,
)


def _validate_bucket(value: str) -> str:
    """Validate bucket name (canonical SSOT ``is_valid_bucket_name``)."""
    if not is_valid_bucket_name(value):
        raise typer.BadParameter(
            "The bucket name must be alphanumeric (hyphens and underscores allowed, "
            "but not at the beginning or end) and at most 63 characters."
        )
    return value


def _parse_host_port(raw: str) -> HostPort:
    """Parse a ``HOST[:PORT]`` endpoint, surfacing errors as CLI errors.

    Shared SSOT: ``hsync_common.endpoint.parse_host_port``.
    """
    try:
        return parse_host_port(raw)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _build_server_url(url: str | None) -> str | None:
    """Normalize the ``--server-url`` value; ``None`` keeps the env fallback.

    Server mode always uses HTTPS (the client refuses to send the API key over
    an unencrypted channel), so a bare ``HOST[:PORT]`` is normalized to
    ``https://host:port`` (port from the SSOT default) and an explicit
    ``https://…[/path]`` URL is validated and returned unchanged. The port is
    carried by the endpoint itself: there is no separate ``--port``.

    Raises:
        typer.BadParameter: on a non-HTTPS scheme, an unparseable host or a port
            outside 1-65535.
    """
    raw = (url or "").strip()
    if not raw:
        return None
    if "://" in raw:
        if not raw.lower().startswith("https://"):
            raise typer.BadParameter("the server URL must use HTTPS.")
        try:
            return parse_https_url(raw)
        except ValueError as exc:
            raise typer.BadParameter(f"invalid server URL: {exc}") from exc
    return build_https_url(_parse_host_port(raw), SERVER_DEFAULT_PORT)


def _build_rendezvous_server_pins(
    values: list[str] | None, endpoints: tuple[HostPort, ...]
) -> FingerprintPins:
    """Resolve ``--rendezvous-server-fingerprint`` into a per-endpoint pin map.

    ``ENDPOINT=FINGERPRINT[|FINGERPRINT]`` binds a pin to the server it belongs
    to, which is the only safe form with several servers (a shared pin would tie
    unrelated identities together). A bare fingerprint is accepted only when the
    command configured exactly one endpoint, where it cannot be ambiguous.
    """
    if not values:
        return {}
    if not endpoints:
        raise typer.BadParameter(
            "--rendezvous-server-fingerprint requires at least one --rendezvous-server endpoint."
        )
    # Imported here: the config module pulls the pydantic models, which the CLI
    # must not load at import time (see tests/test_startup_imports.py).
    from hsync_client.config import resolve_rendezvous_server_pins

    try:
        pins = parse_pin_map(",".join(values), strict=True)
        return resolve_rendezvous_server_pins(pins, endpoints, default_port=RENDEZVOUS_DEFAULT_PORT)
    except ValueError as exc:
        raise typer.BadParameter(f"invalid --rendezvous-server-fingerprint: {exc}") from exc


def _build_rendezvous_servers(
    values: list[str] | None,
    *,
    use_env: bool = False,
) -> tuple[HostPort, ...]:
    """Resolve the rendezvous-server endpoints; return them ordered and deduplicated.

    ``--rendezvous-server`` is repeatable and each value may itself carry a
    comma-separated list, so the option accepts exactly the grammar of
    ``HSYNC_RENDEZVOUS_SERVER_ENDPOINTS`` (one parser, one grammar). An entry that
    omits the port gets the channel default, so ``a`` and ``a:443`` are one
    destination. When ``use_env`` is set (the ``p2p`` command) a missing CLI
    value falls back to the environment. An empty tuple selects the mDNS plan.
    """
    raw = ",".join(value for value in (values or ()) if value.strip())
    try:
        endpoints = parse_endpoint_list(raw, strict=True, default_port=RENDEZVOUS_DEFAULT_PORT)
    except ValueError as exc:
        raise typer.BadParameter(f"invalid discovery server: {exc}") from exc
    if not endpoints and use_env:
        from hsync_client.config import get_rendezvous_server_endpoints

        endpoints = get_rendezvous_server_endpoints()
    return tuple(endpoints)


# =============================================================================
# Shared validation (called by both subcommands)
# =============================================================================


def _parse_and_validate_bandwidth(value: str | None, flag: str, label: str) -> int | None:
    """Parse *value* as a bandwidth rate and enforce the global bounds.

    ``None`` (flag omitted) passes through as ``None`` (unlimited). A parse
    error or an out-of-range rate raises ``typer.BadParameter``.
    """
    if value is None:
        return None
    from hsync_client.utils import parse_bandwidth

    try:
        parsed = parse_bandwidth(value)
    except ValueError as e:
        raise typer.BadParameter(f"Invalid {flag}: {e}")
    if parsed is not None and (parsed < BANDWIDTH_MIN or parsed > BANDWIDTH_MAX):
        raise typer.BadParameter(
            f"{label} bandwidth must be between {BANDWIDTH_MIN} and {BANDWIDTH_MAX} bytes/s."
        )
    return parsed


def _parse_size_option(
    value: str | None,
    flag: str,
    *,
    minimum: int,
    maximum: int,
    default: int,
) -> int:
    """Parse a human-readable size option into bytes and enforce its bounds.

    ``None`` (flag omitted) yields *default*. The unit is mandatory for a
    non-zero value, so ``--min-free-disk-space 500`` can never be silently read
    as 500 bytes when the operator meant 500 MB; ``0`` (disabled) needs none.
    A parse error or an out-of-range size raises ``typer.BadParameter`` with the
    bounds rendered by the shared ``format_bytes``.
    """
    if value is None:
        return default
    from hsync_common.format import format_bytes, parse_size

    try:
        parsed = parse_size(value, require_unit=True)
    except ValueError as e:
        raise typer.BadParameter(f"Invalid {flag}: {e}")
    if parsed < minimum or parsed > maximum:
        raise typer.BadParameter(
            f"{flag} must be between {format_bytes(minimum)} and {format_bytes(maximum)}."
        )
    return parsed


def _load_env_file_or_exit(env_file: str | None) -> None:
    """Load ``--env-file`` before option resolution, exiting on a missing file.

    Both commands resolve some options from the environment after this point
    (the client would otherwise load the file only later, inside ``run_sync``),
    so an ``--env-file`` value must be visible here. Kept in one place so the
    two commands cannot drift on the failure wording or the exit code.
    """
    if not env_file:
        return
    from hsync_client.config import load_env_file

    try:
        load_env_file(env_file)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(1) from None


def _build_run_options(values: Mapping[str, Any], **overrides: Any) -> RunOptions:
    """Map the shared CLI options into the typed ``run_sync`` options.

    ``values`` is the command frame's ``locals()`` (the common options); the
    command-specific or body-computed values arrive as keyword ``overrides``
    keyed by their ``run_options`` field names. Keeping the normalization here
    prevents the p2p and server command bodies from drifting.
    """
    # Imported here: the config module pulls the pydantic models, which the CLI
    # must not load at import time (see tests/test_startup_imports.py).
    from hsync_client.config import DELETION_POLICIES

    policy = values["deletion_policy"]
    if policy is not None and policy not in DELETION_POLICIES:
        allowed = ", ".join(DELETION_POLICIES)
        raise typer.BadParameter(f"invalid --deletion-policy value: {policy!r}; use {allowed}")
    return RunOptions(
        secrets=SecretsOptions(
            env_file=values["env_file"],
            api_key_fd=overrides.get("api_key_fd"),
            p2p_key_fd=overrides.get("p2p_key_fd"),
            p2p_passphrase_fd=overrides.get("p2p_passphrase_fd"),
        ),
        logging=LoggingOptions(
            debug=values["verbose"],
            quiet=values["quiet"],
            logfile=values["logfile"],
            color=values["color"],
            events_json_fd=values["events_json_fd"],
        ),
        endpoints=EndpointOptions(
            bucket=values["bucket"],
            server_url=overrides.get("server_url"),
            rendezvous_server_endpoints=overrides.get("rendezvous_server_endpoints", ()),
            map_data_port=overrides.get("map_data_port"),
            rendezvous_transport=overrides.get("rendezvous_transport"),
        ),
        sync=SyncOptions(
            local_path=values["local_path"],
            continuous=not values["once"],
            resync=values["resync"],
            stability_delay=values["stability_delay"],
            max_wait_timeout=values["max_wait_timeout"],
            filter_path=values["filter_path"],
            dryrun=values["dryrun"],
            deletion_policy=values["deletion_policy"],
            sync_direction=values["sync_direction"],
        ),
        p2p=P2POptions(
            p2p_mode=overrides.get("p2p_mode", False),
            p2p_listen_port=overrides.get("p2p_listen_port", 0),
            peer_rendezvous_timeout=overrides.get("peer_rendezvous_timeout"),
            p2p_group=overrides.get("p2p_group"),
            prefer_master=overrides.get("prefer_master", False),
            never_master=overrides.get("never_master", False),
            p2p_trust=overrides.get("p2p_trust"),
            p2p_allow_unauthenticated=overrides.get("p2p_allow_unauthenticated", False),
            p2p_allow_unauthenticated_writes=overrides.get(
                "p2p_allow_unauthenticated_writes", False
            ),
            p2p_peer_download_enabled=overrides.get("p2p_peer_download_enabled", True),
            mdns_enabled=overrides.get("mdns_enabled", True),
            rendezvous_server_pins=overrides.get("rendezvous_server_pins"),
            rendezvous_server_tofu=overrides.get("rendezvous_server_tofu", False),
            rendezvous_server_trust=overrides.get("rendezvous_server_trust"),
        ),
        transfers=TransferOptions(
            http_timeout=values["http_timeout"],
            reconnect_delay=values["reconnect_delay"],
            compression_enabled=not values["no_compression"],
            max_bandwidth_upload=values["parsed_bw_upload"],
            max_bandwidth_download=values["parsed_bw_download"],
            max_concurrent_transfers=values["max_concurrent_transfers"],
            no_autotune=values["no_autotune"],
            delta_enabled=not values["no_delta"],
            delta_min_file_size=values["delta_min_file_size"],
            delta_min_file_size_p2p=values["delta_min_file_size_p2p"],
            min_free_disk_space=values["parsed_min_free_disk_space"],
            max_inbound_size=values["max_inbound_size"],
            ignore_patterns=[p.strip() for p in (values["ignore_patterns"] or []) if p.strip()],
            mass_delete_guard=values["mass_delete_guard"],
            mass_delete_threshold=values["mass_delete_threshold"],
            mass_delete_percent=values["mass_delete_percent"],
        ),
        safety=SafetyOptions(
            verify_ssl=not values["insecure"],
            ca_cert=values["ca_cert"],
            allow_deletions=values["allow_deletions"],
            server_fingerprints=tuple(overrides.get("server_fingerprints", ()) or ()),
            server_tofu=overrides.get("server_tofu", False),
            server_trust=overrides.get("server_trust"),
        ),
    )


class _SharedValidationValues(TypedDict):
    """Options validated identically by the ``p2p`` and ``server`` commands.

    This TypedDict is the single declaration of the set: its field names drive
    both the frame extraction (:data:`_SHARED_VALIDATION_KEYS`) and the static
    type of every read, so a typo in a key is a mypy error and a command missing
    one is a test failure (``tests/test_cli_option_parity.py``).
    """

    http_timeout: int | None
    resync: int | None
    reconnect_delay: int | None
    stability_delay: int | None
    max_wait_timeout: int
    sync_direction: str
    min_free_disk_space: str | None
    max_inbound_size: int
    max_bandwidth_upload: str | None
    max_bandwidth_download: str | None
    max_concurrent_transfers: int | None
    mass_delete_threshold: int | None
    mass_delete_percent: int | None
    delta_min_file_size: int | None
    delta_min_file_size_p2p: int | None


# Derived from the TypedDict, so the key tuple and the field types cannot drift.
_SHARED_VALIDATION_KEYS: tuple[str, ...] = tuple(_SharedValidationValues.__annotations__)


def _validate_shared_options(values: Mapping[str, Any]) -> tuple[int | None, int | None, int]:
    """Validate the options shared by the ``p2p`` and ``server`` subcommands.

    ``values`` is the command frame's ``locals()`` — the same mapping
    ``_build_run_options`` consumes — so validation and forwarding cannot
    disagree about which shared options exist, and neither command repeats the
    argument list. The accepted names are :class:`_SharedValidationValues`: a
    command that stops exposing one is a programming error, so it fails
    immediately naming the option, not with a bare ``KeyError``.

    Returns the parsed upload/download bandwidth limits (bytes/s, or ``None``)
    and the parsed free-space reserve (bytes).
    """
    try:
        opts = cast(
            _SharedValidationValues,
            {name: values[name] for name in _SHARED_VALIDATION_KEYS},
        )
    except KeyError as exc:
        raise RuntimeError(
            f"shared CLI option {exc.args[0]!r} is missing from the command frame: "
            "declare it on both `p2p` and `server`"
        ) from exc
    http_timeout = opts["http_timeout"]
    resync = opts["resync"]
    reconnect_delay = opts["reconnect_delay"]
    stability_delay = opts["stability_delay"]
    max_wait_timeout = opts["max_wait_timeout"]
    sync_direction = opts["sync_direction"]
    min_free_disk_space = opts["min_free_disk_space"]
    max_inbound_size = opts["max_inbound_size"]
    max_bandwidth_upload = opts["max_bandwidth_upload"]
    max_bandwidth_download = opts["max_bandwidth_download"]
    max_concurrent_transfers = opts["max_concurrent_transfers"]
    mass_delete_threshold = opts["mass_delete_threshold"]
    mass_delete_percent = opts["mass_delete_percent"]
    delta_min_file_size = opts["delta_min_file_size"]
    delta_min_file_size_p2p = opts["delta_min_file_size_p2p"]
    if http_timeout is not None and (
        http_timeout < HTTP_TIMEOUT_MIN or http_timeout > HTTP_TIMEOUT_MAX
    ):
        raise typer.BadParameter(
            f"HTTP timeout must be between {HTTP_TIMEOUT_MIN} and {HTTP_TIMEOUT_MAX}."
        )
    if resync is not None and (resync < RESYNC_MIN or resync > RESYNC_MAX):
        raise typer.BadParameter(f"Resync interval must be between {RESYNC_MIN} and {RESYNC_MAX}.")
    if reconnect_delay is not None and (
        reconnect_delay < RECONNECT_DELAY_MIN or reconnect_delay > RECONNECT_DELAY_MAX
    ):
        raise typer.BadParameter(
            f"Retry delay must be between {RECONNECT_DELAY_MIN} and {RECONNECT_DELAY_MAX}."
        )
    if stability_delay is not None and (
        stability_delay < STABILITY_DELAY_MIN or stability_delay > STABILITY_DELAY_MAX
    ):
        raise typer.BadParameter(
            f"Stability delay must be between {STABILITY_DELAY_MIN} and {STABILITY_DELAY_MAX}."
        )
    if max_wait_timeout < MAX_WAIT_TIMEOUT_MIN or max_wait_timeout > MAX_WAIT_TIMEOUT_MAX:
        raise typer.BadParameter(
            f"Max wait timeout must be between {MAX_WAIT_TIMEOUT_MIN} and {MAX_WAIT_TIMEOUT_MAX}."
        )
    if sync_direction not in SYNC_DIRECTIONS:
        raise typer.BadParameter(f"sync-direction must be one of: {', '.join(SYNC_DIRECTIONS)}.")
    parsed_min_free_disk_space = _parse_size_option(
        min_free_disk_space,
        "--min-free-disk-space",
        minimum=MIN_FREE_DISK_SPACE_MIN,
        maximum=MIN_FREE_DISK_SPACE_MAX,
        default=MIN_FREE_DISK_SPACE_DEFAULT,
    )
    if max_inbound_size < MAX_INBOUND_SIZE_MB_MIN or max_inbound_size > MAX_INBOUND_SIZE_MB_MAX:
        raise typer.BadParameter(
            f"Max download size must be between "
            f"{MAX_INBOUND_SIZE_MB_MIN} and {MAX_INBOUND_SIZE_MB_MAX} MB."
        )

    parsed_bw_upload = _parse_and_validate_bandwidth(
        max_bandwidth_upload, "--limit-upload", "Upload"
    )
    parsed_bw_download = _parse_and_validate_bandwidth(
        max_bandwidth_download, "--limit-download", "Download"
    )

    if max_concurrent_transfers is not None and (
        max_concurrent_transfers < MAX_CONCURRENT_TRANSFERS_MIN
        or max_concurrent_transfers > MAX_CONCURRENT_TRANSFERS_MAX
    ):
        raise typer.BadParameter(
            f"--max-concurrent-transfers must be between "
            f"{MAX_CONCURRENT_TRANSFERS_MIN} and {MAX_CONCURRENT_TRANSFERS_MAX}."
        )

    if mass_delete_threshold is not None and mass_delete_threshold < 0:
        raise typer.BadParameter("--mass-delete-threshold must be >= 0 (0 = disabled).")
    if mass_delete_percent is not None and (mass_delete_percent < 0 or mass_delete_percent > 100):
        raise typer.BadParameter("--mass-delete-percent must be between 0 and 100 (0 = disabled).")
    # Delta size floors (0 = delta for every file size)
    if delta_min_file_size is not None and delta_min_file_size < 0:
        raise typer.BadParameter("--delta-min-file-size must be >= 0.")
    if delta_min_file_size_p2p is not None and delta_min_file_size_p2p < 0:
        raise typer.BadParameter("--delta-min-file-size-p2p must be >= 0.")

    return parsed_bw_upload, parsed_bw_download, parsed_min_free_disk_space


# =============================================================================
# Shared option annotations
#
# Declared once and reused by both subcommand signatures. Typer builds a fresh
# click parameter for each command from the (immutable) Annotated metadata, so
# sharing the aliases cannot cross-bind options between subcommands; the
# per-subcommand surfaces are pinned by the black-box tests, by the
# --capabilities-json introspection and by tests/test_cli_option_parity.py
# (every option is classified as common or mode-specific).
# =============================================================================

BucketOpt = Annotated[
    str,
    typer.Option(
        "-b",
        "--bucket",
        help="Remote bucket name.",
        metavar="NAME",
        callback=lambda v: _validate_bucket(v),
        rich_help_panel="Required",
    ),
]
PathOpt = Annotated[
    str,
    typer.Option(
        "-p",
        "--path",
        help="Local directory path.",
        metavar="DIR",
        rich_help_panel="Sync",
    ),
]
OnceOpt = Annotated[
    bool,
    typer.Option(
        "--once",
        help=(
            "Run a single sync cycle and exit. Without --once, the client "
            "keeps running and retries the connection indefinitely (the "
            "default). In P2P mode it runs one slave sync cycle and exits."
        ),
        rich_help_panel="Sync",
    ),
]
IntervalOpt = Annotated[
    int | None,
    typer.Option(
        "-i",
        "--interval",
        help="Force full sync every SEC seconds (continuous mode only).",
        metavar="SEC",
        rich_help_panel="Sync",
    ),
]
RetryDelayOpt = Annotated[
    int | None,
    typer.Option(
        "--retry-delay",
        help=(
            "Base of the reconnect backoff in seconds (exponential with "
            "jitter, capped at 60; default: auto-detected)."
        ),
        metavar="SEC",
        rich_help_panel="Sync",
    ),
]
StabilityDelayOpt = Annotated[
    int | None,
    typer.Option(
        "--stability-delay",
        help="Seconds a file must stay unchanged before upload. Already-idle files skip the wait.",
        metavar="SEC",
        rich_help_panel="Sync",
    ),
]
MaxWaitTimeoutOpt = Annotated[
    int,
    typer.Option(
        "--max-wait-timeout",
        help=(
            "Maximum seconds to wait for a file to stop being written; the upload is "
            "deferred and retried when it is still changing."
        ),
        metavar="SEC",
        rich_help_panel="Sync",
    ),
]
SubdirOpt = Annotated[
    str | None,
    typer.Option(
        "--subdir",
        help="Sync only files within this subdirectory path.",
        metavar="PATH",
        rich_help_panel="Sync",
    ),
]
DryRunOpt = Annotated[
    bool,
    typer.Option(
        "-n",
        "--dry-run",
        help="Show what would be done without making changes.",
        rich_help_panel="Sync",
    ),
]
DeletionPolicyOpt = Annotated[
    str | None,
    typer.Option(
        "--deletion-policy",
        help=(
            "What a deletion does: 'propagate' (default: local deletions are applied "
            "and propagated) or 'keep' (this device never deletes: local and remote "
            "deletions are ignored, an additive backup)."
        ),
        metavar="POLICY",
        show_default=False,
        rich_help_panel="Sync",
    ),
]
SyncDirectionOpt = Annotated[
    str,
    typer.Option(
        "--sync-direction",
        help="Sync direction: bidirectional, upload-only, or download-only.",
        rich_help_panel="Sync",
    ),
]
LimitUploadOpt = Annotated[
    str | None,
    typer.Option(
        "--limit-upload",
        help="Maximum upload bandwidth (e.g., '1MB', '500KB'). 0 = unlimited.",
        metavar="RATE",
        rich_help_panel="Sync",
    ),
]
LimitDownloadOpt = Annotated[
    str | None,
    typer.Option(
        "--limit-download",
        help="Maximum download bandwidth (e.g., '1MB', '500KB'). 0 = unlimited.",
        metavar="RATE",
        rich_help_panel="Sync",
    ),
]
NoCompressionOpt = Annotated[
    bool,
    typer.Option(
        "--no-compression",
        help="Disable zstd compression for file transfers.",
        rich_help_panel="Sync",
    ),
]
NoDeltaOpt = Annotated[
    bool,
    typer.Option(
        "--no-delta",
        help="Disable rsync-style delta transfer (block-level sync) for large files.",
        rich_help_panel="Sync",
    ),
]
NoPeerDownloadOpt = Annotated[
    bool,
    typer.Option(
        "--no-peer-download",
        help="Disable peer-assisted downloads and local read-only seeding in P2P mode.",
        rich_help_panel="P2P Mode",
    ),
]
DeltaMinFileSizeOpt = Annotated[
    int | None,
    typer.Option(
        "--delta-min-file-size",
        help="Minimum file size in bytes for delta transfer (server mode). Default: 262144 (256 KB).",
        metavar="BYTES",
        rich_help_panel="Sync",
    ),
]
DeltaMinFileSizeP2POpt = Annotated[
    int | None,
    typer.Option(
        "--delta-min-file-size-p2p",
        help="Minimum file size in bytes for delta transfer (P2P mode). Default: 1048576 (1 MB).",
        metavar="BYTES",
        rich_help_panel="Sync",
    ),
]
MaxConcurrentTransfersOpt = Annotated[
    int | None,
    typer.Option(
        "--max-concurrent-transfers",
        help="Maximum number of concurrent file transfers and inbound events applied at once (1-20).",
        metavar="N",
        rich_help_panel="Sync",
    ),
]
NoAutotuneOpt = Annotated[
    bool,
    typer.Option(
        "--no-autotune",
        help="Disable auto-tuning of timeout, retry, and concurrency (use static defaults).",
        rich_help_panel="Sync",
    ),
]
MinFreeDiskSpaceOpt = Annotated[
    str | None,
    typer.Option(
        "--min-free-disk-space",
        help=(
            "Minimum free disk space before sync, as a size (e.g. '500MB', '2GB'). 0 = disabled."
        ),
        metavar="SIZE",
        rich_help_panel="Sync",
    ),
]
MaxInboundSizeOpt = Annotated[
    int,
    typer.Option(
        "--max-inbound-size",
        help=(
            "Maximum size in MB of a single inbound file (download or "
            "received P2P upload), enforced as an absolute cap on top of "
            "the peer-declared size. 0 = disabled."
        ),
        metavar="MB",
        rich_help_panel="Sync",
    ),
]
IgnorePatternOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--ignore-pattern",
        help=("Extra gitignore-style pattern (layered on .hsyncignore). May be repeated."),
        metavar="PATTERN",
        rich_help_panel="Sync",
    ),
]
MassDeleteGuardOpt = Annotated[
    bool,
    typer.Option(
        "--mass-delete-guard/--no-mass-delete-guard",
        help="Enable/disable the mass-deletion safety guard.",
        rich_help_panel="Sync",
    ),
]
MassDeleteThresholdOpt = Annotated[
    int | None,
    typer.Option(
        "--mass-delete-threshold",
        help="Absolute number of deletions per cycle that trips the guard (0 = disabled).",
        metavar="N",
        rich_help_panel="Sync",
    ),
]
MassDeletePercentOpt = Annotated[
    int | None,
    typer.Option(
        "--mass-delete-percent",
        help="Percentage of local files whose deletion trips the guard (0 = disabled).",
        metavar="PCT",
        rich_help_panel="Sync",
    ),
]
HttpTimeoutOpt = Annotated[
    int | None,
    typer.Option(
        "--http-timeout",
        help="HTTP request timeout in seconds (default: auto-detected).",
        metavar="SEC",
        rich_help_panel="Connection",
    ),
]
VerboseOpt = Annotated[
    bool,
    typer.Option(
        "-v",
        "--verbose",
        help="Enable verbose output.",
        rich_help_panel="Output",
    ),
]
QuietOpt = Annotated[
    bool,
    typer.Option(
        "-q",
        "--quiet",
        help="Suppress all output.",
        rich_help_panel="Output",
    ),
]
LogFileOpt = Annotated[
    str | None,
    typer.Option(
        "--log-file",
        help="Mirror the log to FILE (rotated); stdout keeps receiving it.",
        metavar="FILE",
        rich_help_panel="Output",
    ),
]
ColorOpt = Annotated[
    ColorMode,
    typer.Option(
        "--color",
        help="Colorize log output: auto (only on a terminal), always, never.",
        rich_help_panel="Output",
    ),
]
AllowDeletionsOpt = Annotated[
    bool,
    typer.Option(
        "--allow-deletions",
        help=(
            "Accept the deletion risk: a download-only sync may delete local-only "
            "files to mirror the source."
        ),
        rich_help_panel="Sync",
    ),
]
EnvFileOpt = Annotated[
    str | None,
    typer.Option(
        "--env-file",
        help="Load environment variables from file.",
        metavar="PATH",
    ),
]
EventsJsonFdOpt = Annotated[
    int | None,
    typer.Option(
        "--events-json-fd",
        help="Write structured JSON-Lines status events to the given file descriptor "
        "(for a parent GUI process).",
        metavar="FD",
        hidden=True,
        rich_help_panel="Connection",
    ),
]

# =============================================================================
# server-subcommand options
# =============================================================================

HostArg = Annotated[
    str | None,
    typer.Argument(
        help=(
            "Remote host or IP, optionally with a port (HOST[:PORT]); https:// is "
            "always used. Omit to read the full URL from HSYNC_SERVER_URL."
        ),
        metavar="HOST[:PORT]",
    ),
]
ServerUrlOpt = Annotated[
    str | None,
    typer.Option(
        "--server-url",
        help=(
            f"Sync-server URL: a full https:// URL (optionally with a path) or a "
            f"bare HOST[:PORT] normalized to https on the default port "
            f"({SERVER_DEFAULT_PORT}). Omit to read HSYNC_SERVER_URL."
        ),
        metavar="URL",
        rich_help_panel="Connection",
    ),
]
InsecureOpt = Annotated[
    bool,
    typer.Option(
        "--insecure",
        help="Skip SSL certificate verification.",
        show_default=False,
        rich_help_panel="Connection",
    ),
]
CaCertOpt = Annotated[
    str | None,
    typer.Option(
        "--ca-cert",
        help="Path to CA certificate file for self-signed server certs.",
        metavar="FILE",
        rich_help_panel="Connection",
    ),
]
ServerFingerprintOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--server-fingerprint",
        help=(
            "Pin the sync-server certificate fingerprint ('sha256:<hex>' or bare hex). "
            "May be repeated (up to two) for a rotation window."
        ),
        metavar="FINGERPRINT",
        rich_help_panel="Connection",
    ),
]
ServerTofuOpt = Annotated[
    bool,
    typer.Option(
        "--server-tofu",
        help=(
            "Trust the sync-server certificate on first use (TOFU); a later mismatch "
            "is refused as a possible man-in-the-middle."
        ),
        show_default=False,
        rich_help_panel="Connection",
    ),
]
ServerTrustOpt = Annotated[
    str | None,
    typer.Option(
        "--server-trust",
        help=(
            "Sync-server TLS trust policy: 'chain' (default: standard CA validation), "
            "'ask' (confirm an unknown fingerprint on a terminal, fail closed without "
            "one), 'accept-new' (pin the first sighting silently), 'strict' (require a "
            "pin) or 'none' (no verification; loopback/tests only)."
        ),
        metavar="POLICY",
        show_default=False,
        rich_help_panel="Connection",
    ),
]
ApiKeyFdOpt = Annotated[
    int | None,
    typer.Option(
        "--api-key-fd",
        help="Read API key from the given file descriptor (for secure passing from a parent process).",
        metavar="FD",
        hidden=True,
        rich_help_panel="Connection",
    ),
]

# =============================================================================
# p2p-subcommand options
# =============================================================================

ListenPortOpt = Annotated[
    int,
    typer.Option(
        "--listen-port",
        help="Port to listen on when acting as P2P master (0 = auto).",
        metavar="PORT",
        rich_help_panel="P2P Mode",
    ),
]

RendezvousArg = Annotated[
    list[str] | None,
    typer.Option(
        "--rendezvous-server",
        help=(
            "Discovery servers (the hsync-rendezvous daemon speaking rdv/1) for "
            "cross-network rendezvous: HOST[:PORT], QUIC/UDP, no scheme. Repeatable, "
            "and each value may carry a comma-separated list (the same grammar as "
            "HSYNC_RENDEZVOUS_SERVER_ENDPOINTS); the client leases and queries every "
            "reachable entry. Omit to stay on mDNS only. Extra UDP ports to try on "
            "each endpoint go in HSYNC_RENDEZVOUS_SERVER_EXTRA_PORTS."
        ),
        metavar="ENDPOINT",
        rich_help_panel="P2P Mode",
    ),
]

UpnpOpt = Annotated[
    bool,
    typer.Option(
        "--map-data-port",
        help=(
            "Map the p2p data-plane TCP port with UPnP (opt-in; off by default). "
            "Only meaningful with a discovery server: without one, mDNS already "
            "keeps rendezvous on the local network."
        ),
        rich_help_panel="P2P Mode",
    ),
]
NoMdnsOpt = Annotated[
    bool,
    typer.Option(
        "--no-mdns",
        help=(
            "Exclude the local network: discover peers only through the "
            "configured discovery server(s), not mDNS. Requires at least one "
            "--rendezvous-server endpoint."
        ),
        rich_help_panel="P2P Mode",
    ),
]
RendezvousFingerprintOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--rendezvous-server-fingerprint",
        help=(
            "Pin a discovery server's certificate fingerprint ('sha256:<hex>' or "
            "bare hex). May be repeated (up to two per server) for a rotation window."
        ),
        metavar="FINGERPRINT",
        rich_help_panel="P2P Mode",
    ),
]
RendezvousTofuOpt = Annotated[
    bool,
    typer.Option(
        "--rendezvous-server-tofu",
        help=(
            "Trust a discovery server's certificate on first use (TOFU); a later "
            "mismatch is refused as a possible man-in-the-middle."
        ),
        show_default=False,
        rich_help_panel="P2P Mode",
    ),
]
RendezvousTrustOpt = Annotated[
    str | None,
    typer.Option(
        "--rendezvous-server-trust",
        help=(
            "Rendezvous-server TLS trust policy: 'ask' (default: confirm an unknown "
            "fingerprint on a terminal, fail closed without one), 'accept-new' "
            "(pin the first sighting silently), 'strict' (require a pin) or "
            "'none' (no verification; loopback/tests only)."
        ),
        metavar="POLICY",
        show_default=False,
        rich_help_panel="P2P Mode",
    ),
]
RendezvousTransportOpt = Annotated[
    str | None,
    typer.Option(
        "--rendezvous-transport",
        help=(
            "Transport used to reach the rendezvous server(s): 'auto' (default) "
            "races QUIC/UDP (preferred) and TLS/TCP on each endpoint's port(s), "
            "'quic' and 'tcp' use one transport only. Env: HSYNC_RENDEZVOUS_TRANSPORT."
        ),
        metavar="auto|quic|tcp",
        show_default=False,
        rich_help_panel="P2P Mode",
    ),
]
RendezvousTimeoutOpt = Annotated[
    int | None,
    typer.Option(
        "--peer-rendezvous-timeout",
        help=(
            "Time to wait for initial P2P peer rendezvous in seconds "
            f"(default: {RENDEZVOUS_TIMEOUT_DEFAULT}; 0 = wait indefinitely)."
        ),
        metavar="SEC",
        show_default=False,
        rich_help_panel="P2P Mode",
    ),
]
GroupOpt = Annotated[
    str | None,
    typer.Option(
        "--group",
        help="Group name for P2P peer filtering.",
        metavar="GROUP",
        rich_help_panel="P2P Mode",
    ),
]
PreferMasterOpt = Annotated[
    bool,
    typer.Option(
        "--prefer-master",
        help="Prefer this client as master in P2P mode.",
        rich_help_panel="P2P Mode",
    ),
]
NeverMasterOpt = Annotated[
    bool,
    typer.Option(
        "--never-master",
        help="Never become master in P2P mode (always remain a client/slave).",
        rich_help_panel="P2P Mode",
    ),
]
P2PTrustOpt = Annotated[
    str | None,
    typer.Option(
        "--p2p-trust",
        help=(
            "P2P master trust policy: 'ask' (default: confirm an unknown master "
            "fingerprint on a terminal, fail closed without one), 'accept-new' "
            "(pin the first sighting silently) or 'strict' (require an explicit pin)."
        ),
        metavar="POLICY",
        show_default=False,
        rich_help_panel="P2P Mode",
    ),
]
AllowUnauthenticatedWritesOpt = Annotated[
    bool,
    typer.Option(
        "--allow-unauthenticated-writes",
        help=(
            "Dangerous: allow P2P uploads, deletes, and other mutations without "
            "a shared secret (HSYNC_P2P_PASSPHRASE/HSYNC_P2P_KEY). Reads remain "
            "available without this option."
        ),
        rich_help_panel="P2P Mode",
    ),
]
AllowUnauthenticatedP2POpt = Annotated[
    bool,
    typer.Option(
        "--allow-unauthenticated-p2p",
        help=(
            "Run P2P without a shared key: peers are not authenticated and reads "
            "are open to anyone who can reach this node."
        ),
        rich_help_panel="P2P Mode",
    ),
]
P2PKeyFdOpt = Annotated[
    int | None,
    typer.Option(
        "--p2p-key-fd",
        help="Read P2P key from the given file descriptor (for secure passing from a parent process).",
        metavar="FD",
        hidden=True,
        rich_help_panel="P2P Mode",
    ),
]
P2PPassphraseFdOpt = Annotated[
    int | None,
    typer.Option(
        "--p2p-passphrase-fd",
        help=(
            "Read the P2P shared password (>=12 chars) from the given file descriptor; "
            "it is derived to the group key. For secure passing from a parent process."
        ),
        metavar="FD",
        hidden=True,
        rich_help_panel="P2P Mode",
    ),
]


# =============================================================================
# Parent callback (global options) + subcommands
# =============================================================================
