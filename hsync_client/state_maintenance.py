# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Installation state-DB maintenance for HSYNC Client (ADR-0025).

The client prunes its version store at the end of every sync cycle and its
tombstones by TTL, but the freed pages would linger in ``syncstate.db``
without auto-vacuum. This module reclaims them with a throttled
``incremental_vacuum`` at the end of a sync cycle and a full ``VACUUM`` at
graceful shutdown.
"""

from __future__ import annotations

from time import monotonic

from hsync_common.models.folder import default_syncstate_db_path
from hsync_common.models.sqlstore import (
    INCREMENTAL_VACUUM_INTERVAL_S,
    INCREMENTAL_VACUUM_PAGES,
    incremental_vacuum_state_db,
    vacuum_state_db,
)
from loguru import logger as log

__all__ = ["incremental_vacuum_if_due", "vacuum_on_shutdown"]

# Monotonic timestamp of the last incremental vacuum; process-local, so a
# one-shot run reclaims once at its (single) cycle and a continuous run
# reclaims at most once per INCREMENTAL_VACUUM_INTERVAL_S.
_last_incremental: float = 0.0


def incremental_vacuum_if_due() -> None:
    """Reclaim a bounded batch of free pages, at most once per interval."""
    global _last_incremental
    now = monotonic()
    if now - _last_incremental < INCREMENTAL_VACUUM_INTERVAL_S:
        return
    _last_incremental = now
    start = monotonic()
    try:
        freed = incremental_vacuum_state_db(default_syncstate_db_path(), INCREMENTAL_VACUUM_PAGES)
    except Exception:  # pragma: no cover - defensive, DB maintenance must never break a sync
        return
    if freed:
        log.info(
            f"State DB incremental vacuum reclaimed {freed} pages in {monotonic() - start:.3f}s"
        )


def vacuum_on_shutdown() -> None:
    """Fully compact the state DB at process exit (best-effort)."""
    start = monotonic()
    try:
        freed = vacuum_state_db(default_syncstate_db_path())
    except Exception:  # pragma: no cover - defensive
        return
    if freed:
        log.info(
            f"State DB VACUUM (shutdown) completed: reclaimed {freed} pages "
            f"in {monotonic() - start:.3f}s"
        )
