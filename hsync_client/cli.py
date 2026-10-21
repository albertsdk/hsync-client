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
import sys
from os import getcwd
from typing import Annotated

import typer
import typer.main
from hsync_common.endpoint import HostPort, build_https_url, format_authority, parse_host_port
from hsync_common.protocol.pairing import parse_fingerprint
from hsync_common.protocol.tls_trust import TrustPolicy
from hsync_common.runtime import ColorMode

from hsync_client.bootnode_trust import BootnodeTrustStore
from hsync_client.constants import (
    BANDWIDTH_MAX,
    BANDWIDTH_MIN,
    BOOTNODE_DEFAULT_PORT,
    DISCOVERY_TIMEOUT_DEFAULT,
    HSYNC_VERSION,
    HTTP_TIMEOUT_MAX,
    HTTP_TIMEOUT_MIN,
    MAX_CONCURRENT_TRANSFERS_MAX,
    MAX_CONCURRENT_TRANSFERS_MIN,
    MAX_INBOUND_SIZE_MB_DEFAULT,
    MAX_INBOUND_SIZE_MB_MAX,
    MAX_INBOUND_SIZE_MB_MIN,
    MAX_WAIT_TIMEOUT_MAX,
    MAX_WAIT_TIMEOUT_MIN,
    MIN_FREE_DISK_SPACE_MB_DEFAULT,
    MIN_FREE_DISK_SPACE_MB_MAX,
    MIN_FREE_DISK_SPACE_MB_MIN,
    RECONNECT_DELAY_MAX,
    RECONNECT_DELAY_MIN,
    RESYNC_MAX,
    RESYNC_MIN,
    SERVER_DEFAULT_PORT,
    STABILITY_DELAY_MAX,
    STABILITY_DELAY_MIN,
    SYNC_DIRECTIONS,
)
from hsync_client.pinning import PinStore
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


def capabilities_json_callback(ctx: typer.Context, value: bool) -> None:
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


def _merge_port(raw_host: str, parsed_port: int | None, flag_port: int | None) -> int | None:
    """Reject a port supplied twice; return the single effective port or ``None``.

    ``parsed_port`` comes from the ``HOST[:PORT]`` form, ``flag_port`` from
    ``--port``. Supplying both is a usage error (no silent precedence).
    """
    if parsed_port is not None and flag_port is not None:
        raise typer.BadParameter(
            f"specify the port either in {raw_host!r} (HOST:PORT) or with --port, not both"
        )
    if parsed_port is not None:
        return parsed_port
    return flag_port


def _build_server_url(hostname: str | None, port: int | None) -> str | None:
    """Assemble the ``https://`` server URL from the bare host form.

    Server mode always uses HTTPS (the client refuses to send the API key over
    an unencrypted channel), so the scheme is implied; the port defaults to
    ``SERVER_DEFAULT_PORT``, the hsync-server default. A ``None`` hostname
    (env-only usage, ``HSYNC_SERVER``) returns ``None`` so ``run_sync`` keeps
    its documented env fallback. The host may be a bare hostname/IPv4, a
    ``HOST[:PORT]`` pair or a (bracketed) IPv6 literal.

    Raises:
        typer.BadParameter: on URL-like input, an unparseable host, a port
            outside 1-65535, ``--port`` without a hostname, or a port supplied
            both in the host and via ``--port``.
    """
    if port is not None and not 1 <= port <= 65535:
        raise typer.BadParameter("--port must be between 1 and 65535.")
    if hostname is None:
        if port is not None:
            raise typer.BadParameter(
                "--port requires a hostname (or set the full URL via HSYNC_SERVER)."
            )
        return None
    hp = _parse_host_port(hostname)
    effective_port = _merge_port(hostname.strip(), hp.port, port)
    return build_https_url(HostPort(host=hp.host, port=effective_port), SERVER_DEFAULT_PORT)


