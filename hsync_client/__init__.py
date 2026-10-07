# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
HSYNC Client - HTTP-based file synchronization client.

This package provides a CLI client for file synchronization
with the HSYNC server, featuring WebSocket event handling,
bidirectional sync, and file system monitoring.
"""

from hsync_client.constants import HSYNC_VERSION

__version__ = HSYNC_VERSION
