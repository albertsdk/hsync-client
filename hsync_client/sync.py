# HSYNC Client - HTTP-based file synchronization client
# Copyright (C) 2025 Alberto Basso <alberto.sdk [at] gmail [dot] com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
Synchronization logic for HSYNC Client.

This module provides the core synchronization functionality including
file comparison, action determination, and sync execution.
"""

import logging

from functools import partial
from os import makedirs, walk
from os.path import exists, join
from requests import get, RequestException
from sys import exc_info
from traceback import print_tb
from typing import Mapping, Optional, cast

from hsync_client.config import CONFIG, is_debug
from hsync_client.constants import ACTION_LABEL
from hsync_client.exceptions import AuthenticationError, NetworkError, ServerError
from hsync_client.models.dir import Dir, is_hidden_dir
from hsync_client.models.file import File, is_hidden_file
from hsync_client.models.sync_action import SyncAction, SyncActionName
from hsync_client.utils import bytes_to_human_readable, get_ssl_verify, set_permissions


# Logger instance for this module
LOG = logging.getLogger(__name__)


# =============================================================================
# Main Sync Function
# =============================================================================


async def sync() -> None:
    """
    Perform a full synchronization between local and remote files.

    Compares local and remote file lists, determines required actions,
    and executes them (download, upload, or skip).
    """
    # Import here to avoid circular import
    from hsync_client.transfer import download_file, upload_file, nothing_to_do

    try:
        # Get file lists from both sides
        local_files = get_files_local()
        remote_files = get_files_remote()

        # Determine what actions need to be taken
        sync_actions = get_sync_actions(remote_files, local_files)

        LOG.info(f"Sync actions: {len(sync_actions)} files to process")

        # Execute each sync action
        for action in sync_actions:
            if action.name == SyncActionName.download:
                download_file(action.file)
            elif action.name == SyncActionName.upload:
                upload_file(action.file)
            elif action.name == SyncActionName.nothing:
                nothing_to_do(action.file)

        LOG.info("Sync completed successfully")

    except (AuthenticationError, NetworkError, ServerError) as e:
        LOG.error(f"Sync failed: {e}")
        if is_debug():
            exc_type, exc_value, exc_tb = exc_info()
            if exc_tb:
                print_tb(exc_tb)
    except Exception as e:
        LOG.error(f"Unexpected error during sync: {e}")
        if is_debug():
            exc_type, exc_value, exc_tb = exc_info()
            if exc_tb:
                print_tb(exc_tb)


# =============================================================================
# File List Retrieval
# =============================================================================


def get_files_local() -> list[dict]:
    """
    Get list of all local files with their metadata.

    Walks the local bucket directory recursively, skipping hidden
    files and directories. Returns file metadata sorted by the
    configured sync order (mtime or ctime).

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

    # Sort by configured order (mtime or ctime)
    file_list.sort(key=lambda x: x.get(CONFIG["SYNC_ORDER"]))

    return file_list


def get_files_remote() -> list[dict]:
    """
    Get list of all remote files from the server.

    Makes an HTTP GET request to retrieve file metadata from the
    remote bucket, sorted by the configured sync order.

    Returns:
        List of file metadata dictionaries from server.

    Raises:
        NetworkError: If the request fails.
        AuthenticationError: If authentication fails (401/403).
        ServerError: If server returns an error response.
    """
    uri = f"{CONFIG['API']['buckets']}/{CONFIG['REMOTE_BUCKET']}/files"
    try:
        # Build query parameters
        params = {"sync_order": CONFIG["SYNC_ORDER"]}
        # Add path filter if specified
        if (filter_path := CONFIG.get("FILTER_PATH")) is not None:
            params["path"] = filter_path

        response = get(
            uri,
            headers=cast(Mapping[str, str], CONFIG["REQUEST_HEADERS"]),
            params=params,
            verify=get_ssl_verify(),
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


def get_sync_actions(
    remote_files: Optional[list[dict]] = None,
    local_files: Optional[list[dict]] = None,
) -> list[SyncAction]:
    """
    Determine sync actions by comparing local and remote file lists.

    Compares modification times to decide whether each file should
    be uploaded, downloaded, or left alone. Also identifies files
    that exist only on one side.

    Args:
        remote_files: List of file metadata from server.
        local_files: List of file metadata from local filesystem.

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
            # Both exist - compare modification times
            if remote_mtime > local_mtime:
                # Remote is newer - download
                action = SyncActionName.download
                timedelta = remote_mtime - local_mtime
            elif remote_mtime < local_mtime:
                # Local is newer - upload
                action = SyncActionName.upload
                timedelta = local_mtime - remote_mtime
            else:
                # Same mtime - no action needed
                action = SyncActionName.nothing
                timedelta = 0

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
            # File exists only locally - upload it
            file = File.get(local_file_dict["filepath"])
            if file:
                sync_actions.append(SyncAction(file, SyncActionName.upload))

    return sync_actions


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
        LOG.error("Invalid file data: missing filepath")
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
            LOG.debug(f'File not changed: "{file_data.filepath}"')
    else:
        # File doesn't exist locally - download it
        file = File.from_dict(data, CONFIG["LOCAL_BUCKET"])
        download_file(file)

