# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso


"""
HSYNC Client - HTTP-based File Synchronization Client.

This module is the main entry point for the HSYNC client application.
It uses Typer for CLI parsing and delegates to main_logic for execution.

Usage:
    hsync server <hostname> -b <bucket> [options]   (https:// and port 9753 implied)
    hsync p2p -b <bucket> [options]

Environment Variables:
    HSYNC_SERVER_URL: Server URL
    HSYNC_API_KEY: API key for authentication
"""

import multiprocessing


def main() -> None:
    """
    Main entry point for the HSYNC client.

    Diverts ``multiprocessing`` helper re-executions before importing the CLI.
    In a PyInstaller build, ``loguru``'s ``enqueue=True`` sinks make the stdlib
    resource tracker re-run ``sys.executable`` (the frozen ``hsync``) with
    ``-B -c "from multiprocessing.resource_tracker import main; ..."``; without
    this call the child boots Typer and dies with ``No such option: -B``.
    PyInstaller ships the ``pyi_rth_multiprocessing`` hook, which turns this call
    into the helper diversion; outside a frozen build it is a no-op.
    """
    multiprocessing.freeze_support()

    # Imported here, after the helper diversion, so the diverted child never
    # pays for the heavy CLI import.
    from hsync_client.cli import run

    run()


# =============================================================================
# Script Entry Point
# =============================================================================
if __name__ == "__main__":
    main()
