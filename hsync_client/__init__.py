# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
HSYNC Client - HTTP-based file synchronization client.

This package provides a CLI client for file synchronization
with the HSYNC server, featuring WebSocket event handling,
bidirectional sync, and file system monitoring.
"""

from hsync_common.constants import HSYNC_VERSION

# Read from the version SSOT directly, not ``hsync_client.constants``: this
# module runs for every ``hsync_client.*`` import, and the client constants
# module pulls the whole protocol/pairing chain (~20 ms) that a fast path such as
# ``--version`` must not pay (pinned by tests/test_startup_imports.py).
__version__ = HSYNC_VERSION
