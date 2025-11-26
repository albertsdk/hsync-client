# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Per-peer tombstone management for HSYNC Client (LAN P2P mode).

Every peer keeps its own persistent :class:`TombstoneStore`, backed by the
installation ``syncstate.db`` and keyed by the ``.hsyncfolder`` UUID
(ADR-0025). Because the LAN master is elected and can change over time,
knowledge of deletions must not live only on the current master: each peer
records the deletions it observes, merges the master's markers on every sync
(gossip), and serves the accumulated union if it later becomes master. This
way a deletion is never lost across a mastership change and an offline peer
does not resurrect a file on its next reconciliation.

Writes are serialized with a re-entrant lock because the sync engine records
deletions from the parallel transfer thread pool; persistence is decoupled from
mutation so a full sync flushes once instead of per file.
"""

import threading

from hsync_common.models.tombstone import (
    TombstoneEntry,
    TombstoneStore,
    relpath_in_bucket,
)

from hsync_client.config import CONFIG, get_tombstone_ttl_days
from hsync_client.state_backend import resolve_bucket_folder

# Cache of per-folder stores, keyed by folder UUID.
_stores: dict[str, TombstoneStore] = {}

# Re-entrant: the public helpers call get_tombstone_store() while holding it.
_lock = threading.RLock()


def _bucket_dir() -> str:
    """Return the configured local bucket directory."""
    return str(CONFIG.get("LOCAL_BUCKET", "") or "")


def get_tombstone_store() -> TombstoneStore:
    """Return the cached store for the local bucket, loading/pruning on first use."""
    with _lock:
        resolved = resolve_bucket_folder(_bucket_dir())
        if resolved is None:
            return TombstoneStore(ttl_days=get_tombstone_ttl_days())
        folder_id, db_path = resolved
        store = _stores.get(folder_id)
        if store is None:
            store = TombstoneStore(
                db_path=db_path,
                folder_id=folder_id,
                ttl_days=get_tombstone_ttl_days(),
                canonical_path=_bucket_dir(),
            )
            store.load()
            store.prune()
            _stores[folder_id] = store
        return store


def record_deletion(
    abs_path: str,
    sha256sum: str | None = None,
    origin_cid: str | None = None,
    prev_vclock: dict[str, int] | None = None,
    timestamp: int | float | None = None,
) -> None:
    """Record a causal deletion marker for an absolute path and persist it.

    The marker inherits the stored version clock for the path (``prev_vclock``)
    and is ticked by ``origin_cid``, so delete-vs-recreate and delete-vs-delete
    resolve causally (ADR-0003) — the deletion timestamp is informational only.
    """
    bucket_dir = _bucket_dir()
    relpath = relpath_in_bucket(abs_path, bucket_dir)
    if not relpath:
        return
    with _lock:
        store = get_tombstone_store()
        store.record(
            relpath,
            sha256sum=sha256sum,
            origin_cid=origin_cid,
            prev_vclock=prev_vclock,
            timestamp=timestamp,
        )
        store.prune()
        store.save()


def record_deletion_by_key(
    key: str,
    sha256sum: str | None = None,
    origin_cid: str | None = None,
    prev_vclock: dict[str, int] | None = None,
    timestamp: int | float | None = None,
) -> None:
    """Record a causal deletion marker for an already-hashed path key and persist.

    Used by the sync engine when the only available identity is the merge-base
    key (the store is keyed by ``tombstone_key`` and the raw path is not
    recoverable from it). ``origin_cid``/``prev_vclock`` follow
    :func:`record_deletion`; when only the key is known the clock is ticked by
    the local cid so the marker still carries fresh causal provenance.
    """
    if not key:
        return
    with _lock:
        store = get_tombstone_store()
        store.record_by_key(
            key,
            sha256sum=sha256sum,
            origin_cid=origin_cid,
            prev_vclock=prev_vclock,
            timestamp=timestamp,
        )
        store.prune()
        store.save()


def clear_deletion(abs_path: str) -> None:
    """Drop the deletion marker for a re-created path and persist the store."""
    bucket_dir = _bucket_dir()
    relpath = relpath_in_bucket(abs_path, bucket_dir)
    if not relpath:
        return
    with _lock:
        store = get_tombstone_store()
        if store.clear(relpath):
            store.save()


def merge_payload(payload: dict[str, TombstoneEntry] | None) -> int:
    """Merge a remote (master) payload causally; persist and return change count."""
    with _lock:
        store = get_tombstone_store()
        changed = store.merge(payload)
        if changed:
            store.prune()
            store.save()
        return changed


def get_payload() -> dict[str, TombstoneEntry]:
    """Return the JSON-serializable tombstone payload for serving/pushing."""
    with _lock:
        return get_tombstone_store().to_payload()
