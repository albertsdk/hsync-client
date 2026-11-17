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
import sys


def _answer_display_flag() -> bool:
    """Answer the eager global display flags before the CLI is imported.

    ``--version``/``-V`` and ``--help-symbols`` are eager parent-level callbacks:
    Typer prints and exits during parsing, before any command runs, so they can be
    served here for a fraction of the cost. Every real command still goes through
    Typer unchanged.

    Only a *leading* flag is intercepted. ``hsync server --version`` and every
    other argument combination keep their existing Typer semantics (no token
    after the flag is inspected), so this can only replace a path Typer would
    have resolved to the same output.

    Returns ``True`` when it answered the flag and the caller must stop.
    """
    argv = sys.argv[1:]
    if not argv:
        return False

    if argv[0] in ("-V", "--version"):
        from hsync_common.constants import HSYNC_VERSION

        print(f"hsync {HSYNC_VERSION}")
        return True

    if argv[0] == "--help-symbols":
        from hsync_client.log_labels import legend_lines

        for line in legend_lines():
            print(line)
        return True

    return False


def _force_utf8_streams() -> None:
    """Make the standard streams UTF-8 so redirected output never raises.

    On Windows a redirected ``stdout``/``stderr`` uses the ANSI code page while
    the log and the ``--help-symbols`` legend contain non-ASCII glyphs;
    ``errors="replace"`` keeps one bad glyph from aborting a run. Real consoles
    already speak UTF-8 and are unaffected, and streams without ``reconfigure``
    (pytest capture, a windowed build's ``sys.stdout``) are left alone.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


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
    # Before the eager flags: --help-symbols prints glyphs before logging exists.
    _force_utf8_streams()

    if _answer_display_flag():
        return

    # Imported here, after the helper diversion, so the diverted child never
    # pays for the heavy CLI import.
    from hsync_client.cli import run

    run()


# =============================================================================
# Script Entry Point
# =============================================================================
if __name__ == "__main__":
    main()
