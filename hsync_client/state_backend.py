# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Resolve the installation SQLite backend for a local bucket (ADR-0025).

Shared by :mod:`hsync_client.sync_state` and :mod:`hsync_client.tombstones`
so both stores key the same ``syncstate.db`` by the same folder UUID.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from hsync_common.models.folder import (
    default_syncstate_db_path,
    ensure_folder_marker,
    read_folder_marker,
    write_folder_marker,
)
from hsync_common.models.sqlstore import get_state_db

__all__ = ["resolve_bucket_folder"]


def _realpath(path: str | Path) -> str:
    """Best-effort canonical path; fall back to the given string."""
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(path)


def resolve_bucket_folder(bucket_dir: str) -> tuple[str, str] | None:
    """Return ``(folder_id, db_path)`` for *bucket_dir*, or ``None`` if unset.

    Creates ``.hsyncfolder`` on first use. A duplicated marker (copy of a
    still-reachable folder) gets a new UUID. A missing marker whose
    ``canonical_path`` still matches is re-anchored. Consumers:
    ``sync_state`` / ``tombstones``.
    """
    if not bucket_dir:
        return None
    current = _realpath(bucket_dir)
    db_path = str(default_syncstate_db_path())
    db = get_state_db(db_path)
    marker = read_folder_marker(bucket_dir)
    if marker is None:
        existing = db.find_folder_by_path(current)
        if existing is not None:
            write_folder_marker(bucket_dir, existing)
            db.ensure_folder(existing, current)
            return existing, db_path
        folder_id = ensure_folder_marker(bucket_dir)
        db.ensure_folder(folder_id, current)
        return folder_id, db_path
    stored = db.folder_canonical_path(marker)
    if stored is None:
        db.ensure_folder(marker, current)
        return marker, db_path
    stored_real = _realpath(stored)
    if stored_real == current or os.path.normpath(stored) == os.path.normpath(current):
        db.ensure_folder(marker, current)
        return marker, db_path
    if stored_real != current and os.path.isdir(stored_real):
        other_marker = read_folder_marker(stored_real)
        if other_marker == marker:
            new_id = write_folder_marker(bucket_dir, str(uuid.uuid4()))
            db.ensure_folder(new_id, current)
            return new_id, db_path
    db.ensure_folder(marker, current)
    return marker, db_path
