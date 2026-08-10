# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Per-peer tombstone management for HSYNC Client (LAN P2P mode).

Every peer keeps its own persistent :class:`TombstoneStore`, backed by a hidden
side-car file at the local bucket root (``.hsynctombstones``). Because the LAN
master is elected and can change over time, knowledge of deletions must not live
only on the current master: each peer records the deletions it observes, merges
the master's markers on every sync (gossip), and serves the accumulated union if
it later becomes master. This way a deletion is never lost across a mastership
change and an offline peer does not resurrect a file on its next reconciliation.
"""

from hsync_common.constants import HSYNCTOMBSTONES_FILENAME
from hsync_common.models.tombstone import (
    TombstoneEntry,
    TombstoneStore,
    relpath_in_bucket,
)

from hsync_client.config import CONFIG, get_tombstone_ttl_days

# Cache of per-bucket stores, keyed by the bucket directory path. Keying by path
# (not a singleton) keeps tests that switch LOCAL_BUCKET isolated.
_stores: dict[str, TombstoneStore] = {}


def _bucket_dir() -> str:
    """Return the configured local bucket directory."""
    return str(CONFIG.get("LOCAL_BUCKET", "") or "")


def _store_path(bucket_dir: str) -> str:
    """Return the side-car file path for the peer's tombstones."""
    return f"{bucket_dir}/{HSYNCTOMBSTONES_FILENAME}"


def get_tombstone_store() -> TombstoneStore:
    """Return the cached store for the local bucket, loading/pruning on first use."""
    bucket_dir = _bucket_dir()
    store = _stores.get(bucket_dir)
    if store is None:
        store = TombstoneStore(path=_store_path(bucket_dir), ttl_days=get_tombstone_ttl_days())
        store.load()
        store.prune()
        _stores[bucket_dir] = store
    return store


def record_deletion(
    abs_path: str,
    sha256sum: str | None = None,
    timestamp: int | float | None = None,
) -> None:
    """Record a deletion marker for an absolute path and persist the store."""
    bucket_dir = _bucket_dir()
    relpath = relpath_in_bucket(abs_path, bucket_dir)
    if not relpath:
        return
    store = get_tombstone_store()
    store.record(relpath, sha256sum=sha256sum, timestamp=timestamp)
    store.prune()
    store.save()


def record_deletion_by_key(
    key: str,
    sha256sum: str | None = None,
    timestamp: int | float | None = None,
) -> None:
    """Record a deletion marker for an already-hashed path key and persist.

    Used by the sync engine when the only available identity is the merge-base
    key (the store is keyed by ``tombstone_key`` and the raw path is not
    recoverable from it).
    """
    if not key:
        return
    store = get_tombstone_store()
    store.record_by_key(key, sha256sum=sha256sum, timestamp=timestamp)
    store.prune()
    store.save()


def clear_deletion(abs_path: str) -> None:
    """Drop the deletion marker for a re-created path and persist the store."""
    bucket_dir = _bucket_dir()
    relpath = relpath_in_bucket(abs_path, bucket_dir)
    if not relpath:
        return
    store = get_tombstone_store()
    if store.clear(relpath):
        store.save()


def merge_payload(payload: dict[str, TombstoneEntry] | None) -> int:
    """Merge a remote (master) payload using LWW; persist and return change count."""
    store = get_tombstone_store()
    changed = store.merge(payload)
    if changed:
        store.prune()
        store.save()
    return changed


def get_payload() -> dict[str, TombstoneEntry]:
    """Return the JSON-serializable tombstone payload for serving/pushing."""
    return get_tombstone_store().to_payload()
