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
Command-line interface for HSYNC Client.

This module provides the argument parser and related utilities for
the HSYNC command-line interface.
"""

from argparse import ArgumentParser

from hsync_client.constants import HSYNC_VERSION
from hsync_client.validators import (
    valid_bucket,
    valid_http_timeout,
    valid_max_wait_timeout,
    valid_reconnect_delay,
    valid_resync,
    valid_stability_delay,
)


def create_argument_parser() -> ArgumentParser:
    """
    Create and configure the argument parser for HSYNC client.

    Returns:
        Configured ArgumentParser instance.
    """
    from os import getcwd
    from argparse import RawDescriptionHelpFormatter

    # Custom formatter that combines sorting with raw description
    class CustomHelpFormatter(RawDescriptionHelpFormatter):
        def add_arguments(self, actions):
            actions = sorted(
                actions,
                key=lambda action: tuple(action.option_strings)
                if action.option_strings
                else (),
            )
            super().add_arguments(actions)

    epilog = """
Examples:
  # Basic sync with a remote server
  hsync -b photos -s https://server.example.com

  # Sync a specific local directory
  hsync -b documents -s https://server.example.com -p ~/Documents

  # Continuous sync (watch for changes)
  hsync -b projects -s https://server.example.com -w

  # Dry run to see what would be synced
  hsync -b backup -s https://server.example.com -n

  # LAN mode (no central server, peer-to-peer)
  hsync -b shared --lan-mode

  # LAN mode with authentication
  hsync -b shared --lan-mode --lan-key mysecret

Environment variables:
  HSYNC_SERVER      Server URL (alternative to -s)
  HSYNC_API_KEY     API key for authentication
"""

    parser = ArgumentParser(
        prog="hsync",
        description="HSYNC - HTTP-based file synchronization client",
        formatter_class=CustomHelpFormatter,
        epilog=epilog,
    )

    # -------------------------------------------------------------------------
    # Required arguments
    # -------------------------------------------------------------------------
    parser.add_argument(
        "-b",
        "--bucket",
        type=valid_bucket,
        required=True,
        metavar="NAME",
        help="Remote bucket name.",
    )

    parser.add_argument(
        "--env-file",
        type=str,
        required=False,
        metavar="PATH",
        dest="env_file",
        help="Load environment variables from file.",
    )

    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {HSYNC_VERSION}",
    )

    # -------------------------------------------------------------------------
    # Connection options
    # -------------------------------------------------------------------------
    conn_group = parser.add_argument_group("Connection")

    conn_group.add_argument(
        "-s",
        "--server",
        type=str,
        required=False,
        metavar="URL",
        dest="server_url",
        help="Server URL (or set HSYNC_SERVER).",
    )

    conn_group.add_argument(
        "--insecure",
        action="store_false",
        dest="verify_ssl",
        help="Skip SSL certificate verification.",
    )

    conn_group.add_argument(
        "--ca-cert",
        type=str,
        required=False,
        default=None,
        metavar="FILE",
        dest="ca_cert",
        help="Path to CA certificate file for self-signed server certs.",
    )

    conn_group.add_argument(
        "--timeout",
        type=valid_http_timeout,
        required=False,
        default=30,
        metavar="SEC",
        dest="http_timeout",
        help="HTTP request timeout in seconds (default: 30).",
    )

    # -------------------------------------------------------------------------
    # Sync options
    # -------------------------------------------------------------------------
    sync_group = parser.add_argument_group("Sync")

    sync_group.add_argument(
        "-p",
        "--path",
        type=str,
        required=False,
        default=getcwd(),
        metavar="DIR",
        dest="local_path",
        help="Local directory path (default: cwd).",
    )

    sync_group.add_argument(
        "-w",
        "--watch",
        action="store_true",
        dest="continuous",
        help="Watch for changes and sync continuously.",
    )

    sync_group.add_argument(
        "--compare-by",
        type=str,
        required=False,
        default="mtime",
        choices=["mtime", "ctime"],
        metavar="FIELD",
        dest="sync_order",
        help="Compare files by: mtime, ctime (default: mtime).",
    )

    sync_group.add_argument(
        "-i",
        "--interval",
        type=valid_resync,
        required=False,
        metavar="SEC",
        dest="resync",
        help="Force full sync every SEC seconds (requires -w).",
    )

    sync_group.add_argument(
        "--retry-delay",
        type=valid_reconnect_delay,
        required=False,
        default=5,
        metavar="SEC",
        dest="reconnect_delay",
        help="Retry connection after SEC seconds (default: 5).",
    )

    sync_group.add_argument(
        "--stability-delay",
        type=valid_stability_delay,
        required=False,
        default=None,
        metavar="SEC",
        dest="stability_delay",
        help="Seconds to wait before uploading modified files (default: 5).",
    )

    sync_group.add_argument(
        "--max-wait-timeout",
        type=valid_max_wait_timeout,
        required=False,
        default=120,
        metavar="SEC",
        dest="max_wait_timeout",
        help="Maximum time to wait for file stabilization (default: 120s).",
    )

    sync_group.add_argument(
        "--filter-path",
        type=str,
        required=False,
        default=None,
        metavar="PATH",
        help="Sync only files within this subdirectory path.",
    )

    sync_group.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        dest="dryrun",
        help="Show what would be done without making changes.",
    )

    # -------------------------------------------------------------------------
    # LAN P2P options
    # -------------------------------------------------------------------------
    lan_group = parser.add_argument_group("LAN Mode")

    lan_group.add_argument(
        "--lan-mode",
        action="store_true",
        dest="lan_mode",
        help="Enable LAN peer-to-peer mode without central server.",
    )

    lan_group.add_argument(
        "--lan-port",
        type=int,
        required=False,
        default=0,
        metavar="PORT",
        dest="lan_port",
        help="Port to use when acting as LAN master (default: random).",
    )

    lan_group.add_argument(
        "--discovery-timeout",
        type=int,
        required=False,
        default=10,
        metavar="SEC",
        dest="discovery_timeout",
        help="Time to wait for peer discovery (default: 10s).",
    )

    lan_group.add_argument(
        "--lan-key",
        type=str,
        required=False,
        default=None,
        metavar="KEY",
        dest="lan_key",
        help="Shared secret key for LAN authentication.",
    )

    lan_group.add_argument(
        "--lan-group",
        type=str,
        required=False,
        default=None,
        metavar="GROUP",
        dest="lan_group",
        help="Group name for LAN peer filtering.",
    )

    lan_group.add_argument(
        "--no-lan-https",
        action="store_false",
        dest="lan_https",
        default=True,
        help="Disable HTTPS for LAN mode (use plain HTTP).",
    )

    # -------------------------------------------------------------------------
    # Output options
    # -------------------------------------------------------------------------
    output_group = parser.add_argument_group("Output")

    output_group.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        dest="debug",
        help="Enable verbose output.",
    )

    output_group.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress all output.",
    )

    output_group.add_argument(
        "-o",
        "--output",
        type=str,
        required=False,
        metavar="FILE",
        dest="logfile",
        help="Write output to FILE.",
    )

    output_group.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output.",
    )

    output_group.add_argument(
        "--help-symbols",
        action="store_true",
        dest="legend",
        help="Show output symbols legend.",
    )

    return parser

