# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Per-peer sync-state (version) management for HSYNC Client.

Wraps :class:`hsync_common.models.version.SyncStateStore` with one persisted
store per local bucket, backed by the installation ``state DB`` and keyed
by the ``.hsyncfolder`` UUID (ADR-0025). The store records, per path, the
content hash this peer last agreed on with the master (``base_hash``) plus its
version lineage. Comparing local, remote and base lets reconciliation pick the
right direction — or flag a true conflict — without trusting wall-clock
timestamps.

Writes are serialized with a re-entrant lock because transfers record their
synced base from the parallel transfer thread pool; persistence is decoupled
from mutation so a full sync flushes once instead of per file.
"""

import json
import os
import threading
from pathlib import Path

from hsync_common.clock import correct_mtime
from hsync_common.constants import MTIME_PROPAGATION_TOLERANCE_SECONDS
from hsync_common.models.file import File
from hsync_common.models.tombstone import relpath_in_bucket
from hsync_common.models.version import SyncStateStore, merge_vclocks, new_version_id, tick_vclock
from loguru import logger as log

from hsync_client.state_backend import bucket_dir, local_cid, resolve_bucket_folder

# Cache of per-folder stores, keyed by folder UUID. Tests that switch
# LOCAL_BUCKET stay isolated because a new directory gets a new marker.
_stores: dict[str, SyncStateStore] = {}

# Re-entrant: the public helpers call get_sync_state_store() while holding it.
_lock = threading.RLock()


def get_sync_state_store() -> SyncStateStore:
    """Return the cached store for the local bucket, loading on first use."""
    with _lock:
        resolved = resolve_bucket_folder(bucket_dir())
        if resolved is None:
            return SyncStateStore()
        folder_id, db_path = resolved
        store = _stores.get(folder_id)
        if store is None:
            store = SyncStateStore(
                db_path=db_path,
                folder_id=folder_id,
                canonical_path=bucket_dir(),
            )
            store.load()
            _stores[folder_id] = store
        return store


def get_base_hash(abs_path: str) -> str | None:
    """Return the recorded common-ancestor hash for an absolute path, if any."""
    relpath = relpath_in_bucket(abs_path, bucket_dir())
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
    corrected_mtime: int | None = None,
) -> None:
    """Record (in memory) the agreed base version for an absolute path.

    Call :func:`flush` to persist. Recording is a no-op for an empty path or
    hash so a failed hash computation never poisons the store. When
    ``corrected_mtime`` is ``None``/``0`` it falls back to the corrected mtime
    of the file currently on disk.
    """
    relpath = relpath_in_bucket(abs_path, bucket_dir())
    if not relpath or not base_hash:
        return
    resolved_corrected = corrected_mtime
    if not resolved_corrected:
        resolved_corrected = _corrected_mtime_of(abs_path)
    with _lock:
        get_sync_state_store().record(
            relpath,
            base_hash,
            version_id,
            origin_cid,
            vclock=vclock,
            corrected_mtime=resolved_corrected or 0,
        )


def clear_base(abs_path: str) -> None:
    """Drop the recorded version for an absolute path (e.g. on deletion)."""
    relpath = relpath_in_bucket(abs_path, bucket_dir())
    if not relpath:
        return
    with _lock:
        get_sync_state_store().clear(relpath)


def retain(abs_paths: list[str]) -> int:
    """Drop entries for paths not present in ``abs_paths``; return drop count."""
    local_bucket = bucket_dir()
    relpaths = [rp for rp in (relpath_in_bucket(p, local_bucket) for p in abs_paths) if rp]
    with _lock:
        return get_sync_state_store().retain(relpaths)


def iter_merge_bases():
    """Yield ``(relpath, base_hash, vclock)`` for every recorded merge-base entry.

    Used by the sync engine to detect files that were deleted locally between
    one-shot sync cycles (the entry exists in the store but the file is no
    longer listed by get_files_local). The accompanying causal ``vclock`` lets
    the deletion marker inherit the path's version history (ADR-0003).

    Note: the store keys are hashed (``tombstone_key``), so the raw relative
    path is not recoverable from the key alone. The store is scanned against
    the current local listing by the caller; here we yield the *key* and the
    caller compares keys, not paths.
    """
    with _lock:
        for key, version in get_sync_state_store().iter_entries():
            yield key, version["base_hash"], version["vclock"]


def flush() -> None:
    """Persist the local bucket's sync-state (one SQLite transaction)."""
    with _lock:
        get_sync_state_store().save()


def _corrected_mtime_of(abs_path: str) -> int:
    """Corrected mtime of the file on disk, or 0 when unreadable/absent."""
    try:
        return correct_mtime(int(os.stat(abs_path).st_mtime))
    except OSError:
        return 0