def _build_bootnode(
    hostname: str | None,
    port: int | None,
    *,
    upnp: bool,
    use_env: bool = False,
) -> tuple[str | None, int, bool]:
    """Resolve the bootnode plan inputs; return ``(host, port, upnp_enabled)``.

    A ``None`` host selects the mDNS plan (``port``/``upnp`` are then
    irrelevant). When ``use_env`` is set (the ``p2p`` command) a missing CLI
    hostname falls back to ``HSYNC_BOOTNODE``; an explicit ``--port`` overrides
    a port embedded in the environment value (CLI > env), while a port supplied
    both in the CLI ``HOST[:PORT]`` and via ``--port`` is a usage error. The
    bootnode host may be a bare hostname/IPv4, a ``HOST[:PORT]`` pair or a
    (bracketed) IPv6 literal; the returned host is always unbracketed.

    UPnP is opt-in: it is enabled only by ``--upnp`` or ``HSYNC_UPNP_ENABLED``.
    """
    if port is not None and not 1 <= port <= 65535:
        raise typer.BadParameter("--port must be between 1 and 65535.")
    if hostname is None and use_env:
        from hsync_client.config import get_bootnode_endpoint

        endpoint = get_bootnode_endpoint()
        if endpoint is not None:
            hostname = endpoint.host
            if endpoint.port is not None and port is None:
                port = endpoint.port
    if hostname is None:
        if port is not None:
            raise typer.BadParameter(
                "--port requires a bootnode hostname (or set the full host via HSYNC_BOOTNODE)."
            )
        return None, BOOTNODE_DEFAULT_PORT, False
    if use_env and not upnp:
        from hsync_client.config import get_env_settings

        upnp = bool(get_env_settings().upnp_enabled)
    hp = _parse_host_port(hostname)
    effective_port = _merge_port(hostname.strip(), hp.port, port)
    return (
        hp.host,
        effective_port if effective_port is not None else BOOTNODE_DEFAULT_PORT,
        upnp,
    )


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


