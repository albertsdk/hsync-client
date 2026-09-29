# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
State-DB maintenance for HSYNC Client (ADR-0025).

The client prunes its version store at the end of every sync cycle and its
tombstones by TTL, but the freed pages would linger in the per-folder DB
without auto-vacuum. This module reclaims them with a throttled
``incremental_vacuum`` at the end of a sync cycle and a full ``VACUUM`` at
graceful shutdown. Only the DBs this process owns are touched — the current
folder's DB and the per-device index (or the single ``HSYNC_STATE_DB`` file when
that override is set) — never a shared installation file.
"""

from __future__ import annotations

from pathlib import Path
from time import monotonic

from hsync_common.models.folder import client_state_index_db_path, state_db_override
from hsync_common.models.sqlstore import (
    INCREMENTAL_VACUUM_INTERVAL_S,
    INCREMENTAL_VACUUM_PAGES,
    incremental_vacuum_state_db,
    vacuum_state_db,
)
from loguru import logger as log

from hsync_client.identity import default_device_dir
from hsync_client.state_backend import bucket_dir, resolve_bucket_folder

__all__ = ["incremental_vacuum_if_due", "vacuum_on_shutdown"]

# Monotonic timestamp of the last incremental vacuum; process-local, so a
# one-shot run reclaims once at its (single) cycle and a continuous run
# reclaims at most once per INCREMENTAL_VACUUM_INTERVAL_S.
_last_incremental: float = 0.0


def _current_state_db_paths() -> list[Path]:
    """Return the DB files this process owns, or ``[]`` when none resolve.

    Per-folder layout: the current bucket's DB plus the per-device index. With
    ``HSYNC_STATE_DB`` set: that single file. Best-effort (maintenance must
    never break a sync), so an empty bucket or a failed resolution yields an
    empty list and the caller skips.
    """
    override = state_db_override()
    if override is not None:
        return [override]
    resolved = resolve_bucket_folder(bucket_dir())
    if resolved is None:
        return []
    _folder_id, db_path = resolved
    return [Path(db_path), client_state_index_db_path(Path(default_device_dir()))]


def incremental_vacuum_if_due() -> None:
    """Reclaim a bounded batch of free pages, at most once per interval."""
    global _last_incremental
    now = monotonic()
    if now - _last_incremental < INCREMENTAL_VACUUM_INTERVAL_S:
        return
    _last_incremental = now
    for path in _current_state_db_paths():
        start = monotonic()
        try:
            freed = incremental_vacuum_state_db(path, INCREMENTAL_VACUUM_PAGES)
        except Exception:  # pragma: no cover - defensive, DB maintenance must never break a sync
            continue
        if freed:
            log.info(
                f"State DB incremental vacuum reclaimed {freed} pages in {monotonic() - start:.3f}s"
            )


def vacuum_on_shutdown() -> None:
    """Fully compact the process's state DBs at exit (best-effort)."""
    for path in _current_state_db_paths():
        start = monotonic()
        try:
            freed = vacuum_state_db(path)
        except Exception:  # pragma: no cover - defensive
            continue
        if freed:
            log.info(
                f"State DB VACUUM (shutdown) completed: reclaimed {freed} pages "
                f"in {monotonic() - start:.3f}s"
            )
