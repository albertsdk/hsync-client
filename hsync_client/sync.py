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
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from os import cpu_count, remove, walk
from pathlib import Path
from shutil import copy2
from socket import gethostname
from sys import exc_info
from traceback import print_tb
from typing import cast

from hsync_common.models.tombstone import TombstoneEntry, tombstone_key
from hsync_common.models.version import SyncDecision, resolve_three_way
from loguru import logger as log
from requests import RequestException, get, post

from hsync_client import sync_state
from hsync_client.config import CONFIG, is_debug
from hsync_client.constants import HSYNCKEEP_FILENAME, MAX_CONCURRENT_TRANSFERS
from hsync_client.events import UiEventName, emit_event
from hsync_client.exceptions import AuthenticationError, NetworkError, ServerError
from hsync_client.models.dir import Dir, is_hidden_dir
from hsync_client.models.file import (
    File,
    clear_sha256_cache,
    invalidate_sha256_cache,
    is_hidden_file,
)
from hsync_client.models.sync_action import SyncAction, SyncActionName
from hsync_client.retry import retry_on_network_error
from hsync_client.utils import get_ssl_verify, set_permissions, verify_master_fingerprint

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
    from os import walk

    removed_count = 0
    local_bucket = CONFIG["LOCAL_BUCKET"]

    for root, _dirs, files in walk(local_bucket):
        for file in files:
            if file.endswith(".part"):
                partial_path = str(Path(root) / file)
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


