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

from argparse import ArgumentParser, HelpFormatter

from hsync_client.constants import HSYNC_VERSION
from hsync_client.validators import (
    valid_bucket,
    valid_http_timeout,
    valid_max_wait_timeout,
    valid_reconnect_delay,
    valid_resync,
    valid_stability_delay,
)


class SortedHelpFormatter(HelpFormatter):
    """Custom help formatter that sorts arguments alphabetically."""

    def add_arguments(self, actions):
        """Override to sort actions by their option strings."""
        actions = sorted(
            actions,
            key=lambda action: tuple(action.option_strings)
            if action.option_strings
            else (),
        )
        super(SortedHelpFormatter, self).add_arguments(actions)


def create_argument_parser() -> ArgumentParser:
    """
    Create and configure the argument parser for HSYNC client.

    Returns:
        Configured ArgumentParser instance.
    """
    from os import getcwd

    parser = ArgumentParser(
        prog="hsync",
        description="HSYNC - HTTP-based file synchronization client",
        formatter_class=SortedHelpFormatter,
        epilog="Environment: HSYNC_SERVER_URL, HSYNC_API_KEY",
    )

    # -------------------------------------------------------------------------
    # Environment file (processed early, before other args)
    # -------------------------------------------------------------------------
    parser.add_argument(
        "--env-file",
        type=str,
        required=False,
        metavar="PATH",
        dest="env_file",
        help="Load environment variables from file before processing other options.",
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

    # -------------------------------------------------------------------------
    # Connection options
    # -------------------------------------------------------------------------
    parser.add_argument(
        "-s",
        "--server",
        type=str,
        required=False,  # Validated later after env file is loaded
        metavar="URL",
        dest="server_url",
        help="Server URL (or set HSYNC_SERVER).",
    )

    parser.add_argument(
        "--insecure",
        action="store_false",
        dest="verify_ssl",
        help="Skip SSL certificate verification.",
    )

    parser.add_argument(
        "--ca-cert",
        type=str,
        required=False,
        default=None,
        metavar="FILE",
        dest="ca_cert",
        help="Path to CA certificate file for self-signed server certs.",
    )

    parser.add_argument(
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
    parser.add_argument(
        "-p",
        "--path",
        type=str,
        required=False,
        default=getcwd(),
        metavar="DIR",
        dest="local_path",
        help="Local directory path (default: cwd).",
    )

    parser.add_argument(
        "-w",
        "--watch",
        action="store_true",
        dest="continuous",
        help="Watch for changes and sync continuously.",
    )

    parser.add_argument(
        "--order-by",
        type=str,
        required=False,
        default="mtime",
        choices=["mtime", "ctime"],
        metavar="FIELD",
        dest="sync_order",
        help="Compare files by: mtime, ctime (default: mtime).",
    )

    parser.add_argument(
        "-i",
        "--interval",
        type=valid_resync,
        required=False,
        metavar="SEC",
        dest="resync",
        help="Force full sync every SEC seconds (requires -w).",
    )

    parser.add_argument(
        "--retry-delay",
        type=valid_reconnect_delay,
        required=False,
        default=5,
        metavar="SEC",
        dest="reconnect_delay",
        help="Retry connection after SEC seconds (default: 5).",
    )

    parser.add_argument(
        "--stability-delay",
        type=valid_stability_delay,
        required=False,
        default=None,
        metavar="SEC",
        dest="stability_delay",
        help="Seconds to wait before uploading modified files (default: 5). "
             "Can also be set via HSYNC_STABILITY_DELAY env var.",
    )

    parser.add_argument(
        "--max-wait-timeout",
        type=valid_max_wait_timeout,
        required=False,
        default=120,
        metavar="SEC",
        dest="max_wait_timeout",
        help="Maximum time to wait for file stabilization (default: 120s). "
             "Prevents indefinite blocking when files are continuously modified.",
    )

    parser.add_argument(
        "--filter-path",
        type=str,
        required=False,
        default=None,
        metavar="PATH",
        help="Sync only files within this subdirectory path.",
    )

    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        dest="dryrun",
        help="Show what would be done without making changes.",
    )

    # -------------------------------------------------------------------------
    # LAN P2P options
    # -------------------------------------------------------------------------
    parser.add_argument(
        "--lan-mode",
        action="store_true",
        dest="lan_mode",
        help="Enable LAN peer-to-peer mode without central server. "
             "Peers discover each other via mDNS and elect a master.",
    )

    parser.add_argument(
        "--lan-port",
        type=int,
        required=False,
        default=0,
        metavar="PORT",
        dest="lan_port",
        help="Port to use when acting as LAN master (default: random).",
    )

    parser.add_argument(
        "--discovery-timeout",
        type=int,
        required=False,
        default=5,
        metavar="SEC",
        dest="discovery_timeout",
        help="Time to wait for peer discovery in LAN mode (default: 5s).",
    )

    parser.add_argument(
        "--lan-key",
        type=str,
        required=False,
        default=None,
        metavar="KEY",
        dest="lan_key",
        help="Shared secret key for LAN mode authentication. "
             "If not set, no authentication is required (trust LAN). "
             "Can also be set via HSYNC_LAN_KEY environment variable.",
    )

    parser.add_argument(
        "--lan-group",
        type=str,
        required=False,
        default=None,
        metavar="GROUP",
        dest="lan_group",
        help="Group name for LAN peer filtering. "
             "Peers with different groups will ignore each other. "
             "If not set, peers see all others regardless of group. "
             "Can also be set via HSYNC_LAN_GROUP environment variable.",
    )

    parser.add_argument(
        "--no-lan-https",
        action="store_false",
        dest="lan_https",
        default=True,
        help="Disable HTTPS for LAN mode communication. "
             "By default, HTTPS is enabled with a self-signed certificate per session. "
             "Use this flag to use plain HTTP instead.",
    )

    # -------------------------------------------------------------------------
    # Output options
    # -------------------------------------------------------------------------
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        dest="debug",
        help="Enable verbose output.",
    )

    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress all output.",
    )

    parser.add_argument(
        "-o",
        "--output",
        type=str,
        required=False,
        metavar="FILE",
        dest="logfile",
        help="Write output to FILE.",
    )

    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output.",
    )

    parser.add_argument(
        "--help-symbols",
        action="store_true",
        dest="legend",
        help="Show output symbols legend.",
    )

    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {HSYNC_VERSION}",
    )

    return parser

