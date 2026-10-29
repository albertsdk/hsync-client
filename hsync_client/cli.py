# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Command-line interface for HSYNC Client.

This module provides the Typer-based CLI for the HSYNC command-line interface.
The parent callback carries the global options (``--version``,
``--capabilities-json``, ``--help-symbols``); the mutually exclusive
``p2p``/``server`` sync subcommands sit under it, alongside two maintenance
namespaces: ``trust`` (certificate pins) and ``state`` (local sync state).
"""

import json
import re
import signal
import sys
import threading
from collections.abc import Mapping
from os import getcwd
from typing import Annotated, Any, TypedDict, cast

import typer
import typer.main
from hsync_common.endpoint import (
    HostPort,
    build_https_url,
    format_authority,
    parse_endpoint_list,
    parse_host_port,
    parse_https_url,
)
from hsync_common.protocol.pairing import (
    FingerprintPins,
    parse_fingerprint,
    parse_pin_map,
)
from hsync_common.protocol.tls_trust import TrustPolicy, resolve_self_signed_policy
from hsync_common.runtime import ColorMode

from hsync_client.constants import (
    BANDWIDTH_MAX,
    BANDWIDTH_MIN,
    HSYNC_VERSION,
    HTTP_TIMEOUT_MAX,
    HTTP_TIMEOUT_MIN,
    MASS_DELETE_GUARD_DEFAULT,
    MAX_CONCURRENT_TRANSFERS_MAX,
    MAX_CONCURRENT_TRANSFERS_MIN,
    MAX_INBOUND_SIZE_MB_DEFAULT,
    MAX_INBOUND_SIZE_MB_MAX,
    MAX_INBOUND_SIZE_MB_MIN,
    MAX_WAIT_TIMEOUT_DEFAULT,
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
    SYNC_DIRECTION_DEFAULT,
    SYNC_DIRECTIONS,
)
from hsync_client.pinning import PinStore
from hsync_client.rendezvous_trust import RendezvousTrustStore
from hsync_client.server_trust import ServerTrustStore, fetch_server_fingerprint

# =============================================================================
# Typer Application
# =============================================================================

app = typer.Typer(
    name="hsync",
    help="HSYNC - HTTP-based file synchronization client",
    add_completion=False,
    rich_markup_mode="rich",  # Enable rich formatting for grouped options
)

# Default local path is resolved once at import time (B008-safe singleton) so
# every subcommand shares the same default without calling getcwd() per parse.
_DEFAULT_LOCAL_PATH = getcwd()


# =============================================================================
# Eager global callbacks (parent level)
# =============================================================================


def version_callback(value: bool) -> None:
    """Print version and exit."""
    if value:
        typer.echo(f"hsync {HSYNC_VERSION}")
        raise typer.Exit()


def legend_callback(value: bool) -> None:
    """Print the output-symbols legend and exit."""
    if value:
        from hsync_client.log_labels import legend_lines

        for line in legend_lines():
            typer.echo(line)
        raise typer.Exit()


def collect_subcommand_options(typer_app: typer.Typer) -> dict[str, list[str]]:
    """Map each registered subcommand to its supported long option flags.

    Introspects the click group built from ``typer_app`` so the advertised CLI
    surface can never drift from the real interface: every ``--flag`` an option
    declares (primary and secondary) is reported for its own subcommand.
    """
    options: dict[str, list[str]] = {}
    for name, command in typer.main.get_group(typer_app).commands.items():
        flags: set[str] = set()
        for param in command.params:
            if param.param_type_name != "option":
                continue
            for opt in (*param.opts, *param.secondary_opts):
                if opt.startswith("--"):
                    flags.add(opt)
        options[name] = sorted(flags)
    return dict(sorted(options.items()))


def capabilities_json_callback(_ctx: typer.Context, value: bool) -> None:
    """Print the binary's capabilities document as JSON and exit."""
    if not value:
        return
    from hsync_common.protocol.capabilities import build_capabilities

    # The parent context's command carries only the parent-level options: the
    # advertised surface is the per-subcommand one, so introspect each
    # registered subcommand's click command instead of ctx.command.params.
    options_by_subcommand = collect_subcommand_options(app)
    doc = build_capabilities(
        app_version=HSYNC_VERSION,
        subcommands=sorted(options_by_subcommand),
        cli_options_by_subcommand=options_by_subcommand,
    )
    typer.echo(json.dumps(doc, indent=2, sort_keys=True))
    raise typer.Exit()