async def sync(lan_mode: bool = False) -> None:
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
    """
    # Import here to avoid circular import
    from hsync_client.transfer import nothing_to_do

    # Check if local directory exists before starting sync
    if not Path(CONFIG["LOCAL_BUCKET"]).exists():
        log.error(f"Local directory '{CONFIG['LOCAL_BUCKET']}' does not exist. Sync aborted.")
        return

    try:
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

        # In LAN mode the master is elected and can change, so each peer keeps
        # its own persistent store and gossips it: merge the master's markers
        # into our store, decide using the accumulated union, then push our
        # union back so a freshly elected master converges to all deletions.
        if lan_mode:
            from hsync_client.tombstones import get_payload, merge_payload

            merge_payload(tombstones)
            union = get_payload()
            push_tombstones_remote(union)
            tombstones = cast("dict[str, dict]", union)

        # Determine what actions need to be taken
        sync_actions = get_sync_actions(
            remote_files, local_files, lan_mode=lan_mode, tombstones=tombstones
        )

        log.info(f"Sync actions: {len(sync_actions)} files to process")

        # Drop merge-base records for files that no longer exist locally so the
        # sync-state side-car stays bounded (downloads re-record after transfer).
        sync_state.retain([str(f.get("filepath", "")) for f in local_files])

        # Separate actions by type
        downloads = [a.file for a in sync_actions if a.name == SyncActionName.download]
        uploads = [a.file for a in sync_actions if a.name == SyncActionName.upload]
        deletes = [a.file for a in sync_actions if a.name == SyncActionName.delete]
        nothing = [a.file for a in sync_actions if a.name == SyncActionName.nothing]

        # Log files that are already in sync (no I/O needed)
        for file in nothing:
            nothing_to_do(file)

        # Mass-deletion guard: a master that transiently exposes an empty or
        # wrong bucket must not wipe valid local data in one cycle.
        blocked, reason = _mass_deletion_blocked(len(deletes), len(local_files), len(remote_files))
        if blocked:
            log.warning(
                f"[GUARD     ] Mass-deletion guard tripped ({reason}); "
                f"skipping {len(deletes)} deletion(s) this cycle"
            )
            emit_event(
                UiEventName.sync_paused,
                reason=reason,
                deletes=len(deletes),
                local=len(local_files),
            )
            deletes = []

        # Handle deletions (files that exist only locally in LAN mode)
        for file in deletes:
            if file.filepath and Path(file.filepath).exists():
                try:
                    remove(file.filepath)
                    sync_state.clear_base(file.filepath)
                    log.info(f'[DELETE    ] "{file.filepath}" (not on master)')
                    emit_event(UiEventName.transfer, op="delete", file=file.filepath)
                except OSError as e:
                    log.error(f'Error deleting "{file.filepath}": {e}')

        # Preserve locally-modified files before a conflicting download
        # overwrites them (same mtime, different content - no automatic winner).
        for action in sync_actions:
            if action.name == SyncActionName.download and getattr(action, "conflict", False):
                _save_conflict_copy(action.file)

        # Execute transfers in parallel using thread pool
        if downloads or uploads:
            await _execute_parallel_transfers(downloads, uploads)

        # Persist refreshed merge bases (in-sync refresh, retain, clear-on-delete)
        # in a single atomic write at the end of the cycle.
        sync_state.flush()

        log.info("Sync completed successfully")
        emit_event(UiEventName.sync_complete)

    except (AuthenticationError, NetworkError, ServerError) as e:
        log.error(f"Sync failed: {e}")
        emit_event(UiEventName.error, message=f"Sync failed: {e}")
        if is_debug():
            exc_type, exc_value, exc_tb = exc_info()
            if exc_tb:
                print_tb(exc_tb)
    except Exception as e:
        log.error(f"Unexpected error during sync: {e}")
        emit_event(UiEventName.error, message=f"Unexpected error during sync: {e}")
        if is_debug():
            exc_type, exc_value, exc_tb = exc_info()
            if exc_tb:
                print_tb(exc_tb)


async def _execute_parallel_transfers(
    downloads: list[File],
    uploads: list[File],
) -> None:
    """
    Execute file transfers in parallel using a thread pool.

    Uses asyncio.Semaphore to limit concurrent transfers and
    run_in_executor to run synchronous transfer functions in threads.

    Args:
        downloads: List of files to download from remote.
        uploads: List of files to upload to remote.
    """
    from hsync_client.transfer import download_file, upload_file

    # Semaphore to limit concurrent transfers
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TRANSFERS)

    # Get the current event loop
    loop = asyncio.get_running_loop()

    # Create a thread pool for I/O-bound operations
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_TRANSFERS) as executor:

        async def transfer_with_semaphore(func: Callable[[File], None], file: File) -> None:
            """Execute a transfer function with semaphore limiting."""
            async with semaphore:
                try:
                    # Run synchronous function in thread pool
                    await loop.run_in_executor(executor, func, file)
                except Exception as e:
                    log.error(f"Transfer error for {file.filepath}: {e}")

        # Create tasks for all transfers
        tasks = []

        # Add download tasks
        for file in downloads:
            tasks.append(transfer_with_semaphore(download_file, file))

        # Add upload tasks
        for file in uploads:
            tasks.append(transfer_with_semaphore(upload_file, file))

        # Execute all transfers concurrently
        if tasks:
            log.info(
                f"Starting parallel transfers: {len(downloads)} downloads, "
                f"{len(uploads)} uploads (max {MAX_CONCURRENT_TRANSFERS} concurrent)"
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
            sentinel = str(Path(root) / HSYNCKEEP_FILENAME)
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
    Get metadata for a single file (used for parallel processing).

    Retries up to 3 times with exponential backoff if the file is
    temporarily locked or inaccessible (common with files being written).

    Args:
        file_path: Full path to the file.

    Returns:
        File metadata dictionary or None if file cannot be read.
    """
    import time
    from errno import EACCES, EBUSY, EIO, ENOENT

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

    Uses CPU count to determine parallelism, with a configurable maximum
    and sensible defaults for different workload sizes.

    Args:
        file_count: Number of files to process.

    Returns:
        Optimal number of worker threads.
    """
    # Get available CPU cores (default to 4 if detection fails)
    try:
        cpu_cores = cpu_count() or 4
    except Exception:
        cpu_cores = 4

    # Allow configuration via environment variable
    import os

    env_max = os.environ.get("HSYNC_MAX_FILE_WORKERS")
    if env_max:
        try:
            max_allowed = int(env_max)
            return min(max_allowed, file_count, cpu_cores)
        except ValueError:
            pass

    # For small file counts, don't over-parallelize
    if file_count < 10:
        return min(2, file_count)

    # For medium file counts, use moderate parallelism
    if file_count < 100:
        return min(4, file_count, cpu_cores)

    # For large file counts, use more parallelism but cap at 8
    # to avoid overwhelming the system with too many threads
    return min(8, file_count, cpu_cores)


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
    from concurrent.futures import ThreadPoolExecutor

    # Check if local directory exists before walking
    if not Path(CONFIG["LOCAL_BUCKET"]).exists():
        log.warning(
            f"Local directory '{CONFIG['LOCAL_BUCKET']}' does not exist during synchronization"
        )
        return []

    # First pass: collect all file paths (no checksum computation)
    file_paths = []
    for root, dirs, files in walk(CONFIG["LOCAL_BUCKET"]):
        # Skip hidden directories
        dirs[:] = [d for d in dirs if not is_hidden_dir(d)]

        for file in files:
            # Skip hidden files
            if is_hidden_file(file):
                continue
            file_paths.append(str(Path(root) / file))

    # Early return if no files
    if not file_paths:
        return []

    # Determine optimal number of workers based on file count and CPU cores
    max_workers = _get_optimal_workers(len(file_paths))
    log.debug(f"Using {max_workers} workers for {len(file_paths)} files")

    file_list = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all file metadata fetches in parallel
        futures = {executor.submit(_get_single_file_metadata, fp): fp for fp in file_paths}

        # Collect results as they complete
        for future in futures:
            result = future.result()
            if result:
                file_list.append(result)

    # Sort by mtime ascending (oldest first) to prioritize updating older files
    file_list.sort(key=lambda x: x.get("mtime", 0))

    return file_list


@retry_on_network_error(max_attempts=3, min_wait=1, max_wait=10)
def get_files_remote() -> list[dict]:
    """
    Get list of all remote files from the server.

    Makes an HTTP GET request to retrieve file metadata from the
    remote bucket, sorted by the configured sync order. Retries
    automatically on network errors with exponential backoff.

    Returns:
        List of file metadata dictionaries from server.

    Raises:
        NetworkError: If the request fails after retries.
        AuthenticationError: If authentication fails (401/403).
        ServerError: If server returns an error response.
    """
    uri = f"{CONFIG['API']['buckets']}/{CONFIG['REMOTE_BUCKET']}/files"
    try:
        # Verify master certificate fingerprint for LAN HTTPS mode
        if not verify_master_fingerprint(uri):
            raise NetworkError(
                message="Master certificate fingerprint verification failed",
                details={"url": uri},
            )

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
            timeout=CONFIG.get("HTTP_TIMEOUT", 30),
        )

        if response.status_code == 401:
            raise AuthenticationError(message="Invalid API key")
        if response.status_code == 403:
            raise AuthenticationError(message="API key required")
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


@retry_on_network_error(max_attempts=3, min_wait=1, max_wait=10)
def get_tombstones_remote() -> dict[str, dict]:
    """
    Get deletion markers (tombstones) from the server.

    Makes an HTTP GET request to retrieve the tombstone payload: a mapping
    of hashed bucket-relative path to ``{"ts": <deletion timestamp>,
    "sha256": <content hash of the deleted file>}``. Clients use these
    markers to avoid resurrecting files deleted while they were offline.

    Degrades gracefully: a server that does not expose the endpoint (older
    version) or any non-200 response yields an empty mapping, so the rest of
    the sync proceeds unchanged.

    Returns:
        Mapping of hashed path to tombstone entry (possibly empty).

    Raises:
        NetworkError: If the request fails after retries.
        AuthenticationError: If authentication fails (401/403).
    """
    uri = f"{CONFIG['API']['buckets']}/{CONFIG['REMOTE_BUCKET']}/tombstones"
    try:
        # Verify master certificate fingerprint for LAN HTTPS mode
        if not verify_master_fingerprint(uri):
            raise NetworkError(
                message="Master certificate fingerprint verification failed",
                details={"url": uri},
            )

        response = get(
            uri,
            headers=cast(Mapping[str, str], CONFIG["REQUEST_HEADERS"]),
            verify=get_ssl_verify(),
            timeout=CONFIG.get("HTTP_TIMEOUT", 30),
        )

        if response.status_code == 401:
            raise AuthenticationError(message="Invalid API key")
        if response.status_code == 403:
            raise AuthenticationError(message="API key required")
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
        if not verify_master_fingerprint(uri):
            log.debug("Skipping tombstone push: fingerprint verification failed")
            return

        post(
            uri,
            json=cast(dict, dict(payload)),
            headers=cast(dict, CONFIG["REQUEST_HEADERS"]),
            verify=get_ssl_verify(),
            timeout=CONFIG.get("HTTP_TIMEOUT", 30),
        )
    except RequestException as e:
        log.debug(f"Tombstone push failed (non-fatal): {e}")


# =============================================================================
# Sync Action Determination
# =============================================================================

# Tolerance threshold in seconds for mtime comparison.
# Handles FAT32 filesystem precision (2s) and minor clock skew.
MTIME_TOLERANCE = 2


def _tombstone_deletes_local(entry: dict, local_mtime: int, local_sha256: str) -> bool:
    """
    Decide whether a tombstone justifies deleting a solo-local file.

    The rule is intentionally data-safe: a local file that exists only on this
    peer is removed *only* when it is byte-identical to the file that was
    deleted elsewhere and was not (re-)created after that deletion. Any local
    edit (different content) or a clearly newer mtime is treated as an
    intentional re-creation and the file is kept (uploaded) instead.

    Args:
        entry: Tombstone entry ``{"ts": int, "sha256": str | None}``.
        local_mtime: Modification time of the local file.
        local_sha256: Content hash of the local file.

    Returns:
        True if the local file should be deleted, False to keep/upload it.
    """
    deleted_sha = entry.get("sha256")
    ts = int(entry.get("ts", 0))

    if deleted_sha and local_sha256:
        # Same content as the deleted file: a stale copy unless re-created
        # after the deletion (guards against path reuse).
        return deleted_sha == local_sha256 and local_mtime <= ts + MTIME_TOLERANCE

    # No content hash to compare (legacy marker): fall back to timestamps and
    # delete only when the deletion is clearly newer than the local file.
    return ts - local_mtime > MTIME_TOLERANCE


def get_sync_actions(
    remote_files: list[dict] | None = None,
    local_files: list[dict] | None = None,
    lan_mode: bool = False,
    tombstones: dict[str, dict] | None = None,
) -> list[SyncAction]:
    """
    Determine sync actions by comparing local and remote file lists.

    Uses an intelligent comparison strategy:
    1. If mtime difference > MTIME_TOLERANCE (2s): use mtime to decide direction.
    2. If mtime difference <= MTIME_TOLERANCE: fall back to SHA-256 checksum.
       - If SHA-256 differs: use mtime as tiebreaker; if equal, prefer remote.
       - If SHA-256 matches: no action needed (files are identical).

    This handles clock skew, FAT32 timestamp precision (2s), and cases
    where mtime is equal but content differs (copy/restore scenarios).

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

    Returns:
        List of SyncAction objects describing required operations.
    """
    if remote_files is None:
        remote_files = []
    if local_files is None:
        local_files = []
    if tombstones is None:
        tombstones = {}

    sync_actions: list[SyncAction] = []
    remote_filepaths: set[str] = set()

    # Process remote files
    for remote_file_info in remote_files:
        # .hsynckeep is a per-peer local sentinel: never exchange it across peers.
        if Path(str(remote_file_info.get("filepath", ""))).name == HSYNCKEEP_FILENAME:
            continue
        remote_file = File.from_dict(remote_file_info, CONFIG["LOCAL_BUCKET"])
        local_file = File.get(remote_file_info["filepath"], CONFIG["LOCAL_BUCKET"])

        # Track remote paths for later comparison
        if remote_file.filepath:
            remote_filepaths.add(remote_file.filepath)

        # Get mtime values with defaults for comparison
        remote_mtime = remote_file.mtime or 0
        local_mtime = local_file.mtime if local_file and local_file.mtime else 0

        if local_file:
            base_hash = sync_state.get_base_hash(local_file.filepath or "")
            action, timedelta = _compare_files(
                remote_file,
                local_file,
                remote_mtime,
                local_mtime,
                lan_mode,
                base_hash,
            )

            # A download with timedelta == 0 in this branch is a conflict: either
            # the unconvincing legacy tie (same mtime, different content) or a
            # three-way divergence. The remote wins but the local copy must be
            # preserved before being overwritten.
            is_conflict = action == SyncActionName.download and timedelta == 0

            # Files already in sync: refresh the merge base to the shared hash so
            # a later one-sided edit resolves cleanly via the three-way merge.
            if action == SyncActionName.nothing and local_file.sha256sum:
                sync_state.record_base(local_file.filepath or "", local_file.sha256sum)

            sync_actions.append(
                SyncAction(
                    remote_file if action == SyncActionName.download else local_file,
                    action,
                    timedelta,
                    conflict=is_conflict,
                )
            )
        else:
            # File exists only on remote - download it
            sync_actions.append(SyncAction(remote_file, SyncActionName.download))

    # Find local files not present on remote
    for local_file_dict in local_files:
        # .hsynckeep is a per-peer local sentinel: never propagate to remote.
        if Path(str(local_file_dict.get("filepath", ""))).name == HSYNCKEEP_FILENAME:
            continue
        local_file_info = File.from_dict(local_file_dict, CONFIG["LOCAL_BUCKET"])
        if local_file_info.filepath and local_file_info.filepath not in remote_filepaths:
            file = File.get(local_file_dict["filepath"])
            if file:
                if lan_mode and CONFIG.get("SYNC_DIRECTION") == "download-only":
                    # LAN download-only slave mirrors master - delete local-only files
                    sync_actions.append(SyncAction(file, SyncActionName.delete))
                elif (entry := tombstones.get(tombstone_key(local_file_info.filepath))) and (
                    _tombstone_deletes_local(entry, file.mtime or 0, file.sha256sum or "")
                ):
                    # The file was deleted elsewhere while this peer was offline
                    # and the local copy is unchanged: remove it instead of
                    # resurrecting it on the master.
                    log.info(
                        f'[TOMBSTONE  ] "{file.filepath}" deleted remotely while offline; '
                        "removing local copy"
                    )
                    sync_actions.append(SyncAction(file, SyncActionName.delete))
                else:
                    # Default: upload local-only files (server mode and LAN
                    # bidirectional/upload-only converge toward shared state)
                    sync_actions.append(SyncAction(file, SyncActionName.upload))

    # ── Filter actions based on sync direction ──────────────────────
    direction = CONFIG.get("SYNC_DIRECTION", "bidirectional")
    if direction == "upload-only":
        # Remove downloads and remote-triggered deletes; keep uploads and nothing
        sync_actions = [
            a
            for a in sync_actions
            if a.name not in (SyncActionName.download, SyncActionName.delete)
        ]
    elif direction == "download-only":
        # Remove uploads; keep downloads, deletes and nothing
        sync_actions = [a for a in sync_actions if a.name != SyncActionName.upload]

    return sync_actions


def _compare_files(
    remote_file: File,
    local_file: File,
    remote_mtime: int,
    local_mtime: int,
    lan_mode: bool,
    base_hash: str | None = None,
) -> tuple[SyncActionName, int]:
    """
    Compare two files, preferring a clock-independent three-way merge.

    When a common-ancestor ``base_hash`` is recorded, the direction is
    decided from content hashes alone: if only one side changed since the base it
    wins, and if both changed it is a genuine conflict (remote wins, local copy
    preserved). Without a recorded base the legacy mtime + SHA-256 heuristic is
    used so first-ever syncs and upgraded peers behave exactly as before.

    Args:
        remote_file: File metadata from remote/server.
        local_file: File metadata from local filesystem.
        remote_mtime: Remote file modification time.
        local_mtime: Local file modification time.
        lan_mode: Whether running in LAN mode.
        base_hash: Last-synced common-ancestor content hash, if known.

    Returns:
        Tuple of (action, timedelta). A download with ``timedelta == 0`` flags a
        conflict to the caller.
    """
    decision = resolve_three_way(
        local_file.sha256sum or None,
        remote_file.sha256sum or None,
        base_hash,
    )
    if decision is SyncDecision.in_sync:
        return SyncActionName.nothing, 0
    if decision is SyncDecision.download:
        # Only the remote changed since base: take it (clock-independent, so the
        # timedelta is forced positive to never be mistaken for a conflict tie).
        return SyncActionName.download, max(remote_mtime - local_mtime, 1)
    if decision is SyncDecision.upload:
        # Only the local side changed since base: push it.
        return SyncActionName.upload, max(local_mtime - remote_mtime, 1)
    if decision is SyncDecision.conflict:
        # Both sides diverged from the base: remote wins, local copy preserved.
        log.debug(
            f'Three-way conflict for "{remote_file.filepath}": '
            "both local and remote changed since the last sync"
        )
        return SyncActionName.download, 0

    # decision is unknown_base -> fall back to the legacy mtime/SHA-256 heuristic.
    mtime_diff = abs(remote_mtime - local_mtime)

    if mtime_diff > MTIME_TOLERANCE:
        # Clear mtime difference - trust timestamps
        if remote_mtime > local_mtime:
            return SyncActionName.download, remote_mtime - local_mtime
        else:
            return SyncActionName.upload, local_mtime - remote_mtime

    # mtime within tolerance - fall back to SHA-256 checksum
    remote_sha256 = remote_file.sha256sum or ""
    local_sha256 = local_file.sha256sum or ""

    if remote_sha256 == local_sha256:
        # Content is identical - no action needed
        return SyncActionName.nothing, 0

    # Content differs despite similar mtime - need to pick a direction
    log.debug(
        f'SHA-256 mismatch within mtime tolerance for "{remote_file.filepath}": '
        f"remote_sha256={remote_sha256[:8]}... local_sha256={local_sha256[:8]}... "
        f"mtime_diff={mtime_diff}s"
    )
    if remote_mtime > local_mtime:
        return SyncActionName.download, remote_mtime - local_mtime
    elif remote_mtime < local_mtime:
        return SyncActionName.upload, local_mtime - remote_mtime
    else:
        # Exact same mtime but different content - prefer remote
        # (server/master is authoritative for conflict resolution)
        return SyncActionName.download, 0


def _save_conflict_copy(file: File) -> str | None:
    """
    Preserve a locally-modified file before a conflicting download overwrites it.

    Invoked when local and remote content differ but share an identical mtime,
    so no winner can be determined automatically and the remote version is taken.
    The local content is copied to ``<filepath>.conflict-<host>-<timestamp>`` so
    the user can recover it.

    Args:
        file: The file whose local content is about to be overwritten.

    Returns:
        The path of the conflict copy, or None if nothing was saved.
    """
    if not (file.filepath and Path(file.filepath).exists()):
        return None
    host = gethostname() or "unknown"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    conflict_path = f"{file.filepath}.conflict-{host}-{stamp}"
    try:
        copy2(file.filepath, conflict_path)
        log.warning(
            f'[CONFLICT  ] "{file.filepath}" differs from remote at identical '
            f'mtime; local copy saved as "{conflict_path}"'
        )
        return conflict_path
    except OSError as e:
        log.error(f'Could not save conflict copy for "{file.filepath}": {e}')
        return None


# =============================================================================
# Directory and File Updates
# =============================================================================


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
    # Get local directory if it exists
    dir = Dir.get(data["path"], CONFIG["LOCAL_BUCKET"])

    if dir:
        # Directory exists - check if permissions need updating
        dir_data = Dir.from_dict(data, CONFIG["LOCAL_BUCKET"])
        if dir_data.permissions != dir.permissions and dir.path and dir_data.permissions:
            dir.permissions = dir_data.permissions
            set_permissions(dir.path, dir.permissions)
    else:
        # Directory doesn't exist - create it
        dir = Dir.from_dict(data, CONFIG["LOCAL_BUCKET"])
        if dir.path:
            Path(dir.path).mkdir(parents=True, exist_ok=True)
            emit_event(UiEventName.dir_created, path=dir.path)
            if dir.permissions:
                set_permissions(dir.path, dir.permissions)

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

    file_data = File.from_dict(data, CONFIG["LOCAL_BUCKET"], False)

    if not file_data.filepath:
        log.error("Invalid file data: missing filepath")
        return

    if Path(file_data.filepath).exists():
        # File exists locally - check if update needed
        file = File.get(file_data.filepath)

        if file and file.sha256sum != file_data.sha256sum:
            # Content differs - download new version. Preserve the local copy
            # first when it is as-new-or-newer than the incoming version: an
            # inbound event always overwrites, so a concurrent local edit would
            # be lost silently (same protection get_sync_actions applies to
            # mtime-tie conflicts during full reconciliation).
            if (
                file.mtime is not None
                and file_data.mtime is not None
                and file.mtime >= file_data.mtime
            ):
                _save_conflict_copy(file)
            file_data = File.from_dict(data, CONFIG["LOCAL_BUCKET"])
            download_file(file_data)
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
        download_file(file)
