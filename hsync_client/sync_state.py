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
from hsync_common.models.version import SyncStateStore, merge_vclocks, new_version_id, tick_vclock

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
    version_id: str | None = None,
    origin_cid: str | None = None,
    vclock: dict[str, int] | None = None,
) -> None:
    """Record (in memory) the agreed base version for an absolute path.

    Call :func:`flush` to persist. Recording is a no-op for an empty path or
    hash so a failed hash computation never poisons the store.
    """
    relpath = relpath_in_bucket(abs_path, _bucket_dir())
    if not relpath or not base_hash:
        return
    with _lock:
        get_sync_state_store().record(relpath, base_hash, version_id, origin_cid, vclock=vclock)


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


def _local_cid() -> str:
    """Return this node's cid (the author for self-recorded edits)."""
    return str(CONFIG.get("CID", "") or "")


def local_version_for_compare(abs_path: str, current_hash: str | None):
    """Return the *effective* version for comparison without persisting (pure).

    If the current content hash differs from the stored base, a ticked copy is
    returned (clock advanced by this node's cid) — the "would-be" version the
    reconcile would record. Used by dry-run preview so it never mutates the
    store but still computes accurate decisions (ADR-0022 §3.4.7).
    """
    relpath = relpath_in_bucket(abs_path, _bucket_dir())
    if not relpath:
        return None
    with _lock:
        entry = get_sync_state_store().get(relpath)
        if entry is None:
            # No recorded history: a never-synced file with content gets a
            # fresh tick so its provenance is honest.
            if current_hash:
                cid = _local_cid()
                if cid:
                    return {
                        "base_hash": current_hash,
                        "version_id": "",
                        "origin_cid": cid,
                        "ts": 0,
                        "vclock": tick_vclock(None, cid),
                    }
            return None
        if current_hash and entry["base_hash"] != current_hash:
            cid = _local_cid()
            if cid:
                return {
                    "base_hash": current_hash,
                    "version_id": entry["version_id"],
                    "origin_cid": cid,
                    "ts": entry["ts"],
                    "vclock": tick_vclock(entry["vclock"], cid),
                }
        return entry


def note_local_edit(abs_path: str, current_hash: str) -> None:
    """Persist a self-recorded local edit (anti-aliasing invariant, ADR-0022).

    If the current content hash differs from the stored base, the store entry
    is advanced: clock ticked by this node's cid, new version_id, origin_cid
    set to self. Idempotent between the watcher and reconcile (a second call
    with the same hash is a no-op since base_hash now matches).
    """
    relpath = relpath_in_bucket(abs_path, _bucket_dir())
    if not relpath or not current_hash:
        return
    cid = _local_cid()
    if not cid:
        return
    with _lock:
        store = get_sync_state_store()
        entry = store.get(relpath)
        if entry is not None and entry["base_hash"] == current_hash:
            return  # already current — no-op
        store.record(
            relpath,
            base_hash=current_hash,
            vclock=tick_vclock(entry["vclock"] if entry else None, cid),
            version_id=new_version_id(),
            origin_cid=cid,
        )


def get_vclock(abs_path: str) -> dict[str, int] | None:
    """Return the stored vclock for a path, or None if unrecorded."""
    relpath = relpath_in_bucket(abs_path, _bucket_dir())
    if not relpath:
        return None
    with _lock:
        entry = get_sync_state_store().get(relpath)
        return entry["vclock"] if entry else None


def upload_version_fields(abs_path: str, current_hash: str | None) -> tuple[str, str]:
    """Return ``(vclock_json, version_id)`` wire form values for an upload.

    Shared by the full-upload (``transfer.py``) and delta-apply
    (``delta_transfer.py``) paths so both advertise the same effective causal
    version. Returns ``("{}", "")`` when no version is recorded and the content
    is absent (the server treats an empty clock natively).
    """
    from json import dumps

    eff = local_version_for_compare(abs_path, current_hash)
    if eff is not None:
        return dumps(eff["vclock"]), eff.get("version_id", "") or ""
    return "{}", ""


def merge_and_record(abs_path: str, base_hash: str, remote_vclock: dict[str, int] | None) -> None:
    """Record the agreed version with a merged clock (convergence invariant D11).

    The recorded clock is ``merge(local_effective, remote_advertised)``: in
    dominance it equals the dominant clock, in conflict it includes both
    histories so third nodes recognise either side as an ancestor.
    """
    relpath = relpath_in_bucket(abs_path, _bucket_dir())
    if not relpath or not base_hash:
        return
    with _lock:
        store = get_sync_state_store()
        entry = store.get(relpath)
        local_vclock = entry["vclock"] if entry else {}
        merged = merge_vclocks(local_vclock, remote_vclock)
        store.record(
            relpath,
            base_hash=base_hash,
            vclock=merged,
            version_id=entry["version_id"] if entry else new_version_id(),
            origin_cid=entry["origin_cid"] if entry else None,
        )