def _validate_bucket(value: str) -> str:
    """Validate bucket name (alphanumeric, hyphens and underscores allowed)."""
    if not re.match(r"^[a-zA-Z0-9]([a-zA-Z0-9_-]*[a-zA-Z0-9])?$", value):
        raise typer.BadParameter(
            "The bucket name must be alphanumeric (hyphens and underscores allowed, "
            "but not at the beginning or end)."
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


def _common_run_sync_kwargs(values: Mapping[str, Any]) -> dict[str, Any]:
    """Map the shared CLI options to ``run_sync`` keyword arguments.

    The p2p and server commands expose the same sync/transfer options; keeping
    the normalization here prevents the two command bodies from drifting.
    """
    # Imported here: the config module pulls the pydantic models, which the CLI
    # must not load at import time (see tests/test_startup_imports.py).
    from hsync_client.config import DELETION_POLICIES

    policy = values["deletion_policy"]
    if policy is not None and policy not in DELETION_POLICIES:
        allowed = ", ".join(DELETION_POLICIES)
        raise typer.BadParameter(f"invalid --deletion-policy value: {policy!r}; use {allowed}")
    return {
        "bucket": values["bucket"],
        "env_file": values["env_file"],
        "verify_ssl": not values["insecure"],
        "ca_cert": values["ca_cert"],
        "http_timeout": values["http_timeout"],
        "events_json_fd": values["events_json_fd"],
        "local_path": values["local_path"],
        "continuous": not values["once"],
        "resync": values["resync"],
        "reconnect_delay": values["reconnect_delay"],
        "stability_delay": values["stability_delay"],
        "max_wait_timeout": values["max_wait_timeout"],
        "filter_path": values["filter_path"],
        "dryrun": values["dryrun"],
        "deletion_policy": values["deletion_policy"],
        "sync_direction": values["sync_direction"],
        "compression_enabled": not values["no_compression"],
        "delta_enabled": not values["no_delta"],
        "delta_min_file_size": values["delta_min_file_size"],
        "delta_min_file_size_p2p": values["delta_min_file_size_p2p"],
        "max_bandwidth_upload": values["parsed_bw_upload"],
        "max_bandwidth_download": values["parsed_bw_download"],
        "debug": values["verbose"],
        "quiet": values["quiet"],
        "logfile": values["logfile"],
        "color": values["color"],
        "allow_deletions": values["allow_deletions"],
        "min_free_disk_space": values["parsed_min_free_disk_space"],
        "max_inbound_size": values["max_inbound_size"],
        "ignore_patterns": [p.strip() for p in (values["ignore_patterns"] or []) if p.strip()],
        "max_concurrent_transfers": values["max_concurrent_transfers"],
        "no_autotune": values["no_autotune"],
        "mass_delete_guard": values["mass_delete_guard"],
        "mass_delete_threshold": values["mass_delete_threshold"],
        "mass_delete_percent": values["mass_delete_percent"],
    }


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
    ``_common_run_sync_kwargs`` consumes — so validation and forwarding cannot
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
        help="Maximum number of concurrent file transfers (1-20).",
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


@app.callback()
def main(
    version: Annotated[
        bool | None,
        typer.Option(
            "-V",
            "--version",
            help="Show version and exit.",
            is_eager=True,
            callback=version_callback,
        ),
    ] = None,
    capabilities_json: Annotated[
        bool | None,
        typer.Option(
            "--capabilities-json",
            help="Print the binary's capabilities document as JSON and exit.",
            is_eager=True,
            callback=capabilities_json_callback,
        ),
    ] = None,
    help_symbols: Annotated[
        bool,
        typer.Option(
            "--help-symbols",
            help="Show output symbols legend.",
            is_eager=True,
            callback=legend_callback,
        ),
    ] = False,
) -> None:
    """
    HSYNC - HTTP-based file synchronization client.

    Synchronize files between a local directory and a remote bucket. A bucket
    is a shared directory hosted on a central HSYNC server or on another peer
    in P2P Mode. In [bold]Server Mode[/bold], clients connect to a centralized server.
    In [bold]P2P Mode[/bold] (P2P), clients discover each other via mDNS and one
    automatically becomes the master—no central server required.

    [bold]Subcommands:[/bold]

        [cyan]server[/cyan]   Synchronize with a central HSYNC server (REST + WebSocket)
        [cyan]p2p[/cyan]      Synchronize peer-to-peer over the local network

    [bold]Global Options:[/bold]

        [cyan]-V, --version[/cyan]        Show version and exit
        [cyan]--capabilities-json[/cyan] Print the binary's capabilities document and exit
        [cyan]--help-symbols[/cyan]      Show the output-symbols legend

    [bold]Examples:[/bold]

        [dim]# Basic sync with a remote server (https:// and port 9753 are implied)[/dim]
        $ hsync server server.example.com -b photos

        [dim]# Sync a specific local directory[/dim]
        $ hsync server server.example.com -b documents -p ~/Documents

        [dim]# One-shot sync (single cycle, then exit)[/dim]
        $ hsync server server.example.com -b projects --once

        [dim]# Dry run to see what would be synced[/dim]
        $ hsync server server.example.com -b backup -n

        [dim]# P2P mode (no central server, peer-to-peer)[/dim]
        $ hsync p2p -b shared

        [dim]# P2P mode with authentication (user password)[/dim]
        $ HSYNC_P2P_PASSPHRASE=YOUR_SHARED_PASSWORD_AT_LEAST_12_CHARACTERS hsync p2p -b shared

    [bold]Environment Variables:[/bold]

        HSYNC_SERVER_URL          Full server URL (alternative to the HOSTNAME argument)
        HSYNC_API_KEY         API key for authentication
        HSYNC_P2P_PASSPHRASE  Shared password (>=12 chars, derived to the group key)
        HSYNC_P2P_KEY         Raw shared key (>=32 chars), used as-is

    [bold]Project:[/bold]

        https://github.com/albertsdk/hsync-client

    [bold]Authors:[/bold]

        Alberto Basso
    """


@app.command()
def p2p(
    bucket: BucketOpt,
    rendezvous_server: RendezvousArg = None,
    http_timeout: HttpTimeoutOpt = None,
    p2p_listen_port: ListenPortOpt = 0,
    peer_rendezvous_timeout: RendezvousTimeoutOpt = None,
    p2p_group: GroupOpt = None,
    prefer_master: PreferMasterOpt = False,
    never_master: NeverMasterOpt = False,
    p2p_trust: P2PTrustOpt = None,
    allow_unauthenticated_writes: AllowUnauthenticatedWritesOpt = False,
    allow_unauthenticated_p2p: AllowUnauthenticatedP2POpt = False,
    p2p_key_fd: P2PKeyFdOpt = None,
    p2p_passphrase_fd: P2PPassphraseFdOpt = None,
    map_data_port: UpnpOpt = False,
    insecure: InsecureOpt = False,
    ca_cert: CaCertOpt = None,
    rendezvous_server_fingerprint: RendezvousFingerprintOpt = None,
    rendezvous_server_tofu: RendezvousTofuOpt = False,
    rendezvous_server_trust: RendezvousTrustOpt = None,
    events_json_fd: EventsJsonFdOpt = None,
    local_path: PathOpt = _DEFAULT_LOCAL_PATH,
    once: OnceOpt = False,
    resync: IntervalOpt = None,
    reconnect_delay: RetryDelayOpt = None,
    stability_delay: StabilityDelayOpt = None,
    max_wait_timeout: MaxWaitTimeoutOpt = MAX_WAIT_TIMEOUT_DEFAULT,
    filter_path: SubdirOpt = None,
    dryrun: DryRunOpt = False,
    deletion_policy: DeletionPolicyOpt = None,
    sync_direction: SyncDirectionOpt = SYNC_DIRECTION_DEFAULT,
    max_bandwidth_upload: LimitUploadOpt = None,
    max_bandwidth_download: LimitDownloadOpt = None,
    no_compression: NoCompressionOpt = False,
    no_delta: NoDeltaOpt = False,
    no_peer_download: NoPeerDownloadOpt = False,
    delta_min_file_size: DeltaMinFileSizeOpt = None,
    delta_min_file_size_p2p: DeltaMinFileSizeP2POpt = None,
    max_concurrent_transfers: MaxConcurrentTransfersOpt = None,
    no_autotune: NoAutotuneOpt = False,
    mass_delete_guard: MassDeleteGuardOpt = MASS_DELETE_GUARD_DEFAULT,
    mass_delete_threshold: MassDeleteThresholdOpt = None,
    mass_delete_percent: MassDeletePercentOpt = None,
    min_free_disk_space: MinFreeDiskSpaceOpt = None,
    max_inbound_size: MaxInboundSizeOpt = MAX_INBOUND_SIZE_MB_DEFAULT,
    ignore_patterns: IgnorePatternOpt = None,
    verbose: VerboseOpt = False,
    quiet: QuietOpt = False,
    logfile: LogFileOpt = None,
    color: ColorOpt = ColorMode.AUTO,
    allow_deletions: AllowDeletionsOpt = False,
    env_file: EnvFileOpt = None,
) -> None:
    """
    Synchronize a bucket over P2P peer-to-peer (P2P).

    Peers on the same local network discover each other via mDNS and one is
    elected master (an HTTPS/WSS transport hub). No central server is required.

    [bold]Examples:[/bold]

        [dim]# Basic P2P sync[/dim]
        $ hsync p2p -b shared

        [dim]# Prefer this peer as the master[/dim]
        $ hsync p2p -b shared --prefer-master

        [dim]# Isolate a sync group and authenticate it (shared password, 12+ chars)[/dim]
        $ HSYNC_P2P_PASSPHRASE=a-long-shared-password hsync p2p -b shared --group home

        [dim]# Advanced: a full raw group key instead of a password (32+ chars)[/dim]
        $ HSYNC_P2P_KEY=YOUR_SHARED_KEY_AT_LEAST_32_CHARACTERS hsync p2p -b shared --group home

        [dim]# Discover peers across networks through a discovery server[/dim]
        $ hsync p2p rdv.example.net -b shared

        [dim]# Rendezvous extra ports when your egress firewall blocks UDP 443[/dim]
        $ HSYNC_RENDEZVOUS_SERVER_EXTRA_PORTS=53 hsync p2p rdv.example.net -b shared

    [bold]Environment Variables:[/bold]

        HSYNC_P2P_PASSPHRASE  Shared password (>=12 chars, derived to the group key)
        HSYNC_P2P_KEY         Raw shared key (>=32 chars), used as-is
        HSYNC_P2P_TRUST       P2P master trust policy: ask | accept-new | strict
    """
    (
        parsed_bw_upload,
        parsed_bw_download,
        parsed_min_free_disk_space,
    ) = _validate_shared_options(locals())

    # P2P-only validation
    if peer_rendezvous_timeout is not None and peer_rendezvous_timeout < 0:
        raise typer.BadParameter(
            "Peer rendezvous timeout must be 0 or greater (0 = wait indefinitely)."
        )

    # --prefer-master is incompatible with directional sync (upload/download-only)
    if prefer_master and sync_direction != "bidirectional":
        raise typer.BadParameter(
            f"--prefer-master is incompatible with --sync-direction={sync_direction}. "
            f"The P2P master always operates in bidirectional mode."
        )

    # --prefer-master and --never-master are mutually exclusive (contradictory)
    if prefer_master and never_master:
        raise typer.BadParameter(
            "--prefer-master and --never-master are mutually exclusive: a client "
            "cannot both prefer the master role and be forbidden from it."
        )

    # Resolve the rendezvous-server endpoints from the CLI option, then the env
    # (loaded here so an --env-file value is visible before run_sync loads it
    # again later). One grammar on both surfaces.
    _load_env_file_or_exit(env_file)

    rendezvous_endpoints = _build_rendezvous_servers(rendezvous_server, use_env=True)
    if map_data_port:
        effective_map_data_port = True
    else:
        from hsync_client.config import get_map_data_port

        effective_map_data_port = get_map_data_port()

    # Rendezvous-server TLS trust (ADR-0034): --ca-cert is a server-mode knob, not
    # a rendezvous-channel knob, so refuse it rather than silently ignore it.
    if ca_cert:
        raise typer.BadParameter(
            "--ca-cert is not used for the rendezvous-server channel; pin the server "
            "with --rendezvous-server-fingerprint (or use --rendezvous-server-tofu) instead."
        )

    # Pins are per endpoint (``ENDPOINT=FINGERPRINT[|FINGERPRINT]``, repeatable;
    # a bare fingerprint is the unambiguous single-endpoint shorthand). The map
    # is bound to the configured endpoints here, so a pin for a server the client
    # never dials is a usage error rather than a silently ignored value.
    pins = _build_rendezvous_server_pins(rendezvous_server_fingerprint, rendezvous_endpoints)

    if insecure and (pins or rendezvous_server_tofu):
        raise typer.BadParameter(
            "--insecure is mutually exclusive with "
            "--rendezvous-server-fingerprint/--rendezvous-server-tofu."
        )

    if (pins or rendezvous_server_tofu) and not rendezvous_endpoints:
        raise typer.BadParameter(
            "--rendezvous-server-fingerprint/--rendezvous-server-tofu require at least "
            "one --rendezvous-server endpoint."
        )

    if p2p_trust is not None:
        try:
            resolve_self_signed_policy(p2p_trust)
        except ValueError as exc:
            raise typer.BadParameter(f"invalid --p2p-trust value: {exc}") from exc

    from hsync_client.main_logic import run_sync

    run_sync(
        **_common_run_sync_kwargs(locals()),
        server_url=None,
        api_key_fd=None,
        p2p_key_fd=p2p_key_fd,
        p2p_passphrase_fd=p2p_passphrase_fd,
        p2p_mode=True,
        p2p_listen_port=p2p_listen_port,
        rendezvous_server_endpoints=rendezvous_endpoints,
        map_data_port=effective_map_data_port,
        rendezvous_server_pins=pins,
        rendezvous_server_tofu=bool(rendezvous_server_tofu),
        rendezvous_server_trust=rendezvous_server_trust,
        peer_rendezvous_timeout=peer_rendezvous_timeout,
        p2p_group=p2p_group,
        prefer_master=prefer_master,
        never_master=never_master,
        p2p_trust=p2p_trust,
        p2p_peer_download_enabled=not no_peer_download,
        p2p_allow_unauthenticated_writes=allow_unauthenticated_writes,
        p2p_allow_unauthenticated=allow_unauthenticated_p2p,
    )


@app.command("derive-group-key")
def derive_group_key_command(
    passphrase_fd: Annotated[
        int | None,
        typer.Option(
            "--passphrase-fd",
            help="Read the shared password from the given file descriptor.",
            metavar="FD",
            hidden=True,
        ),
    ] = None,
) -> None:
    """Print the P2P group key (64 hex) derived from a shared password.

    Use the output to populate a rendezvous-server key file. Treat it as a secret: it is
    the raw group key the discovery server derives bucket tokens from.
    """
    import os

    from hsync_common.protocol.rdv_tokens import (
        WeakGroupPassphrase,
        derive_group_key,
        validate_group_passphrase,
    )

    if passphrase_fd is not None:
        with os.fdopen(passphrase_fd, "r") as handle:
            passphrase = handle.read()
    else:
        passphrase = typer.prompt("Shared password", hide_input=True)

    try:
        validate_group_passphrase(passphrase)
    except WeakGroupPassphrase as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None

    typer.echo(derive_group_key(passphrase))
    typer.echo(
        "The value above is the group key secret: store it where only the service can read it.",
        err=True,
    )


# =============================================================================
# trust management (P2P peers, discovery servers, sync server) — one ``hsync trust``
# namespace
# =============================================================================
# ``hsync trust …`` mirrors OpenSSH's known-hosts workflow so an operator can
# inspect, pre-pin and forget fingerprints without editing the JSON stores by
# hand. One namespace covers the three certificate-pinning transports: the P2P
# master/seeders (``known_peers``), the discovery servers and the sync server
# (``known_hosts``). ``hsync server``/``hsync p2p`` stay actions, so the trust
# surface lives here rather than under them (ADR-0036).
_trust_root = typer.Typer(
    help="Inspect and manage certificate trust for the P2P peers, discovery servers and server.",
    no_args_is_help=True,
)
_trust_rendezvous_app = typer.Typer(
    help="Manage the rendezvous-server known-hosts store.",
    no_args_is_help=True,
)
_trust_peer_app = typer.Typer(
    help="Manage the P2P known-peers store.",
    no_args_is_help=True,
)
_trust_server_app = typer.Typer(
    help="Manage the sync-server known-hosts store.",
    no_args_is_help=True,
)
app.add_typer(_trust_root, name="trust")
_trust_root.add_typer(_trust_rendezvous_app, name="rendezvous-server")
_trust_root.add_typer(_trust_peer_app, name="peer")
_trust_root.add_typer(_trust_server_app, name="server")


_TrustStore = RendezvousTrustStore | ServerTrustStore


def _trust_list(store: _TrustStore, none_message: str, as_json: bool) -> None:
    """Print a known-hosts store, as text or JSON."""
    entries = store.entries()
    if as_json:
        typer.echo(json.dumps(entries, indent=2, sort_keys=True))
        return
    if not entries:
        typer.echo(none_message)
        return
    for entry in entries:
        typer.echo(
            f"{format_authority(entry['host'], entry['port'])} {entry['fingerprint']} "
            f"(source={entry['source']}, first_seen={entry['first_seen']})"
        )


def _trust_show(store: _TrustStore, host: str, port: int) -> None:
    """Print one pinned fingerprint or exit non-zero when absent."""
    fingerprint = store.get(host, port)
    if fingerprint is None:
        typer.echo(f"{format_authority(host, port)} is not in the known-hosts store.", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"{format_authority(host, port)} {fingerprint}")


def _trust_add(store: _TrustStore, host: str, port: int, fingerprint: str) -> None:
    """Validate and pin a fingerprint."""
    normalized = parse_fingerprint(fingerprint)
    if normalized is None:
        typer.echo("Not a valid sha256 fingerprint (expected 64 hex digits).", err=True)
        raise typer.Exit(code=1)
    store.set(host, port, normalized, source="cli")
    typer.echo(f"Pinned {format_authority(host, port)} {normalized} in {store.path}.")


def _trust_remove(store: _TrustStore, host: str, port: int) -> None:
    """Forget a pinned authority, reporting whether it was present."""
    if store.remove(host, port):
        typer.echo(f"Removed {format_authority(host, port)} from the known-hosts store.")
    else:
        typer.echo(f"{format_authority(host, port)} was not in the known-hosts store.")


def _trust_host_port(hostname: str | None) -> tuple[str, int]:
    """Resolve the ``HOST[:PORT]`` argument of a trust subcommand.

    The port is part of the endpoint (no ``--port`` flag): an entry without one
    uses the channel default so a pin keyed ``host`` and one keyed ``host:443``
    name the same node.
    """
    if hostname is None:
        raise typer.BadParameter("a rendezvous-server host[:port] is required")
    endpoint = _parse_host_port(hostname)
    return endpoint.host, endpoint.port if endpoint.port is not None else RENDEZVOUS_DEFAULT_PORT


@_trust_rendezvous_app.command("list")
def rendezvous_trust_list(
    as_json: bool = typer.Option(False, "--json", help="Emit entries as JSON."),
) -> None:
    """List the pinned rendezvous-server fingerprints (like ``known_hosts``)."""
    _trust_list(RendezvousTrustStore(), "No discovery servers are trusted yet.", as_json)


@_trust_rendezvous_app.command("show")
def rendezvous_trust_show(hostname: HostArg) -> None:
    """Show the pinned fingerprint for one discovery server."""
    host, resolved_port = _trust_host_port(hostname)
    _trust_show(RendezvousTrustStore(), host, resolved_port)


@_trust_rendezvous_app.command("add")
def rendezvous_trust_add(
    hostname: HostArg,
    fingerprint: str = typer.Argument(..., metavar="FINGERPRINT"),
) -> None:
    """Pin a rendezvous-server fingerprint (verify it out of band first)."""
    host, resolved_port = _trust_host_port(hostname)
    _trust_add(RendezvousTrustStore(), host, resolved_port, fingerprint)


@_trust_rendezvous_app.command("remove")
def rendezvous_trust_remove(hostname: HostArg) -> None:
    """Forget a pinned discovery server (like ``ssh-keygen -R``)."""
    host, resolved_port = _trust_host_port(hostname)
    _trust_remove(RendezvousTrustStore(), host, resolved_port)


@_trust_rendezvous_app.command("fingerprint")
def rendezvous_trust_fingerprint(hostname: HostArg) -> None:
    """Print a discovery server's certificate fingerprint without trusting it."""
    from hsync_client.rdv_client import RendezvousClient, RendezvousError

    host, resolved_port = _trust_host_port(hostname)
    client = RendezvousClient(host, resolved_port, policy=TrustPolicy.none)
    try:
        observed = client.fetch_fingerprint()
    except RendezvousError as exc:
        typer.echo(f"Could not read the fingerprint: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(observed)


# =============================================================================
# peer trust (known_peers) management
# =============================================================================

# ``hsync trust peer …`` manages the P2P known-peers store
# (``p2p_known_peers.json``), keyed ``cid|bucket``.


@_trust_peer_app.command("list")
def peer_trust_list(
    as_json: bool = typer.Option(False, "--json", help="Emit entries as JSON."),
) -> None:
    """List the pinned P2P peer fingerprints (like ``known_hosts``)."""
    entries = PinStore().entries()
    if as_json:
        typer.echo(json.dumps(entries, indent=2, sort_keys=True))
        return
    if not entries:
        typer.echo("No peers are trusted yet.")
        return
    for entry in entries:
        typer.echo(f"{entry['cid']}|{entry['bucket']} {entry['fingerprint']}")


@_trust_peer_app.command("add")
def peer_trust_add(
    bucket: BucketOpt,
    cid: str = typer.Argument(..., metavar="CID"),
    fingerprint: str = typer.Argument(..., metavar="FINGERPRINT"),
) -> None:
    """Pin a P2P peer fingerprint (verify it out of band first)."""
    normalized = parse_fingerprint(fingerprint)
    if normalized is None:
        typer.echo("Not a valid sha256 fingerprint (expected 64 hex digits).", err=True)
        raise typer.Exit(code=1)
    store = PinStore()
    store.set(cid, bucket, normalized)
    typer.echo(f"Pinned {cid}|{bucket} {normalized} in {store.path}.")


@_trust_peer_app.command("remove")
def peer_trust_remove(
    bucket: BucketOpt,
    cid: str = typer.Argument(..., metavar="CID"),
) -> None:
    """Forget a pinned P2P peer (like ``ssh-keygen -R``)."""
    if PinStore().remove(cid, bucket):
        typer.echo(f"Removed {cid}|{bucket} from the known-peers store.")
    else:
        typer.echo(f"{cid}|{bucket} was not in the known-peers store.")


# =============================================================================
# server trust (known_hosts) management
# =============================================================================


def _server_trust_host_port(hostname: str | None) -> tuple[str, int]:
    """Resolve the ``HOST[:PORT]`` argument of a server trust subcommand."""
    if hostname is None:
        raise typer.BadParameter("a server host[:port] is required")
    hp = _parse_host_port(hostname)
    return hp.host, SERVER_DEFAULT_PORT if hp.port is None else hp.port


@_trust_server_app.command("list")
def server_trust_list(
    as_json: bool = typer.Option(False, "--json", help="Emit entries as JSON."),
) -> None:
    """List the pinned sync-server fingerprints (like ``known_hosts``)."""
    _trust_list(ServerTrustStore(), "No servers are trusted yet.", as_json)


@_trust_server_app.command("show")
def server_trust_show(hostname: HostArg) -> None:
    """Show the pinned fingerprint for one server."""
    host, resolved_port = _server_trust_host_port(hostname)
    _trust_show(ServerTrustStore(), host, resolved_port)


@_trust_server_app.command("add")
def server_trust_add(
    hostname: HostArg,
    fingerprint: str = typer.Argument(..., metavar="FINGERPRINT"),
) -> None:
    """Pin a sync-server fingerprint (verify it out of band first)."""
    host, resolved_port = _server_trust_host_port(hostname)
    _trust_add(ServerTrustStore(), host, resolved_port, fingerprint)


@_trust_server_app.command("remove")
def server_trust_remove(hostname: HostArg) -> None:
    """Forget a pinned sync server (like ``ssh-keygen -R``)."""
    host, resolved_port = _server_trust_host_port(hostname)
    _trust_remove(ServerTrustStore(), host, resolved_port)


@_trust_server_app.command("fingerprint")
def server_trust_fingerprint(hostname: HostArg) -> None:
    """Print a server's certificate fingerprint without trusting it."""
    host, resolved_port = _server_trust_host_port(hostname)
    observed = fetch_server_fingerprint(host, resolved_port)
    if observed is None:
        typer.echo(
            f"Could not read a certificate fingerprint from {format_authority(host, resolved_port)}.",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(observed)


@app.command()
def server(
    bucket: BucketOpt,
    server_url: ServerUrlOpt = None,
    insecure: InsecureOpt = False,
    ca_cert: CaCertOpt = None,
    server_fingerprint: ServerFingerprintOpt = None,
    server_tofu: ServerTofuOpt = False,
    server_trust: ServerTrustOpt = None,
    http_timeout: HttpTimeoutOpt = None,
    api_key_fd: ApiKeyFdOpt = None,
    events_json_fd: EventsJsonFdOpt = None,
    local_path: PathOpt = _DEFAULT_LOCAL_PATH,
    once: OnceOpt = False,
    resync: IntervalOpt = None,
    reconnect_delay: RetryDelayOpt = None,
    stability_delay: StabilityDelayOpt = None,
    max_wait_timeout: MaxWaitTimeoutOpt = MAX_WAIT_TIMEOUT_DEFAULT,
    filter_path: SubdirOpt = None,
    dryrun: DryRunOpt = False,
    deletion_policy: DeletionPolicyOpt = None,
    sync_direction: SyncDirectionOpt = SYNC_DIRECTION_DEFAULT,
    max_bandwidth_upload: LimitUploadOpt = None,
    max_bandwidth_download: LimitDownloadOpt = None,
    no_compression: NoCompressionOpt = False,
    no_delta: NoDeltaOpt = False,
    delta_min_file_size: DeltaMinFileSizeOpt = None,
    delta_min_file_size_p2p: DeltaMinFileSizeP2POpt = None,
    max_concurrent_transfers: MaxConcurrentTransfersOpt = None,
    no_autotune: NoAutotuneOpt = False,
    mass_delete_guard: MassDeleteGuardOpt = MASS_DELETE_GUARD_DEFAULT,
    mass_delete_threshold: MassDeleteThresholdOpt = None,
    mass_delete_percent: MassDeletePercentOpt = None,
    min_free_disk_space: MinFreeDiskSpaceOpt = None,
    max_inbound_size: MaxInboundSizeOpt = MAX_INBOUND_SIZE_MB_DEFAULT,
    ignore_patterns: IgnorePatternOpt = None,
    verbose: VerboseOpt = False,
    quiet: QuietOpt = False,
    logfile: LogFileOpt = None,
    color: ColorOpt = ColorMode.AUTO,
    allow_deletions: AllowDeletionsOpt = False,
    env_file: EnvFileOpt = None,
) -> None:
    """
    Synchronize a bucket with a central HSYNC server (REST + WebSocket).

    The server hosts the shared bucket and coordinates synchronization between
    all connected clients. ``--server-url`` accepts a full ``https://`` URL
    (optionally with a reverse-proxy path) or a bare ``HOST[:PORT]``, which
    implies ``https://`` on the default port (9753, the hsync-server default).
    The port is always carried by the endpoint: there is no separate ``--port``.

    [bold]Examples:[/bold]

        [dim]# Basic sync[/dim]
        $ hsync server --server-url server.example.com -b photos

        [dim]# Continuous sync (the default: keeps retrying if the server is unreachable)[/dim]
        $ hsync server --server-url server.example.com -b projects

        [dim]# One-shot sync[/dim]
        $ hsync server --server-url server.example.com -b projects --once

        [dim]# Reverse proxy on the standard HTTPS port, behind a path[/dim]
        $ hsync server --server-url https://server.example.com/hsync -b photos
    """
    # Bare-endpoint or full URL: normalize to the https:// URL the sync core
    # consumes. None → the documented HSYNC_SERVER_URL env fallback.
    resolved_url = _build_server_url(server_url)

    # The server URL is mandatory in server mode: when it is omitted, the full
    # URL must come from HSYNC_SERVER_URL (shell env or --env-file). Resolve it
    # here so a missing server fails fast with a clean usage error instead of
    # deep inside run_sync, after the banner and logging are already up.
    if resolved_url is None:
        _load_env_file_or_exit(env_file)
        from hsync_client.config import get_server_url

        resolved_url = get_server_url()
        if not resolved_url:
            raise typer.BadParameter(
                "Missing sync-server URL: pass --server-url (e.g. "
                "'hsync server --server-url server.example.com') or set the "
                "HSYNC_SERVER_URL environment variable."
            )

    (
        parsed_bw_upload,
        parsed_bw_download,
        parsed_min_free_disk_space,
    ) = _validate_shared_options(locals())

    # Server TLS trust (ADR-0036): validate the pin flags before running.
    server_fingerprints = tuple(server_fingerprint or ())
    if len(server_fingerprints) > 2:
        raise typer.BadParameter(
            "at most two --server-fingerprint values are allowed (rotation window)."
        )
    for fp in server_fingerprints:
        if parse_fingerprint(fp) is None:
            raise typer.BadParameter(f"invalid --server-fingerprint value: {fp!r}")
    if insecure and (server_fingerprints or server_tofu):
        raise typer.BadParameter(
            "--insecure is mutually exclusive with --server-fingerprint/--server-tofu."
        )

    from hsync_client.main_logic import run_sync

    run_sync(
        **_common_run_sync_kwargs(locals()),
        server_url=resolved_url,
        api_key_fd=api_key_fd,
        p2p_key_fd=None,
        server_fingerprints=server_fingerprints,
        server_tofu=bool(server_tofu),
        server_trust=server_trust,
        p2p_mode=False,
        p2p_listen_port=0,
        peer_rendezvous_timeout=None,
        p2p_group=None,
        prefer_master=False,
        never_master=False,
        p2p_trust=None,
        p2p_peer_download_enabled=False,
        p2p_allow_unauthenticated_writes=False,
    )


# =============================================================================
# state management — ``hsync state reset``
# =============================================================================

# The per-folder state DB (merge bases + deletion history) is client-owned and
# guarded by the per-directory ``InstanceLock``, so forgetting it belongs here
# rather than in a front-end that cannot see a concurrent sync.
_state_root = typer.Typer(
    help="Inspect and reset the local sync-state database.",
    no_args_is_help=True,
)
app.add_typer(_state_root, name="state")


@_state_root.command("reset")
def state_reset(
    folder: Annotated[
        str,
        typer.Option(
            "--folder",
            help="Synced folder whose local state (merge bases and deletion history) is forgotten.",
            metavar="DIR",
        ),
    ],
    as_json: bool = typer.Option(False, "--json", help="Emit the summary as JSON."),
    quarantine: bool = typer.Option(
        False,
        "--quarantine",
        help="Move an unreadable state DB aside instead of failing.",
    ),
) -> None:
    """Forget one folder's sync state so the next cycle reconciles from scratch.

    Clears the merge bases (version store) and the deletion markers
    (tombstones) of the folder. No synced file is touched and nothing is
    transferred: the next sync re-scans every file and resolves the surviving
    differences with the causal rules. Refuses while any hsync process is
    syncing the same folder.
    """
    # Lazy imports keep the CLI fast paths free of the state/engine modules.
    import sqlite3
    from dataclasses import asdict
    from pathlib import Path

    from hsync_client.exitcodes import ExitCode
    from hsync_client.identity import InstanceLockError
    from hsync_client.logger import setup_logging
    from hsync_client.state_reset import reset_folder_state

    # A one-shot maintenance command reports on stdout only: the default loguru
    # sink would echo the lock's DEBUG lines on stderr.
    setup_logging(quiet=True)

    if not folder.strip():
        typer.echo("--folder requires a directory path.", err=True)
        raise typer.Exit(code=int(ExitCode.FATAL))
    target = Path(folder).expanduser()
    if not target.is_dir():
        typer.echo(f"Not a directory: {target}", err=True)
        raise typer.Exit(code=int(ExitCode.FATAL))

    try:
        report = reset_folder_state(target, quarantine=quarantine)
    except InstanceLockError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=int(ExitCode.FATAL)) from None
    except sqlite3.DatabaseError as exc:
        typer.echo(
            f"Could not reset the state DB: {exc}. Re-run with --quarantine to move it aside.",
            err=True,
        )
        raise typer.Exit(code=int(ExitCode.FATAL)) from None
    except OSError as exc:
        typer.echo(f"Could not reset the state DB: {exc}", err=True)
        raise typer.Exit(code=int(ExitCode.FATAL)) from None

    if as_json:
        typer.echo(json.dumps(asdict(report), indent=2, sort_keys=True))
        return
    if not report.found:
        typer.echo(f"No sync state found for {report.folder}; nothing to reset.")
        return
    if report.quarantined:
        typer.echo(
            f"The state DB for {report.folder} was unreadable; moved to "
            f"{report.quarantined}. The next sync starts from scratch."
        )
        return
    typer.echo(
        f"Reset the sync state of {report.folder}: {report.versions_deleted} version entries "
        f"and {report.tombstones_deleted} deletion markers removed. "
        "The next sync will scan every file."
    )


# What Typer's runner exits with after an interruption (``typer/core.py::main``
# converts ``KeyboardInterrupt``/``EOFError`` into ``Exit(130)``).
_INTERRUPTED_EXIT_CODE = 130


def _raise_keyboard_interrupt(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def _install_sigterm_handler() -> None:
    """Turn ``SIGTERM`` into ``KeyboardInterrupt`` so a stop shuts down cleanly.

    The default action kills the process on the spot and skips every ``finally``:
    the P2P/server tasks are never stopped, the last records stay in the
    asynchronous log queue, and the sinks keep the semaphores the resource
    tracker then reports as leaked. Raising ``KeyboardInterrupt`` in the main
    thread reuses the ``SIGINT`` path (``run_p2p_mode`` catches it, stops the
    servers, flushes), which is what ``systemctl stop``, ``docker stop`` and the
    desktop's Stop button (``SIGTERM``, then ``SIGKILL`` after 5 s) expect.
    """
    # ``signal.signal`` is main-thread only; embedded callers (tests, the
    # desktop's own process) simply keep the default action.
    if threading.current_thread() is not threading.main_thread():
        return
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)


def run() -> None:
    """Entry point for the CLI application."""
    _install_sigterm_handler()
    try:
        app()
    except KeyboardInterrupt:
        # An operator stop is not a failure: every mode logs its own shutdown and
        # the process leaves with status 0, without a traceback.
        pass
    except SystemExit as exc:
        # Typer answers an interruption with ``Exit(_INTERRUPTED_EXIT_CODE)``
        # before the exception reaches a handler (``typer/core.py::main``), so a
        # SIGTERM in a mode that does not catch ``KeyboardInterrupt`` itself
        # surfaces here: a stop must be a clean exit, not a failed one.
        if exc.code != _INTERRUPTED_EXIT_CODE:
            raise
    finally:
        # Process-level safety net for the asynchronous log sinks: whatever
        # exit path was taken (normal, SystemExit, unhandled error, SIGTERM),
        # the queued records reach the terminal and the sinks are closed before
        # the interpreter tears the stream down — closing them is also what
        # releases the semaphores ``resource_tracker`` would otherwise report.
        #
        # Drained only when the logging stack was actually initialised: the same
        # entry point serves the fast paths (``--version`` / ``--help``), whose
        # contract is that importing ``hsync_client.cli`` pulls none of the heavy
        # dependencies (pinned by ``tests/test_startup_imports.py``), so the
        # import stays lazy and conditional.
        if "hsync_client.logger" in sys.modules:
            from hsync_client.logger import shutdown_logging

            shutdown_logging()
