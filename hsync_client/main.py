#!/usr/bin/python3
# HSYNC Client - HTTP-based file synchronization client
# Copyright (C) 2025 Alberto Basso <alberto.sdk [at] gmail [dot] com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
HSYNC Client - HTTP-based File Synchronization Client.

This module is the main entry point for the HSYNC client application.
It handles command-line argument parsing and orchestrates the sync process.

Usage:
    hsync -b <bucket> -s <server_url> [options]

Environment Variables:
    HSYNC_SERVER_URL: Server URL
    HSYNC_API_KEY: API key for authentication
"""

import logging
import sys

from asyncio import run
from os.path import basename, exists
from re import sub

from requests import get as http_get, RequestException

from hsync_client.cli import create_argument_parser
from hsync_client.config import (
    get_api_key,
    get_discovery_timeout,
    get_heartbeat_interval,
    get_lan_group,
    get_lan_key,
    get_lan_port,
    get_peer_timeout,
    get_server_url,
    get_stability_delay,
    load_env_file,
    HsyncConfig,
    set_config,
    set_debug,
)
from hsync_client.constants import ACTION_LABEL, FILE_STABILITY_INTERVAL_DEFAULT, HSYNC_VERSION
from hsync_client.lan import LANSync
from hsync_client.lan_server import LANServer
from hsync_client.logger import setup_logging, set_client_id
from hsync_client.sync import sync
from hsync_client.utils import get_cid, is_valid_logfile
from hsync_client.websocket_handler import remote_connect


# Logger instance - initialized in main()
LOG: logging.Logger = logging.getLogger("hsync")


def _get_stability_delay(cli_value: int | None) -> int:
    """
    Determine the stability delay value.

    Priority: CLI argument > environment variable > default constant.

    Args:
        cli_value: Value from command line, or None if not specified.

    Returns:
        The stability delay in seconds.
    """
    if cli_value is not None:
        return cli_value
    env_value = get_stability_delay()
    if env_value is not None:
        try:
            return int(env_value)
        except ValueError:
            pass
    return FILE_STABILITY_INTERVAL_DEFAULT


def main() -> None:
    """
    Main entry point for the HSYNC client.

    Parses command-line arguments, validates configuration, sets up
    the global CONFIG dictionary, and starts either a single sync
    or continuous synchronization mode.
    """
    # Parse command-line arguments
    parser = create_argument_parser()
    args = parser.parse_args()

    # =========================================================================
    # Load Environment File (if specified)
    # =========================================================================

    if args.env_file:
        try:
            load_env_file(args.env_file)
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # =========================================================================
    # Argument Validation
    # =========================================================================

    # Initialize logging early so we can log validation errors
    LOG = setup_logging(
        debug=args.debug,
        quiet=args.quiet,
        logfile=args.logfile if hasattr(args, 'logfile') and args.logfile and is_valid_logfile(args.logfile) else None,
        no_color=args.no_color,
    )

    # Validate local directory exists
    if not exists(args.local_path):
        LOG.error(f"Directory '{args.local_path}' does not exist")
        return

    # Validate log file path
    if args.logfile and not is_valid_logfile(args.logfile):
        LOG.error(f"Invalid output file '{args.logfile}'")
        return

    # =========================================================================
    # Build Configuration
    # =========================================================================

    # Check if LAN mode is enabled
    lan_mode = getattr(args, 'lan_mode', False)

    # Determine server URL (CLI argument takes precedence over env var)
    server_url = args.server_url or get_server_url()

    # Validate server URL is provided (not required in LAN mode)
    if not server_url and not lan_mode:
        LOG.error("Server URL is required (use -s or set HSYNC_SERVER, or use --lan-mode)")
        return

    # In LAN mode without server URL, we'll set a placeholder that will be
    # replaced once we discover the master peer
    if lan_mode and not server_url:
        server_url = "http://localhost:8765"  # Placeholder for LAN mode

    # At this point server_url is guaranteed to be set
    assert server_url is not None

    # Convert HTTP URL to WebSocket URL for events endpoint
    ws_url = sub(r"^http:", "ws:", server_url)
    ws_url = sub(r"^https:", "wss:", ws_url)

    # Generate unique client ID for this session
    cid = get_cid()

    # Set client ID in logging context for correlation
    set_client_id(cid)

    # Populate global configuration dictionary
    config: HsyncConfig = {
        # API endpoints (RESTful paths)
        "API": {
            "buckets": f"{server_url}/api/v1/buckets",
            "events": f"{ws_url}/api/v1/buckets",
        },
        # Unique client identifier for this session
        "CID": cid,
        # Dry-run mode flag
        "DRYRUN": args.dryrun,
        # Local directory path
        "LOCAL_BUCKET": args.local_path,
        # Log file path
        "LOGFILE": args.logfile,
        # SSL verification flag (inverted: verify_ssl=False means NO_VERIFY_SSL=True)
        "NO_VERIFY_SSL": not args.verify_ssl,
        # Custom CA certificate for self-signed server certificates
        "CA_CERT": args.ca_cert,
        # Quiet mode flag
        "QUIET": args.quiet,
        # Remote bucket name
        "REMOTE_BUCKET": args.bucket,
        # HTTP request headers
        "REQUEST_HEADERS": {
            "accept": "application/json",
            "Authorization": f"Bearer {get_api_key()}",
            "X-Client-Name": basename(__file__),
            "X-Client-Version": HSYNC_VERSION,
        },
        # Reconnection delay in seconds
        "RECONNECT_DELAY": args.reconnect_delay,
        # Periodic resync interval in seconds
        "RESYNC": args.resync,
        # Sync ordering (mtime or ctime)
        "SYNC_ORDER": args.sync_order,
        # Filter path for subdirectory sync
        "FILTER_PATH": args.filter_path,
        # File stability delay in seconds (CLI > env > default)
        "STABILITY_DELAY": _get_stability_delay(args.stability_delay),
        # HTTP request timeout in seconds
        "HTTP_TIMEOUT": args.http_timeout,
        # Maximum wait timeout for file stability in seconds
        "MAX_WAIT_TIMEOUT": args.max_wait_timeout,
        # LAN P2P mode settings (CLI > env var > default)
        "LAN_MODE": lan_mode,
        "LAN_PORT": getattr(args, 'lan_port', None) or get_lan_port() or 0,
        "DISCOVERY_TIMEOUT": getattr(args, 'discovery_timeout', None) or get_discovery_timeout() or 5,
        "HEARTBEAT_INTERVAL": get_heartbeat_interval() or 5,
        "PEER_TIMEOUT": get_peer_timeout() or 15,
        "LAN_KEY": getattr(args, 'lan_key', None) or get_lan_key(),
        "LAN_GROUP": getattr(args, 'lan_group', None) or get_lan_group(),
    }

    # Set global configuration and debug flag
    set_config(config)
    set_debug(args.debug)

    # Log configuration in debug mode
    LOG.debug("Local configuration:", extra={'json_data': config})

    # =========================================================================
    # Display Legend (if requested)
    # =========================================================================
    if args.legend:
        LOG.info("Log legend:")
        log_labels = {
            ACTION_LABEL['nothing']: "File already in sync (no action required)",
            ACTION_LABEL['skip']: "File skipped",
            ACTION_LABEL['download']: "Download file from server",
            ACTION_LABEL['upload']: "Upload file to server",
            ACTION_LABEL['move']: "Move/Rename file or directory",
            ACTION_LABEL['remove']: "Remove file or directory",
            ACTION_LABEL['create']: "Create directory",
            ACTION_LABEL['permission']: "Set file/directory permission",
            ACTION_LABEL['symlink']: "Create symbolic link",
        }
        for label, description in log_labels.items():
            LOG.info(f"  {label} {description}")

    # =========================================================================
    # LAN Mode: Peer Discovery and Master Election
    # =========================================================================
    lan_sync = None
    if config["LAN_MODE"]:
        LOG.info("LAN P2P mode enabled")

        # Start LAN discovery
        lan_sync = LANSync(
            cid=config["CID"],
            bucket=config["REMOTE_BUCKET"],
            port=config["LAN_PORT"],
            group=config["LAN_GROUP"],
        )
        peer_manager = lan_sync.start()

        # Wait for peer discovery
        lan_sync.wait_for_discovery(timeout=config["DISCOVERY_TIMEOUT"])

        # Check if we are the master
        lan_server = None
        if peer_manager.is_master():
            LOG.info("This client is the MASTER - acting as sync coordinator")

            # Start embedded HTTP server
            lan_server = LANServer(port=lan_sync.port)
            lan_server.start()

            LOG.info(f"Master server listening on port {lan_sync.port}")
            if config["LAN_KEY"]:
                LOG.info("Authentication enabled (--lan-key)")
            else:
                LOG.info("No authentication (trust LAN mode)")

            # Wait for peers and handle sync requests
            # The server runs in background, we keep the main thread alive
            LOG.info("Press Ctrl+C to stop...")
            try:
                import time
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                LOG.info("Shutting down master server...")
        else:
            # We are a client - connect to the master
            master = peer_manager.get_master()
            if master:
                # Update config with master's URL
                master_url = master.get_server_url()
                LOG.info(f"Connecting to master: {master.cid} at {master_url}")

                # Update API endpoints to point to master
                config["API"]["buckets"] = f"{master_url}/api/v1/buckets"
                ws_master_url = sub(r"^http:", "ws:", master_url)
                config["API"]["events"] = f"{ws_master_url}/api/v1/buckets"

                # Update request headers for LAN authentication
                if config["LAN_KEY"]:
                    config["REQUEST_HEADERS"]["Authorization"] = f"Bearer {config['LAN_KEY']}"

                # Update global config
                set_config(config)

                # Perform sync with master
                run(sync())
            else:
                LOG.warning("No master elected - this client is alone")
                run(sync())

        # Cleanup
        if lan_server:
            lan_server.stop()
        lan_sync.stop()
        return

    # =========================================================================
    # Server Connectivity Check (skip in LAN mode)
    # =========================================================================
    LOG.debug(f"Checking server connectivity: {server_url}")
    try:
        # Use a short timeout for the connectivity check
        check_timeout = min(config["HTTP_TIMEOUT"], 10)
        response = http_get(
            f"{server_url}/api/v1/health",
            timeout=check_timeout,
            verify=not config["NO_VERIFY_SSL"],
        )
        if response.status_code >= 500:
            LOG.error(f"Server error: {server_url} returned status {response.status_code}")
            return
        LOG.debug(f"Server is reachable (status: {response.status_code})")
    except RequestException as e:
        LOG.error(f"Cannot connect to server: {server_url} ({e})")
        return

    # =========================================================================
    # Execute Sync
    # =========================================================================
    if config["DRYRUN"] and args.continuous:
        # Dry-run is incompatible with continuous mode
        LOG.warning("Continuous sync is disabled due to dryrun option set")
    elif not config["DRYRUN"] and args.continuous:
        # Start continuous synchronization with WebSocket events
        run(remote_connect())
    else:
        # Perform single sync operation
        run(sync())


# =============================================================================
# Script Entry Point
# =============================================================================
if __name__ == "__main__":
    main()
