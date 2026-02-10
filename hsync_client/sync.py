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
from collections.abc import Callable, Mapping
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

from hsync_common.clock import clamp_incoming_corrected_mtime, offset_store
from hsync_common.models.dir import Dir, is_hidden_dir
from hsync_common.models.file import (
    File,
    clear_sha256_cache,
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
from hsync_client.retry import retry_on_network_error
from hsync_client.utils import (
    get_ssl_verify,
    set_permissions,
    set_permissions_on_dir,
)

# ── Sync-cycle coordination flag ──────────────────────────────────────────
# A threading.Event (not a Lock) because the watcher must NOT be blocked:
# it still broadcasts events during a sync cycle. The flag only lets the
# periodic resync skip a redundant cycle when one is already in progress.
# See ADR 0006 / llm-wiki/decisions.md.
_sync_cycle_active = threading.Event()


def is_sync_cycle_active() -> bool:
    """Return True while a full sync() cycle is in progress.

    The periodic resync task consults this to avoid scheduling a
    second concurrent sync() cycle. The flag is advisory (non-blocking):
    callers never wait on it.
    """
    return _sync_cycle_active.is_set()


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

    Returns:
        Number of partial files removed.
    """
    removed_count = 0
    local_bucket = CONFIG["LOCAL_BUCKET"]

    for root, _dirs, files in walk(local_bucket):
        for file in files:
            if is_partial_file(file):
                partial_path = os.path.join(root, file)
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


async def sync(lan_mode: bool = False) -> SyncCycleResult:
    """
    Perform a full synchronization between local and remote files.

    Compares local and remote file lists, determines required actions,
    and executes them in parallel (download, upload, delete, or skip).

    Uses a thread pool executor with semaphore to limit concurrent
    transfers and avoid overwhelming the server.

    Args:
        lan_mode: If True, sync is coordinated against a LAN master peer.
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

    # Check if local directory exists before starting sync
    if not Path(CONFIG["LOCAL_BUCKET"]).exists():
        log.error(f"Local directory '{CONFIG['LOCAL_BUCKET']}' does not exist. Sync aborted.")
        return SyncCycleResult(
            completed=False, transfers=0, failed=0, conflicts=0, guard_tripped=False
        )

    _sync_cycle_active.set()
    try:
        offset_store.begin_epoch()
        # Clear SHA-256 cache so every file is re-hashed with fresh content
        clear_sha256_cache()

        # Clean up any leftover partial downloads from previous sessions
        _cleanup_partial_files()

        # Ensure empty directories have sentinel files
        ensure_hsynckeep_files()

        # Get file lists from both sides
        local_files = get_files_local()
        remote_files = get_files_remote()

        # Fetch deletion markers so files deleted while this peer was offline
        # are not resurrected during the full reconciliation below.
        tombstones = get_tombstones_remote()

        # Merge the remote markers into our persistent local store and push the
        # accumulated union back so the server / master converges to every known
        # deletion.  In LAN mode the master can change (elected), making gossip
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
                )
                minted = True
        # Push the union once after all offline-deletion markers are minted:
        # the intermediate per-marker pushes were redundant (the master only
        # ever consumes the final union, and the push is best-effort by
        # contract), and each push is a full HTTP round-trip.
        if minted:
            union = get_payload()
            push_tombstones_remote(union)
            tombstones = cast("dict[str, dict]", union)

        # Determine what actions need to be taken
        sync_actions = get_sync_actions(
            remote_files, local_files, lan_mode=lan_mode, tombstones=tombstones
        )

        # Directory permission-only convergence (download-only): the remote
        # mode wins. Directories never appear in the /files listing, so the
        # dedicated /dirs listing is fetched on demand (best-effort: an older
        # peer without the endpoint simply skips the converge).
        if CONFIG.get("SYNC_DIRECTION", "bidirectional") == "download-only":
            converge_directory_permissions(get_dirs_remote())

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

        # Handle deletions (files that exist only locally in LAN mode)
        for file in deletes:
            if file.filepath and Path(file.filepath).exists():
                try:
                    remove(file.filepath)
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
                _save_conflict_copy(action.file)

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
                    download_symlink(link)
                except Exception as e:  # pragma: no cover - best-effort
                    log.error(f'Error downloading symlink "{link.filepath}": {e}')

        # Execute transfers in parallel using thread pool
        transfer_counters: dict[str, int] = {"failed": 0}
        if downloads or uploads or permission_uploads:
            # Re-derive concurrency from the live transfer queue profile
            _refresh_concurrency_from_queue(downloads, uploads + permission_uploads)
            await _execute_parallel_transfers(
                downloads, uploads, transfer_counters, permission_uploads
            )

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

    async def transfer_with_semaphore(func: Callable[[File], None], file: File) -> None:
        """Execute a transfer function with semaphore limiting."""
        async with semaphore:
            # Skip downloads that already failed permanently in this
            # cycle (the skip-set is consulted here too because a realtime
            # event can enqueue a file after the full-sync pre-filter ran).
            if func is download_file and _is_permanently_failed(file):
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
    for file in downloads:
        tasks.append(transfer_with_semaphore(download_file, file))

    # Add upload tasks
    for file in uploads:
        tasks.append(transfer_with_semaphore(upload_file, file))

    # Permission-only uploads skip the content dedup on the peer (same-SHA
    # branch applies the new mode); they re-use the same upload machinery.
    if permission_uploads:
        from functools import partial

        perm_upload = partial(upload_file, permissions_only=True)
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


