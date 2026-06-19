# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Command-line interface for HSYNC Client.

This module provides the Typer-based CLI for the HSYNC command-line interface.
"""

from os import getcwd
from typing import Annotated

import typer

from hsync_client.constants import (
    BANDWIDTH_MAX,
    BANDWIDTH_MIN,
    HSYNC_VERSION,
    HTTP_TIMEOUT_MAX,
    HTTP_TIMEOUT_MIN,
    MAX_WAIT_TIMEOUT_MAX,
    MAX_WAIT_TIMEOUT_MIN,
    MIN_FREE_DISK_SPACE_MB_DEFAULT,
    MIN_FREE_DISK_SPACE_MB_MAX,
    MIN_FREE_DISK_SPACE_MB_MIN,
    RECONNECT_DELAY_MAX,
    RECONNECT_DELAY_MIN,
    RESYNC_MAX,
    RESYNC_MIN,
    STABILITY_DELAY_MAX,
    STABILITY_DELAY_MIN,
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


def version_callback(value: bool) -> None:
    """Print version and exit."""
    if value:
        typer.echo(f"hsync {HSYNC_VERSION}")
        raise typer.Exit()


def _validate_bucket(value: str) -> str:
    """Validate bucket name (alphanumeric, hyphens and underscores allowed)."""
    import re

    if not re.match(r"^[a-zA-Z0-9]([a-zA-Z0-9_-]*[a-zA-Z0-9])?$", value):
        raise typer.BadParameter(
            "The bucket name must be alphanumeric (hyphens and underscores allowed, "
            "but not at the beginning or end)."
        )
    return value


@app.command()
def main(
    # -------------------------------------------------------------------------
    # Required
    # -------------------------------------------------------------------------
    bucket: Annotated[
        str,
        typer.Option(
            "-b",
            "--bucket",
            help="Remote bucket name.",
            metavar="NAME",
            callback=lambda v: _validate_bucket(v),
            rich_help_panel="Required",
        ),
    ],
    # -------------------------------------------------------------------------
    # Connection
    # -------------------------------------------------------------------------
    server_url: Annotated[
        str | None,
        typer.Option(
            "-s",
            "--server",
            help="Server URL (or set HSYNC_SERVER).",
            metavar="URL",
            rich_help_panel="Connection",
        ),
    ] = None,
    insecure: Annotated[
        bool,
        typer.Option(
            "--insecure",
            help="Skip SSL certificate verification.",
            show_default=False,
            rich_help_panel="Connection",
        ),
    ] = False,
    ca_cert: Annotated[
        str | None,
        typer.Option(
            "--ca-cert",
            help="Path to CA certificate file for self-signed server certs.",
            metavar="FILE",
            rich_help_panel="Connection",
        ),
    ] = None,
    http_timeout: Annotated[
        int,
        typer.Option(
            "--timeout",
            help="HTTP request timeout in seconds.",
            metavar="SEC",
            rich_help_panel="Connection",
        ),
    ] = 30,
    api_key_fd: Annotated[
        int | None,
        typer.Option(
            "--api-key-fd",
            help="Read API key from the given file descriptor (for secure passing from a parent process).",
            metavar="FD",
            hidden=True,
            rich_help_panel="Connection",
        ),
    ] = None,
    lan_key_fd: Annotated[
        int | None,
        typer.Option(
            "--lan-key-fd",
            help="Read LAN key from the given file descriptor (for secure passing from a parent process).",
            metavar="FD",
            hidden=True,
            rich_help_panel="Connection",
        ),
    ] = None,
    # -------------------------------------------------------------------------
    # Sync
    # -------------------------------------------------------------------------
    local_path: Annotated[
        str,
        typer.Option(
            "-p",
            "--path",
            help="Local directory path.",
            metavar="DIR",
            rich_help_panel="Sync",
        ),
    ] = getcwd(),
    continuous: Annotated[
        bool,
        typer.Option(
            "-w",
            "--watch",
            help="Watch for changes and sync continuously.",
            rich_help_panel="Sync",
        ),
    ] = False,
    resync: Annotated[
        int | None,
        typer.Option(
            "-i",
            "--interval",
            help="Force full sync every SEC seconds (requires -w).",
            metavar="SEC",
            rich_help_panel="Sync",
        ),
    ] = None,
    reconnect_delay: Annotated[
        int,
        typer.Option(
            "--retry-delay",
            help="Retry connection after SEC seconds.",
            metavar="SEC",
            rich_help_panel="Sync",
        ),
    ] = 5,
    stability_delay: Annotated[
        int | None,
        typer.Option(
            "--stability-delay",
            help="Seconds to wait before uploading modified files.",
            metavar="SEC",
            rich_help_panel="Sync",
        ),
    ] = None,
    max_wait_timeout: Annotated[
        int,
        typer.Option(
            "--max-wait-timeout",
            help="Maximum time to wait for file stabilization.",
            metavar="SEC",
            rich_help_panel="Sync",
        ),
    ] = 120,
    filter_path: Annotated[
        str | None,
        typer.Option(
            "--filter-path",
            help="Sync only files within this subdirectory path.",
            metavar="PATH",
            rich_help_panel="Sync",
        ),
    ] = None,
    dryrun: Annotated[
        bool,
        typer.Option(
            "-n",
            "--dry-run",
            help="Show what would be done without making changes.",
            rich_help_panel="Sync",
        ),
    ] = False,
    ignore_delete: Annotated[
        bool,
        typer.Option(
            "--ignore-delete",
            help="Do not propagate file deletions (useful for backup scenarios).",
            rich_help_panel="Sync",
        ),
    ] = False,
    sync_direction: Annotated[
        str,
        typer.Option(
            "--sync-direction",
            help="Sync direction: bidirectional, upload-only, or download-only.",
            rich_help_panel="Sync",
        ),
    ] = "bidirectional",
    max_bandwidth_upload: Annotated[
        str | None,
        typer.Option(
            "--max-bandwidth-upload",
            help="Maximum upload bandwidth (e.g., '1MB', '500KB'). 0 = unlimited.",
            metavar="RATE",
            rich_help_panel="Sync",
        ),
    ] = None,
    max_bandwidth_download: Annotated[
        str | None,
        typer.Option(
            "--max-bandwidth-download",
            help="Maximum download bandwidth (e.g., '1MB', '500KB'). 0 = unlimited.",
            metavar="RATE",
            rich_help_panel="Sync",
        ),
    ] = None,
    no_compression: Annotated[
        bool,
        typer.Option(
            "--no-compression",
            help="Disable zstd compression for file transfers.",
            rich_help_panel="Sync",
        ),
    ] = False,
    min_free_disk_space: Annotated[
        int,
        typer.Option(
            "--min-free-disk-space",
            help="Minimum free disk space in MB before sync. 0 = disabled.",
            metavar="MB",
            rich_help_panel="Sync",
        ),
    ] = MIN_FREE_DISK_SPACE_MB_DEFAULT,
    sync_hidden: Annotated[
        bool,
        typer.Option(
            "--sync-hidden",
            help="Include hidden/system files in sync (disables built-in filters).",
            rich_help_panel="Sync",
        ),
    ] = False,
    ignore_patterns: Annotated[
        str | None,
        typer.Option(
            "--ignore-patterns",
            help="Comma-separated extra gitignore-style patterns (layered on .hsyncignore).",
            metavar="PATTERNS",
            rich_help_panel="Sync",
        ),
    ] = None,
    # -------------------------------------------------------------------------
    # LAN Mode
    # -------------------------------------------------------------------------
    lan_mode: Annotated[
        bool,
        typer.Option(
            "--lan-mode",
            help="Enable LAN peer-to-peer mode without central server.",
            rich_help_panel="LAN Mode",
        ),
    ] = False,
    lan_port: Annotated[
        int,
        typer.Option(
            "--lan-port",
            help="Port to use when acting as LAN master.",
            metavar="PORT",
            rich_help_panel="LAN Mode",
        ),
    ] = 0,
    discovery_timeout: Annotated[
        int,
        typer.Option(
            "--discovery-timeout",
            help="Time to wait for peer discovery.",
            metavar="SEC",
            rich_help_panel="LAN Mode",
        ),
    ] = 10,
    lan_group: Annotated[
        str | None,
        typer.Option(
            "--lan-group",
            help="Group name for LAN peer filtering.",
            metavar="GROUP",
            rich_help_panel="LAN Mode",
        ),
    ] = None,
    lan_https: Annotated[
        bool,
        typer.Option(
            "--no-lan-https/",
            help="Disable HTTPS for LAN mode (use plain HTTP).",
            show_default=False,
            rich_help_panel="LAN Mode",
        ),
    ] = True,
    lan_persistent: Annotated[
        bool,
        typer.Option(
            "--no-lan-persistent/",
            help="Disable persistent mode (exit when no peers found).",
            show_default=False,
            rich_help_panel="LAN Mode",
        ),
    ] = True,
    force_master: Annotated[
        bool,
        typer.Option(
            "--force-master",
            help="Force this client to always be master in LAN mode.",
            rich_help_panel="LAN Mode",
        ),
    ] = False,
    never_master: Annotated[
        bool,
        typer.Option(
            "--never-master",
            help="Never become master in LAN mode (always remain a client/slave).",
            rich_help_panel="LAN Mode",
        ),
    ] = False,
    # -------------------------------------------------------------------------
    # Output
    # -------------------------------------------------------------------------
    debug: Annotated[
        bool,
        typer.Option(
            "-v",
            "--verbose",
            help="Enable verbose output.",
            rich_help_panel="Output",
        ),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option(
            "-q",
            "--quiet",
            help="Suppress all output.",
            rich_help_panel="Output",
        ),
    ] = False,
    logfile: Annotated[
        str | None,
        typer.Option(
            "-o",
            "--output",
            help="Write output to FILE.",
            metavar="FILE",
            rich_help_panel="Output",
        ),
    ] = None,
    no_color: Annotated[
        bool,
        typer.Option(
            "--no-color",
            help="Disable colored output.",
            rich_help_panel="Output",
        ),
    ] = False,
    legend: Annotated[
        bool,
        typer.Option(
            "--help-symbols",
            help="Show output symbols legend.",
            rich_help_panel="Output",
        ),
    ] = False,
    # -------------------------------------------------------------------------
    # General
    # -------------------------------------------------------------------------
    yes: Annotated[
        bool,
        typer.Option(
            "-y",
            "--yes",
            help="Skip interactive confirmation prompts (still shows warnings).",
        ),
    ] = False,
    env_file: Annotated[
        str | None,
        typer.Option(
            "--env-file",
            help="Load environment variables from file.",
            metavar="PATH",
        ),
    ] = None,
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
) -> None:
    """
    HSYNC - HTTP-based file synchronization client.

    Synchronize files between a local directory and a remote bucket. A bucket
    is a shared directory hosted on a central HSYNC server or on another peer
    in LAN Mode. In [bold]Server Mode[/bold], clients connect to a centralized server.
    In [bold]LAN Mode[/bold] (P2P), clients discover each other via mDNS and one
    automatically becomes the master—no central server required.

    [bold]Option Groups:[/bold]

        [cyan]Required[/cyan]     Mandatory options to start synchronization
        [cyan]Connection[/cyan]   Server URL and SSL/TLS settings
        [cyan]Sync[/cyan]         Synchronization behavior and timing
        [cyan]LAN Mode[/cyan]     Peer-to-peer sync without central server
        [cyan]Output[/cyan]       Logging verbosity and output destination

    [bold]Examples:[/bold]

        [dim]# Basic sync with a remote server[/dim]
        $ hsync -b photos -s https://server.example.com

        [dim]# Sync a specific local directory[/dim]
        $ hsync -b documents -s https://server.example.com -p ~/Documents

        [dim]# Continuous sync (watch for changes)[/dim]
        $ hsync -b projects -s https://server.example.com -w

        [dim]# Dry run to see what would be synced[/dim]
        $ hsync -b backup -s https://server.example.com -n

        [dim]# LAN mode (no central server, peer-to-peer)[/dim]
        $ hsync -b shared --lan-mode

        [dim]# LAN mode with authentication[/dim]
        $ HSYNC_LAN_KEY=mysecret hsync -b shared --lan-mode

    [bold]Environment Variables:[/bold]

        HSYNC_SERVER    Server URL (alternative to -s)
        HSYNC_API_KEY   API key for authentication
        HSYNC_LAN_KEY   Shared secret for LAN mode authentication

    [bold]Project:[/bold]

        https://github.com/albertobasso/hsync-client

    [bold]Authors:[/bold]

        Alberto Basso
    """
    # Validate numeric ranges
    if http_timeout < HTTP_TIMEOUT_MIN or http_timeout > HTTP_TIMEOUT_MAX:
        raise typer.BadParameter(
            f"HTTP timeout must be between {HTTP_TIMEOUT_MIN} and {HTTP_TIMEOUT_MAX}."
        )
    if resync is not None and (resync < RESYNC_MIN or resync > RESYNC_MAX):
        raise typer.BadParameter(f"Resync interval must be between {RESYNC_MIN} and {RESYNC_MAX}.")
    if reconnect_delay < RECONNECT_DELAY_MIN or reconnect_delay > RECONNECT_DELAY_MAX:
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
    _VALID_SYNC_DIRECTIONS = ("bidirectional", "upload-only", "download-only")
    if sync_direction not in _VALID_SYNC_DIRECTIONS:
        raise typer.BadParameter(
            f"sync-direction must be one of: {', '.join(_VALID_SYNC_DIRECTIONS)}."
        )

    # --force-master is incompatible with directional sync (upload/download-only)
    if force_master and sync_direction != "bidirectional":
        raise typer.BadParameter(
            f"--force-master is incompatible with --sync-direction={sync_direction}. "
            f"The LAN master always operates in bidirectional mode."
        )

    # --force-master and --never-master are mutually exclusive (contradictory)
    if force_master and never_master:
        raise typer.BadParameter(
            "--force-master and --never-master are mutually exclusive: a client "
            "cannot be forced to be master and forbidden from being master at once."
        )
    if (
        min_free_disk_space < MIN_FREE_DISK_SPACE_MB_MIN
        or min_free_disk_space > MIN_FREE_DISK_SPACE_MB_MAX
    ):
        raise typer.BadParameter(
            f"Minimum free disk space must be between "
            f"{MIN_FREE_DISK_SPACE_MB_MIN} and {MIN_FREE_DISK_SPACE_MB_MAX} MB."
        )

    # Parse and validate bandwidth limits
    from hsync_client.utils import parse_bandwidth

    parsed_bw_upload: int | None = None
    parsed_bw_download: int | None = None

    if max_bandwidth_upload is not None:
        try:
            parsed_bw_upload = parse_bandwidth(max_bandwidth_upload)
        except ValueError as e:
            raise typer.BadParameter(f"Invalid --max-bandwidth-upload: {e}")
        if parsed_bw_upload is not None and (
            parsed_bw_upload < BANDWIDTH_MIN or parsed_bw_upload > BANDWIDTH_MAX
        ):
            raise typer.BadParameter(
                f"Upload bandwidth must be between {BANDWIDTH_MIN} and {BANDWIDTH_MAX} bytes/s."
            )

    if max_bandwidth_download is not None:
        try:
            parsed_bw_download = parse_bandwidth(max_bandwidth_download)
        except ValueError as e:
            raise typer.BadParameter(f"Invalid --max-bandwidth-download: {e}")
        if parsed_bw_download is not None and (
            parsed_bw_download < BANDWIDTH_MIN or parsed_bw_download > BANDWIDTH_MAX
        ):
            raise typer.BadParameter(
                f"Download bandwidth must be between {BANDWIDTH_MIN} and {BANDWIDTH_MAX} bytes/s."
            )

    # Import and call the actual main logic
    from hsync_client.main_logic import run_sync

    run_sync(
        bucket=bucket,
        env_file=env_file,
        server_url=server_url,
        verify_ssl=not insecure,
        ca_cert=ca_cert,
        http_timeout=http_timeout,
        api_key_fd=api_key_fd,
        lan_key_fd=lan_key_fd,
        local_path=local_path,
        continuous=continuous,
        resync=resync,
        reconnect_delay=reconnect_delay,
        stability_delay=stability_delay,
        max_wait_timeout=max_wait_timeout,
        filter_path=filter_path,
        dryrun=dryrun,
        ignore_delete=ignore_delete,
        sync_direction=sync_direction,
        compression_enabled=not no_compression,
        max_bandwidth_upload=parsed_bw_upload,
        max_bandwidth_download=parsed_bw_download,
        lan_mode=lan_mode,
        lan_port=lan_port,
        discovery_timeout=discovery_timeout,
        lan_group=lan_group,
        lan_https=lan_https,
        lan_persistent=lan_persistent,
        force_master=force_master,
        never_master=never_master,
        debug=debug,
        quiet=quiet,
        logfile=logfile,
        no_color=no_color,
        legend=legend,
        auto_confirm=yes,
        min_free_disk_space=min_free_disk_space,
        sync_hidden=sync_hidden,
        ignore_patterns=[p.strip() for p in (ignore_patterns or "").split(",") if p.strip()],
    )


def run() -> None:
    """Entry point for the CLI application."""
    app()
