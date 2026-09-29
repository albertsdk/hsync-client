# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Command-line interface for HSYNC Client.

This module provides the Typer-based CLI for the HSYNC command-line interface.
It exposes two mutually exclusive subcommands — ``p2p`` (P2P peer-to-peer sync)
and ``server`` (central-server sync) — under a parent callback that carries the
global options (``--version``, ``--capabilities-json``, ``--help-symbols``).
"""

import json
import re
import sys
from os import getcwd
from typing import Annotated

import typer
import typer.main
from hsync_common.protocol.bootnode_trust import TrustPolicy
from hsync_common.protocol.pairing import parse_fingerprint

from hsync_client.bootnode_trust import BootnodeTrustStore
from hsync_client.constants import (
    BANDWIDTH_MAX,
    BANDWIDTH_MIN,
    BOOTNODE_DEFAULT_PORT,
    DISCOVERY_TIMEOUT_DEFAULT,
    HOSTNAME_PATTERN,
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


# Bare hostname / IPv4 charset (SSOT: HOSTNAME_PATTERN in hsync_client.constants,
# shared with the HSYNC_SERVER validator in config.py). IPv6 literals are
# bracketed instead.
_HOSTNAME_PATTERN = re.compile(rf"^{HOSTNAME_PATTERN}$")
_IPV6_BRACKETED_PATTERN = re.compile(r"^\[[0-9A-Fa-f:.]+\]$")


def _build_server_url(hostname: str | None, port: int | None) -> str | None:
    """Assemble the ``https://`` server URL from the bare host form.

    Server mode always uses HTTPS (the client refuses to send the API key over
    an unencrypted channel), so the scheme is implied; the port defaults to
    ``SERVER_DEFAULT_PORT``, the hsync-server default. A ``None`` hostname
    (env-only usage, ``HSYNC_SERVER``) returns ``None`` so ``run_sync`` keeps
    its documented env fallback. IPv6 literals are bracketed automatically — a
    bare hostname never contains ``:``.

    Raises:
        typer.BadParameter: on URL-like input, an unparseable host, a port
            outside 1-65535, or ``--port`` without a hostname.
    """
    if port is not None and not 1 <= port <= 65535:
        raise typer.BadParameter("--port must be between 1 and 65535.")
    if hostname is None:
        if port is not None:
            raise typer.BadParameter(
                "--port requires a hostname (or set the full URL via HSYNC_SERVER)."
            )
        return None
    host = hostname.strip()
    if "://" in host or "/" in host:
        raise typer.BadParameter(
            f"pass the bare hostname, not a URL ('{hostname}'): the https:// "
            "scheme is implied, e.g. 'hsync server server.example.com'."
        )
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        host = f"[{host}]"  # bare IPv6 literal
    if not (_HOSTNAME_PATTERN.match(host) or _IPV6_BRACKETED_PATTERN.match(host)):
        raise typer.BadParameter(
            f"invalid hostname: {hostname!r} (a hostname, IPv4 or IPv6 literal "
            "is expected; use --port for the port, not host:port)."
        )
    return f"https://{host}:{SERVER_DEFAULT_PORT if port is None else port}"


def _build_bootnode(
    hostname: str | None,
    port: int | None,
    *,
    no_upnp: bool,
) -> tuple[str | None, int, bool]:
    """Resolve the bootnode plan inputs; return ``(host, port, upnp_enabled)``.

    A ``None`` host selects the mDNS plan (``port``/``upnp`` are then
    irrelevant). ``--port`` without a bootnode is a hard error, never a
    silently ignored flag; ``HSYNC_BOOTNODE`` is a host-only fallback (no port
    is read from the environment — the QUIC port comes from ``--port`` or the
    443 default). Raises ``typer.BadParameter`` on a URL-like or port-bearing
    host, mirroring ``_build_server_url``.
    """
    if port is not None and not 1 <= port <= 65535:
        raise typer.BadParameter("--port must be between 1 and 65535.")
    if hostname is None:
        if port is not None:
            raise typer.BadParameter(
                "--port requires a bootnode hostname (or set the full host via HSYNC_BOOTNODE)."
            )
        return None, BOOTNODE_DEFAULT_PORT, False
    host = hostname.strip()
    if "://" in host or "/" in host:
        raise typer.BadParameter(
            f"pass the bare bootnode hostname, not a URL ('{hostname}'): the QUIC "
            "scheme is implied, e.g. 'hsync p2p boot.example.net -b shared'."
        )
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        host = f"[{host}]"  # bare IPv6 literal
    if not (_HOSTNAME_PATTERN.match(host) or _IPV6_BRACKETED_PATTERN.match(host)):
        raise typer.BadParameter(
            f"invalid bootnode hostname: {hostname!r} (a hostname, IPv4 or IPv6 "
            "literal is expected; use --port for the port, not host:port)."
        )
    return host, port if port is not None else BOOTNODE_DEFAULT_PORT, not no_upnp


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
    # Validate numeric ranges
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

    # Parse and validate bandwidth limits
    parsed_bw_upload = _parse_and_validate_bandwidth(
        max_bandwidth_upload, "--limit-upload", "Upload"
    )
    parsed_bw_download = _parse_and_validate_bandwidth(
        max_bandwidth_download, "--limit-download", "Download"
    )

    # Validate max-concurrent-transfers range
    if max_concurrent_transfers is not None and (
        max_concurrent_transfers < MAX_CONCURRENT_TRANSFERS_MIN
        or max_concurrent_transfers > MAX_CONCURRENT_TRANSFERS_MAX
    ):
        raise typer.BadParameter(
            f"--max-concurrent-transfers must be between "
            f"{MAX_CONCURRENT_TRANSFERS_MIN} and {MAX_CONCURRENT_TRANSFERS_MAX}."
        )

    # Validate mass-delete-threshold range
    if mass_delete_threshold is not None and mass_delete_threshold < 0:
        raise typer.BadParameter("--mass-delete-threshold must be >= 0 (0 = disabled).")
    # Validate mass-delete-percent range
    if mass_delete_percent is not None and (mass_delete_percent < 0 or mass_delete_percent > 100):
        raise typer.BadParameter("--mass-delete-percent must be between 0 and 100 (0 = disabled).")
    # Validate delta size floors (0 = delta for every file size)
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
NoColorOpt = Annotated[
    bool,
    typer.Option(
        "--no-color",
        help="Disable colored output.",
        rich_help_panel="Output",
    ),
]
YesOpt = Annotated[
    bool,
    typer.Option(
        "-y",
        "--yes",
        help="Skip interactive confirmation prompts (still shows warnings).",
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
            "Remote server hostname or IP (https:// is always used; omit to "
            "read the full URL from HSYNC_SERVER)."
        ),
        metavar="HOSTNAME",
    ),
]
ServerPortOpt = Annotated[
    int | None,
    typer.Option(
        "--port",
        help=f"Remote server port (default: {SERVER_DEFAULT_PORT}).",
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
            "Optional bootnode host for cross-network discovery (a bare hostname, "
            "like `server HOSTNAME`); omit it to stay on mDNS only."
        ),
        metavar="BOOTNODE",
    ),
]

BootnodePortOpt = Annotated[
    int | None,
    typer.Option(
        "--port",
        help=f"UDP port of the remote bootnode (QUIC); default {BOOTNODE_DEFAULT_PORT}.",
        metavar="PORT",
        rich_help_panel="P2P Mode",
    ),
]

NoUpnpOpt = Annotated[
    bool,
    typer.Option(
        "--no-upnp",
        help=(
            "Do not try to map the master TCP port with UPnP. Only meaningful with "
            "a bootnode: without one, mDNS already keeps discovery on the local network."
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
ForceMasterOpt = Annotated[
    bool,
    typer.Option(
        "--force-master",
        help="Force this client to always be master in P2P mode.",
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
    force_master: ForceMasterOpt = False,
    never_master: NeverMasterOpt = False,
    require_pairing: RequirePairingOpt = False,
    allow_unauthenticated_writes: AllowUnauthenticatedWritesOpt = False,
    p2p_key_fd: P2PKeyFdOpt = None,
    port: BootnodePortOpt = None,
    no_upnp: NoUpnpOpt = False,
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
    no_color: NoColorOpt = False,
    yes: YesOpt = False,
    env_file: EnvFileOpt = None,
) -> None:
    """
    Synchronize a bucket over P2P peer-to-peer (P2P).

    Peers on the same local network discover each other via mDNS and one is
    elected master (an HTTPS/WSS transport hub). No central server is required.

    [bold]Examples:[/bold]

        [dim]# Basic P2P sync[/dim]
        $ hsync p2p -b shared

        [dim]# Force this peer to be the master[/dim]
        $ hsync p2p -b shared --force-master

        [dim]# Isolate a sync group and authenticate it[/dim]
        $ HSYNC_P2P_KEY=mysecret hsync p2p -b shared --group home

        [dim]# Discover peers across networks through a bootnode[/dim]
        $ hsync p2p boot.example.net -b shared
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

    # --force-master is incompatible with directional sync (upload/download-only)
    if force_master and sync_direction != "bidirectional":
        raise typer.BadParameter(
            f"--force-master is incompatible with --sync-direction={sync_direction}. "
            f"The P2P master always operates in bidirectional mode."
        )

    # --force-master and --never-master are mutually exclusive (contradictory)
    if force_master and never_master:
        raise typer.BadParameter(
            "--force-master and --never-master are mutually exclusive: a client "
            "cannot be forced to be master and forbidden from being master at once."
        )

    bootnode_host, bootnode_port, upnp_enabled = _build_bootnode(bootnode, port, no_upnp=no_upnp)

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
        force_master=force_master,
        never_master=never_master,
        p2p_require_pairing=require_pairing,
        p2p_peer_download_enabled=not no_peer_download,
        p2p_allow_unauthenticated_writes=allow_unauthenticated_writes,
        debug=verbose,
        quiet=quiet,
        logfile=logfile,
        no_color=no_color,
        auto_confirm=yes,
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
# bootnode trust (known_hosts) management
# =============================================================================

