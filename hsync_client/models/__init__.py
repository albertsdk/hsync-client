# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Data models package for HSYNC Client.

This package contains data classes for file synchronization operations.
"""

from hsync_client.models.dir import Dir, is_hidden_dir
from hsync_client.models.event import Event, EventName
from hsync_client.models.file import File, clear_md5_cache, invalidate_md5_cache, is_hidden_file
from hsync_client.models.sync_action import SyncAction, SyncActionName

__all__ = [
    "Dir",
    "Event",
    "EventName",
    "File",
    "clear_md5_cache",
    "invalidate_md5_cache",
    "is_hidden_dir",
    "is_hidden_file",
    "SyncAction",
    "SyncActionName",
]