def mtime_diverges(local_mtime: int, remote_mtime: int) -> bool:
    """Return whether two known corrected mtimes differ beyond tolerance.

    An unknown value (0) never diverges: without an authoritative mtime on both
    sides there is nothing to converge (ADR-0023). Shared by the reconcile
    (:mod:`hsync_client.sync`) and the touch-detection in :func:`_local_compare`.
    """
    if local_mtime <= 0 or remote_mtime <= 0:
        return False
    return abs(local_mtime - remote_mtime) > MTIME_PROPAGATION_TOLERANCE_SECONDS


def local_version_for_compare(abs_path: str, current_hash: str | None):
    """Return the *effective* version for comparison without persisting (pure).

    If the current content hash differs from the stored base, a ticked copy is
    returned (clock advanced by this node's cid) — the "would-be" version the
    reconcile would record. Used by dry-run preview so it never mutates the
    store but still computes accurate decisions (ADR-0022 §3.4.7).
    """
    return _local_compare(abs_path, current_hash)[0]


def local_version_and_base(
    abs_path: str,
    current_hash: str | None,
) -> tuple[dict | None, str | None]:
    """Return ``(effective_version, stored_base_hash)`` in one store read.

    The reconcile core used to call :func:`local_version_for_compare` and then
    :func:`get_base_hash` per file — two relpath resolutions, two lock
    acquisitions and two reads of the same store entry. The single read returns
    both: the *stored* ``base_hash`` is the common ancestor the causal matrix
    needs (the effective dict's own ``base_hash`` is the would-be content hash,
    not the ancestor, so it cannot stand in once the content differs).
    """
    return _local_compare(abs_path, current_hash)


def _local_compare(
    abs_path: str,
    current_hash: str | None,
) -> tuple[dict | None, str | None]:
    relpath = relpath_in_bucket(abs_path, bucket_dir())
    if not relpath:
        return None, None
    with _lock:
        store = get_sync_state_store()
        entry = store.get(relpath)
        stored_base = entry["base_hash"] if entry else None
        if entry is None:
            # No recorded history: a never-synced file with content gets a
            # fresh tick so its provenance is honest.
            if current_hash:
                cid = local_cid()
                if cid:
                    return (
                        {
                            "base_hash": current_hash,
                            "version_id": "",
                            "origin_cid": cid,
                            "ts": 0,
                            "vclock": tick_vclock(None, cid),
                            "corrected_mtime": _corrected_mtime_of(abs_path),
                        },
                        stored_base,
                    )
            return None, stored_base
        if current_hash and entry["base_hash"] != current_hash:
            cid = local_cid()
            if cid:
                return (
                    {
                        "base_hash": current_hash,
                        "version_id": entry["version_id"],
                        "origin_cid": cid,
                        "ts": entry["ts"],
                        "vclock": tick_vclock(entry["vclock"], cid),
                        "corrected_mtime": _corrected_mtime_of(abs_path),
                    },
                    stored_base,
                )
        return entry, stored_base


def note_local_edit(abs_path: str, current_hash: str) -> None:
    """Persist a self-recorded local edit (anti-aliasing invariant, ADR-0022).

    If the current content hash differs from the stored base, the store entry
    is advanced: clock ticked by this node's cid, new version_id, origin_cid
    set to self. Idempotent between the watcher and reconcile (a second call
    with the same hash is a no-op since base_hash now matches).
    """
    relpath = relpath_in_bucket(abs_path, bucket_dir())
    if not relpath or not current_hash:
        return
    cid = local_cid()
    if not cid:
        return
    with _lock:
        store = get_sync_state_store()
        entry = store.get(relpath)
        if entry is not None and entry["base_hash"] == current_hash:
            # Content unchanged. A bare ``touch`` is still a local edit: record
            # the new on-disk corrected mtime (and tick the clock) so the
            # change is causally visible and peers converge on it. Without this
            # the stored entry keeps the old mtime and no peer ever learns of
            # the touch.
            if not mtime_diverges(
                _corrected_mtime_of(abs_path), int(entry.get("corrected_mtime", 0) or 0)
            ):
                return  # already current — no-op
        store.record(
            relpath,
            base_hash=current_hash,
            vclock=tick_vclock(entry["vclock"] if entry else None, cid),
            version_id=new_version_id(),
            origin_cid=cid,
            corrected_mtime=_corrected_mtime_of(abs_path),
        )


def get_vclock(abs_path: str) -> dict[str, int] | None:
    """Return the stored vclock for a path, or None if unrecorded."""
    relpath = relpath_in_bucket(abs_path, bucket_dir())
    if not relpath:
        return None
    with _lock:
        entry = get_sync_state_store().get(relpath)
        return entry["vclock"] if entry else None


def abs_in_bucket(filepath: str) -> str:
    """Resolve *filepath* against ``LOCAL_BUCKET`` when it is relative."""
    if Path(filepath).is_absolute():
        return filepath
    return str(Path(bucket_dir()) / filepath)