# ``hsync bootnode trust …`` mirrors OpenSSH's known-hosts workflow so an
# operator can inspect, pre-pin and forget bootnode fingerprints without editing
# the JSON store by hand (ADR-0035).
_bootnode_app = typer.Typer(
    help="Inspect and manage bootnode trust (the client's known hosts).",
    no_args_is_help=True,
)
_trust_app = typer.Typer(
    help="Manage the bootnode known-hosts store.",
    no_args_is_help=True,
)
_bootnode_app.add_typer(_trust_app, name="trust")
app.add_typer(_bootnode_app, name="bootnode")


def _trust_host_port(hostname: str | None, port: int | None) -> tuple[str, int]:
    """Resolve ``hostname``/``--port`` for the trust subcommands (host required)."""
    if hostname is None:
        raise typer.BadParameter("a bootnode hostname is required")
    host, resolved_port, _ = _build_bootnode(hostname, port, no_upnp=False)
    if host is None:  # ``_build_bootnode`` only returns None for an absent host
        raise typer.BadParameter("a bootnode hostname is required")
    return host, resolved_port


@_trust_app.command("list")
def bootnode_trust_list() -> None:
    """List the pinned bootnode fingerprints (like ``known_hosts``)."""
    entries = BootnodeTrustStore().entries()
    if not entries:
        typer.echo("No bootnodes are trusted yet.")
        return
    for entry in entries:
        typer.echo(
            f"{entry['host']}:{entry['port']} {entry['fingerprint']} "
            f"(source={entry['source']}, first_seen={entry['first_seen']})"
        )


