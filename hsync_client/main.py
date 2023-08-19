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

from asyncio import run
from os.path import basename, exists
from re import sub

from hsync_client.cli import create_argument_parser
from hsync_client.config import HSYNC_API_KEY, HSYNC_SERVER_URL, set_config, set_debug
from hsync_client.constants import ACTION_LABEL, HSYNC_VERSION
from hsync_client.logger import setup_logging, set_client_id
from hsync_client.sync import sync
from hsync_client.utils import get_cid, is_valid_logfile
from hsync_client.websocket_handler import remote_connect


# Logger instance - initialized in main()
LOG: logging.Logger = logging.getLogger("hsync")


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

    # Determine server URL (CLI argument takes precedence over env var)
    server_url = args.server_url or HSYNC_SERVER_URL

    # Validate server URL is provided
    if not server_url:
        LOG.error("Server URL is required (use -s or set HSYNC_SERVER)")
        return

    # Convert HTTP URL to WebSocket URL for events endpoint
    ws_url = sub(r"^http:", "ws:", server_url)
    ws_url = sub(r"^https:", "wss:", ws_url)

    # Generate unique client ID for this session
    cid = get_cid()

    # Set client ID in logging context for correlation
    set_client_id(cid)

    # Populate global configuration dictionary
    config = {
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
            "Authorization": f"Bearer {HSYNC_API_KEY}",
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
