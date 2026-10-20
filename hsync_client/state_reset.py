# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Forget the local sync state of one synced folder (``hsync state reset``).

The per-folder state DB holds the merge bases (``versions``) and the deletion
markers (``tombstones``). Dropping them makes the next cycle reconcile every
file from scratch: no base means no causal proof, so the cycle never deletes
anything (deletions require a dominating tombstone clock) — the worst case is
bandwidth and CPU, not data loss.

The folder lives under a cross-process :class:`InstanceLock` while the reset
runs, so a live sync on the same directory makes the command refuse instead of
clearing state a running process keeps writing.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from hsync_common.models.folder import (
    client_state_db_path,
    client_state_index_db_path,
    read_folder_marker,
    state_db_override,
)
from hsync_common.models.sqlstore import StateDb, get_state_db, reset_state_db_cache

from hsync_client.identity import InstanceLock, default_device_dir

__all__ = ["ResetReport", "reset_folder_state", "resolve_state_db"]


@dataclass(frozen=True)
class ResetReport:
    """Outcome of one ``hsync state reset`` run.

    ``found`` is False when the folder has no state yet (never synced, or the
    DB was already removed): the command is then a successful no-op.
    ``quarantined`` carries the new path when an unreadable DB was moved aside.
    """

    folder: str
    folder_id: str = ""
    db_path: str = ""
    found: bool = False
    versions_deleted: int = 0
    tombstones_deleted: int = 0
    quarantined: str = ""


def _canonical(path: str | Path) -> str:
    """Best-effort canonical path; fall back to the given string."""
    try:
        return os.path.realpath(str(path))
    except OSError:
        return str(path)


def _index_db() -> StateDb | None:
    """Return the folder-index DB the engine would consult, or None if absent.

    With ``HSYNC_STATE_DB`` the engine resolves every folder from that single
    file, so a marker-less re-anchor must search it too — looking at ``index.db``
    would report "nothing to reset" for state that lives in the shared DB.
    Guarded by ``exists()`` because opening a missing DB would create it.
    """
    override = state_db_override()
    path = (
        Path(override)
        if override is not None
        else client_state_index_db_path(Path(default_device_dir()))
    )
    return get_state_db(path) if path.exists() else None


def _folder_id_for(folder: Path) -> str | None:
    """Resolve the folder UUID the way the sync engine would, without writing.

    Mirrors the read-only decisions of ``state_backend.resolve_bucket_folder``:
    a missing marker re-anchors through the per-device index; a *duplicated*
    marker (a copy of a folder that is still reachable) yields ``None`` because
    the engine would mint a fresh UUID for the copy, so the copy has no state to
    forget — and resetting the shared UUID would clobber the original folder.
    Unlike the engine, no marker is created and no index row is written.
    """
    current = _canonical(folder)
    marker = read_folder_marker(current)
    index = _index_db()

    if marker is None:
        return None if index is None else index.find_folder_by_path(current)

    stored = None if index is None else index.folder_canonical_path(marker)
    if stored is None:
        return marker
    stored_real = _canonical(stored)
    if stored_real == current or os.path.normpath(stored) == os.path.normpath(current):
        return marker
    if os.path.isdir(stored_real) and read_folder_marker(stored_real) == marker:
        return None
    return marker


def resolve_state_db(folder: Path) -> tuple[str, Path] | None:
    """Return ``(folder_id, db_path)`` for *folder*, or None when it has no state."""
    folder_id = _folder_id_for(folder)
    if folder_id is None:
        return None
    override = state_db_override()
    db_path = (
        Path(override)
        if override is not None
        else client_state_db_path(folder_id, Path(default_device_dir()))
    )
    if not db_path.exists():
        return None
    return folder_id, db_path


def _quarantine(db_path: Path) -> Path:
    """Move an unreadable DB (and its WAL sidecars) aside; return the new path."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    target = db_path.with_name(f"{db_path.name}.corrupt-{stamp}")
    for suffix in ("", "-wal", "-shm", "-journal"):
        source = Path(f"{db_path}{suffix}")
        if source.exists():
            os.replace(source, Path(f"{target}{suffix}"))
    reset_state_db_cache()
    return target


def reset_folder_state(folder: str | Path, *, quarantine: bool = False) -> ResetReport:
    """Forget *folder*'s merge bases and deletion markers.

    Raises:
        InstanceLockError: another hsync process is syncing the same directory.
        sqlite3.DatabaseError: the state DB is unreadable and *quarantine* is off.
        OSError: the DB could not be opened, written or moved aside.
    """
    path = Path(folder)
    canonical = _canonical(path)
    resolved = resolve_state_db(path)
    if resolved is None:
        return ResetReport(folder=canonical)

    folder_id, db_path = resolved
    lock = InstanceLock.acquire(canonical)
    try:
        try:
            versions, tombstones = get_state_db(db_path).reset_folder(folder_id)
        except sqlite3.DatabaseError:
            if not quarantine:
                raise
            return ResetReport(
                folder=canonical,
                folder_id=folder_id,
                db_path=str(db_path),
                found=True,
                quarantined=str(_quarantine(db_path)),
            )
    finally:
        lock.release()

    return ResetReport(
        folder=canonical,
        folder_id=folder_id,
        db_path=str(db_path),
        found=True,
        versions_deleted=versions,
        tombstones_deleted=tombstones,
    )
