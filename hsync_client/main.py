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
It uses Typer for CLI parsing and delegates to main_logic for execution.

Usage:
    hsync -b <bucket> -s <server_url> [options]

Environment Variables:
    HSYNC_SERVER_URL: Server URL
    HSYNC_API_KEY: API key for authentication
"""

from hsync_client.cli import run


def main() -> None:
    """
    Main entry point for the HSYNC client.

    Invokes the Typer CLI application.
    """
    run()


# =============================================================================
# Script Entry Point
# =============================================================================
if __name__ == "__main__":
    main()