def ensure_hsynckeep_files() -> None:
    """
    Ensure every empty directory under LOCAL_BUCKET contains a sentinel
    `.hsynckeep` file so that the directory structure is preserved during sync.

    A directory is considered "empty" when it contains no syncable files
    (after filtering out hidden files).  Already-existing `.hsynckeep`
    files count as syncable, so they prevent a second creation.
    """
    local_bucket = CONFIG["LOCAL_BUCKET"]
    for root, dirs, files in walk(local_bucket):
        # Skip hidden directories (same filter as get_files_local)
        dirs[:] = [d for d in dirs if not is_hidden_dir(d)]

        # Check if there are any syncable (non-hidden) files
        has_syncable = any(not is_hidden_file(f) for f in files)

        if not has_syncable:
            sentinel = os.path.join(root, HSYNCKEEP_FILENAME)
            if not Path(sentinel).exists():
                try:
                    open(sentinel, "w").close()  # noqa: SIM115
                    log.debug(f'Created sentinel "{sentinel}"')
                except OSError as e:
                    log.warning(f'Cannot create sentinel "{sentinel}": {e}')


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
    file_paths = []
    for root, dirs, files in walk(CONFIG["LOCAL_BUCKET"]):
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
                if link is not None and not is_safe_symlink_target(
                    file_path, link.target, CONFIG["LOCAL_BUCKET"]
                ):
                    log.warning(
                        f"{ActionLabel.SECURITY} Symlink target escapes bucket, "
                        f'skipping: "{file_path}" -> "{link.target}"'
                    )
                    continue
            file_paths.append(file_path)

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
    Push this peer's deletion markers to the current LAN master (gossip up).

    Best-effort by design: a master that does not expose the endpoint (older
    version), an empty payload, or any transport/HTTP failure is swallowed so
    the rest of the sync proceeds unchanged. This is only used in LAN mode,
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
    cid = str(CONFIG.get("CID", "") or "")
    return cid or None


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


# ── Download-only directory permission converge (dir chmod) ───────────────
# Directories carry no content hash and never appear in the /files listing, so
# the reconcile compares the dedicated /dirs listing against the local tree and
# applies the remote mode when it diverges. This runs only inside sync() —
# never in previews: a dry-run calls get_sync_actions(persist_state=False)
# directly and never reaches this path.


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
    """Apply divergent remote directory permissions locally (download-only).

    Each entry is validated (untrusted wire input) and compared against the
    local directory mode; on divergence the remote mode wins. Every chmod is
    best-effort — a failure is logged and never aborts the cycle.
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


def get_sync_actions(
    remote_files: list[dict] | None = None,
    local_files: list[dict] | None = None,
    lan_mode: bool = False,
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
        lan_mode: If True, sync targets a LAN master peer. Local-only files
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
        # The remote listing is untrusted input (hostile server or LAN
        # master): drop entries whose path is unsafe or names an internal
        # engine artifact instead of acting on them.
        if validate_remote_relpath(str(remote_file_info.get("filepath", ""))) is None:
            log.warning(
                f"{ActionLabel.SECURITY} Ignoring unsafe remote listing entry: "
                f'"{remote_file_info.get("filepath")}"'
            )
            continue
        # Symlink entry: replicate STRUCTURALLY (the link, never the target
        # content). The same code path serves remote (server listing) and LAN
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
            eff = sync_state.local_version_for_compare(abs_local, local_file.sha256sum)
            local_vclock = eff["vclock"] if eff else None
            local_corrected = eff.get("corrected_mtime", 0) if eff else 0
            base_hash = sync_state.get_base_hash(abs_local)
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
                if lan_mode and CONFIG.get("SYNC_DIRECTION") == "download-only":
                    # LAN download-only slave mirrors master - delete local-only files
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
                    # Default: upload local-only files (server mode and LAN
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

    ts = conflict_timestamp()
    cid = CONFIG.get("CID", "") or "unknown"
    safe_cid = "".join(c for c in cid if c.isalnum() or c in "-_.")[:32]
    conflict_path = (
        f"{src_path.parent}/{src_path.stem}_conflicted_copy_{ts}_{safe_cid}{src_path.suffix}"
    )
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


def update_file(data: dict) -> None:
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
                eff = sync_state.local_version_for_compare(file.filepath, file.sha256sum)
                local_vclock = eff["vclock"] if eff else None
                local_corrected = eff.get("corrected_mtime", 0) if eff else 0
                base_hash = sync_state.get_base_hash(file.filepath)
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
                download_file(file_data)
            except PermanentDownloadError as e:
                _record_permanent_download_failure(file_data, e)
                return
            # Invalidate cached SHA-256 — file content has changed
            if file_data.filepath:
                invalidate_sha256_cache(file_data.filepath)
        elif file and file.permissions != file_data.permissions:
            # Only permissions differ - update them
            if file_data.permissions:
                set_permissions(file_data.filepath, file_data.permissions)
        else:
            log.debug(f'File not changed: "{file_data.filepath}"')
    else:
        # File doesn't exist locally - download it
        file = File.from_dict(data, CONFIG["LOCAL_BUCKET"])
        # Skip if this exact file already failed permanently in this cycle.
        if _is_permanently_failed(file):
            log.debug(f'Skipping permanently-failed download: "{file.filepath}"')
            return
        try:
            download_file(file)
        except PermanentDownloadError as e:
            _record_permanent_download_failure(file, e)
