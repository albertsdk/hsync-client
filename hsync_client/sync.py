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

from concurrent.futures import ThreadPoolExecutor
from functools import partial
from os import listdir, makedirs, remove, rmdir, walk
from os.path import basename, dirname, exists, join
from requests import get, RequestException
from sys import exc_info
from traceback import print_tb
from typing import Callable, Mapping, Optional, cast

from loguru import logger as log

from hsync_client.config import CONFIG, is_debug
from hsync_client.constants import (
    ACTION_LABEL,
    HSYNCKEEP_FILENAME,
    MAX_CONCURRENT_TRANSFERS,
)
from hsync_client.exceptions import AuthenticationError, NetworkError, ServerError
from hsync_client.models.dir import Dir, is_hidden_dir
from hsync_client.models.file import File, is_hidden_file
from hsync_client.models.sync_action import SyncAction, SyncActionName
from hsync_client.retry import retry_on_network_error
from hsync_client.utils import (
    bytes_to_human_readable,
    get_ssl_verify,
    set_permissions,
    verify_master_fingerprint,
)


# =============================================================================
# Main Sync Function
# =============================================================================


async def sync(lan_mode: bool = False) -> None:
    """
    Perform a full synchronization between local and remote files.

    Compares local and remote file lists, determines required actions,
    and executes them in parallel (download, upload, delete, or skip).

    Uses a thread pool executor with semaphore to limit concurrent
    transfers and avoid overwhelming the server.

    Args:
        lan_mode: If True, master is source of truth - files existing only
            locally will be deleted instead of uploaded.
    """
    # Import here to avoid circular import
    from hsync_client.transfer import nothing_to_do

    try:
        # Ensure empty directories have sentinel files
        ensure_hsynckeep_files()

        # Get file lists from both sides
        local_files = get_files_local()
        remote_files = get_files_remote()

        # Determine what actions need to be taken
        sync_actions = get_sync_actions(remote_files, local_files, lan_mode=lan_mode)

        log.info(f"Sync actions: {len(sync_actions)} files to process")

        # Separate actions by type
        downloads = [a.file for a in sync_actions if a.name == SyncActionName.download]
        uploads = [a.file for a in sync_actions if a.name == SyncActionName.upload]
        deletes = [a.file for a in sync_actions if a.name == SyncActionName.delete]
        nothing = [a.file for a in sync_actions if a.name == SyncActionName.nothing]

        # Log files that are already in sync (no I/O needed)
        for file in nothing:
            nothing_to_do(file)

        # Handle deletions (files that exist only locally in LAN mode)
        for file in deletes:
            if file.filepath and exists(file.filepath):
                try:
                    remove(file.filepath)
                    log.info(f'[DELETE    ] "{file.filepath}" (not on master)')
                    # If the deleted file was a sentinel, clean up the
                    # now-empty parent directory instead of recreating it.
                    if basename(file.filepath) == HSYNCKEEP_FILENAME:
                        parent = dirname(file.filepath)
                        try:
                            if exists(parent) and not listdir(parent):
                                rmdir(parent)
                                log.debug(f'Removed empty directory "{parent}"')
                        except OSError:
                            pass
                except OSError as e:
                    log.error(f'Error deleting "{file.filepath}": {e}')

        # Execute transfers in parallel using thread pool
        if downloads or uploads:
            await _execute_parallel_transfers(downloads, uploads)

        log.info("Sync completed successfully")

    except (AuthenticationError, NetworkError, ServerError) as e:
        log.error(f"Sync failed: {e}")
        if is_debug():
            exc_type, exc_value, exc_tb = exc_info()
            if exc_tb:
                print_tb(exc_tb)
    except Exception as e:
        log.error(f"Unexpected error during sync: {e}")
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
            sentinel = join(root, HSYNCKEEP_FILENAME)
            if not exists(sentinel):
                try:
                    open(sentinel, "w").close()  # noqa: SIM115
                    log.debug(f'Created sentinel "{sentinel}"')
                except OSError as e:
                    log.warning(f'Cannot create sentinel "{sentinel}": {e}')


# =============================================================================
# File List Retrieval
# =============================================================================


def get_files_local() -> list[dict]:
    """
    Get list of all local files with their metadata.

    Walks the local bucket directory recursively, skipping hidden
    files and directories. Returns file metadata sorted by mtime
    ascending (oldest first) to prioritize updating older files.

    Returns:
        List of file metadata dictionaries.
    """
    file_list = []
    for root, dirs, files in walk(CONFIG["LOCAL_BUCKET"]):
        # Skip hidden directories
        dirs[:] = [d for d in dirs if not is_hidden_dir(d)]

        for file in files:
            # Skip hidden files
            if is_hidden_file(file):
                continue

            file_obj = File.get(join(root, file))
            if file_obj:
                file_list.append(file_obj.to_dict())

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


# =============================================================================
# Sync Action Determination
# =============================================================================

# Tolerance threshold in seconds for mtime comparison.
# Handles FAT32 filesystem precision (2s) and minor clock skew.
MTIME_TOLERANCE = 2


