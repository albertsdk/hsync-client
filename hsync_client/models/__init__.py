# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Data models package for HSYNC Client.

Re-exports all models from hsync_common.models for backward compatibility.
"""

from hsync_common.models import (  # noqa: F401
    Dir,
    Event,
    EventDict,
    EventName,
    File,
    FileDict,
    Symlink,
    SyncAction,
    SyncActionName,
    clear_md5_cache,
    invalidate_md5_cache,
    is_hidden_dir,
    is_hidden_file,
)

__all__ = [
    "Dir",
    "Event",
    "EventDict",
    "EventName",
    "File",
    "FileDict",
    "SyncAction",
    "SyncActionName",
    "Symlink",
    "clear_md5_cache",
    "invalidate_md5_cache",
    "is_hidden_dir",
    "is_hidden_file",
]
