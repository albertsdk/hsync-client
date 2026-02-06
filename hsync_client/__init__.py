# HSYNC Client - HTTP-based file synchronization client
# Copyright (C) 2025 Alberto Basso
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
HSYNC Client - HTTP-based file synchronization client.

This package provides a CLI client for file synchronization
with the HSYNC server, featuring WebSocket event handling,
bidirectional sync, and file system monitoring.
"""

from hsync_client.constants import HSYNC_VERSION

__version__ = HSYNC_VERSION