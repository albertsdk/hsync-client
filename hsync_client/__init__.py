# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
HSYNC Client - HTTP-based file synchronization client.

This package provides a CLI client for file synchronization
with the HSYNC server, featuring WebSocket event handling,
bidirectional sync, and file system monitoring.
"""

from hsync_client.constants import HSYNC_VERSION

__version__ = HSYNC_VERSION