def _validate_shared_options(
    *,
    http_timeout: int | None,
    resync: int | None,
    reconnect_delay: int | None,
    stability_delay: int | None,
    max_wait_timeout: int,
    sync_direction: str,
    min_free_disk_space: int,
    max_inbound_size: int,
    max_bandwidth_upload: str | None,
    max_bandwidth_download: str | None,
    max_concurrent_transfers: int | None,
    mass_delete_threshold: int | None,
    mass_delete_percent: int | None,
    delta_min_file_size: int | None,
    delta_min_file_size_p2p: int | None,
) -> tuple[int | None, int | None]:
    """Validate the options shared by the ``p2p`` and ``server`` subcommands.

    Returns the parsed upload/download bandwidth limits (bytes/s, or ``None``).
    """
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
    if (
        min_free_disk_space < MIN_FREE_DISK_SPACE_MB_MIN
        or min_free_disk_space > MIN_FREE_DISK_SPACE_MB_MAX
    ):
        raise typer.BadParameter(
            f"Minimum free disk space must be between "
            f"{MIN_FREE_DISK_SPACE_MB_MIN} and {MIN_FREE_DISK_SPACE_MB_MAX} MB."
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

    return parsed_bw_upload, parsed_bw_download


# =============================================================================
# Shared option annotations
#
# Declared once and reused by both subcommand signatures. Typer builds a fresh
# click parameter for each command from the (immutable) Annotated metadata, so
# sharing the aliases cannot cross-bind options between subcommands; the
# per-subcommand surfaces are pinned by the black-box tests and by the
# --capabilities-json introspection.
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
        help="Maximum time to wait for file stabilization.",
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
IgnoreDeleteOpt = Annotated[
    bool,
    typer.Option(
        "--ignore-delete",
        help="Do not propagate file deletions (useful for backup scenarios).",
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
    int,
    typer.Option(
        "--min-free-disk-space",
        help="Minimum free disk space in MB before sync. 0 = disabled.",
        metavar="MB",
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
        help="Write output to FILE.",
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
            "Remote server host or IP, optionally with a port (HOST[:PORT]); "
            "https:// is always used. Omit to read the full URL from HSYNC_SERVER."
        ),
        metavar="HOST[:PORT]",
    ),
]
ServerPortOpt = Annotated[
    int | None,
    typer.Option(
        "--port",
        help=(
            f"Remote server port (default: {SERVER_DEFAULT_PORT}). Mutually "
            "exclusive with a port in the HOST[:PORT] argument."
        ),
        metavar="PORT",
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

BootnodeArg = Annotated[
    str | None,
    typer.Argument(
        help=(
            "Optional bootnode host for cross-network discovery, optionally "
            "with a port (HOST[:PORT]); omit it to stay on mDNS only. Extra UDP "
            "ports to try for the SAME node (client-egress fallback, e.g. when "
            "UDP 443 is blocked) go in HSYNC_BOOTNODE_PORTS."
        ),
        metavar="BOOTNODE[:PORT]",
    ),
]

BootnodePortOpt = Annotated[
    int | None,
    typer.Option(
        "--port",
        help=(
            f"UDP port of the remote bootnode (QUIC); default {BOOTNODE_DEFAULT_PORT}. "
            "Mutually exclusive with a port in the BOOTNODE[:PORT] argument. "
            "HSYNC_BOOTNODE_PORTS adds extra ports to try for the same node."
        ),
        metavar="PORT",
        rich_help_panel="P2P Mode",
    ),
]

UpnpOpt = Annotated[
    bool,
    typer.Option(
        "--upnp",
        help=(
            "Map the master TCP port with UPnP (opt-in; off by default). Only "
            "meaningful with a bootnode: without one, mDNS already keeps discovery "
            "on the local network."
        ),
        rich_help_panel="P2P Mode",
    ),
]
BootnodeFingerprintOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--bootnode-fingerprint",
        help=(
            "Pin the bootnode certificate fingerprint ('sha256:<hex>' or bare hex). "
            "May be repeated (up to two) for a rotation window."
        ),
        metavar="FINGERPRINT",
        rich_help_panel="P2P Mode",
    ),
]
BootnodeTofuOpt = Annotated[
    bool,
    typer.Option(
        "--bootnode-tofu",
        help=(
            "Trust the bootnode certificate on first use (TOFU); a later mismatch "
            "is refused as a possible man-in-the-middle."
        ),
        show_default=False,
        rich_help_panel="P2P Mode",
    ),
]
BootnodeTrustOpt = Annotated[
    str | None,
    typer.Option(
        "--bootnode-trust",
        help=(
            "Bootnode TLS trust policy: 'ask' (default: confirm an unknown "
            "fingerprint on a terminal, fail closed without one), 'accept-new' "
            "(pin the first sighting silently), 'strict' (require a pin) or "
            "'none' (no verification; loopback/tests only)."
        ),
        metavar="POLICY",
        show_default=False,
        rich_help_panel="P2P Mode",
    ),
]
DiscoveryTimeoutOpt = Annotated[
    int | None,
    typer.Option(
        "--discovery-timeout",
        help=(
            "Time to wait for initial P2P peer discovery in seconds "
            f"(default: {DISCOVERY_TIMEOUT_DEFAULT}; 0 = wait indefinitely)."
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
RequirePairingOpt = Annotated[
    bool,
    typer.Option(
        "--require-pairing",
        help=(
            "Strict pairing: refuse a first-seen P2P master certificate until it is "
            "confirmed out of band (disables Trust-On-First-Use auto-pinning)."
        ),
        rich_help_panel="P2P Mode",
    ),
]
AllowUnauthenticatedWritesOpt = Annotated[
    bool,
    typer.Option(
        "--allow-unauthenticated-writes",
        help=(
            "Dangerous: allow P2P uploads, deletes, and other mutations without "
            "HSYNC_P2P_KEY. Reads remain available without this option."
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

        [dim]# P2P mode with authentication[/dim]
        $ HSYNC_P2P_KEY=mysecret hsync p2p -b shared

    [bold]Environment Variables:[/bold]

        HSYNC_SERVER    Full server URL (alternative to the HOSTNAME argument)
        HSYNC_API_KEY   API key for authentication
        HSYNC_P2P_KEY   Shared secret for P2P mode authentication

    [bold]Project:[/bold]

        https://github.com/albertsdk/hsync-client

    [bold]Authors:[/bold]

        Alberto Basso
    """


@app.command()
def p2p(
    bucket: BucketOpt,
    bootnode: BootnodeArg = None,
    http_timeout: HttpTimeoutOpt = None,
    p2p_listen_port: ListenPortOpt = 0,
    discovery_timeout: DiscoveryTimeoutOpt = None,
    p2p_group: GroupOpt = None,
    prefer_master: PreferMasterOpt = False,
    never_master: NeverMasterOpt = False,
    require_pairing: RequirePairingOpt = False,
    allow_unauthenticated_writes: AllowUnauthenticatedWritesOpt = False,
    allow_unauthenticated_p2p: AllowUnauthenticatedP2POpt = False,
    p2p_key_fd: P2PKeyFdOpt = None,
    port: BootnodePortOpt = None,
    upnp: UpnpOpt = False,
    insecure: InsecureOpt = False,
    ca_cert: CaCertOpt = None,
    bootnode_fingerprint: BootnodeFingerprintOpt = None,
    bootnode_tofu: BootnodeTofuOpt = False,
    bootnode_trust: BootnodeTrustOpt = None,
    events_json_fd: EventsJsonFdOpt = None,
    local_path: PathOpt = _DEFAULT_LOCAL_PATH,
    once: OnceOpt = False,
    resync: IntervalOpt = None,
    reconnect_delay: RetryDelayOpt = None,
    stability_delay: StabilityDelayOpt = None,
    max_wait_timeout: MaxWaitTimeoutOpt = 120,
    filter_path: SubdirOpt = None,
    dryrun: DryRunOpt = False,
    ignore_delete: IgnoreDeleteOpt = False,
    sync_direction: SyncDirectionOpt = "bidirectional",
    max_bandwidth_upload: LimitUploadOpt = None,
    max_bandwidth_download: LimitDownloadOpt = None,
    no_compression: NoCompressionOpt = False,
    no_delta: NoDeltaOpt = False,
    no_peer_download: NoPeerDownloadOpt = False,
    delta_min_file_size: DeltaMinFileSizeOpt = None,
    delta_min_file_size_p2p: DeltaMinFileSizeP2POpt = None,
    max_concurrent_transfers: MaxConcurrentTransfersOpt = None,
    no_autotune: NoAutotuneOpt = False,
    mass_delete_guard: MassDeleteGuardOpt = True,
    mass_delete_threshold: MassDeleteThresholdOpt = None,
    mass_delete_percent: MassDeletePercentOpt = None,
    min_free_disk_space: MinFreeDiskSpaceOpt = MIN_FREE_DISK_SPACE_MB_DEFAULT,
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

        [dim]# Isolate a sync group and authenticate it[/dim]
        $ HSYNC_P2P_KEY=mysecret hsync p2p -b shared --group home

        [dim]# Discover peers across networks through a bootnode[/dim]
        $ hsync p2p boot.example.net -b shared

        [dim]# Bootnode extra ports when your egress firewall blocks UDP 443[/dim]
        $ HSYNC_BOOTNODE_PORTS=53 hsync p2p boot.example.net -b shared
    """
    parsed_bw_upload, parsed_bw_download = _validate_shared_options(
        http_timeout=http_timeout,
        resync=resync,
        reconnect_delay=reconnect_delay,
        stability_delay=stability_delay,
        max_wait_timeout=max_wait_timeout,
        sync_direction=sync_direction,
        min_free_disk_space=min_free_disk_space,
        max_inbound_size=max_inbound_size,
        max_bandwidth_upload=max_bandwidth_upload,
        max_bandwidth_download=max_bandwidth_download,
        max_concurrent_transfers=max_concurrent_transfers,
        mass_delete_threshold=mass_delete_threshold,
        mass_delete_percent=mass_delete_percent,
        delta_min_file_size=delta_min_file_size,
        delta_min_file_size_p2p=delta_min_file_size_p2p,
    )

    # P2P-only validation
    if discovery_timeout is not None and discovery_timeout < 0:
        raise typer.BadParameter("Discovery timeout must be 0 or greater (0 = wait indefinitely).")

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

    # Resolve the bootnode endpoint from the CLI positional, then the env
    # (loaded here so an --env-file bootnode is visible before run_sync loads it
    # again later). An explicit --port overrides a port embedded in the env.
    if env_file:
        from hsync_client.config import load_env_file

        try:
            load_env_file(env_file)
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            raise typer.Exit(1)

    bootnode_host, bootnode_port, upnp_enabled = _build_bootnode(
        bootnode, port, upnp=upnp, use_env=True
    )

    # Bootnode TLS trust (ADR-0034): --ca-cert is a server-mode knob, not a
    # bootnode channel knob, so refuse it rather than silently ignore it.
    if ca_cert:
        raise typer.BadParameter(
            "--ca-cert is not used for the bootnode channel; pin the node with "
            "--bootnode-fingerprint (or use --bootnode-tofu) instead."
        )

    fingerprints = tuple(bootnode_fingerprint or ())
    if len(fingerprints) > 2:
        raise typer.BadParameter(
            "at most two --bootnode-fingerprint values are allowed (rotation window)."
        )
    for fp in fingerprints:
        if parse_fingerprint(fp) is None:
            raise typer.BadParameter(f"invalid --bootnode-fingerprint value: {fp!r}")

    if insecure and (fingerprints or bootnode_tofu):
        raise typer.BadParameter(
            "--insecure is mutually exclusive with --bootnode-fingerprint/--bootnode-tofu."
        )

    if (fingerprints or bootnode_tofu) and not bootnode_host:
        raise typer.BadParameter("--bootnode-fingerprint/--bootnode-tofu require a bootnode host.")

    from hsync_client.main_logic import run_sync

    run_sync(
        bucket=bucket,
        env_file=env_file,
        server_url=None,
        verify_ssl=not insecure,
        ca_cert=ca_cert,
        http_timeout=http_timeout,
        api_key_fd=None,
        p2p_key_fd=p2p_key_fd,
        events_json_fd=events_json_fd,
        local_path=local_path,
        continuous=not once,
        resync=resync,
        reconnect_delay=reconnect_delay,
        stability_delay=stability_delay,
        max_wait_timeout=max_wait_timeout,
        filter_path=filter_path,
        dryrun=dryrun,
        ignore_delete=ignore_delete,
        sync_direction=sync_direction,
        compression_enabled=not no_compression,
        delta_enabled=not no_delta,
        delta_min_file_size=delta_min_file_size,
        delta_min_file_size_p2p=delta_min_file_size_p2p,
        max_bandwidth_upload=parsed_bw_upload,
        max_bandwidth_download=parsed_bw_download,
        p2p_mode=True,
        p2p_listen_port=p2p_listen_port,
        p2p_bootnode=bootnode_host,
        p2p_bootnode_port=bootnode_port,
        p2p_upnp_enabled=upnp_enabled,
        bootnode_fingerprints=fingerprints,
        bootnode_tofu=bool(bootnode_tofu),
        bootnode_trust=bootnode_trust,
        discovery_timeout=discovery_timeout,
        p2p_group=p2p_group,
        prefer_master=prefer_master,
        never_master=never_master,
        p2p_require_pairing=require_pairing,
        p2p_peer_download_enabled=not no_peer_download,
        p2p_allow_unauthenticated_writes=allow_unauthenticated_writes,
        p2p_allow_unauthenticated=allow_unauthenticated_p2p,
        debug=verbose,
        quiet=quiet,
        logfile=logfile,
        color=color,
        allow_deletions=allow_deletions,
        min_free_disk_space=min_free_disk_space,
        max_inbound_size=max_inbound_size,
        ignore_patterns=[p.strip() for p in (ignore_patterns or []) if p.strip()],
        max_concurrent_transfers=max_concurrent_transfers,
        no_autotune=no_autotune,
        mass_delete_guard=mass_delete_guard,
        mass_delete_threshold=mass_delete_threshold,
        mass_delete_percent=mass_delete_percent,
    )


# =============================================================================
# trust management (P2P peers, bootnode, server) — one ``hsync trust`` namespace
# =============================================================================

# ``hsync trust …`` mirrors OpenSSH's known-hosts workflow so an operator can
# inspect, pre-pin and forget fingerprints without editing the JSON stores by
# hand. One namespace covers the three certificate-pinning transports: the P2P
# master/seeders (``known_peers``), the bootnode and the sync server
# (``known_hosts``). ``hsync bootnode``/``hsync server`` stay actions, so the trust
# surface lives here rather than under them (ADR-0036).
_trust_root = typer.Typer(
    help="Inspect and manage certificate trust for the P2P peers, bootnode and server.",
    no_args_is_help=True,
)
_trust_bootnode_app = typer.Typer(
    help="Manage the bootnode known-hosts store.",
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
_trust_root.add_typer(_trust_bootnode_app, name="bootnode")
_trust_root.add_typer(_trust_peer_app, name="peer")
_trust_root.add_typer(_trust_server_app, name="server")


def _trust_host_port(hostname: str | None, port: int | None) -> tuple[str, int]:
    """Resolve ``hostname``/``--port`` for the trust subcommands (host required)."""
    if hostname is None:
        raise typer.BadParameter("a bootnode hostname is required")
    host, resolved_port, _ = _build_bootnode(hostname, port, upnp=False)
    if host is None:  # ``_build_bootnode`` only returns None for an absent host
        raise typer.BadParameter("a bootnode hostname is required")
    return host, resolved_port


@_trust_bootnode_app.command("list")
def bootnode_trust_list(
    as_json: bool = typer.Option(False, "--json", help="Emit entries as JSON."),
) -> None:
    """List the pinned bootnode fingerprints (like ``known_hosts``)."""
    entries = BootnodeTrustStore().entries()
    if as_json:
        typer.echo(json.dumps(entries, indent=2, sort_keys=True))
        return
    if not entries:
        typer.echo("No bootnodes are trusted yet.")
        return
    for entry in entries:
        typer.echo(
            f"{format_authority(entry['host'], entry['port'])} {entry['fingerprint']} "
            f"(source={entry['source']}, first_seen={entry['first_seen']})"
        )


@_trust_bootnode_app.command("show")
def bootnode_trust_show(hostname: HostArg, port: BootnodePortOpt = None) -> None:
    """Show the pinned fingerprint for one bootnode."""
    host, resolved_port = _trust_host_port(hostname, port)
    fingerprint = BootnodeTrustStore().get(host, resolved_port)
    if fingerprint is None:
        typer.echo(
            f"{format_authority(host, resolved_port)} is not in the known-hosts store.", err=True
        )
        raise typer.Exit(code=1)
    typer.echo(f"{format_authority(host, resolved_port)} {fingerprint}")


@_trust_bootnode_app.command("add")
def bootnode_trust_add(
    hostname: HostArg,
    fingerprint: str = typer.Argument(..., metavar="FINGERPRINT"),
    port: BootnodePortOpt = None,
) -> None:
    """Pin a bootnode fingerprint (verify it out of band first)."""
    host, resolved_port = _trust_host_port(hostname, port)
    normalized = parse_fingerprint(fingerprint)
    if normalized is None:
        typer.echo("Not a valid sha256 fingerprint (expected 64 hex digits).", err=True)
        raise typer.Exit(code=1)
    store = BootnodeTrustStore()
    store.set(host, resolved_port, normalized, source="cli")
    typer.echo(f"Pinned {format_authority(host, resolved_port)} {normalized} in {store.path}.")


@_trust_bootnode_app.command("remove")
def bootnode_trust_remove(hostname: HostArg, port: BootnodePortOpt = None) -> None:
    """Forget a pinned bootnode (like ``ssh-keygen -R``)."""
    host, resolved_port = _trust_host_port(hostname, port)
    if BootnodeTrustStore().remove(host, resolved_port):
        typer.echo(f"Removed {format_authority(host, resolved_port)} from the known-hosts store.")
    else:
        typer.echo(f"{format_authority(host, resolved_port)} was not in the known-hosts store.")


@_trust_bootnode_app.command("fingerprint")
def bootnode_trust_fingerprint(hostname: HostArg, port: BootnodePortOpt = None) -> None:
    """Print a bootnode's certificate fingerprint without trusting it."""
    from hsync_client.boot_client import BootnodeClient, BootnodeError

    host, resolved_port = _trust_host_port(hostname, port)
    client = BootnodeClient(host, resolved_port, policy=TrustPolicy.none)
    try:
        observed = client.fetch_fingerprint()
    except BootnodeError as exc:
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


def _server_trust_host_port(hostname: str | None, port: int | None) -> tuple[str, int]:
    """Resolve ``hostname``/``--port`` for the server trust subcommands."""
    if hostname is None:
        raise typer.BadParameter("a server hostname is required")
    hp = _parse_host_port(hostname)
    effective_port = _merge_port(hostname.strip(), hp.port, port)
    return hp.host, SERVER_DEFAULT_PORT if effective_port is None else effective_port


@_trust_server_app.command("list")
def server_trust_list(
    as_json: bool = typer.Option(False, "--json", help="Emit entries as JSON."),
) -> None:
    """List the pinned sync-server fingerprints (like ``known_hosts``)."""
    entries = ServerTrustStore().entries()
    if as_json:
        typer.echo(json.dumps(entries, indent=2, sort_keys=True))
        return
    if not entries:
        typer.echo("No servers are trusted yet.")
        return
    for entry in entries:
        typer.echo(
            f"{format_authority(entry['host'], entry['port'])} {entry['fingerprint']} "
            f"(source={entry['source']}, first_seen={entry['first_seen']})"
        )


@_trust_server_app.command("show")
def server_trust_show(hostname: HostArg, port: ServerPortOpt = None) -> None:
    """Show the pinned fingerprint for one server."""
    host, resolved_port = _server_trust_host_port(hostname, port)
    fingerprint = ServerTrustStore().get(host, resolved_port)
    if fingerprint is None:
        typer.echo(
            f"{format_authority(host, resolved_port)} is not in the known-hosts store.", err=True
        )
        raise typer.Exit(code=1)
    typer.echo(f"{format_authority(host, resolved_port)} {fingerprint}")


@_trust_server_app.command("add")
def server_trust_add(
    hostname: HostArg,
    fingerprint: str = typer.Argument(..., metavar="FINGERPRINT"),
    port: ServerPortOpt = None,
) -> None:
    """Pin a sync-server fingerprint (verify it out of band first)."""
    host, resolved_port = _server_trust_host_port(hostname, port)
    normalized = parse_fingerprint(fingerprint)
    if normalized is None:
        typer.echo("Not a valid sha256 fingerprint (expected 64 hex digits).", err=True)
        raise typer.Exit(code=1)
    store = ServerTrustStore()
    store.set(host, resolved_port, normalized, source="cli")
    typer.echo(f"Pinned {format_authority(host, resolved_port)} {normalized} in {store.path}.")


@_trust_server_app.command("remove")
def server_trust_remove(hostname: HostArg, port: ServerPortOpt = None) -> None:
    """Forget a pinned sync server (like ``ssh-keygen -R``)."""
    host, resolved_port = _server_trust_host_port(hostname, port)
    if ServerTrustStore().remove(host, resolved_port):
        typer.echo(f"Removed {format_authority(host, resolved_port)} from the known-hosts store.")
    else:
        typer.echo(f"{format_authority(host, resolved_port)} was not in the known-hosts store.")


@_trust_server_app.command("fingerprint")
def server_trust_fingerprint(hostname: HostArg, port: ServerPortOpt = None) -> None:
    """Print a server's certificate fingerprint without trusting it."""
    host, resolved_port = _server_trust_host_port(hostname, port)
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
    hostname: HostArg = None,
    port: ServerPortOpt = None,
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
    max_wait_timeout: MaxWaitTimeoutOpt = 120,
    filter_path: SubdirOpt = None,
    dryrun: DryRunOpt = False,
    ignore_delete: IgnoreDeleteOpt = False,
    sync_direction: SyncDirectionOpt = "bidirectional",
    max_bandwidth_upload: LimitUploadOpt = None,
    max_bandwidth_download: LimitDownloadOpt = None,
    no_compression: NoCompressionOpt = False,
    no_delta: NoDeltaOpt = False,
    delta_min_file_size: DeltaMinFileSizeOpt = None,
    delta_min_file_size_p2p: DeltaMinFileSizeP2POpt = None,
    max_concurrent_transfers: MaxConcurrentTransfersOpt = None,
    no_autotune: NoAutotuneOpt = False,
    mass_delete_guard: MassDeleteGuardOpt = True,
    mass_delete_threshold: MassDeleteThresholdOpt = None,
    mass_delete_percent: MassDeletePercentOpt = None,
    min_free_disk_space: MinFreeDiskSpaceOpt = MIN_FREE_DISK_SPACE_MB_DEFAULT,
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
    all connected clients. The bare hostname form implies ``https://`` and the
    default port (9753, the hsync-server default): a reverse proxy on 443 is
    supported with ``--port 443``.

    [bold]Examples:[/bold]

        [dim]# Basic sync[/dim]
        $ hsync server server.example.com -b photos

        [dim]# Continuous sync (the default: keeps retrying if the server is unreachable)[/dim]
        $ hsync server server.example.com -b projects

        [dim]# One-shot sync[/dim]
        $ hsync server server.example.com -b projects --once

        [dim]# Reverse proxy on the standard HTTPS port[/dim]
        $ hsync server server.example.com --port 443 -b photos
    """
    # Bare-hostname form: assemble the https:// URL (port falls back to the
    # hsync-server default). None → the documented HSYNC_SERVER env fallback.
    server_url = _build_server_url(hostname, port)

    # The bare hostname is mandatory in server mode: when it is omitted, the
    # full URL must come from HSYNC_SERVER (shell env or --env-file). Resolve
    # it here so a missing server fails fast with a clean usage error instead of
    # deep inside run_sync, after the banner and logging are already up.
    if server_url is None:
        if env_file:
            from hsync_client.config import load_env_file

            try:
                load_env_file(env_file)
            except FileNotFoundError as e:
                print(f"Error: {e}", file=sys.stderr)
                raise typer.Exit(1)
        from hsync_client.config import get_server_url

        server_url = get_server_url()
        if not server_url:
            raise typer.BadParameter(
                "Missing server hostname: pass it as the HOSTNAME argument "
                "(e.g. 'hsync server server.example.com') or set the "
                "HSYNC_SERVER environment variable."
            )

    parsed_bw_upload, parsed_bw_download = _validate_shared_options(
        http_timeout=http_timeout,
        resync=resync,
        reconnect_delay=reconnect_delay,
        stability_delay=stability_delay,
        max_wait_timeout=max_wait_timeout,
        sync_direction=sync_direction,
        min_free_disk_space=min_free_disk_space,
        max_inbound_size=max_inbound_size,
        max_bandwidth_upload=max_bandwidth_upload,
        max_bandwidth_download=max_bandwidth_download,
        max_concurrent_transfers=max_concurrent_transfers,
        mass_delete_threshold=mass_delete_threshold,
        mass_delete_percent=mass_delete_percent,
        delta_min_file_size=delta_min_file_size,
        delta_min_file_size_p2p=delta_min_file_size_p2p,
    )

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
        bucket=bucket,
        env_file=env_file,
        server_url=server_url,
        verify_ssl=not insecure,
        ca_cert=ca_cert,
        server_fingerprints=server_fingerprints,
        server_tofu=bool(server_tofu),
        server_trust=server_trust,
        http_timeout=http_timeout,
        api_key_fd=api_key_fd,
        p2p_key_fd=None,
        events_json_fd=events_json_fd,
        local_path=local_path,
        continuous=not once,
        resync=resync,
        reconnect_delay=reconnect_delay,
        stability_delay=stability_delay,
        max_wait_timeout=max_wait_timeout,
        filter_path=filter_path,
        dryrun=dryrun,
        ignore_delete=ignore_delete,
        sync_direction=sync_direction,
        compression_enabled=not no_compression,
        delta_enabled=not no_delta,
        delta_min_file_size=delta_min_file_size,
        delta_min_file_size_p2p=delta_min_file_size_p2p,
        max_bandwidth_upload=parsed_bw_upload,
        max_bandwidth_download=parsed_bw_download,
        p2p_mode=False,
        p2p_listen_port=0,
        discovery_timeout=None,
        p2p_group=None,
        prefer_master=False,
        never_master=False,
        p2p_require_pairing=False,
        p2p_peer_download_enabled=False,
        p2p_allow_unauthenticated_writes=False,
        debug=verbose,
        quiet=quiet,
        logfile=logfile,
        color=color,
        allow_deletions=allow_deletions,
        min_free_disk_space=min_free_disk_space,
        max_inbound_size=max_inbound_size,
        ignore_patterns=[p.strip() for p in (ignore_patterns or []) if p.strip()],
        max_concurrent_transfers=max_concurrent_transfers,
        no_autotune=no_autotune,
        mass_delete_guard=mass_delete_guard,
        mass_delete_threshold=mass_delete_threshold,
        mass_delete_percent=mass_delete_percent,
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


def run() -> None:
    """Entry point for the CLI application."""
    try:
        app()
    finally:
        # Process-level safety net for the asynchronous log sinks: whatever
        # exit path was taken (normal, SystemExit, unhandled error), the queued
        # records reach the terminal before the interpreter tears the stream
        # down.
        #
        # Drained only when the logging stack was actually initialised: the same
        # entry point serves the fast paths (``--version`` / ``--help``), whose
        # contract is that importing ``hsync_client.cli`` pulls none of the heavy
        # dependencies (pinned by ``tests/test_startup_imports.py``), so the
        # import stays lazy and conditional.
        if "hsync_client.logger" in sys.modules:
            from hsync_client.logger import flush_logs

            flush_logs()