def upload_version_fields(abs_path: str, current_hash: str | None) -> tuple[str, str, int]:
    """Return ``(vclock_json, version_id, corrected_mtime)`` upload form values.

    Shared by the full-upload (``transfer.py``) and delta-apply
    (``delta_transfer.py``) paths so both advertise the same effective causal
    version and the ADR-0023 corrected mtime. Returns ``("{}", "", 0)`` when
    no version is recorded and the content is absent (the server treats an
    empty clock and a 0 mtime natively).
    """
    eff = local_version_for_compare(abs_path, current_hash)
    if eff is not None:
        return (
            json.dumps(eff["vclock"]),
            eff.get("version_id", "") or "",
            int(eff.get("corrected_mtime", 0) or 0),
        )
    return "{}", "", 0


def resolve_upload_version_fields(file: File) -> tuple[str, str, int]:
    """Resolve upload version fields for *file*, never raising.

    Joins a relative ``file.filepath`` against the local bucket, then delegates
    to :func:`upload_version_fields`. Bookkeeping failures return the empty
    clock so a transfer is never aborted for a version lookup.
    """
    try:
        if file.filepath:
            return upload_version_fields(abs_in_bucket(file.filepath), file.sha256sum)
    except Exception as e:  # pragma: no cover - bookkeeping must never break transfer
        log.debug(f'Could not resolve upload version for "{file.filepath}": {e}')
    return "{}", "", 0


def move_base(abs_src: str, abs_dst: str) -> None:
    """Move the recorded merge base of a renamed path to its new location.

    A rename is not a content edit: the hash, clock and identity are carried
    over unchanged (no tick), matching the central server's ``move_version``.
    Without it the store keeps a base for a path that no longer exists and none
    for the new one, so the next comparison would treat a moved file as an
    out-of-band edit and tick a fresh lineage.

    Consumers: the P2P master's rename endpoint (``p2p_server._handle_rename``),
    which applies a slave's move and must keep its arbitration view coherent.
    """
    src_rel = relpath_in_bucket(abs_src, bucket_dir())
    dst_rel = relpath_in_bucket(abs_dst, bucket_dir())
    if not src_rel or not dst_rel:
        return
    with _lock:
        store = get_sync_state_store()
        entry = store.get(src_rel)
        if entry is None:
            return
        store.clear(src_rel)
        store.record(
            dst_rel,
            base_hash=entry["base_hash"],
            version_id=entry["version_id"],
            origin_cid=entry["origin_cid"],
            timestamp=entry["ts"],
            vclock=entry["vclock"],
            corrected_mtime=entry.get("corrected_mtime", 0),
        )


def version_info_for_path(abs_path: str) -> dict | None:
    """Return the wire ``version`` object recorded for *abs_path*, or ``None``.

    Mirrors the server's ``server_version_info`` shape (``vclock``,
    ``version_id``, ``origin_cid``, ``ts``, ``corrected_mtime``) so a peer's
    rename response carries the same causal payload from either transport.

    Consumers: the P2P master's rename endpoint (``p2p_server._handle_rename``).
    """
    relpath = relpath_in_bucket(abs_path, bucket_dir())
    if not relpath:
        return None
    with _lock:
        entry = get_sync_state_store().get(relpath)
    if entry is None:
        return None
    return {
        "vclock": entry["vclock"],
        "version_id": entry["version_id"],
        "origin_cid": entry["origin_cid"],
        "ts": entry["ts"],
        "corrected_mtime": entry.get("corrected_mtime", 0),
    }


def merge_and_record(
    abs_path: str,
    base_hash: str,
    remote_vclock: dict[str, int] | None,
    remote_corrected_mtime: int = 0,
) -> None:
    """Record the agreed version with a merged clock (convergence invariant, ADR-0022 §5).

    The recorded clock is ``merge(local_effective, remote_advertised)``: in
    dominance it equals the dominant clock, in conflict it includes both
    histories so third nodes recognise either side as an ancestor. When
    ``remote_corrected_mtime`` is unknown (0) the agreed version falls back to
    the corrected mtime of the file now on disk.
    """
    relpath = relpath_in_bucket(abs_path, bucket_dir())
    if not relpath or not base_hash:
        return
    with _lock:
        store = get_sync_state_store()
        entry = store.get(relpath)
        local_vclock = entry["vclock"] if entry else {}
        merged = merge_vclocks(local_vclock, remote_vclock)
        corrected = remote_corrected_mtime
        if corrected <= 0:
            # Unknown remote corrected time: the file now on disk holds the
            # agreed content, so its corrected mtime is the honest record.
            corrected = _corrected_mtime_of(abs_path)
        store.record(
            relpath,
            base_hash=base_hash,
            vclock=merged,
            version_id=entry["version_id"] if entry else new_version_id(),
            origin_cid=entry["origin_cid"] if entry else None,
            corrected_mtime=corrected,
        )
