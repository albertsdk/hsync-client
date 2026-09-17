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

from hsync_client.config import CONFIG

__all__ = [
    "bucket_dir",
    "local_cid",
    "reset_bucket_folder_cache",
    "resolve_bucket_folder",
]

# Cache of ``(folder_id, db_path)`` resolutions, keyed by the raw ``bucket_dir``
# string. The folder id is deterministic for a given bucket path + installation
# DB for the life of the process: move/copy/delete-marker re-anchoring is
# resolved at first sighting (see ``resolve_bucket_folder``), and changing the
# bucket path (move/copy) means a new ``bucket_dir`` string, hence a new key.
# ``sync_state`` / ``tombstones`` call this resolver on every per-file operation,
# so the cache-hit path must stay a bare dict lookup: keying on the raw string
# avoids the ``Path.resolve()`` syscall and the ``HSYNC_STATE_DIR`` env read that
# would otherwise re-run per call (L1 benchmark: ~0.055 ms → ~0.012 ms on the
# already-cached path; the residual was that per-call key computation).
_folder_cache: dict[str, tuple[str, str]] = {}


def reset_bucket_folder_cache() -> None:
    """Drop every cached folder resolution (tests; folder re-discovery)."""
    _folder_cache.clear()


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
    ``canonical_path`` still matches is re-anchored.

    The result is memoized per raw ``bucket_dir`` string: it is deterministic for
    the life of the process, so the steady-state per-file call pays a bare dict
    lookup instead of a ``.hsyncfolder`` read + two SQLite statements + the
    ``Path.resolve()``/env-read key computation (see :data:`_folder_cache`).
    Consumers: ``sync_state`` / ``tombstones``.
    """
    if not bucket_dir:
        return None
    cached = _folder_cache.get(bucket_dir)
    if cached is not None:
        return cached

    current = _realpath(bucket_dir)
    db_path = str(default_syncstate_db_path())
    db = get_state_db(db_path)
    marker = read_folder_marker(bucket_dir)
    if marker is None:
        existing = db.find_folder_by_path(current)
        if existing is not None:
            folder_id = existing
            write_folder_marker(bucket_dir, existing)
        else:
            folder_id = ensure_folder_marker(bucket_dir)
        db.ensure_folder(folder_id, current)
    else:
        stored = db.folder_canonical_path(marker)
        if stored is None:
            folder_id = marker
        else:
            stored_real = _realpath(stored)
            if stored_real == current or os.path.normpath(stored) == os.path.normpath(current):
                folder_id = marker
            elif stored_real != current and os.path.isdir(stored_real):
                other_marker = read_folder_marker(stored_real)
                if other_marker == marker:
                    folder_id = write_folder_marker(bucket_dir, str(uuid.uuid4()))
                else:
                    folder_id = marker
            else:
                folder_id = marker
        db.ensure_folder(folder_id, current)

    result = (folder_id, db_path)
    _folder_cache[bucket_dir] = result
    return result


def bucket_dir() -> str:
    """Return the configured local bucket directory (empty when unset).

    Shared by :mod:`hsync_client.sync_state`, :mod:`hsync_client.tombstones`
    and :mod:`hsync_client.arbitration` so the bucket resolution has a single
    definition.
    """
    return str(CONFIG.get("LOCAL_BUCKET", "") or "")


def local_cid() -> str:
    """Return this node's cid (empty string when unconfigured).

    Shared by :mod:`hsync_client.sync_state`, :mod:`hsync_client.arbitration`
    and :mod:`hsync_client.sync` as the author identity for self-recorded edits
    and minted deletion markers.
    """
    return str(CONFIG.get("CID", "") or "")