@_trust_app.command("show")
def bootnode_trust_show(hostname: HostArg, port: BootnodePortOpt = None) -> None:
    """Show the pinned fingerprint for one bootnode."""
    host, resolved_port = _trust_host_port(hostname, port)
    fingerprint = BootnodeTrustStore().get(host, resolved_port)
    if fingerprint is None:
        typer.echo(f"{host}:{resolved_port} is not in the known-hosts store.", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"{host}:{resolved_port} {fingerprint}")


@_trust_app.command("add")
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
    BootnodeTrustStore().set(host, resolved_port, normalized, source="cli")
    typer.echo(f"Pinned {host}:{resolved_port} {normalized}.")


@_trust_app.command("remove")
def bootnode_trust_remove(hostname: HostArg, port: BootnodePortOpt = None) -> None:
    """Forget a pinned bootnode (like ``ssh-keygen -R``)."""
    host, resolved_port = _trust_host_port(hostname, port)
    if BootnodeTrustStore().remove(host, resolved_port):
        typer.echo(f"Removed {host}:{resolved_port} from the known-hosts store.")
    else:
        typer.echo(f"{host}:{resolved_port} was not in the known-hosts store.")


@_trust_app.command("fingerprint")
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


@app.command()
def server(
    bucket: BucketOpt,
    hostname: HostArg = None,
    port: ServerPortOpt = None,
    insecure: InsecureOpt = False,
    ca_cert: CaCertOpt = None,
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
    no_color: NoColorOpt = False,
    yes: YesOpt = False,
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

    from hsync_client.main_logic import run_sync

    run_sync(
        bucket=bucket,
        env_file=env_file,
        server_url=server_url,
        verify_ssl=not insecure,
        ca_cert=ca_cert,
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
        force_master=False,
        never_master=False,
        p2p_require_pairing=False,
        p2p_peer_download_enabled=False,
        p2p_allow_unauthenticated_writes=False,
        debug=verbose,
        quiet=quiet,
        logfile=logfile,
        no_color=no_color,
        auto_confirm=yes,
        min_free_disk_space=min_free_disk_space,
        max_inbound_size=max_inbound_size,
        ignore_patterns=[p.strip() for p in (ignore_patterns or []) if p.strip()],
        max_concurrent_transfers=max_concurrent_transfers,
        no_autotune=no_autotune,
        mass_delete_guard=mass_delete_guard,
        mass_delete_threshold=mass_delete_threshold,
        mass_delete_percent=mass_delete_percent,
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
