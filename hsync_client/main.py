# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso


"""
HSYNC Client - HTTP-based File Synchronization Client.

This module is the main entry point for the HSYNC client application.
It uses Typer for CLI parsing and delegates to main_logic for execution.

Usage:
    hsync server -b <bucket> -s <server_url> [options]
    hsync lan -b <bucket> [options]

Environment Variables:
    HSYNC_SERVER: Server URL
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
