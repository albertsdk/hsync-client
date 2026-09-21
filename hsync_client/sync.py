# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Synchronization logic for HSYNC Client.

This module provides the core synchronization functionality including
file comparison, action determination, and sync execution.
"""

import asyncio
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from errno import EACCES, EBUSY, EIO, ENOENT
from os import cpu_count, remove, walk
from os.path import basename
from pathlib import Path
from shutil import copy2
from sys import exc_info
from time import monotonic
from traceback import print_tb
from typing import cast

from hsync_common.clock import clamp_incoming_corrected_mtime, offset_store, uncorrect_mtime
from hsync_common.models.dir import Dir, is_hidden_dir
from hsync_common.models.file import (
    File,
    invalidate_sha256_cache,
    is_hidden_file,
    is_partial_file,
)
from hsync_common.models.symlink import Symlink
from hsync_common.models.sync_action import SyncAction, SyncActionName
from hsync_common.models.tombstone import TombstoneEntry, relpath_in_bucket, tombstone_key
from hsync_common.models.version import (
    SyncDecision,
    VClockOrder,
    compare_vclocks,
    parse_version_info,
    resolve_three_way,
)
from hsync_common.path_validation import (
    is_internal_name,
    is_safe_symlink_target,
    resolve_within,
    validate_remote_relpath,
)
from hsync_common.permissions import sanitize_remote_permissions
from loguru import logger as log
from requests import RequestException

from hsync_client import sync_state
from hsync_client.autotune import update_bw_ema
from hsync_client.config import CONFIG, is_debug
from hsync_client.constants import (
    HSYNCKEEP_FILENAME,
    HTTP_TIMEOUT_DEFAULT,
    TRANSFER_RETRY_ATTEMPTS_DEFAULT,
    TRANSFER_RETRY_MAX_WAIT_DEFAULT,
    TRANSFER_RETRY_MIN_WAIT_DEFAULT,
    WORKER_MEMORY_BUDGET_BYTES,
    ActionLabel,
)
from hsync_client.events import UiEventName, emit_event
from hsync_client.exceptions import (
    AuthenticationError,
    NetworkError,
    PermanentDownloadError,
    ServerError,
)
from hsync_client.p2p_inventory import NOOP_INVENTORY_OBSERVER, InventoryObserver
from hsync_client.p2p_sources import P2PCandidateMap, P2PSourceResolver
from hsync_client.retry import retry_on_network_error
from hsync_client.state_backend import local_cid
from hsync_client.utils import (
    get_ssl_verify,
    is_inflight_partial,
    set_permissions,
    set_permissions_on_dir,
)

# ── Sync-cycle coordination flag ──────────────────────────────────────────
# A threading.Event (not a Lock) because the watcher must NOT be blocked:
# it still broadcasts events during a sync cycle. The flag only lets the
# periodic resync skip a redundant cycle when one is already in progress.

_sync_cycle_active = threading.Event()

# Cycle bookkeeping for the safety-net watchdog below. The flag alone cannot
# answer "is the safety net still working?": a cycle that never returns leaves
# the flag set forever and every later resync is skipped in silence.
_cycle_bookkeeping_lock = threading.Lock()
_sync_cycle_started_at: float | None = None
_last_cycle_ended_at: float | None = None


def is_sync_cycle_active() -> bool:
    """Return True while a full sync() cycle is in progress.

    The periodic resync task consults this to avoid scheduling a
    second concurrent sync() cycle. The flag is advisory (non-blocking):
    callers never wait on it.
    """
    return _sync_cycle_active.is_set()


def sync_cycle_age() -> float | None:
    """Seconds the current sync cycle has been running, or None when idle.

    Used by the periodic resync to tell "busy" apart from "stuck": a cycle
    that outlives the resync interval means the safety net is no longer
    firing (it is skipped every period), which is a silent loss of the only
    recovery mechanism for events missed by the watcher.
    """
    with _cycle_bookkeeping_lock:
        if _sync_cycle_started_at is None:
            return None
        return monotonic() - _sync_cycle_started_at


def seconds_since_last_cycle() -> float | None:
    """Seconds since the last sync cycle *ended* (success or failure), or None.

    Observability for the safety-net watchdog: together with
    :func:`sync_cycle_age` it answers "is the periodic recovery still running?".
    """
    with _cycle_bookkeeping_lock:
        if _last_cycle_ended_at is None:
            return None
        return monotonic() - _last_cycle_ended_at


async def periodic_resync(
    *,
    p2p_mode: bool = False,
    inventory_observer: InventoryObserver | None = None,
    source_resolver: P2PSourceResolver | None = None,
) -> None:
    """Run full sync cycles at ``CONFIG["RESYNC"]`` until cancelled.

    Shared by remote continuous mode and the P2P slave. ``CancelledError`` is
    re-raised so task cancellation actually stops the loop.

    A skipped cycle is DEBUG while the running one is still within its
    interval and **WARNING** once it outlives it: at that point this loop is
    no longer recovering anything and the operator had no signal at all — a
    DEBUG line made a permanently disabled safety net invisible.
    """
    label = "P2P force resync" if p2p_mode else "Force sync"
    skip_msg = (
        "P2P force resync skipped: sync already in progress"
        if p2p_mode
        else "Force sync skipped: a sync cycle is already in progress"
    )
    stop_msg = "P2P resync task stopped" if p2p_mode else "Resync task stopped"
    interval = CONFIG["RESYNC"] or 60
    try:
        while True:
            await asyncio.sleep(interval)
            if is_sync_cycle_active():
                age = sync_cycle_age()
                if age is not None and age > interval:
                    log.warning(
                        f"{skip_msg} for {age:.0f}s (interval {interval}s): the periodic "
                        "safety net is NOT running, so file-system events missed in the "
                        "meantime are not being recovered"
                    )
                else:
                    log.debug(skip_msg)
                continue
            log.info(f"{label} ({CONFIG['RESYNC']} sec.)")
            await sync(
                p2p_mode=p2p_mode,
                inventory_observer=inventory_observer,
                source_resolver=source_resolver,
            )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.debug(f"{stop_msg}: {e}")


from hsync_client.utils import (
    http_get as get,
)
from hsync_client.utils import (
    http_post as post,
)

# =============================================================================
# Permanent failure skip-set
# =============================================================================

# Filepaths that hit a permanent download failure (e.g. declared size exceeds
# the configured cap) within the current sync cycle. Keyed by
# ``filepath|sha256sum`` so a file that legitimately changes on the remote
# (new content hash) is retried, while the identical hostile entry is not
# re-downloaded on every resync tick. Cleared at the start of each cycle.
_permanent_download_failures: set[str] = set()


def _permanent_skip_key(file: File) -> str:
    """Stable key for a permanently-failed download (path + content hash)."""
    return f"{file.filepath}|{file.sha256sum or ''}"


def _reset_permanent_failures() -> None:
    """Clear the skip-set at the beginning of each sync cycle."""
    _permanent_download_failures.clear()


def _is_permanently_failed(file: File) -> bool:
    """True if this exact file (path + hash) already failed permanently."""
    return _permanent_skip_key(file) in _permanent_download_failures


def _record_permanent_download_failure(file: File, e: PermanentDownloadError) -> None:
    """Record a permanent download failure: skip-set entry + log + UI event."""
    _permanent_download_failures.add(_permanent_skip_key(file))
    log.warning(f'Permanent download failure for "{file.filepath}" (skipped for this cycle): {e}')
    emit_event(UiEventName.transfer_failed, file=file.filepath, reason=str(e))


# =============================================================================
# Main Sync Function
# =============================================================================


def _cleanup_partial_files() -> int:
    """
    Clean up incomplete .part files from previous interrupted downloads.

    This is called at the start of each sync to remove any leftover
    partial files that may have been left behind by interrupted downloads.

    Artifacts currently being written by a live transfer are skipped: inbound
    downloads (P2P fan-out, remote event loop) run outside the sync cycle that
    owns this sweep, so without the check the sweep would delete the ``.part``
    of a stream in flight — the transfer then fails at its final atomic rename
    (logged as "Error saving file", no retry) and the path stays unsynced until
    the next cycle.

    Returns:
        Number of partial files removed.
    """
    removed_count = 0
    local_bucket = CONFIG["LOCAL_BUCKET"]

    for root, _dirs, files in walk(local_bucket):
        for file in files:
            if is_partial_file(file):
                partial_path = os.path.join(root, file)
                if is_inflight_partial(partial_path):
                    log.debug(f"Keeping in-flight partial: {partial_path}")
                    continue
                try:
                    remove(partial_path)
                    log.debug(f"Removed incomplete download: {partial_path}")
                    removed_count += 1
                except OSError as e:
                    log.warning(f"Could not remove partial file {partial_path}: {e}")

    if removed_count > 0:
        log.info(f"Cleaned up {removed_count} incomplete download(s)")

    return removed_count


def _mass_deletion_blocked(
    delete_count: int, local_count: int, remote_count: int
) -> tuple[bool, str]:
    """
    Decide whether a cycle's deletions look like an accidental mass wipe.

    A master that transiently exposes an empty or wrong bucket must not be able
    to delete valid local data in a single reconciliation cycle. The guard trips
    when the master exposes 0 files while local data exists (always suspect), or
    when the deletions exceed the configured absolute count or percentage of the
    local files. When disabled via config the guard never trips.

    Args:
        delete_count: Number of files this cycle would delete.
        local_count: Total number of local files before deletion.
        remote_count: Number of files exposed by the remote/master.

    Returns:
        A ``(blocked, reason)`` tuple; ``reason`` is empty when not blocked.
    """
    if not CONFIG.get("MASS_DELETE_GUARD", True) or delete_count == 0:
        return False, ""

    # An empty master is treated as a suspect state, never a wipe command.
    if remote_count == 0 and local_count > 0:
        return True, "master exposes an empty bucket"

    threshold = CONFIG.get("MASS_DELETE_THRESHOLD", 0)
    if threshold and delete_count >= threshold:
        return True, f"{delete_count} deletions reach the absolute threshold ({threshold})"

    percent = CONFIG.get("MASS_DELETE_PERCENT", 0)
    if percent and local_count > 0 and (delete_count / local_count) * 100 >= percent:
        return True, f"{delete_count}/{local_count} deletions reach {percent}% of local files"

    return False, ""


@dataclass(frozen=True)
class SyncCycleResult:
    """Outcome of a single sync cycle, for exit-status derivation.

    Populated by :func:`sync` on the one-shot path and mapped to a process
    exit code by :func:`hsync_client.exitcodes.exit_code_for_result`. The
    continuous paths (watcher and periodic-resync loops) call ``sync()``
    too but ignore the result, which keeps the API backward compatible.
    """

    completed: bool
    transfers: int
    failed: int
    conflicts: int
    guard_tripped: bool


def log_initial_sync_start() -> None:
    """Emit the colored ``[INITIAL SYNC]`` phase marker for a run's first sync.

    Called by the three initial-sync entry points (P2P slave handler, remote
    reconnect loop, one-shot CLI run) immediately before the first ``sync()``,
    so the log marks the "initial reconciliation" phase explicitly ahead of the
    per-file ``[DOWNLOAD]``/``[UPLOAD]`` lines and before the trailing
    "Initial sync complete" message.
    """
    log.info(f"{ActionLabel.INITIAL_SYNC} Initial sync started")


async def sync(
    p2p_mode: bool = False,
    inventory_observer: InventoryObserver | None = None,
    source_resolver: P2PSourceResolver | None = None,
) -> SyncCycleResult:
    """
    Perform a full synchronization between local and remote files.

    Compares local and remote file lists, determines required actions,
    and executes them in parallel (download, upload, delete, or skip).

    Uses a thread pool executor with semaphore to limit concurrent
    transfers and avoid overwhelming the server.

    Args:
        p2p_mode: If True, sync is coordinated against a P2P master peer.
            Local-only files are uploaded to the master in bidirectional and
            upload-only modes; they are deleted only in download-only mode
            (slave mirrors the master).

    Returns:
        The cycle outcome. Fatal failures (authentication, network, server
        or unexpected errors) are raised, not returned, so the caller can
        map them to a distinct exit status.
    """
    # Import here to avoid circular import
    from hsync_client.transfer import nothing_to_do

    # Rebinding the cycle bookkeeping is module state, not a local: without the
    # declaration Python would bind locals and the watchdog would always see
    # None (i.e. silently report "idle" for a stuck cycle).
    global _last_cycle_ended_at, _sync_cycle_started_at

    inventory_observer = inventory_observer or NOOP_INVENTORY_OBSERVER

    # Check if local directory exists before starting sync
    if not Path(CONFIG["LOCAL_BUCKET"]).exists():
        log.error(f"Local directory '{CONFIG['LOCAL_BUCKET']}' does not exist. Sync aborted.")
        return SyncCycleResult(
            completed=False, transfers=0, failed=0, conflicts=0, guard_tripped=False
        )

    with _cycle_bookkeeping_lock:
        _sync_cycle_started_at = monotonic()
    _sync_cycle_active.set()
    try:
        offset_store.begin_epoch()
        # The SHA-256 cache is intentionally NOT cleared per cycle anymore:
        # entries are keyed on the nanosecond stat identity (mtime_ns, ctime_ns,
        # size) validated on every hit, and watchers invalidate single entries
        # on observed mutations — so an unchanged file is never re-hashed by a
        # later cycle (continuous-mode force resyncs become metadata-only),
        # while any real write evicts the stale digest. One-shot runs get one
        # cycle per process, so the old clear was a no-op there.

        # Clean up any leftover partial downloads from previous sessions
        _cleanup_partial_files()

        # Ensure empty directories have sentinel files
        ensure_hsynckeep_files()

        # Get file lists from both sides
        local_files = get_files_local()
        inventory_observer.replace_snapshot(local_files)
        remote_files = get_files_remote()

        # Fetch deletion markers so files deleted while this peer was offline
        # are not resurrected during the full reconciliation below.
        tombstones = get_tombstones_remote()

        # Merge the remote markers into our persistent local store and push the
        # accumulated union back so the server / master converges to every known
        # deletion.  In P2P mode the master can change (elected), making gossip
        # mandatory; in remote mode the server is the single authority, but
        # pushing our local tombstones is necessary when this peer deleted files
        # while offline and is now reconnecting — otherwise those files would be
        # resurrected from the server.
        from hsync_client.tombstones import get_payload, merge_payload

        merge_payload(tombstones)
        union = get_payload()
        push_tombstones_remote(union)
        tombstones = cast("dict[str, dict]", union)

        # Record tombstones for files tracked by the merge base that are no
        # longer present locally: they were deleted while the peer was offline
        # or deleted below the watcher's radar (the live watcher records its
        # own markers, but it is inactive during a one-shot sync, so this scan
        # is the one-shot catch-all). The tombstone is keyed by the
        # last-known content hash so the server can distinguish a true
        # deletion from a path that was merely renamed or moved.
        # Merge-base keys are hashed (tombstone_key), so compare them against
        # the hashed local paths — a raw relpath would never match and every
        # tracked file would be mistaken for deleted. The existence snapshot
        # deliberately ignores .hsyncignore rules: a file hidden by an ignore
        # pattern is NOT deleted ("exclude protects, it does not delete"), so
        # it must never mint a tombstone that would make peers remove their
        # copies.
        # get_files_local() yields *absolute* filepaths while the store keys
        # hash *bucket-relative* paths: normalize through relpath_in_bucket
        # (a raw path hash would never match, mistaking every visible file
        # for a deletion).
        existing_paths = _existing_file_paths(CONFIG["LOCAL_BUCKET"])
        existing_keys = {
            tombstone_key(rp)
            for p in existing_paths
            if (rp := relpath_in_bucket(p, CONFIG["LOCAL_BUCKET"]))
        }
        # Causal provenance (ADR-0003): the marker inherits the path's
        # recorded clock and is ticked by this peer.
        #
        # The existence check uses the ignore-agnostic ``existing_keys``
        # snapshot only: it is a superset of the syncable ``local_keys`` for
        # on-disk files, so dropping ``local_keys`` is behaviour-preserving in
        # the steady state. It fixes a mid-scan race where ``get_files_local``
        # (run early in the cycle) snapshots a file that is then deleted while
        # ``get_files_remote`` retries bridge a transiently-unreachable master:
        # the stale ``local_keys`` would block minting and the file would be
        # resurrected. ``existing_keys`` is computed right before this loop, so
        # it reflects the live filesystem at mint time.
        minted = False
        for key, base_hash, vclock in list(sync_state.iter_merge_bases()):
            if key and key not in existing_keys:
                from hsync_client.tombstones import record_deletion_by_key

                record_deletion_by_key(
                    key,
                    sha256sum=base_hash or None,
                    origin_cid=_local_cid_or_none(),
                    prev_vclock=vclock,
                    persist=False,
                )
                minted = True
        # Persist the minted markers once (one SQLite transaction) and push the
        # union once after all offline-deletion markers are recorded: the
        # intermediate per-marker persists/pushes were redundant (the master
        # only ever consumes the final union, and each push is a full HTTP
        # round-trip / each persist a SQLite write).
        if minted:
            from hsync_client.tombstones import flush as flush_tombstones

            flush_tombstones()
            union = get_payload()
            push_tombstones_remote(union)
            tombstones = cast("dict[str, dict]", union)

        # Determine what actions need to be taken
        sync_actions = get_sync_actions(
            remote_files, local_files, p2p_mode=p2p_mode, tombstones=tombstones
        )

        # A fresh sync cycle starts — clear the per-cycle skip-set of
        # permanently-failed downloads so a file that failed last tick is
        # retried once (it may have been fixed on the remote).
        _reset_permanent_failures()

        log.info(f"Sync actions: {len(sync_actions)} files to process")

        # Drop merge-base records for files that no longer exist locally so the
        # version store stays bounded (downloads re-record after transfer).
        # ``local_files`` entries carry bucket-relative paths, so resolve them
        # against LOCAL_BUCKET: ``retain`` expects absolute paths and a bare
        # relative path would never match a recorded entry, silently dropping
        # every merge base at the end of each cycle. Files hidden by
        # .hsyncignore are added back via the ignore-agnostic existence
        # snapshot: "exclude protects" means their merge base also survives,
        # so re-including them later does not degrade to a false conflict.
        listed_abs = []
        for file_info in local_files:
            fp = str(file_info.get("filepath", ""))
            if not fp:
                continue
            p = Path(fp)
            listed_abs.append(str(p) if p.is_absolute() else str(Path(CONFIG["LOCAL_BUCKET"]) / fp))
        sync_state.retain(listed_abs + existing_paths)

        # Partition actions by type in a single pass (one O(n) scan replaces
        # eight separate list comprehensions over the same list).
        downloads: list = []
        uploads: list = []
        permission_uploads: list = []
        deletes: list = []
        nothing: list = []
        symlink_uploads: list = []
        symlink_downloads: list = []
        tombstone_deletes: list = []
        guarded_deletes: list = []
        for a in sync_actions:
            name = a.name
            if name == SyncActionName.download:
                downloads.append(a.file)
            elif name == SyncActionName.upload:
                if a.permissions_only:
                    permission_uploads.append(a.file)
                else:
                    uploads.append(a.file)
            elif name == SyncActionName.delete:
                deletes.append(a.file)
                if a.tombstone_confirmed:
                    tombstone_deletes.append(a.file)
                else:
                    guarded_deletes.append(a.file)
            elif name == SyncActionName.nothing:
                nothing.append(a.file)
            elif name == SyncActionName.upload_symlink:
                symlink_uploads.append(a.file)
            elif name == SyncActionName.download_symlink:
                symlink_downloads.append(a.file)

        # Drop downloads that already failed permanently earlier in this
        # same cycle (e.g. two sync_actions entries for the same file, or a
        # real-time event re-queuing it while the full sync is running).
        downloads = [f for f in downloads if not _is_permanently_failed(f)]

        # Log files that are already in sync (no I/O needed)
        for file in nothing:
            nothing_to_do(file)

        # Deletions were already split into tombstone-confirmed (backed by a
        # deletion marker proving the file was removed elsewhere) and
        # unconfirmed in the single partition pass above. The mass-deletion
        # guard must never block a tombstone-confirmed deletion: a tombstone
        # is positive proof the removal is intentional, so an empty master
        # bucket is the expected outcome, not a suspect transient state.
        # Only unconfirmed deletions are subject to the guard.
        blocked, reason = _mass_deletion_blocked(
            len(guarded_deletes), len(local_files), len(remote_files)
        )
        guard_tripped = blocked
        if blocked:
            log.warning(
                f"{ActionLabel.GUARD} Mass-deletion guard tripped ({reason}); "
                f"skipping {len(guarded_deletes)} unconfirmed deletion(s) this cycle "
                f"({len(tombstone_deletes)} tombstone-confirmed deletion(s) still applied)"
            )
            emit_event(
                UiEventName.sync_paused,
                reason=reason,
                deletes=len(guarded_deletes),
                local=len(local_files),
            )
            guarded_deletes = []

        # Merge the surviving deletions back for execution
        deletes = tombstone_deletes + guarded_deletes

        # --ignore-delete skips ALL local deletions (including tombstone
        # confirmed ones), mirroring the filter in get_sync_actions.
        if CONFIG.get("IGNORE_DELETE", False):
            log.info(
                f"{ActionLabel.GUARD} Ignoring {len(deletes)} local deletion(s) "
                "(ignore-delete mode)"
            )
            deletes = []

        # Handle deletions (files that exist only locally in P2P mode)
        for file in deletes:
            if file.filepath and Path(file.filepath).exists():
                try:
                    remove(file.filepath)
                    inventory_observer.remove_file(file.filepath)
                    sync_state.clear_base(file.filepath)
                    log.info(f'{ActionLabel.DELETE} "{file.filepath}" (not on master)')
                    emit_event(UiEventName.transfer, op="delete", file=file.filepath)
                except OSError as e:
                    log.error(f'Error deleting "{file.filepath}": {e}')

        # Handle remote deletions: files that exist only on the server but carry
        # a matching local tombstone (deleted here while offline). The stale
        # server copy must be removed, not downloaded back.
        from hsync_client.transfer import delete_file_remote

        for action in sync_actions:
            if action.name == SyncActionName.delete_remote:
                try:
                    delete_file_remote(action.file)
                except Exception as e:
                    # Best-effort: the tombstone stays in the local store, so
                    # the next cycle will retry the remote deletion.
                    log.error(f'Error deleting "{action.file.filepath}" on server: {e}')

        # Preserve locally-modified files before a conflicting download
        # overwrites them (concurrent clocks lost the causal tie-break —
        # there is never a wall-clock winner).
        conflicts = 0
        for action in sync_actions:
            if action.name == SyncActionName.download and action.conflict:
                conflicts += 1
                conflict_path = _save_conflict_copy(action.file)
                if conflict_path:
                    conflict_file = File.get(conflict_path)
                    if conflict_file is not None:
                        inventory_observer.upsert(conflict_file)

        # ── Symlink structural replication ──────────────────────────────
        # Uploads and downloads of symlinks run inline (they carry no payload:
        # a single POST /symlinks or a local os.symlink each), so no thread
        # pool is needed. Execution is sequential and best-effort: a failure
        # leaves the action for the next cycle.
        if symlink_uploads or symlink_downloads:
            from hsync_client.transfer import download_symlink, upload_symlink

            for link in symlink_uploads:
                try:
                    upload_symlink(link)
                except Exception as e:  # pragma: no cover - best-effort
                    log.error(f'Error uploading symlink "{link.filepath}": {e}')
            for link in symlink_downloads:
                try:
                    download_symlink(link, inventory_observer=inventory_observer)
                except Exception as e:  # pragma: no cover - best-effort
                    log.error(f'Error downloading symlink "{link.filepath}": {e}')

        # Execute transfers in parallel using thread pool
        transfer_counters: dict[str, int] = {"failed": 0}
        if downloads or uploads or permission_uploads:
            # Re-derive concurrency from the live transfer queue profile
            _refresh_concurrency_from_queue(downloads, uploads + permission_uploads)
            source_candidates = (
                source_resolver.resolve(downloads) if source_resolver is not None else None
            )
            await _execute_parallel_transfers(
                downloads,
                uploads,
                transfer_counters,
                permission_uploads,
                inventory_observer,
                source_resolver,
                source_candidates,
            )

        # Directory permission convergence: the authoritative peer's mode
        # wins wherever this node accepts remote content (see
        # ``_dir_converge_enabled``). Directories never appear in the /files
        # listing, so the dedicated /dirs listing is fetched on demand
        # (best-effort: an older peer without the endpoint simply skips the
        # converge). It runs *after* the transfers because the download path
        # creates missing parents with ``mkdir`` defaults: only at this point
        # does the whole replicated tree exist, so a single reconcile repairs
        # the current cycle's creations as well as any earlier divergence —
        # including the modes of directories whose live event was dropped
        # while the WSS was down during a burst.
        if _dir_converge_enabled(p2p_mode):
            converge_directory_permissions(get_dirs_remote())

        # Persist refreshed merge bases (in-sync refresh, retain, clear-on-delete,
        # and every _record_synced_base() from completed transfers) in a single
        # atomic write at the end of the cycle, avoiding redundant per-file
        # rewrites of the entire version map.
        sync_state.flush()

        # Reclaim freed pages from syncstate.db (throttled; lightweight no-op
        # when nothing was pruned this cycle or the interval has not elapsed).
        from hsync_client.state_maintenance import incremental_vacuum_if_due

        incremental_vacuum_if_due()

        log.info("Sync completed successfully")
        emit_event(UiEventName.sync_complete)
        from hsync_client.utils import _write_ready_file as _signal_ready

        _signal_ready()
        return SyncCycleResult(
            completed=True,
            transfers=len(downloads) + len(uploads),
            failed=transfer_counters["failed"],
            conflicts=conflicts,
            guard_tripped=guard_tripped,
        )

    except (AuthenticationError, NetworkError, ServerError) as e:
        log.error(f"Sync failed: {e}")
        emit_event(UiEventName.error, message=f"Sync failed: {e}")
        if is_debug():
            exc_tb = exc_info()[2]
            if exc_tb:
                print_tb(exc_tb)
        # Propagate so the CLI exits non-zero: a failed sync must never be
        # reported to the caller (or to a shell script) as success.
        raise
    except Exception as e:
        log.error(f"Unexpected error during sync: {e}")
        emit_event(UiEventName.error, message=f"Unexpected error during sync: {e}")
        if is_debug():
            exc_tb = exc_info()[2]
            if exc_tb:
                print_tb(exc_tb)
        raise
    finally:
        offset_store.end_epoch()
        with _cycle_bookkeeping_lock:
            _last_cycle_ended_at = monotonic()
            _sync_cycle_started_at = None
        _sync_cycle_active.clear()


def _refresh_concurrency_from_queue(downloads: list[File], uploads: list[File]) -> None:
    """Re-derive concurrency from the live transfer queue profile (per cycle, zero I/O).

    No-op unless autotune is enabled **and** ``MAX_CONCURRENT_TRANSFERS`` is an
    autotune-managed key (i.e. resolved from autotune/default, not a CLI/env
    override): an explicit ``--max-concurrent-transfers`` / ``HSYNC_MAX_*`` must
    survive the per-cycle refresh, preserving the CLI > env > autotune > default
    precedence encoded in ``AUTOTUNE_MANAGED_KEYS``.
    """
    import hsync_client.autotune as autotune_mod
    from hsync_client.autotune import (
        StorageType,
        classify_bandwidth,
        derive_concurrency,
        profile_transfer_queue,
    )
    from hsync_client.constants import (
        MAX_CONCURRENT_TRANSFERS_MAX,
        MAX_CONCURRENT_TRANSFERS_MIN,
    )

    if not CONFIG.get("AUTOTUNE_ENABLED"):
        return
    if "MAX_CONCURRENT_TRANSFERS" not in CONFIG.get("AUTOTUNE_MANAGED_KEYS", []):
        return

    all_files = downloads + uploads
    if not all_files:
        return

    queue_profile = profile_transfer_queue(all_files)
    # AUTOTUNE_STORAGE_TYPE is "" when autotune is disabled (main_logic),
    # which is not a valid StorageType — map it to UNKNOWN.
    storage_type = StorageType(str(CONFIG.get("AUTOTUNE_STORAGE_TYPE") or "unknown"))
    bw_class = classify_bandwidth(autotune_mod._bw_ema)

    concurrency = derive_concurrency(
        storage_type,
        bw_class,
        queue_profile,
        MAX_CONCURRENT_TRANSFERS_MIN,
        MAX_CONCURRENT_TRANSFERS_MAX,
    )

    current = CONFIG.get("MAX_CONCURRENT_TRANSFERS")
    if current != concurrency:
        log.debug(f"autotune: queue={queue_profile.value} -> concurrency={concurrency}")
        CONFIG["MAX_CONCURRENT_TRANSFERS"] = concurrency


def _count_transfer_failure(counters: dict[str, int] | None) -> None:
    """Record one failed transfer in *counters* when statistics are collected."""
    if counters is not None:
        counters["failed"] = counters.get("failed", 0) + 1


# Process-wide transfer executor (see _get_transfer_executor). Kept across
# sync cycles so worker threads — and their per-thread requests.Session
# keep-alive connections — are not torn down at the end of every cycle.
# The lazy-singleton machinery is shared by every hsync pool:
# ``hsync_client.pools`` (the reference stays a module attribute so the
# test-suite isolation fixture can reset it).
_transfer_executor: ThreadPoolExecutor | None = None


def _get_transfer_executor(max_workers: int) -> ThreadPoolExecutor:
    """Return the process-wide transfer executor, resizing it if needed.

    A persistent pool keeps its worker threads (and therefore their
    per-thread ``requests.Session`` keep-alive connections) alive across sync
    cycles: recreating the pool per cycle discarded every warm TLS session
    and forced a fresh handshake for each worker's first transfer. The pool
    is resized (old instance shut down without waiting) when the requested
    worker count changes, e.g. after an autotune concurrency refresh.
    """
    from hsync_client.pools import get_singleton_executor

    return get_singleton_executor(
        sys.modules[__name__],
        "_transfer_executor",
        max_workers=max_workers,
        thread_name_prefix="hsync-transfer",
        resizable=True,
    )


async def _execute_parallel_transfers(
    downloads: list[File],
    uploads: list[File],
    counters: dict[str, int] | None = None,
    permission_uploads: list[File] | None = None,
    inventory_observer: InventoryObserver = NOOP_INVENTORY_OBSERVER,
    source_resolver: P2PSourceResolver | None = None,
    source_candidates: P2PCandidateMap | None = None,
) -> None:
    """
    Execute file transfers in parallel using a thread pool.

    Uses asyncio.Semaphore to limit concurrent transfers and
    run_in_executor to run synchronous transfer functions in threads.

    Args:
        downloads: List of files to download from remote.
        uploads: List of files to upload to remote.
        counters: Optional mutable mapping collecting per-cycle stats. When
            provided, the ``"failed"`` key is incremented for every transfer
            that raises (permanent or transient). Callers that do not need
            the stats (continuous loops) can omit it.
    """
    from hsync_client.constants import MAX_CONCURRENT_TRANSFERS_DEFAULT
    from hsync_client.transfer import download_file, upload_file

    max_concurrent = CONFIG.get("MAX_CONCURRENT_TRANSFERS", MAX_CONCURRENT_TRANSFERS_DEFAULT)

    # Semaphore to limit concurrent transfers
    semaphore = asyncio.Semaphore(max_concurrent)

    # Get the current event loop
    loop = asyncio.get_running_loop()

    # Persistent process-wide thread pool for I/O-bound operations: keeping
    # the workers alive across cycles keeps their per-thread keep-alive
    # sessions warm instead of paying a fresh TLS handshake per worker at
    # the start of every sync cycle.
    executor = _get_transfer_executor(max_concurrent)

    async def transfer_with_semaphore(
        func: Callable[[File], None], file: File, *, is_download: bool = False
    ) -> None:
        """Execute a transfer function with semaphore limiting."""
        async with semaphore:
            # Skip downloads that already failed permanently in this
            # cycle (the skip-set is consulted here too because a realtime
            # event can enqueue a file after the full-sync pre-filter ran).
            if is_download and _is_permanently_failed(file):
                return
            t0 = monotonic()
            try:
                # Run synchronous function in thread pool
                await loop.run_in_executor(executor, func, file)
            except PermanentDownloadError as e:
                # Permanent failure (e.g. over the size cap): record it so
                # the file is not retried on every resync tick, and notify
                # the UI. The partial file is already dropped in transfer.py.
                _count_transfer_failure(counters)
                _record_permanent_download_failure(file, e)
            except Exception as e:
                _count_transfer_failure(counters)
                log.error(f"Transfer error for {file.filepath}: {e}")
            else:
                file_size = file.size or 0
                if file_size > 0:
                    elapsed = monotonic() - t0
                    if elapsed > 0:
                        update_bw_ema(file_size, elapsed)

    # Create tasks for all transfers
    tasks = []

    # Add download tasks
    from functools import partial

    observed_download: Callable[[File], None] = partial(
        download_file,
        candidates=source_candidates,
        source_resolver=source_resolver,
        inventory_observer=inventory_observer,
    )
    for file in downloads:
        tasks.append(transfer_with_semaphore(observed_download, file, is_download=True))

    # Add upload tasks
    observed_upload = partial(upload_file, inventory_observer=inventory_observer)
    for file in uploads:
        tasks.append(transfer_with_semaphore(observed_upload, file))

    # Permission-only uploads skip the content dedup on the peer (same-SHA
    # branch applies the new mode); they re-use the same upload machinery.
    if permission_uploads:
        perm_upload = partial(
            upload_file,
            permissions_only=True,
            inventory_observer=inventory_observer,
        )
        for file in permission_uploads:
            tasks.append(transfer_with_semaphore(perm_upload, file))

    # Execute all transfers concurrently
    if tasks:
        log.info(
            f"Starting parallel transfers: {len(downloads)} downloads, "
            f"{len(uploads)} uploads ({len(permission_uploads or [])} permission-only), "
            f"max {max_concurrent} concurrent"
        )
        await asyncio.gather(*tasks, return_exceptions=True)


# =============================================================================
# Sentinel File for Empty Directories
# =============================================================================


def ensure_sentinel(dir_path: str, *, label: str | None = None) -> bool:
    """Create a ``.hsynckeep`` sentinel in *dir_path* if it is empty.

    A directory is considered empty when it has no syncable (non-hidden)
    entries. An already-present sentinel is never recreated (guarded by an
    existence check). Returns True when a sentinel was created.

    *label* is an optional ``ActionLabel`` prefix for the debug log line
    (e.g. the slave watcher passes ``ActionLabel.LOCAL_KEEP``); callers that
    create sentinels outside the watcher leave it unset.

    Shared by :func:`ensure_hsynckeep_files` (walk-all at cycle start),
    ``p2p_websocket.P2PSlaveFileEventHandler`` (single dir on create) and
    ``p2p_server.P2PRequestHandler`` (parent-of-deleted).
    """
    try:
        entries = os.listdir(dir_path)
    except OSError:
        return False
    if any(not is_hidden_file(e) for e in entries):
        return False
    sentinel = os.path.join(dir_path, HSYNCKEEP_FILENAME)
    if os.path.exists(sentinel):
        return False
    prefix = f"{label} " if label else ""
    try:
        open(sentinel, "w").close()  # noqa: SIM115
        log.debug(f'{prefix}Created sentinel "{sentinel}"')
        return True
    except OSError as e:
        log.warning(f'Cannot create sentinel "{sentinel}": {e}')
        return False


def ensure_hsynckeep_files() -> None:
    """
    Ensure every empty directory under LOCAL_BUCKET contains a sentinel
    `.hsynckeep` file so that the directory structure is preserved during sync.

    A directory is considered "empty" when it contains no syncable files
    (after filtering out hidden files).  Already-existing `.hsynckeep`
    files count as syncable, so they prevent a second creation.
    """
    local_bucket = CONFIG["LOCAL_BUCKET"]
    for root, dirs, _files in walk(local_bucket):
        # Skip hidden directories (same filter as get_files_local)
        dirs[:] = [d for d in dirs if not is_hidden_dir(d)]
        ensure_sentinel(root)


# =============================================================================
# File List Retrieval
# =============================================================================


def _get_single_file_metadata(file_path: str) -> dict | None:
    """
    Get metadata for a single file or symlink (used for parallel processing).

    Retries up to 3 times with exponential backoff if the file is
    temporarily locked or inaccessible (common with files being written).

    Args:
        file_path: Full path to the file.

    Returns:
        File or Symlink metadata dictionary or None if file cannot be read.
    """
    # Symlink first: emit a symlink entry without computing a content hash.
    if Path(file_path).is_symlink():
        link = Symlink.get(file_path)
        if link is not None:
            return link.to_dict()
        return None

    # Retry up to 3 times for transient file access errors
    max_retries = 3
    retry_errors = (
        EACCES,
        EBUSY,
        EIO,
        ENOENT,
    )  # Permission denied, resource busy, I/O error, no such file

    for attempt in range(max_retries):
        try:
            file_obj = File.get(file_path)
            if file_obj:
                return file_obj.to_dict()
            # File doesn't exist (File.get returned None) - no retry needed
            return None
        except OSError as e:
            if e.errno in retry_errors and attempt < max_retries - 1:
                wait_time = 0.1 * (2**attempt)  # Exponential backoff: 0.1s, 0.2s, 0.4s
                log.debug(
                    f"Retry {attempt + 1}/{max_retries} for {file_path}: {e} (waiting {wait_time}s)"
                )
                time.sleep(wait_time)
                continue
            # Non-retryable error or max retries reached
            log.warning(f"Error reading file metadata for {file_path}: {e}")
            return None
        except Exception as e:
            # Unexpected error - log and return None
            log.warning(f"Error reading file metadata for {file_path}: {e}")
            return None

    return None


def _get_optimal_workers(file_count: int) -> int:
    """
    Calculate optimal number of worker threads for parallel file processing.

    Uses the effective CPU quota (cgroup-aware, reflects container limits)
    to determine parallelism, with a configurable maximum and sensible
    defaults for different workload sizes. When a cgroup memory limit is
    detected (containers), it additionally caps parallelism so the worker
    pool's combined memory footprint stays within the limit; on bare metal
    the memory probe is None and only the CPU quota applies.

    Args:
        file_count: Number of files to process.

    Returns:
        Optimal number of worker threads.
    """
    # Resolve the effective CPU core count.  Inside a container (Docker,
    # Podman, Kubernetes) the cgroup quota is authoritative; on bare metal
    # or when cgroups are unavailable, fall back to ``os.cpu_count()``.
    # Default to 4 if every probe fails.
    try:
        from hsync_common.runtime import get_cpu_quota_cores, get_memory_limit_bytes

        cpu_cores = int(get_cpu_quota_cores() or cpu_count() or 4)
        mem_limit = get_memory_limit_bytes()
    except Exception:
        cpu_cores = 4
        mem_limit = None

    # Memory-derived worker cap: bounded by the per-worker working set
    # (``WORKER_MEMORY_BUDGET_BYTES``). Floor of 1 worker; None (bare metal or
    # unknown) leaves the CPU-derived cap untouched.
    resource_cap = cpu_cores
    if mem_limit is not None:
        mem_workers = max(1, mem_limit // WORKER_MEMORY_BUDGET_BYTES)
        resource_cap = min(resource_cap, mem_workers)

    # Allow configuration via environment variable
    env_max = os.environ.get("HSYNC_MAX_FILE_WORKERS")
    if env_max:
        try:
            max_allowed = int(env_max)
            return min(max_allowed, file_count, resource_cap)
        except ValueError:
            pass

    # For small file counts, don't over-parallelize. The resource cap is
    # intentionally not applied to this trivial 2-worker floor (mirroring the
    # existing non-application of the CPU quota here).
    if file_count < 10:
        return min(2, file_count)

    # For medium file counts, use moderate parallelism
    if file_count < 100:
        return min(4, file_count, resource_cap)

    # For large file counts, use more parallelism but cap at 8
    # to avoid overwhelming the system with too many threads
    return min(8, file_count, resource_cap)


def iter_bucket_entries(bucket: str) -> Iterator[tuple[str, Symlink | None]]:
    """Yield ``(abs_path, symlink_or_none)`` for every syncable bucket entry.

    Walks *bucket* with ``followlinks=False`` (the default), skipping hidden
    directories, hidden files, and unsafe file-symlinks (a link whose target
    escapes the bucket would be dereferenced and exfiltrated). Safe symlinks
    yield their :class:`Symlink` (never dereferenced); regular files and
    unreadable symlinks yield ``None``.

    Shared by :func:`get_files_local` and the P2P master's ``_handle_list_files``
    so the two listing surfaces can never drift on the security filter.
    """
    for root, dirs, files in walk(bucket):
        # Skip hidden directories. ``os.walk`` runs with followlinks=False (the
        # default), so a symlinked directory *inside* the bucket pointing
        # outside it is never descended into; only its name appears in ``dirs``
        # and, being a directory, it is never emitted as a ``File`` entry.
        dirs[:] = [d for d in dirs if not is_hidden_dir(d)]

        for file in files:
            # Skip hidden files
            if is_hidden_file(file):
                continue
            file_path = os.path.join(root, file)
            # Security: a file-symlink whose target escapes the bucket must never
            # be followed. ``os.walk`` (followlinks=False, the default) already
            # prevents descending into symlinked *directories*, but a symlink to a
            # file is still enumerated in ``files``. Without this guard,
            # ``File.get`` (exists/stat/sha256) and the upload path would
            # dereference it and exfiltrate the out-of-bucket target — e.g.
            # ``secret -> /etc/passwd`` would leak its content to the peer.
            if Path(file_path).is_symlink():
                link = Symlink.get(file_path)
                if link is not None and not is_safe_symlink_target(file_path, link.target, bucket):
                    log.warning(
                        f"{ActionLabel.SECURITY} Symlink target escapes bucket, "
                        f'skipping: "{file_path}" -> "{link.target}"'
                    )
                    continue
                yield file_path, link
            else:
                yield file_path, None


def get_files_local() -> list[dict]:
    """
    Get list of all local files with their metadata.

    Walks the local bucket directory recursively, skipping hidden
    files and directories. Returns file metadata sorted by mtime
    ascending (oldest first) to prioritize updating older files.

    Uses parallel processing for SHA-256 computation to improve performance
    on systems with multiple CPU cores. The number of worker threads
    is automatically determined based on CPU cores and file count.

    Returns:
        List of file metadata dictionaries.
    """
    # Check if local directory exists before walking
    if not Path(CONFIG["LOCAL_BUCKET"]).exists():
        log.warning(
            f"Local directory '{CONFIG['LOCAL_BUCKET']}' does not exist during synchronization"
        )
        return []

    # First pass: collect all file paths (no checksum computation)
    file_paths = [abs_path for abs_path, _link in iter_bucket_entries(CONFIG["LOCAL_BUCKET"])]

    # Early return if no files
    if not file_paths:
        return []

    # Determine optimal number of workers based on file count and CPU cores
    max_workers = _get_optimal_workers(len(file_paths))
    log.debug(f"Using {max_workers} workers for {len(file_paths)} files")

    file_list = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all file metadata fetches in parallel
        futures = [executor.submit(_get_single_file_metadata, fp) for fp in file_paths]

        # Collect results as they complete: as_completed avoids head-of-line
        # blocking on the first submitted (and potentially slowest) hash and
        # keeps every worker busy for the whole batch. The dict key in the old
        # future->path mapping was never read.
        for future in as_completed(futures):
            result = future.result()
            if result:
                file_list.append(result)

    # Sort by mtime ascending (oldest first) to prioritize updating older files
    file_list.sort(key=lambda x: x.get("mtime", 0))

    return file_list


def get_files_remote() -> list[dict]:
    """
    Get list of all remote files from the server.

    Makes an HTTP GET request to retrieve file metadata from the
    remote bucket, sorted by the configured sync order. Retries
    automatically on network errors with exponential backoff (attempt
    count, min/max wait read from CONFIG so autotune and env vars
    apply).

    Returns:
        List of file metadata dictionaries from server.

    Raises:
        NetworkError: If the request fails after retries.
        AuthenticationError: If authentication fails (401/403).
        ServerError: If server returns an error response.
    """
    max_attempts = cast(int, CONFIG.get("TRANSFER_RETRY_ATTEMPTS", TRANSFER_RETRY_ATTEMPTS_DEFAULT))
    min_wait = cast(float, CONFIG.get("TRANSFER_RETRY_MIN_WAIT", TRANSFER_RETRY_MIN_WAIT_DEFAULT))
    max_wait = cast(float, CONFIG.get("TRANSFER_RETRY_MAX_WAIT", TRANSFER_RETRY_MAX_WAIT_DEFAULT))

    def _attempt() -> list[dict]:
        uri = f"{CONFIG['API']['buckets']}/{CONFIG['REMOTE_BUCKET']}/files"
        try:
            # Build query parameters
            params: dict[str, str] = {}
            # Add path filter if specified
            if (filter_path := CONFIG.get("FILTER_PATH")) is not None:
                params["path"] = filter_path

            response = get(
                uri,
                headers=cast(Mapping[str, str], CONFIG["REQUEST_HEADERS"]),
                params=params,
                verify=get_ssl_verify(),
                timeout=CONFIG.get("HTTP_TIMEOUT", HTTP_TIMEOUT_DEFAULT),
            )

            _check_auth_status(response.status_code)
            if response.status_code != 200:
                raise ServerError(
                    message=f"Server returned status {response.status_code}",
                    details={"status_code": response.status_code, "response": response.text},
                )

            return response.json()
        except RequestException as e:
            raise NetworkError(
                message=f"Error downloading files list: {e}",
                details={"url": uri},
            )

    attempt = retry_on_network_error(max_attempts, min_wait, max_wait)(_attempt)
    return attempt()


def get_tombstones_remote() -> dict[str, dict]:
    """
    Get deletion markers (tombstones) from the server.

    Makes an HTTP GET request to retrieve the tombstone payload: a mapping
    of hashed bucket-relative path to the causal marker
    ``{"sha256", "version_id", "origin_cid", "ts", "vclock"}`` (ADR-0003).
    Clients use these markers to avoid resurrecting files deleted while
    they were offline: a marker whose vector clock dominates the local
    version is positive proof the deletion descends from the common history.

    Degrades gracefully: a server that does not expose the endpoint (older
    version) or any non-200 response yields an empty mapping, so the rest of
    the sync proceeds unchanged.

    Retries transient network errors with exponential backoff (attempt
    count, min/max wait read from CONFIG so autotune and env vars apply).

    Returns:
        Mapping of hashed path to tombstone entry (possibly empty).

    Raises:
        NetworkError: If the request fails after retries.
        AuthenticationError: If authentication fails (401/403).
    """
    max_attempts = cast(int, CONFIG.get("TRANSFER_RETRY_ATTEMPTS", TRANSFER_RETRY_ATTEMPTS_DEFAULT))
    min_wait = cast(float, CONFIG.get("TRANSFER_RETRY_MIN_WAIT", TRANSFER_RETRY_MIN_WAIT_DEFAULT))
    max_wait = cast(float, CONFIG.get("TRANSFER_RETRY_MAX_WAIT", TRANSFER_RETRY_MAX_WAIT_DEFAULT))

    def _attempt() -> dict[str, dict]:
        uri = f"{CONFIG['API']['buckets']}/{CONFIG['REMOTE_BUCKET']}/tombstones"
        try:
            response = get(
                uri,
                headers=cast(Mapping[str, str], CONFIG["REQUEST_HEADERS"]),
                verify=get_ssl_verify(),
                timeout=CONFIG.get("HTTP_TIMEOUT", HTTP_TIMEOUT_DEFAULT),
            )

            _check_auth_status(response.status_code)
            if response.status_code != 200:
                log.debug(
                    f"Tombstones unavailable (status {response.status_code}); "
                    "skipping deletion reconciliation"
                )
                return {}

            data = response.json()
            return data if isinstance(data, dict) else {}
        except RequestException as e:
            raise NetworkError(
                message=f"Error downloading tombstones: {e}",
                details={"url": uri},
            )

    attempt = retry_on_network_error(max_attempts, min_wait, max_wait)(_attempt)
    return attempt()


def push_tombstones_remote(payload: Mapping[str, TombstoneEntry]) -> None:
    """
    Push this peer's deletion markers to the current P2P master (gossip up).

    Best-effort by design: a master that does not expose the endpoint (older
    version), an empty payload, or any transport/HTTP failure is swallowed so
    the rest of the sync proceeds unchanged. This is only used in P2P mode,
    where every peer persists its own store and a freshly elected master must
    converge to the union of all known deletions.

    Args:
        payload: Mapping of hashed path to tombstone entry to send.
    """
    if not payload:
        return

    uri = f"{CONFIG['API']['buckets']}/{CONFIG['REMOTE_BUCKET']}/tombstones"
    try:
        post(
            uri,
            json=cast(dict, dict(payload)),
            headers=cast(dict, CONFIG["REQUEST_HEADERS"]),
            verify=get_ssl_verify(),
            timeout=CONFIG.get("HTTP_TIMEOUT", HTTP_TIMEOUT_DEFAULT),
        )
    except RequestException as e:
        log.debug(f"Tombstone push failed (non-fatal): {e}")


# =============================================================================
# Sync Action Determination
# =============================================================================


def _check_auth_status(status_code: int) -> None:
    """Raise AuthenticationError on 401/403, shared by all HTTP call sites."""
    if status_code == 401:
        raise AuthenticationError(message="Invalid API key")
    if status_code == 403:
        raise AuthenticationError(message="API key required")


def _local_cid_or_none() -> str | None:
    """Return this node's cid if configured, else None.

    Used as the author of deletion markers minted by the sync engine when the
    only known identity for a path is its merge-base key (ADR-0003).
    """
    return local_cid() or None


def _existing_file_paths(bucket_dir: str) -> list[str]:
    """Absolute paths of every real file under ``bucket_dir``, ignore-agnostic.

    Deliberately bypasses the active :class:`hsync_common.ignore.IgnoreMatcher`:
    a file hidden by ``.hsyncignore`` still exists on disk and must never be
    mistaken for an offline deletion — "exclude protects, it does not delete"
    (rsync ``--exclude``, Syncthing ``.stignore`` semantics). Only internal
    engine artifacts are filtered out: they never hold a sync merge base and
    must never mint tombstones.

    Returns:
        Absolute paths of physically present, non-engine files.
    """
    paths: list[str] = []
    try:
        for root, dirs, files in walk(bucket_dir):
            # is_internal_name() covers engine side-cars AND the .part suffix.
            dirs[:] = [d for d in dirs if not is_internal_name(d)]
            for name in files:
                if is_internal_name(name):
                    continue
                paths.append(os.path.join(root, name))
    except OSError:
        # An unreadable bucket must not be treated as "everything deleted".
        return []
    return paths


def _tombstone_clock_dominates(entry: dict, other_vclock: dict[str, int] | None) -> bool:
    """True when the tombstone clock causally dominates ``other_vclock``.

    Dominance is positive proof that the deletion descends from the version
    ``other_vclock`` describes, i.e. that version is a stale ancestor of the
    deletion (ADR-0003). A marker without a structurally valid causal clock
    carries no proof at all and never dominates. The wall clock is never
    consulted.
    """
    tomb_vclock = entry.get("vclock")
    if not isinstance(tomb_vclock, dict):
        # A marker without a valid causal clock carries no proof of deletion
        # ancestry — never delete data on its say-so.
        return False
    return compare_vclocks(tomb_vclock, other_vclock) is VClockOrder.dominates


def _tombstone_deletes_local(entry: dict, local_vclock: dict[str, int] | None) -> bool:
    """Decide whether a tombstone causally justifies deleting a solo-local file.

    A local file that exists only on this peer is removed only when the
    tombstone's vector clock **dominates** the local version clock: the
    deletion provably descends from the common history (the local copy is a
    stale ancestor). A local edit concurrent with (not dominated by) the
    deletion is an intentional re-creation and the file is kept (uploaded).
    The wall clock is never consulted (ADR-0003).
    """
    return _tombstone_clock_dominates(entry, local_vclock)


def _parse_advertised_version(
    remote_version: object,
) -> tuple[dict[str, int] | None, int]:
    """Parse a wire ``version`` payload into ``(vclock, corrected_mtime)``.

    Absent or malformed metadata degrades to an empty clock and corrected
    mtime 0 (no error): the causal matrix handles the empty clock natively
    and the divergence tie-break falls back to the deterministic content
    hash (ADR-0022/ADR-0023).
    """
    if not isinstance(remote_version, dict):
        return None, 0
    parsed = parse_version_info(remote_version)
    if not parsed:
        return None, 0
    return parsed["vclock"], int(clamp_incoming_corrected_mtime(float(parsed["corrected_mtime"])))


# ── Directory permission converge (dir chmod) ─────────────────────────────
# Directories carry no content hash and never appear in the /files listing, so
# the reconcile compares the dedicated /dirs listing against the local tree and
# applies the authoritative peer's mode when it diverges. This runs only inside
# sync() — never in previews: a dry-run calls get_sync_actions(persist_state=False)
# directly and never reaches this path.


def _dir_converge_enabled(p2p_mode: bool) -> bool:
    """Whether the current cycle reconciles directory modes from the peer.

    The authoritative peer (central server in download-only, P2P master
    always) owns directory modes; this node mirrors them.

    * ``upload-only`` — never: this node pushes content and takes nothing
      back, so a remote mode must not be forced onto the local tree.
    * P2P (``p2p_mode``) — always otherwise. The master is the guaranteed
      source of the whole tree and the slave is its replica, but a directory
      mode has no representation in the /files listing: it travels as a live
      dir event only. Any dropped event (WSS drop during a burst, watcher
      gap, reconnect window) would therefore leave a divergence no later
      cycle could ever repair — the periodic reconcile makes mode parity
      eventually consistent instead of event-delivery dependent.
    * Remote central server — only in ``download-only``: there mirroring the
      server *is* the contract. In bidirectional mode the local modes are
      user intent (the server only stores file-backed parents whose modes are
      mkdir defaults), so they are never rewritten from the listing.
    """
    direction = CONFIG.get("SYNC_DIRECTION", "bidirectional")
    if direction == "upload-only":
        return False
    return p2p_mode or direction == "download-only"


def get_dirs_remote() -> list[dict]:
    """Fetch the remote directory metadata (path + permissions) listing.

    Best-effort by design: a peer that does not expose the endpoint (older
    version) or any non-200 response yields an empty list, so the
    directory-permission converge is simply skipped. Mirrors the tombstone
    listing availability contract.
    """
    uri = f"{CONFIG['API']['buckets']}/{CONFIG['REMOTE_BUCKET']}/dirs"
    try:
        response = get(
            uri,
            headers=cast(Mapping[str, str], CONFIG["REQUEST_HEADERS"]),
            verify=get_ssl_verify(),
            timeout=CONFIG.get("HTTP_TIMEOUT", HTTP_TIMEOUT_DEFAULT),
        )
    except RequestException as e:
        log.debug(f"Dirs listing unavailable: {e}")
        return []
    if response.status_code != 200:
        log.debug(f"Dirs listing unavailable (status {response.status_code})")
        return []
    data = response.json()
    return data if isinstance(data, list) else []


def converge_directory_permissions(remote_dirs: list[dict]) -> None:
    """Apply divergent remote directory permissions locally.

    Each entry is validated (untrusted wire input) and compared against the
    local directory mode; on divergence the authoritative peer's mode wins.
    A directory that does not exist locally yet is skipped — the caller runs
    this after the transfers, so every directory of the replicated tree is
    already present and a missing entry can only be a directory the peer
    itself does not materialise. Every chmod is best-effort — a failure is
    logged and never aborts the cycle.
    """
    for info in remote_dirs:
        if not isinstance(info, dict):
            continue
        rel = info.get("path")
        remote_permissions = info.get("permissions")
        if not isinstance(rel, str) or not isinstance(remote_permissions, str):
            continue
        if validate_remote_relpath(rel) is None:
            log.warning(f"{ActionLabel.SECURITY} Ignoring unsafe remote directory entry: {rel!r}")
            continue
        # Hidden check on the ABSOLUTE path: the ignore matcher compares
        # slash-joined patterns, so a relative path could miss (or falsely
        # match) directory-prefix rules.
        if is_hidden_dir(str(Path(CONFIG["LOCAL_BUCKET"]) / rel)):
            continue
        local_dir = Dir.get(rel, CONFIG["LOCAL_BUCKET"])
        if local_dir is None or not local_dir.path:
            continue
        try:
            sanitized = sanitize_remote_permissions(remote_permissions)
        except ValueError:
            continue
        if local_dir.permissions == sanitized:
            continue
        try:
            set_permissions_on_dir(local_dir.path, sanitized)
            log.debug(f'Permission-only converge (remote): "{local_dir.path}" ({sanitized})')
        except OSError as e:
            log.warning(f'Could not set directory permissions for "{local_dir.path}": {e}')


# ── Direction-filter exclusion sets (module-level, pre-computed once) ─────
_UPLOAD_ONLY_EXCLUDE: frozenset[SyncActionName] = frozenset(
    {
        SyncActionName.download,
        SyncActionName.download_symlink,
        SyncActionName.delete,
        SyncActionName.delete_remote,
    }
)
_DOWNLOAD_ONLY_EXCLUDE: frozenset[SyncActionName] = frozenset(
    {
        SyncActionName.upload,
        SyncActionName.upload_symlink,
        SyncActionName.delete_remote,
    }
)


def _apply_local_mtime(abs_path: str, corrected_mtime: int) -> None:
    """Adopt a peer's corrected mtime on the local file (metadata-only).

    The value is an ADR-0023 *corrected* time, so it is converted back to this
    node's raw clock before ``utime`` (otherwise the next ``correct_mtime``
    re-adds the offset and the file stays divergent). Content is untouched; the
    merge-base refresh that follows records the adopted mtime as the agreed
    version.
    """
    try:
        raw = uncorrect_mtime(corrected_mtime)
        os.utime(abs_path, (raw, raw))
        log.debug(f'Metadata-only converge (mtime): "{abs_path}" ({corrected_mtime})')
    except OSError as exc:
        log.debug(f'Could not apply mtime to "{abs_path}": {exc}')


def get_sync_actions(
    remote_files: list[dict] | None = None,
    local_files: list[dict] | None = None,
    p2p_mode: bool = False,
    tombstones: dict[str, dict] | None = None,
    persist_state: bool = True,
) -> list[SyncAction]:
    """
    Determine sync actions by comparing local and remote file lists.

    Uses the causal vector-clock matrix (ADR-0022): content identity is
    checked first (hash), then causal dominance via the vector clocks, then
    the corrected-mtime tie-break on true divergence (ADR-0023) with the
    deterministic SHA-256 fallback. The raw wall clock is never consulted,
    so clock skew cannot cause an incorrect direction.

    Args:
        remote_files: List of file metadata from server.
        local_files: List of file metadata from local filesystem.
        p2p_mode: If True, sync targets a P2P master peer. Local-only files
            are uploaded in bidirectional/upload-only modes and deleted only
            when SYNC_DIRECTION is "download-only".
        tombstones: Optional deletion-marker payload from the server, keyed by
            hashed bucket-relative path. When a file exists only locally and a
            matching tombstone shows it was deleted elsewhere, the local copy
            is removed instead of being re-uploaded (resurrection guard).
        persist_state: When False (preview/dry-run), the store is never
            mutated — ``merge_and_record`` on in-sync files is skipped so the
            installation ``syncstate.db`` stays unchanged. Comparison is always pure
            via ``local_version_for_compare``.

    Returns:
        List of SyncAction objects describing required operations.
    """
    if remote_files is None:
        remote_files = []
    if local_files is None:
        local_files = []
    if tombstones is None:
        tombstones = {}

    # Pre-compute the direction filter: excluded actions never enter the list.
    _direction = CONFIG.get("SYNC_DIRECTION", "bidirectional")
    _excluded: frozenset[SyncActionName] = (
        _UPLOAD_ONLY_EXCLUDE
        if _direction == "upload-only"
        else _DOWNLOAD_ONLY_EXCLUDE
        if _direction == "download-only"
        else frozenset()
    )

    sync_actions: list[SyncAction] = []
    remote_filepaths: set[str] = set()

    # Process remote files
    for remote_file_info in remote_files:
        # .hsynckeep is a per-peer local sentinel: never exchange it across peers.
        if basename(str(remote_file_info.get("filepath", ""))) == HSYNCKEEP_FILENAME:
            continue
        # The remote listing is untrusted input (hostile server or P2P
        # master): drop entries whose path is unsafe or names an internal
        # engine artifact instead of acting on them.
        if validate_remote_relpath(str(remote_file_info.get("filepath", ""))) is None:
            log.warning(
                f"{ActionLabel.SECURITY} Ignoring unsafe remote listing entry: "
                f'"{remote_file_info.get("filepath")}"'
            )
            continue
        # Symlink entry: replicate STRUCTURALLY (the link, never the target
        # content). The same code path serves remote (server listing) and P2P
        # (master listing); the target containment is re-validated on receive.
        if remote_file_info.get("type") == "symlink":
            link = Symlink.from_dict(remote_file_info, CONFIG["LOCAL_BUCKET"])
            if link.filepath:
                remote_filepaths.add(str(link.filepath).lstrip("/"))
            sync_actions.append(SyncAction(link, SyncActionName.download_symlink))
            continue
        # One malformed entry (bogus types for size/mtime, unreadable local
        # stat) must not stall the whole reconciliation cycle.
        try:
            remote_file = File.from_dict(remote_file_info, CONFIG["LOCAL_BUCKET"])
            local_file = File.get(remote_file_info["filepath"], CONFIG["LOCAL_BUCKET"])
        except (ValueError, TypeError, OverflowError, AttributeError, OSError) as e:
            log.warning(
                f"{ActionLabel.SECURITY} Ignoring malformed remote listing entry "
                f'"{remote_file_info.get("filepath")}": {e}'
            )
            # The entry IS on the remote (the listing says so) even though it
            # cannot be compared: skipping it as "absent" would queue the
            # local copy for deletion in download-only mode (or a spurious
            # re-upload otherwise). Count it as remotely present instead.
            remote_filepaths.add(str(remote_file_info["filepath"]).lstrip("/"))
            continue

        # Track remote paths for later comparison
        if remote_file.filepath:
            remote_filepaths.add(remote_file.filepath)

        if local_file:
            abs_local = local_file.filepath or ""
            # Compute the effective version for comparison (pure: no store
            # mutation). The tick is applied in-memory when the local content
            # differs from the stored base, so the advertised version is
            # causally honest without destroying the recorded common ancestor.
            # Persistence happens only after a successful sync (merge_and_record).
            eff, base_hash = sync_state.local_version_and_base(abs_local, local_file.sha256sum)
            local_vclock = eff["vclock"] if eff else None
            local_corrected = eff.get("corrected_mtime", 0) if eff else 0
            # Extract the remote advertised version (absent ⇒ empty clock).
            remote_vclock, remote_corrected = _parse_advertised_version(
                remote_file_info.get("version")
            )
            action, timedelta = _compare_files(
                remote_file,
                local_file,
                local_vclock,
                remote_vclock,
                base_hash,
                local_corrected_mtime=int(local_corrected),
                remote_corrected_mtime=int(remote_corrected),
            )

            # Permission-only divergence (chmod): the causal matrix above
            # compares content hashes, so an identical content with different
            # permissions resolves to ``nothing``. Permissions still propagate
            # as a normal `modified` change (no special policy):
            # - bidirectional / upload-only: the local mode wins → upload the
            #   file (the server/master same-SHA branch applies only the
            #   permissions and notifies the other clients);
            # - download-only: the remote mode wins → converge locally.
            # Gated on persist_state so a dry-run/preview never mutates the
            # local filesystem (same purity contract as merge_and_record).
            permissions_only = False
            if action == SyncActionName.nothing and persist_state:
                local_perms = local_file.permissions
                remote_perms = remote_file.permissions
                if local_perms and remote_perms and local_perms != remote_perms:
                    if CONFIG.get("SYNC_DIRECTION", "bidirectional") == "download-only":
                        if local_file.filepath:
                            set_permissions(local_file.filepath, remote_perms)
                            log.debug(
                                f'Permission-only converge (remote): "{local_file.filepath}" '
                                f"({remote_perms})"
                            )
                    else:
                        action = SyncActionName.upload
                        permissions_only = True

            # mtime-only divergence: the content hashes match (the causal
            # matrix resolved to ``nothing``) but the corrected mtimes differ.
            # A ``touch`` is still metadata that must converge, so the newer
            # corrected mtime wins (same tie-break philosophy as ADR-0023): the
            # winner pushes the value (metadata-only upload, no bytes) and the
            # loser adopts it locally. Unknown mtimes (0) never diverge.
            if not p2p_mode and action == SyncActionName.nothing and persist_state and abs_local:
                # Content is identical, so the causal matrix short-circuits; a
                # divergent on-disk mtime is a ``touch`` that must still
                # converge. The side whose version is causally ahead made the
                # change (a touch ticks the local clock in ``sync_state``): the
                # editor pushes the value (metadata-only upload) and the other
                # adopts it locally. Ties resolve by sync direction.
                local_mtime = int(sync_state._corrected_mtime_of(abs_local))
                remote_mtime = int(remote_corrected)
                if sync_state.mtime_diverges(local_mtime, remote_mtime):
                    direction = CONFIG.get("SYNC_DIRECTION", "bidirectional")
                    if direction == "download-only":
                        adopt_remote = True
                    elif direction == "upload-only":
                        adopt_remote = False
                    else:
                        # Bidirectional: the side whose version is causally
                        # ahead made the touch.
                        adopt_remote = (
                            compare_vclocks(local_vclock, remote_vclock) is VClockOrder.dominated
                        )
                    if adopt_remote:
                        if local_file.filepath:
                            _apply_local_mtime(local_file.filepath, remote_mtime)
                    else:
                        action = SyncActionName.upload
                        permissions_only = True

            # A download with timedelta == 0 is a conflict_download: the remote
            # won the tie-break, so the local copy must be preserved before
            # being overwritten.
            is_conflict = action == SyncActionName.download and timedelta == 0

            # Files already in sync: refresh the merge base with a merged clock
            # so a later one-sided edit resolves cleanly (convergence D11).
            # Skipped in preview mode (persist_state=False) so a dry-run never
            # mutates the persisted installation store.
            if action == SyncActionName.nothing and local_file.sha256sum and persist_state:
                sync_state.merge_and_record(abs_local, local_file.sha256sum, remote_vclock)

            sync_actions.append(
                SyncAction(
                    remote_file if action == SyncActionName.download else local_file,
                    action,
                    timedelta,
                    conflict=is_conflict,
                    permissions_only=permissions_only,
                )
            )
        else:
            # File exists only on remote. Before downloading it, check the
            # deletion markers: the marker is a tombstone recorded by THIS peer
            # when it deleted the path (live, with the master unreachable, or
            # offline between sync cycles). The remote copy is deleted only on
            # positive causal proof — the tombstone clock dominating the
            # remote's advertised version clock (ADR-0003) — which proves the
            # remote copy is a stale ancestor of the deletion.
            #
            # Content equality is deliberately NOT a delete proof. A file
            # re-created byte-for-byte identically (an empty file being the
            # trivial case) carries a hash equal to the tombstone's, but it is
            # an intentional re-creation, so it must be downloaded, not
            # deleted (add-wins semantics, mirroring Syncthing's
            # modification-beats-deletion policy). A marker without a valid
            # causal clock carries no proof of ancestry and never deletes.
            rel = str(remote_file_info.get("filepath", "")).lstrip("/")
            entry = tombstones.get(tombstone_key(rel))
            remote_vclock, _remote_corrected = _parse_advertised_version(
                remote_file_info.get("version")
            )
            causal_match = bool(entry and _tombstone_clock_dominates(entry, remote_vclock))
            if causal_match and CONFIG.get("SYNC_DIRECTION", "bidirectional") != "download-only":
                log.info(
                    f'{ActionLabel.TOMBSTONE} "{rel}" was deleted locally while offline; '
                    "deleting stale copy on remote"
                )
                sync_actions.append(SyncAction(remote_file, SyncActionName.delete_remote))
            else:
                # File exists only on remote - download it
                sync_actions.append(SyncAction(remote_file, SyncActionName.download))

    # Find local files not present on remote
    for local_file_dict in local_files:
        # .hsynckeep is a per-peer local sentinel: never propagate to remote.
        if basename(str(local_file_dict.get("filepath", ""))) == HSYNCKEEP_FILENAME:
            continue
        # Symlink entry: replicate the LINK (pair path+target), never the bytes
        # of its target. Escaping targets were already skipped by
        # get_files_local; the upload path re-validates the target anyway.
        if local_file_dict.get("type") == "symlink":
            link = Symlink.from_dict(local_file_dict, CONFIG["LOCAL_BUCKET"])
            if link.filepath and str(link.filepath).lstrip("/") not in remote_filepaths:
                sync_actions.append(SyncAction(link, SyncActionName.upload_symlink))
            continue
        local_file_info = File.from_dict(local_file_dict, CONFIG["LOCAL_BUCKET"])
        if local_file_info.filepath and local_file_info.filepath not in remote_filepaths:
            file = File.get(local_file_dict["filepath"])
            if file:
                if p2p_mode and CONFIG.get("SYNC_DIRECTION") == "download-only":
                    # P2P download-only slave mirrors master - delete local-only files
                    sync_actions.append(SyncAction(file, SyncActionName.delete))
                elif entry := tombstones.get(tombstone_key(local_file_info.filepath)):
                    # Causal delete-vs-recreate (ADR-0003): compare the tombstone
                    # clock against the *effective* local clock — ticked when the
                    # current content differs from the recorded base, so a local
                    # edit concurrent with the deletion is never mistaken for a
                    # stale ancestor. The wall clock is never consulted.
                    local_eff = sync_state.local_version_for_compare(
                        file.filepath or "", file.sha256sum
                    )
                    if _tombstone_deletes_local(entry, local_eff["vclock"] if local_eff else None):
                        # The deletion provably descends from the common history:
                        # remove the local copy instead of resurrecting it.
                        log.info(
                            f'{ActionLabel.TOMBSTONE} "{file.filepath}" deleted remotely while offline; '
                            "removing local copy"
                        )
                        sync_actions.append(
                            SyncAction(file, SyncActionName.delete, tombstone_confirmed=True)
                        )
                    else:
                        sync_actions.append(SyncAction(file, SyncActionName.upload))
                else:
                    # Default: upload local-only files (server mode and P2P
                    # bidirectional/upload-only converge toward shared state)
                    sync_actions.append(SyncAction(file, SyncActionName.upload))

    # ── Filter actions based on sync direction (pre-computed frozenset) ────
    if _excluded:
        sync_actions[:] = [a for a in sync_actions if a.name not in _excluded]

    return sync_actions


def _compare_files(
    remote_file: File,
    local_file: File,
    local_vclock: dict[str, int] | None,
    remote_vclock: dict[str, int] | None,
    base_hash: str | None = None,
    local_corrected_mtime: int = 0,
    remote_corrected_mtime: int = 0,
) -> tuple[SyncActionName, int]:
    """
    Compare two files using the causal vector-clock matrix (ADR-0022).

    Content identity is checked first (hash), then causal dominance, then the
    corrected-mtime tie-break on true divergence (ADR-0023), with the
    deterministic SHA-256 fallback when either corrected mtime is unknown or
    the two tie. The raw wall clock is never consulted.

    Args:
        remote_file: File metadata from remote/server.
        local_file: File metadata from local filesystem.
        local_vclock: Stored local clock (honest via self-record, §3.4.7).
        remote_vclock: Advertised remote clock (None ⇒ empty).
        base_hash: Last-synced common-ancestor content hash, if known.
        local_corrected_mtime: Corrected mtime of the local version (0 ⇒ unknown).
        remote_corrected_mtime: Corrected mtime of the remote version (0 ⇒ unknown).

    Returns:
        Tuple of (action, timedelta). ``timedelta == 0`` flags a conflict
        (``conflict_download``: preserve local + download remote).
    """
    decision = resolve_three_way(
        local_file.sha256sum or None,
        local_vclock,
        remote_file.sha256sum or None,
        remote_vclock,
        base_hash,
        local_corrected_mtime=local_corrected_mtime,
        remote_corrected_mtime=remote_corrected_mtime,
    )
    if decision is SyncDecision.in_sync:
        return SyncActionName.nothing, 0
    if decision is SyncDecision.download:
        return SyncActionName.download, 1
    if decision is SyncDecision.upload:
        return SyncActionName.upload, 1
    if decision is SyncDecision.conflict_upload:
        # Local wins tie-break: upload; the receiver preserves its losing copy.
        return SyncActionName.upload, 1
    # conflict_download: remote wins tie-break, preserve local + download.
    log.debug(
        f'Causal conflict for "{remote_file.filepath}": '
        "both local and remote changed since the last sync (tie-break: remote wins)"
    )
    return SyncActionName.download, 0


def _save_conflict_copy(file: File) -> str | None:
    """
    Preserve a locally-modified file before a conflicting download overwrites it.

    Invoked on ``conflict_download`` (the remote won the causal tie-break):
    local and remote diverged concurrently and the remote content is taken, so
    the local content is copied to
    ``<stem>_conflicted_copy_<ISO8601-UTC>_<cid>.<ext>`` — the same naming
    convention the server uses (``arbitration.py``) so conflict copies are
    consistent regardless of which side detected the divergence.

    Args:
        file: The file whose local content is about to be overwritten.

    Returns:
        The path of the conflict copy, or None if nothing was saved.
    """
    if not file.filepath:
        return None

    # ``file.filepath`` may be bucket-relative (files parsed from a remote
    # listing in get_sync_actions) or absolute (real-time events). The guard
    # and the copy below must operate on the *actual local file*, so a
    # relative path is resolved against LOCAL_BUCKET first — a raw relative
    # path would be evaluated against the process cwd and silently skip the
    # preservation, losing the divergent local content.
    raw = Path(file.filepath)
    if raw.is_absolute():
        src_path = raw
    else:
        from hsync_client.transfer import _local_dest_path

        resolved = _local_dest_path(file)
        if resolved is None:
            log.warning(
                f"{ActionLabel.SECURITY} Refusing to preserve unsafe conflict path "
                f'"{file.filepath}"'
            )
            return None
        src_path = Path(resolved)

    if not src_path.exists():
        return None
    from hsync_common.time_utils import conflict_timestamp

    from hsync_client.arbitration import conflicted_copy_name

    ts = conflict_timestamp()
    conflict_path = conflicted_copy_name(str(src_path), CONFIG.get("CID", ""), ts)
    try:
        copy2(str(src_path), conflict_path)
        log.warning(
            f'{ActionLabel.CONFLICT} "{src_path}" lost the causal tie-break; '
            f'local copy saved as "{conflict_path}"'
        )
        emit_event(
            UiEventName.conflict_resolved,
            file=str(src_path),
            conflict_copy=conflict_path,
            winner="remote",
        )
        return conflict_path
    except OSError as e:
        log.error(f'Could not save conflict copy for "{src_path}": {e}')
        return None


def update_dir(data: dict) -> Dir | None:
    """
    Update a local directory based on remote event data.

    Creates the directory if it doesn't exist, or updates its
    permissions if they differ from remote.

    Args:
        data: Dictionary with directory metadata from remote event.

    Returns:
        Dir object representing the local directory, or None if invalid.
    """
    # Remote event data is untrusted input: refuse unsafe paths.
    if validate_remote_relpath(str(data.get("path", ""))) is None:
        log.warning(
            f'{ActionLabel.SECURITY} Rejected unsafe remote directory: "{data.get("path")}"'
        )
        return None
    # Lexical safety is not enough: a symlink *inside* the bucket could still
    # redirect the mkdir/permission change outside it.
    if resolve_within(CONFIG["LOCAL_BUCKET"], str(data.get("path", ""))) is None:
        log.warning(
            f'{ActionLabel.SECURITY} Rejected remote directory escaping bucket: "{data.get("path")}"'
        )
        return None

    # Get local directory if it exists
    dir = Dir.get(data["path"], CONFIG["LOCAL_BUCKET"])

    if dir:
        # Directory exists - check if permissions need updating
        dir_data = Dir.from_dict(data, CONFIG["LOCAL_BUCKET"])
        if dir_data.permissions != dir.permissions and dir.path and dir_data.permissions:
            dir.permissions = dir_data.permissions
            set_permissions_on_dir(dir.path, dir.permissions)
    else:
        # Directory doesn't exist - create it
        dir = Dir.from_dict(data, CONFIG["LOCAL_BUCKET"])
        if dir.path:
            Path(dir.path).mkdir(parents=True, exist_ok=True)
            emit_event(UiEventName.dir_created, path=dir.path)
            if dir.permissions:
                set_permissions_on_dir(dir.path, dir.permissions)

    return dir


def update_file(
    data: dict,
    inventory_observer: InventoryObserver = NOOP_INVENTORY_OBSERVER,
    source_resolver: P2PSourceResolver | None = None,
) -> None:
    """
    Update a local file based on remote event data.

    Downloads the file if it doesn't exist locally or if the
    SHA-256 checksum differs. Updates permissions if only they differ.

    Args:
        data: Dictionary with file metadata from remote event.
    """
    # Import here to avoid circular import
    from hsync_client.transfer import download_file

    # Remote event data is untrusted input: refuse unsafe paths.
    if validate_remote_relpath(str(data.get("filepath", ""))) is None:
        log.warning(f'{ActionLabel.SECURITY} Rejected unsafe remote file: "{data.get("filepath")}"')
        return
    # Lexical safety is not enough: a symlink *inside* the bucket could still
    # redirect local metadata operations outside it.
    if resolve_within(CONFIG["LOCAL_BUCKET"], str(data.get("filepath", ""))) is None:
        log.warning(
            f'{ActionLabel.SECURITY} Rejected remote file escaping bucket: "{data.get("filepath")}"'
        )
        return

    file_data = File.from_dict(data, CONFIG["LOCAL_BUCKET"], False)

    if not file_data.filepath:
        log.error("Invalid file data: missing filepath")
        return

    if Path(file_data.filepath).exists():
        # File exists locally - check if update needed
        file = File.get(file_data.filepath)

        if file and file.sha256sum != file_data.sha256sum:
            # Content differs: decide via the causal matrix (ADR-0022). The
            # remote side is the advertised version (if any) on the event's
            # src File; the local side is the effective stored version. The
            # wall clock is never consulted.
            remote_vclock, remote_corrected = _parse_advertised_version(data.get("version"))
            local_vclock = None
            local_corrected = 0
            base_hash = None
            if file.filepath:
                eff, base_hash = sync_state.local_version_and_base(file.filepath, file.sha256sum)
                local_vclock = eff["vclock"] if eff else None
                local_corrected = eff.get("corrected_mtime", 0) if eff else 0
            decision = resolve_three_way(
                file.sha256sum or None,
                local_vclock,
                file_data.sha256sum or None,
                remote_vclock,
                base_hash,
                local_corrected_mtime=int(local_corrected),
                remote_corrected_mtime=int(remote_corrected),
            )
            # conflict_upload: local wins tie-break — skip the download; the
            # local change is uploaded on the next reconcile. No conflict copy
            # is saved (the local version is the winner).
            if decision is SyncDecision.conflict_upload:
                log.debug(
                    f'Causal conflict for "{file_data.filepath}": '
                    "local wins tie-break, skipping download (will upload on next reconcile)"
                )
                inventory_observer.upsert(file)
                return
            # conflict_download: remote wins tie-break — preserve the local
            # copy before overwriting it.
            if decision is SyncDecision.conflict_download:
                _save_conflict_copy(file)
            # download / dominance: take the remote version silently.
            file_data = File.from_dict(data, CONFIG["LOCAL_BUCKET"])
            # Skip if this exact file already failed permanently in this
            # cycle (realtime event re-queuing while the full sync runs).
            if _is_permanently_failed(file_data):
                log.debug(f'Skipping permanently-failed download: "{file_data.filepath}"')
                return
            try:
                download_file(
                    file_data,
                    inventory_observer=inventory_observer,
                    source_resolver=source_resolver,
                    refresh_peer_sources=source_resolver is not None,
                )
            except PermanentDownloadError as e:
                _record_permanent_download_failure(file_data, e)
                return
            # Invalidate cached SHA-256 — file content has changed
            if file_data.filepath:
                invalidate_sha256_cache(file_data.filepath)
        elif file is not None:
            # Content identical. A plain ``touch`` announced by a peer still
            # converges: adopt the peer's corrected mtime locally when it
            # diverges and this side is not causally ahead (an ahead local side
            # will push its own metadata on the next reconcile instead).
            remote_vclock, remote_corrected = _parse_advertised_version(data.get("version"))
            if (
                remote_corrected > 0
                and file.filepath
                and sync_state.mtime_diverges(
                    int(sync_state._corrected_mtime_of(file.filepath)), int(remote_corrected)
                )
            ):
                local_eff, _base = sync_state.local_version_and_base(file.filepath, file.sha256sum)
                if (
                    compare_vclocks(local_eff["vclock"] if local_eff else None, remote_vclock)
                    is not VClockOrder.dominates
                ):
                    _apply_local_mtime(file.filepath, int(remote_corrected))
                    sync_state.merge_and_record(file.filepath, file.sha256sum, remote_vclock)
                    inventory_observer.upsert(file)
                    return
            if file.permissions != file_data.permissions:
                # Only permissions differ - update them
                if file_data.permissions:
                    set_permissions(file_data.filepath, file_data.permissions)
            else:
                log.debug(f'File not changed: "{file_data.filepath}"')
            inventory_observer.upsert(file)
    else:
        # File doesn't exist locally - download it
        file = File.from_dict(data, CONFIG["LOCAL_BUCKET"])
        # Skip if this exact file already failed permanently in this cycle.
        if _is_permanently_failed(file):
            log.debug(f'Skipping permanently-failed download: "{file.filepath}"')
            return
        try:
            download_file(
                file,
                inventory_observer=inventory_observer,
                source_resolver=source_resolver,
                refresh_peer_sources=source_resolver is not None,
            )
        except PermanentDownloadError as e:
            _record_permanent_download_failure(file, e)
