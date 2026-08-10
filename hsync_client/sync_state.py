# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Per-peer sync-state (version) management for HSYNC Client.

Wraps :class:`hsync_common.models.version.SyncStateStore` with one persisted
store per local bucket, backed by a hidden ``.hsyncstate`` side-car at the bucket
root. The store records, per path, the content hash this peer last agreed on with
the master (``base_hash``) plus its version lineage. Comparing local, remote and
base lets reconciliation pick the right direction — or flag a true conflict —
without trusting wall-clock timestamps.

Writes are serialized with a re-entrant lock because transfers record their
synced base from the parallel transfer thread pool; persistence is decoupled
from mutation so a full sync flushes once instead of per file.
"""

import threading

from hsync_common.constants import HSYNCSTATE_FILENAME
from hsync_common.models.tombstone import relpath_in_bucket
from hsync_common.models.version import SyncStateStore

from hsync_client.config import CONFIG

# Cache of per-bucket stores, keyed by the bucket directory path. Keying by path
# (not a singleton) keeps tests that switch LOCAL_BUCKET isolated.
_stores: dict[str, SyncStateStore] = {}

# Re-entrant: the public helpers call get_sync_state_store() while holding it.
_lock = threading.RLock()


def _bucket_dir() -> str:
    """Return the configured local bucket directory."""
    return str(CONFIG.get("LOCAL_BUCKET", "") or "")


def _store_path(bucket_dir: str) -> str:
    """Return the side-car file path for the peer's sync-state."""
    return f"{bucket_dir}/{HSYNCSTATE_FILENAME}"


def get_sync_state_store() -> SyncStateStore:
    """Return the cached store for the local bucket, loading on first use."""
    with _lock:
        bucket_dir = _bucket_dir()
        store = _stores.get(bucket_dir)
        if store is None:
            store = SyncStateStore(path=_store_path(bucket_dir))
            store.load()
            _stores[bucket_dir] = store
        return store


def get_base_hash(abs_path: str) -> str | None:
    """Return the recorded common-ancestor hash for an absolute path, if any."""
    relpath = relpath_in_bucket(abs_path, _bucket_dir())
    if not relpath:
        return None
    with _lock:
        return get_sync_state_store().base_hash(relpath)


def record_base(
    abs_path: str,
    base_hash: str,
    change_id: str | None = None,
    origin_cid: str | None = None,
) -> None:
    """Record (in memory) the agreed base version for an absolute path.

    Call :func:`flush` to persist. Recording is a no-op for an empty path or
    hash so a failed hash computation never poisons the store.
    """
    relpath = relpath_in_bucket(abs_path, _bucket_dir())
    if not relpath or not base_hash:
        return
    with _lock:
        get_sync_state_store().record(relpath, base_hash, change_id, origin_cid)


def clear_base(abs_path: str) -> None:
    """Drop the recorded version for an absolute path (e.g. on deletion)."""
    relpath = relpath_in_bucket(abs_path, _bucket_dir())
    if not relpath:
        return
    with _lock:
        get_sync_state_store().clear(relpath)


def retain(abs_paths: list[str]) -> int:
    """Drop entries for paths not present in ``abs_paths``; return drop count."""
    bucket_dir = _bucket_dir()
    relpaths = [rp for rp in (relpath_in_bucket(p, bucket_dir) for p in abs_paths) if rp]
    with _lock:
        return get_sync_state_store().retain(relpaths)


def iter_merge_bases():
    """Yield ``(relpath, base_hash)`` for every recorded merge-base entry.

    Used by the sync engine to detect files that were deleted locally between
    one-shot sync cycles (the entry exists in the store but the file is no
    longer listed by get_files_local).

    Note: the store keys are hashed (``tombstone_key``), so the raw relative
    path is not recoverable from the key alone. The store is scanned against
    the current local listing by the caller; here we yield the *key* and the
    caller compares keys, not paths.
    """
    with _lock:
        for key, version in get_sync_state_store().iter_entries():
            yield key, version["base_hash"]


def flush() -> None:
    """Persist the local bucket's sync-state to its side-car file."""
    with _lock:
        get_sync_state_store().save()