def get_sync_actions(
    remote_files: Optional[list[dict]] = None,
    local_files: Optional[list[dict]] = None,
    lan_mode: bool = False,
) -> list[SyncAction]:
    """
    Determine sync actions by comparing local and remote file lists.

    Uses an intelligent comparison strategy:
    1. If mtime difference > MTIME_TOLERANCE (2s): use mtime to decide direction.
    2. If mtime difference <= MTIME_TOLERANCE: fall back to MD5 checksum.
       - If MD5 differs: use mtime as tiebreaker; if equal, prefer remote.
       - If MD5 matches: no action needed (files are identical).

    This handles clock skew, FAT32 timestamp precision (2s), and cases
    where mtime is equal but content differs (copy/restore scenarios).

    Args:
        remote_files: List of file metadata from server.
        local_files: List of file metadata from local filesystem.
        lan_mode: If True, master is source of truth - files existing only
            locally will be deleted instead of uploaded.

    Returns:
        List of SyncAction objects describing required operations.
    """
    if remote_files is None:
        remote_files = []
    if local_files is None:
        local_files = []

    sync_actions: list[SyncAction] = []
    remote_filepaths: set[str] = set()

    # Process remote files
    for remote_file_info in remote_files:
        remote_file = File.from_dict(remote_file_info, CONFIG["LOCAL_BUCKET"])
        local_file = File.get(remote_file_info["filepath"], CONFIG["LOCAL_BUCKET"])

        # Track remote paths for later comparison
        if remote_file.filepath:
            remote_filepaths.add(remote_file.filepath)

        # Get mtime values with defaults for comparison
        remote_mtime = remote_file.mtime or 0
        local_mtime = local_file.mtime if local_file and local_file.mtime else 0

        if local_file:
            action, timedelta = _compare_files(
                remote_file, local_file, remote_mtime, local_mtime, lan_mode,
            )

            sync_actions.append(
                SyncAction(
                    remote_file if action == SyncActionName.download else local_file,
                    action,
                    timedelta,
                )
            )
        else:
            # File exists only on remote - download it
            sync_actions.append(SyncAction(remote_file, SyncActionName.download))

    # Find local files not present on remote
    for local_file_dict in local_files:
        local_file_info = File.from_dict(local_file_dict, CONFIG["LOCAL_BUCKET"])
        if local_file_info.filepath and local_file_info.filepath not in remote_filepaths:
            file = File.get(local_file_dict["filepath"])
            if file:
                if lan_mode:
                    # In LAN mode, master is source of truth - delete local files not on master
                    sync_actions.append(SyncAction(file, SyncActionName.delete))
                else:
                    # In server mode, upload local files not on server
                    sync_actions.append(SyncAction(file, SyncActionName.upload))

    return sync_actions


def _compare_files(
    remote_file: File,
    local_file: File,
    remote_mtime: int,
    local_mtime: int,
    lan_mode: bool,
) -> tuple[SyncActionName, int]:
    """
    Compare two files using intelligent mtime + MD5 strategy.

    Args:
        remote_file: File metadata from remote/server.
        local_file: File metadata from local filesystem.
        remote_mtime: Remote file modification time.
        local_mtime: Local file modification time.
        lan_mode: Whether running in LAN mode.

    Returns:
        Tuple of (action, timedelta).
    """
    mtime_diff = abs(remote_mtime - local_mtime)

    if mtime_diff > MTIME_TOLERANCE:
        # Clear mtime difference - trust timestamps
        if remote_mtime > local_mtime:
            return SyncActionName.download, remote_mtime - local_mtime
        else:
            return SyncActionName.upload, local_mtime - remote_mtime

    # mtime within tolerance - fall back to MD5 checksum
    remote_md5 = remote_file.md5sum or ""
    local_md5 = local_file.md5sum or ""

    if remote_md5 == local_md5:
        # Content is identical - no action needed
        return SyncActionName.nothing, 0

    # Content differs despite similar mtime - need to pick a direction
    log.debug(
        f'MD5 mismatch within mtime tolerance for "{remote_file.filepath}": '
        f"remote_md5={remote_md5[:8]}... local_md5={local_md5[:8]}... "
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


# =============================================================================
# Directory and File Updates
# =============================================================================


def update_dir(data: dict) -> Optional[Dir]:
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
            makedirs(dir.path, exist_ok=True)
            if dir.permissions:
                set_permissions(dir.path, dir.permissions)

    return dir


def update_file(data: dict) -> None:
    """
    Update a local file based on remote event data.

    Downloads the file if it doesn't exist locally or if the
    MD5 checksum differs. Updates permissions if only they differ.

    Args:
        data: Dictionary with file metadata from remote event.
    """
    # Import here to avoid circular import
    from hsync_client.transfer import download_file

    file_data = File.from_dict(data, CONFIG["LOCAL_BUCKET"], False)

    if not file_data.filepath:
        log.error("Invalid file data: missing filepath")
        return

    if exists(file_data.filepath):
        # File exists locally - check if update needed
        file = File.get(file_data.filepath)

        if file and file.md5sum != file_data.md5sum:
            # Content differs - download new version
            file_data = File.from_dict(data, CONFIG["LOCAL_BUCKET"])
            download_file(file_data)
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

