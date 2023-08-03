#!/usr/bin/python3
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
HSYNC Client - HTTP-based File Synchronization Client.

This module implements a file synchronization client that communicates with
an HSYNC server over HTTP/WebSocket protocols. It supports:

- Bidirectional file synchronization (upload/download based on timestamps)
- Real-time file watching using the watchdog library
- WebSocket-based event propagation for multi-client sync
- Continuous synchronization mode with automatic reconnection
- Dry-run mode for previewing sync operations

Usage:
    hsync -b <bucket> -u <server_url> [options]

Environment Variables:
    HSYNC_SERVER_URL: Server URL
    HSYNC_API_KEY: API key for authentication
"""

import logging
import stat

from hsync_client.event_queue import EventQueue
from hsync_client.logger import setup_logging, log_with_data
from hsync_client.models.event import Event, EventName
from hsync_client.models.dir import Dir, is_hidden_dir
from hsync_client.models.file import File, is_hidden_file
from hsync_client.models.symlink import Symlink
from hsync_client.models.sync_action import SyncAction, SyncActionName
from argparse import ArgumentParser, ArgumentTypeError, HelpFormatter
from asyncio import create_task, get_running_loop, run, run_coroutine_threadsafe, sleep, Task
from copy import deepcopy
from hashlib import md5
from json import dumps, loads
from os import (
    chmod,
    environ,
    getcwd,
    makedirs,
    remove,
    rename,
    stat as os_stat,
    symlink as os_symlink,
    unlink,
    utime,
    walk,
)
from os.path import (
    abspath,
    basename,
    dirname,
    exists,
    getmtime,
    getsize,
    islink,
    join,
    normpath,
)
from pathlib import Path
from random import choices
from re import sub
from requests import get, post
from requests.exceptions import RequestException
from shutil import move, rmtree
from ssl import CERT_NONE, CERT_REQUIRED, create_default_context, Purpose
from string import ascii_letters, digits
from sys import exc_info
from tempfile import NamedTemporaryFile
from time import sleep as time_sleep
from traceback import print_tb
from typing import Optional
from websockets import connect
from websockets.exceptions import ConnectionClosed
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


# Application version
HSYNC_VERSION = "0.1.0"

# Logger instance - initialized in main()
LOG: logging.Logger = logging.getLogger("hsync")


# Action prefixes for sync operations (kept for visual distinction)
ACTION_PREFIX = {
    "download": "↓",      # Download file
    "upload": "↑",        # Upload file
    "move": "→",          # Move/Rename
    "remove": "✕",        # Remove/Delete
    "create": "+",        # Create directory
    "permission": "⚙",    # Set permission
    "skip": "·",          # No change (skipped)
}


def log(message: str = "", data: Optional[object] = None) -> None:
    """
    Log a message with optional JSON data.

    Uses the standard logging module with a format matching the server.
    Automatically determines log level and removes redundant prefixes.

    Args:
        message: The message to be logged.
        data: Optional data to include as formatted JSON (dict, list, etc.).
    """
    # Determine log level and clean message
    level = logging.INFO

    # Handle error messages
    if message.startswith("[E] "):
        level = logging.ERROR
        message = message[4:]  # Remove "[E] " prefix
    # Handle warning messages
    elif message.startswith("[W] "):
        level = logging.WARNING
        message = message[4:]  # Remove "[W] " prefix
    # Handle debug/skip messages
    elif message.startswith("[-] "):
        level = logging.DEBUG
        message = f"{ACTION_PREFIX['skip']} {message[4:]}"
    elif message.startswith("[I] ") and DEBUG:
        level = logging.DEBUG
        message = message[4:]  # Remove "[I] " prefix
    elif message.startswith("[I] "):
        message = message[4:]  # Remove "[I] " prefix
    # Handle action prefixes - convert to symbols
    elif message.startswith("[D] "):
        message = f"{ACTION_PREFIX['download']} {message[4:]}"
    elif message.startswith("[U] "):
        message = f"{ACTION_PREFIX['upload']} {message[4:]}"
    elif message.startswith("[M] "):
        message = f"{ACTION_PREFIX['move']} {message[4:]}"
    elif message.startswith("[R] "):
        message = f"{ACTION_PREFIX['remove']} {message[4:]}"
    elif message.startswith("[C] "):
        message = f"{ACTION_PREFIX['create']} {message[4:]}"
    elif message.startswith("[P] "):
        message = f"{ACTION_PREFIX['permission']} {message[4:]}"

    log_with_data(LOG, level, message, data)


def is_valid_logfile(path_string: str) -> bool:
    """
    Validate a log file path for correctness.

    Checks for forbidden characters and reserved names on Windows.
    If a directory is specified, the path must be absolute.

    Args:
        path_string: Path to validate.

    Returns:
        True if the path is valid, False otherwise.
    """
    try:
        path = Path(path_string)

        # If there's no parent directory, assume it's just a filename
        if path.parent == Path("."):
            # Basic check for forbidden characters in the filename
            forbidden_chars = {"<", ">", ":", '"', "/", "\\", "|", "?", "*"}
            if any(char in path.name for char in forbidden_chars):
                return False

            # Verify the filename doesn't use reserved names (Windows check)
            reserved_names = {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1"}
            if path.name.upper() in reserved_names:
                return False

        else:
            # Check for absolute path if a directory is present
            if not path.is_absolute():
                return False

            # Check each part of the path for forbidden characters
            forbidden_chars = {"<", ">", ":", '"', "\\", "|", "?", "*"}
            for part in path.parts:
                if any(char in part for char in forbidden_chars):
                    return False

        return True
    except Exception:
        return False


def is_same_paths(path1: str, path2: str) -> bool:
    """
    Check if two paths refer to the same location.

    Normalizes and resolves paths to absolute form before comparison.

    Args:
        path1: First path to compare.
        path2: Second path to compare.

    Returns:
        True if paths are equivalent, False otherwise.
    """
    return normpath(abspath(path1)) == normpath(abspath(path2))


def get_cid(length: int = 10) -> str:
    """
    Generate a random client ID.

    Creates a unique identifier for this client session used to
    identify the source of events and prevent echo.

    Args:
        length: Length of the ID string (default: 10).

    Returns:
        Random alphanumeric string.
    """
    characters = ascii_letters + digits
    random_string = "".join(choices(characters, k=length))
    return random_string


def bytes_to_human_readable(bytes: float) -> str:
    """
    Convert bytes to human-readable format.

    Automatically selects the appropriate unit (B, KB, MB, GB, etc.)
    and formats with 2 decimal places.

    Args:
        bytes: Number of bytes to convert.

    Returns:
        Formatted string like "1.50MB" or "256.00KB".
    """
    units = ["B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB"]
    index = 0
    while bytes >= 1024 and index < len(units) - 1:
        bytes /= 1024.0
        index += 1
    return f"{bytes:.2f}{units[index]}"


def compute_md5(filepath: str) -> str:
    """
    Compute the MD5 checksum of a file.

    Reads the entire file into memory for hashing.

    Args:
        filepath: Path to the file.

    Returns:
        MD5 hash as hexadecimal string.
    """
    with open(filepath, "rb") as f:
        return md5(f.read()).hexdigest()


def get_permissions(obj_path: str) -> str:
    """
    Get file permissions in symbolic notation.

    Args:
        obj_path: Path to the file or directory.

    Returns:
        Permission string like '-rw-r--r--'.
    """
    obj_stat = os_stat(obj_path)
    permissions = obj_stat.st_mode
    return stat.filemode(permissions)


def get_chunk_size(file_size: Optional[int]) -> int:
    """
    Calculate optimal chunk size based on file size.

    Uses larger chunks for bigger files to improve transfer efficiency
    while keeping memory usage reasonable.

    Args:
        file_size: Size of the file in bytes, or None if unknown.

    Returns:
        Optimal chunk size in bytes (8KB to 256KB).
    """
    if file_size is None:
        return 64 * 1024  # Default 64KB for unknown size

    if file_size < 1_000_000:        # < 1MB
        return 8 * 1024              # 8KB
    elif file_size < 100_000_000:    # < 100MB
        return 64 * 1024             # 64KB
    else:
        return 256 * 1024            # 256KB


def set_permissions(file_path: str, permissions: str) -> None:
    """
    Set file permissions from symbolic notation.

    Converts symbolic permission string (e.g., '-rw-r--r--') to
    octal mode and applies it to the file.

    Args:
        file_path: Path to the file.
        permissions: Symbolic permission string (10 characters).
    """
    # Skip if permissions already match
    if exists(file_path) and permissions == get_permissions(file_path):
        return

    # Parse symbolic permissions to octal mode
    mode = 0
    permission_types = {"r": 4, "w": 2, "x": 1}
    for i, section in enumerate(
        [permissions[1:4], permissions[4:7], permissions[7:10]]
    ):
        for char in section:
            if char in permission_types:
                mode |= permission_types[char] << (6 - (i * 3))

    if DEBUG:
        log(f'[P] Set permission "{permissions}" to {file_path}')
    chmod(file_path, mode)


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
        Exception: If the request fails or returns non-200 status.
    """
    try:
        uri = f"{CONFIG["API"]["buckets"]}/{CONFIG["REMOTE_BUCKET"]}/files"
        response = get(
            uri,
            headers=CONFIG["REQUEST_HEADERS"],
            params={
                # TODO: implement path filtering
                "sync_order": CONFIG["SYNC_ORDER"]
            },
            verify=CONFIG["NO_VERIFY_SSL"],
        )

        if response.status_code != 200:
            raise Exception(response.text)

        return response.json()
    except Exception as e:
        raise Exception(f"Error downloading files list: {e}")


def download_file(file: File) -> None:
    """
    Download a file from the remote server.

    Streams the file to a temporary location, verifies MD5 checksum,
    then moves it to the final destination. Preserves timestamps and
    permissions from the remote file.

    Args:
        file: File object with remote metadata (filepath, md5sum, etc.).
    """
    # Validate required file attributes
    if not file.filepath or not file.name:
        log(f"[E] Invalid file metadata: missing filepath or name")
        return

    uri = f"{CONFIG["API"]["buckets"]}/{CONFIG["REMOTE_BUCKET"]}/files/download"
    response = get(
        uri,
        headers=CONFIG["REQUEST_HEADERS"],
        params={"filename": file.filepath},
        stream=True,
        verify=CONFIG["NO_VERIFY_SSL"],
    )

    if DEBUG:
        log(f"[I] Request URI: {uri}")
        log(f"[I] Request Header:", CONFIG["REQUEST_HEADERS"])

    if response.status_code == 200:
        # Create temporary file for download
        with NamedTemporaryFile(delete=False) as temp_file:
            temp_file_path = temp_file.name

        try:
            # Calculate optimal chunk size based on file size
            chunk_size = get_chunk_size(file.size)

            # Stream file content to temporary file
            with open(temp_file_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if not chunk:
                        break
                    f.write(chunk)

            # Verify MD5 checksum
            md5sum = compute_md5(temp_file_path)
            if md5sum != file.md5sum:
                log(
                    f'[E] "{file.filepath}" '
                    f"(Invalid checksum: expected {file.md5sum}, "
                    f"{md5sum} given)"
                )
                return

            # Prepare destination directory
            relative_dir = dirname(file.filepath)
            dest_dir = join(CONFIG["LOCAL_BUCKET"], relative_dir.lstrip("/"))
            makedirs(dest_dir, exist_ok=True)

            # Check if local file is already up-to-date
            full_save_path = join(dest_dir, file.name.lstrip("/"))
            if exists(full_save_path):
                local_mtime = getmtime(full_save_path)
                if file.mtime is not None and local_mtime >= file.mtime:
                    nothing_to_do(file)
                    return

            # Move temporary file to final destination
            move(temp_file_path, full_save_path)
            log(f'[D] "{full_save_path}" {bytes_to_human_readable(file.size)}')

            # Preserve timestamps from remote file
            if file.ctime is not None and file.mtime is not None:
                utime(full_save_path, (file.ctime, file.mtime))

            # Apply permissions
            if file.permissions:
                set_permissions(full_save_path, file.permissions)

        except Exception as e:
            log(f"[E] Error saving file: {e}")
        finally:
            # Clean up temporary file
            if exists(temp_file_path):
                remove(temp_file_path)
    else:
        log(f"[E] Error downloading file: {response.status_code}")


def is_file_stable(file: File, interval: int = 2) -> bool:
    """
    Check if a file has stopped being written to.

    Compares file size at two points in time to detect if the file
    is still being modified. Used to wait for file writes to complete
    before uploading.

    Args:
        file: File object to check.
        interval: Seconds to wait between size checks.

    Returns:
        True if file size is stable, False if still changing.
    """
    # Validate filepath before checking size
    if not file.filepath:
        return True

    last_size = getsize(file.filepath)
    time_sleep(interval)
    current_size = getsize(file.filepath)

    if DEBUG:
        log(
            f'[I] Writing file: "{file.filepath}" '
            f"({bytes_to_human_readable(last_size)} >"
            f" {bytes_to_human_readable(current_size)})"
        )

    return last_size == current_size


def upload_file(file: File, check_size: bool = True) -> None:
    """
    Upload a file to the remote server.

    If check_size is True, waits for the file to stop being written
    to before uploading. Sends file with metadata including timestamps,
    MD5 checksum, and permissions.

    Args:
        file: File object to upload.
        check_size: If True, wait for file writes to complete first.
    """
    # Validate required file attributes
    if not file.filepath:
        log(f"[E] Cannot upload file: missing filepath")
        return

    if check_size:
        # Wait for file to stop being written
        while not is_file_stable(file):
            time_sleep(1)

        # Refresh file metadata after write is complete
        file.size = getsize(file.filepath)
        file.md5sum = compute_md5(file.filepath)

        if DEBUG:
            log(
                f'[I] Written file: "{file.filepath}" '
                f"({bytes_to_human_readable(file.size)})"
            )

        # Recursive call with check_size=False to do actual upload
        upload_file(file, False)
        return

    try:
        # Prepare upload request
        uri = f"{CONFIG["API"]["buckets"]}/{CONFIG["REMOTE_BUCKET"]}/files"
        prefix = f"{CONFIG["LOCAL_BUCKET"]}".rstrip("/")
        path = dirname(file.filepath).rstrip("/").removeprefix(prefix).lstrip("/")

        # Build form data with file metadata
        data = {
            "cid": CONFIG["CID"],
            "path": path,
            "ctime": file.ctime,
            "mtime": file.mtime,
            "size": file.size,
            "md5sum": file.md5sum,
            "permissions": file.permissions,
        }

        if DEBUG:
            log(f"[I] Upload file: {file.filepath}")
            log(f"[I]         URI: {uri}")
            log(f"[I]      Header:", CONFIG["REQUEST_HEADERS"])
            log(f"[I]        data:", data)

        with open(file.filepath, "rb") as upload:
            files = {"file": (basename(file.filepath), upload)}
            response = post(
                uri,
                headers=CONFIG["REQUEST_HEADERS"],
                data=data,
                files=files,
                verify=CONFIG["NO_VERIFY_SSL"],
            )
            if response.status_code == 201:
                log(f'[U] "{file.filepath}" {bytes_to_human_readable(file.size)}')
            elif response.status_code == 409:
                if DEBUG:
                    log(f'[I] "{file.filepath}" (already present)')
            else:
                response.raise_for_status()
    except RequestException as e:
        log(f'[E] Error uploading: "{file.filepath}" {e}')
    except Exception as e:
        log(f"[E] Exception: {e}")


def nothing_to_do(file: File) -> None:
    """
    Log that a file requires no action (already in sync).

    Only logs in DEBUG mode to reduce noise.

    Args:
        file: File that is already synchronized.
    """
    if DEBUG:
        log(f'[-] "{file.filepath}"')


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
        remote_file = File().from_dict(remote_file_info, CONFIG["LOCAL_BUCKET"])
        local_file = File().get(remote_file_info["filepath"], CONFIG["LOCAL_BUCKET"])

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
        local_file_info = File().from_dict(local_file_dict, CONFIG["LOCAL_BUCKET"])
        if local_file_info.filepath and local_file_info.filepath not in remote_filepaths:
            # File exists only locally - upload it
            file = File.get(local_file_dict["filepath"])
            if file:
                sync_actions.append(SyncAction(file, SyncActionName.upload))

    return sync_actions


def valid_reconnect_delay(value: str) -> int:
    """
    Validate reconnection delay argument.

    Used as an argparse type validator.

    Args:
        value: String value from command line.

    Returns:
        Validated integer value.

    Raises:
        ArgumentTypeError: If value is not between 1 and 60.
    """
    try:
        ivalue = int(value)
    except ValueError:
        raise ArgumentTypeError(f"The value '{value}' must be integer.")

    if ivalue <= 1 or ivalue >= 60:
        raise ArgumentTypeError("The value must be between 1 and 60 sec.")

    return ivalue


def valid_resync(value: str) -> int:
    """
    Validate resync interval argument.

    Used as an argparse type validator.

    Args:
        value: String value from command line.

    Returns:
        Validated integer value.

    Raises:
        ArgumentTypeError: If value is not between 20 and 3600.
    """
    try:
        ivalue = int(value)
    except ValueError:
        raise ArgumentTypeError(f"The value '{value}' must be integer.")

    if ivalue <= 20 or ivalue >= 3600:
        raise ArgumentTypeError("The value must be between 20 and 3600 sec.")

    return ivalue


def valid_bucket(value: str) -> str:
    """
    Validate bucket name argument.

    Bucket names must be alphanumeric only.

    Args:
        value: Bucket name from command line.

    Returns:
        Validated bucket name.

    Raises:
        ArgumentTypeError: If name contains non-alphanumeric characters.
    """
    if not value.isalnum():
        raise ArgumentTypeError("The bucket name must be alphanumeric.")
    return value


async def sync() -> None:
    """
    Perform a single synchronization cycle.

    Fetches file lists from both local and remote, determines
    required sync actions, and executes them. In dry-run mode,
    only logs what would be done without making changes.
    """
    if not CONFIG["DRYRUN"]:
        log("[I] Sync starting…")

    sync_actions: list[SyncAction] = []

    # Action handlers - defined outside try block to ensure it's always bound
    actions: dict[SyncActionName, object] = {
        SyncActionName.download: lambda file: download_file(file),
        SyncActionName.upload: lambda file: upload_file(file, False),
        SyncActionName.nothing: lambda file: nothing_to_do(file),
    }

    try:
        # Get file lists from both sides
        remote_files = get_files_remote()
        local_files = get_files_local()

        # Determine required actions
        sync_actions = get_sync_actions(remote_files, local_files)

        if DEBUG:
            log(f"[I] Local files:", local_files)
            log(f"[I] Remote files:", remote_files)
            log(f"[I] Sync actions: {sync_actions}")

    except Exception as e:
        if DEBUG:
            exc_type, exc_value, exc_traceback = exc_info()
            log(f"[E] Exception type: {exc_type}")
            log(f"[E] Exception message: {exc_value}")
            log(f"[E] Traceback details: {print_tb(exc_traceback)}")
        else:
            log(f"[E] Exception: {e}")

    # Track number of actions performed
    todo = 0

    if CONFIG["DRYRUN"]:
        # Dry-run mode: just log what would be done
        for sync_action in sync_actions:
            if DEBUG:
                log(f"[I] Sync action:", sync_action.to_dict())
            else:
                # Map action to log symbol
                if sync_action.name == SyncActionName.download:
                    action = "D"
                elif sync_action.name == SyncActionName.upload:
                    action = "U"
                else:
                    action = "-"

                # Format relative filepath for display
                filepath = (
                    sync_action.file.filepath.removeprefix(CONFIG["LOCAL_BUCKET"]).lstrip("/")
                    if sync_action.file.filepath
                    else ""
                )
                log(
                    f'[{action}] "{filepath}" '
                    f"{bytes_to_human_readable(sync_action.file.size)} "
                    f"({sync_action.timedelta})"
                )
    else:
        # Execute sync actions
        for sync_action in sync_actions:
            if sync_action.name and sync_action.name in {SyncActionName.download, SyncActionName.upload}:
                action_func = actions.get(sync_action.name)
                if callable(action_func):
                    action_func(sync_action.file)
                todo += 1

    if not CONFIG["DRYRUN"]:
        if todo == 0:
            log("[I] Nothing to sync")
        log("[I] Sync completed.")


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
        dir_data = Dir().from_dict(data, CONFIG["LOCAL_BUCKET"])
        if dir_data.permissions != dir.permissions and dir.path and dir_data.permissions:
            dir.permissions = dir_data.permissions
            set_permissions(dir.path, dir.permissions)
    else:
        # Directory doesn't exist - create it
        dir = Dir().from_dict(data, CONFIG["LOCAL_BUCKET"])
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
    file_data = File().from_dict(data, CONFIG["LOCAL_BUCKET"], False)

    if not file_data.filepath:
        log(f"[E] Invalid file data: missing filepath")
        return

    if exists(file_data.filepath):
        # File exists locally - check if update needed
        file = File.get(file_data.filepath)

        if file and file.md5sum != file_data.md5sum:
            # Content differs - download new version
            file_data = File().from_dict(data, CONFIG["LOCAL_BUCKET"])
            download_file(file_data)
        elif file and file.permissions != file_data.permissions:
            # Only permissions differ - update them
            if file_data.permissions:
                set_permissions(file_data.filepath, file_data.permissions)
        else:
            if DEBUG:
                log(f'[I] File not changed: "{file_data.filepath}"')
    else:
        # File doesn't exist locally - download it
        file = File().from_dict(data, CONFIG["LOCAL_BUCKET"])
        download_file(file)


async def remote_events() -> None:
    """
    Handle continuous synchronization via WebSocket events.

    Connects to the server's WebSocket endpoint and processes
    incoming events (file additions, modifications, renames,
    deletions). Also sets up local file watching to detect
    changes and broadcast them to other clients.

    This function runs until the WebSocket connection is closed.
    """
    log("[I] Continuous sync starting…")

    # Build WebSocket URI (no token in URL for security)
    api_base = CONFIG["API"]["events"]
    cid = CONFIG["CID"]
    bucket = CONFIG["REMOTE_BUCKET"]
    uri = f"{api_base}/{bucket}/events/{cid}"

    # Configure SSL context for secure WebSocket connections
    ssl_context = None
    if str(api_base).startswith("wss"):
        ssl_context = create_default_context(Purpose.SERVER_AUTH)
        if CONFIG["NO_VERIFY_SSL"]:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = CERT_NONE
        else:
            ssl_context.check_hostname = True
            ssl_context.verify_mode = CERT_REQUIRED

    # Connect to WebSocket endpoint with heartbeat
    # ping_interval: Send ping every 30 seconds to detect dead connections
    # ping_timeout: Wait up to 10 seconds for pong response
    # close_timeout: Wait up to 5 seconds for close handshake
    async with connect(
        uri,
        ssl=ssl_context,
        ping_interval=30,
        ping_timeout=10,
        close_timeout=5,
    ) as websocket:
        log(f"[I] WebSocket connected to {uri}")

        # Authenticate via first message (more secure than URL token)
        auth_message = {"type": "auth", "token": HSYNC_API_KEY}
        await websocket.send(dumps(auth_message))

        # Wait for authentication confirmation
        auth_response = await websocket.recv()
        auth_result = loads(auth_response)
        if auth_result.get("type") != "auth" or auth_result.get("status") != "ok":
            log(f"[E] WebSocket authentication failed: {auth_result}")
            return

        log("[I] WebSocket authenticated successfully")

        if DEBUG:
            log(f"[I] Sync reconnection delay: {CONFIG["RECONNECT_DELAY"]} sec.")
            log(f"[I] Force resync: {CONFIG["RESYNC"]} sec.")

        log(f"[I] Client ID (CID): {CONFIG["CID"]}")

        # Set up event queue for reliable message delivery
        loop = get_running_loop()
        event_queue = EventQueue(websocket)

        # Start event queue processor as background task
        queue_task = create_task(event_queue.process())

        # Set up local file system watcher with event queue
        event_handler = FileEventHandler(websocket, loop, event_queue)
        observer = Observer()
        observer.schedule(event_handler, CONFIG["LOCAL_BUCKET"], recursive=True)
        observer.start()

        try:
            # Main event loop - process incoming WebSocket messages
            while True:
                data = await websocket.recv()
                message = loads(data)

                if DEBUG:
                    log(f"[I] Received message for CID {CONFIG["CID"]}: {message}")

                # Validate event type
                if message.get("name") not in {
                    EventName.added.value,
                    EventName.modified.value,
                    EventName.renamed.value,
                    EventName.deleted.value,
                }:
                    log(f"[E] Invalid event: {message.get("name")}")
                    continue

                # Skip events from this client (echo prevention)
                if message.get("cid") == CONFIG["CID"]:
                    if DEBUG:
                        log("[W] Same CID, dropped.")
                    continue

                # Skip events from different buckets
                if message.get("bucket") != bucket:
                    if DEBUG:
                        log("[W] Different bucket, dropped.")
                    continue

                # Extract event data
                src = message.get("src", None)
                dst = message.get("dst", None)
                event_name = EventName(message.get("name"))

                # Handle Added / Modified events
                if event_name in {EventName.added, EventName.modified}:
                    if src and "path" in src:
                        # Directory event
                        update_dir(src)
                    elif src and "filepath" in src:
                        # File event
                        update_file(src)

                # Handle Renamed events
                elif event_name == EventName.renamed:
                    if src and "path" in src and dst:
                        # Directory rename
                        # Parse source and destination directories
                        src_dir = Dir().from_dict(src, CONFIG["LOCAL_BUCKET"])
                        dst_dir = Dir().from_dict(dst, CONFIG["LOCAL_BUCKET"])

                        if src_dir.path and dst_dir.path and exists(src_dir.path):
                            # Source exists - perform rename
                            rename(src_dir.path, dst_dir.path)
                        else:
                            # Source doesn't exist - log warning and create destination
                            log(f"[W] Cannot rename: {src_dir.path} -> {dst_dir.path}")
                            log(f"[W] Not found: {src_dir.path}")
                            update_dir(dst)

                    elif src and "filepath" in src and dst:
                        # File rename event
                        src_file = File().from_dict(src, CONFIG["LOCAL_BUCKET"], False)
                        dst_file = File().from_dict(dst, CONFIG["LOCAL_BUCKET"], False)

                        if src_file.filepath and dst_file.filepath and exists(src_file.filepath):
                            # Source exists - perform rename
                            rename(src_file.filepath, dst_file.filepath)
                        else:
                            # Source doesn't exist - log warning and download destination
                            log(
                                f'[W] Cannot rename: "{src_file.filepath}" -> "{dst_file.filepath}"'
                            )
                            log(f'[W] Not found: "{src_file.filepath}"')
                            download_file(dst_file)

                # Handle Deleted events
                elif event_name == EventName.deleted:
                    if src and "path" in src:
                        # Directory deletion event
                        dir = Dir().from_dict(src, CONFIG["LOCAL_BUCKET"])
                        if dir.path and exists(dir.path):
                            # Directory exists - remove recursively
                            rmtree(dir.path, ignore_errors=False)
                        else:
                            log(f'[W] Cannot delete: "{dir.path}"')
                            log(f'[W] Not found: "{dir.path}"')

                    elif src and "filepath" in src:
                        # File deletion event
                        file = File().from_dict(src, CONFIG["LOCAL_BUCKET"], False)
                        if file.filepath and exists(file.filepath):
                            # File exists - remove it
                            remove(file.filepath)
                        else:
                            log(f'[W] Cannot delete: "{file.filepath}"')
                            log(f'[W] Not found: "{file.filepath}"')

                # Handle Symlink events
                elif event_name == EventName.symlink:
                    if src and src.get("type") == "symlink":
                        # Create symlink locally
                        link = Symlink.from_dict(src)
                        link.filepath = join(
                            CONFIG["LOCAL_BUCKET"], link.filepath.lstrip("/")
                        )
                        target = link.target

                        if link.filepath and target:
                            log(f'[L] Creating symlink: "{link.filepath}" -> "{target}"')
                            # Ensure parent directory exists
                            parent_dir = dirname(link.filepath)
                            if not exists(parent_dir):
                                makedirs(parent_dir, exist_ok=True)

                            # Remove existing file/symlink if present
                            if islink(link.filepath) or exists(link.filepath):
                                unlink(link.filepath)

                            # Create the symlink
                            os_symlink(target, link.filepath)

        except ConnectionClosed as e:
            # WebSocket connection was closed
            if e.reason:
                log(f"[W] Connection closed: {e.reason}")
            else:
                log("[W] Connection closed.")

        except Exception as e:
            # Unexpected error during event processing
            log(f"[E] Exception: {e}")

        finally:
            # Stop event queue processor
            event_queue.stop()
            queue_task.cancel()
            try:
                await queue_task
            except Exception:
                pass  # Task was cancelled

            # Clean up file system observer
            observer.stop()
            observer.join()


# Global resync task reference for cancellation on reconnect
_resync_task: Optional[Task[None]] = None


async def resync() -> None:
    """
    Periodic resynchronization task.

    Runs as a background task during continuous sync mode,
    performing full sync cycles at configured intervals to
    catch any missed events or resolve inconsistencies.
    """
    try:
        while True:
            await sleep(CONFIG["RESYNC"])
            log(f"[I] Force sync ({CONFIG['RESYNC']} sec.)")
            await sync()
    except Exception as e:
        # Task was cancelled or error occurred
        if DEBUG:
            log(f"[I] Resync task stopped: {e}")


async def remote_connect() -> None:
    """
    Manage WebSocket connection with automatic reconnection.

    Performs initial sync, optionally starts periodic resync,
    then enters continuous event monitoring. Automatically
    reconnects on connection loss after configured delay.
    Cancels previous resync task before creating a new one.
    """
    global _resync_task

    while True:
        try:
            # Perform initial sync before entering event loop
            await sync()

            # Cancel previous resync task if it exists
            if _resync_task is not None and not _resync_task.done():
                _resync_task.cancel()
                try:
                    await _resync_task
                except Exception:
                    pass  # Task was cancelled, ignore

            # Start background resync task if configured
            if CONFIG["RESYNC"]:
                _resync_task = create_task(resync())

            # Enter continuous event monitoring
            await remote_events()

        except ConnectionClosed:
            # WebSocket disconnected - wait before reconnecting
            await sleep(CONFIG["RECONNECT_DELAY"])

        except Exception as e:
            # Unexpected error - log and retry
            log(f"[E] An unexpected error occurred: {e}.")
            await sleep(CONFIG["RECONNECT_DELAY"])

        finally:
            log(f"[I] Reconnecting in {CONFIG['RECONNECT_DELAY']} sec.…")


class FileEventHandler(FileSystemEventHandler):
    """
    File system event handler for local file watching.

    Monitors the local bucket directory for file system changes and
    broadcasts events to other HSYNC clients via WebSocket. Uses the
    watchdog library's Observer pattern to detect create, modify,
    move, and delete events.

    Attributes:
        websocket: WebSocket connection to the server.
        loop: asyncio event loop for scheduling coroutines.
    """

    def __init__(self, websocket, loop, event_queue: EventQueue = None) -> None:
        """
        Initialize the file event handler.

        Args:
            websocket: Active WebSocket connection to the server.
            loop: Running asyncio event loop for thread-safe operations.
            event_queue: Optional event queue for reliable message delivery.
        """
        self.websocket = websocket
        self.loop = loop
        self.event_queue = event_queue

    def _send_message(self, message: Event) -> None:
        """
        Send an event message to the server.

        Uses event queue if available for reliable delivery with retry,
        otherwise sends directly via WebSocket.

        Args:
            message: Event to send.
        """
        if DEBUG:
            log(f"[I] Send message: {message}")

        message_dict = message.to_dict()

        if self.event_queue:
            # Use event queue for reliable delivery
            run_coroutine_threadsafe(
                self.event_queue.enqueue(message_dict), self.loop
            )
        else:
            # Direct send (legacy)
            run_coroutine_threadsafe(
                self.websocket.send(dumps(message_dict)), self.loop
            )

    def on_any_event(self, event) -> None:
        """
        Log debug information for all events.

        Called for every file system event. In DEBUG mode, logs when
        hidden files or directories are being skipped.

        Args:
            event: File system event from watchdog.
        """
        if DEBUG:
            hidden_prefix = "dir" if event.is_directory else "file"
            paths = {"src": event.src_path, "dest": event.dest_path}

            for kind, path in paths.items():
                if path and (
                    (event.is_directory and is_hidden_dir(path))
                    or (not event.is_directory and is_hidden_file(path))
                ):
                    log(f"[I] Skip hidden {kind} {hidden_prefix}: {path}")

    def on_created(self, event) -> None:
        """
        Handle file/directory creation events.

        For directories: broadcasts 'added' event to server.
        For symlinks: broadcasts 'symlink' event to server.

        Args:
            event: File system creation event from watchdog.
        """
        if event.is_directory and not is_hidden_dir(event.src_path):
            # Directory created - broadcast event
            log(f'[C] "{event.src_path}"')
            dir = Dir.get(event.src_path)
            if dir:
                message = Event(
                    CONFIG["CID"],
                    CONFIG["LOCAL_BUCKET"],
                    CONFIG["REMOTE_BUCKET"],
                    EventName.added,
                    dir,
                )
                self._send_message(message)
        elif not is_hidden_file(event.src_path) and islink(event.src_path):
            # Symlink created - broadcast symlink event
            log(f'[L] "{event.src_path}"')
            symlink = Symlink.get(event.src_path)
            if symlink:
                message = Event(
                    CONFIG["CID"],
                    CONFIG["LOCAL_BUCKET"],
                    CONFIG["REMOTE_BUCKET"],
                    EventName.symlink,
                    symlink,
                )
                self._send_message(message)

    def on_modified(self, event) -> None:
        """
        Handle file/directory modification events.

        For directories: broadcasts 'modified' event if permissions changed.
        File modifications are handled by on_closed() instead.

        Args:
            event: File system modification event from watchdog.
        """
        if (
            event.is_directory
            and not is_hidden_dir(event.src_path)
            and not is_same_paths(CONFIG["LOCAL_BUCKET"], event.src_path)
        ):
            # Directory modified (permissions change) - broadcast event
            log(f'[M] "{event.src_path}"')
            dir = Dir.get(event.src_path)
            if dir:
                message = Event(
                    CONFIG["CID"],
                    CONFIG["LOCAL_BUCKET"],
                    CONFIG["REMOTE_BUCKET"],
                    EventName.modified,
                    dir,
                )
                self._send_message(message)

    def on_closed(self, event) -> None:
        """
        Handle file close events (file write completed).

        Triggered when a file is closed after writing. This is the
        appropriate time to upload the file since writing is complete.

        Args:
            event: File close event from watchdog.
        """
        # Only process files, not directories or hidden files
        if not event.is_directory and not is_hidden_file(event.src_path):
            try:
                if exists(event.src_path):
                    # File exists - upload it
                    file = File.get(event.src_path)
                    if file:
                        upload_file(file)
                else:
                    # File was deleted before we could upload
                    log(
                        f'[W] File "{event.src_path}" not found. '
                        f"It might have been altered by an external tool."
                    )
            except Exception as e:
                log(f'[E] Error handling closed event for "{event.src_path}": {e}')

    def on_moved(self, event) -> None:
        """
        Handle file/directory move/rename events.

        Broadcasts 'renamed' events when files or directories are moved.
        Handles special cases like moves involving hidden directories.

        Args:
            event: File system move event from watchdog.
        """
        if DEBUG:
            if event.dest_path:
                log(f'[M] "{event.src_path}" -> "{event.dest_path}"')
            else:
                log(f'[M] "{event.src_path}"')

        message = None

        if event.is_directory:
            # Handle various hidden directory scenarios
            if is_hidden_dir(event.src_path) and not event.dest_path:
                # Hidden source, no destination - ignore
                return
            if is_hidden_dir(event.src_path) and is_hidden_dir(event.dest_path):
                # Both hidden - ignore
                return
            if is_hidden_dir(event.src_path) and not is_hidden_dir(event.dest_path):
                # Hidden to visible - not yet implemented
                log("[W] TODO ???")
                return
            if is_hidden_dir(event.dest_path):
                # Moving to hidden directory - treat as deletion
                log(f"[W] Switch event to deleted: {event.dest_path}")
                self.on_deleted(event.dest_path)
                return

            # Build event message for directory rename
            # Destination already exists, source is the old path
            # Source object inherits destination permissions since
            # the event object doesn't contain them
            dst = Dir.get(event.dest_path)
            if dst:
                src = deepcopy(dst)
                src.path = event.src_path

                if event.dest_path:
                    # Standard rename with destination
                    message = Event(
                        CONFIG["CID"],
                        CONFIG["LOCAL_BUCKET"],
                        CONFIG["REMOTE_BUCKET"],
                        EventName.renamed,
                        src,
                        dst,
                    )
                else:
                    # No destination - treat as modification
                    message = Event(
                        CONFIG["CID"],
                        CONFIG["LOCAL_BUCKET"],
                        CONFIG["REMOTE_BUCKET"],
                        EventName.modified,
                        src,
                    )
        else:
            # File move/rename
            if is_hidden_file(event.src_path):
                return

            # Build event message for file rename
            # Destination already exists, source is the old file
            # Source object inherits destination data since
            # the event object doesn't contain them
            dst = File.get(event.dest_path)
            if dst:
                src = deepcopy(dst)
                src.filepath = event.src_path
                message = Event(
                    CONFIG["CID"],
                    CONFIG["LOCAL_BUCKET"],
                    CONFIG["REMOTE_BUCKET"],
                    EventName.renamed,
                    src,
                    dst,
                )

        # Send event to server via WebSocket
        if message:
            self._send_message(message)

    def on_deleted(self, event) -> None:
        """
        Handle file/directory deletion events.

        Broadcasts 'deleted' events when files or directories are removed
        from the local filesystem. Ignores hidden files and directories.

        Args:
            event: File system deletion event from watchdog.
        """
        # Skip hidden directories
        if event.is_directory and is_hidden_dir(event.src_path):
            return

        # Skip hidden files
        if not event.is_directory and is_hidden_file(event.src_path):
            return

        log(f'[R] "{event.src_path}"')

        # Create appropriate object type for the deleted item
        obj = Dir(event.src_path) if event.is_directory else File(event.src_path)

        # Build deletion event message
        message = Event(
            CONFIG["CID"],
            CONFIG["LOCAL_BUCKET"],
            CONFIG["REMOTE_BUCKET"],
            EventName.deleted,
            obj,
        )

        # Send event to server via WebSocket
        self._send_message(message)


# =============================================================================
# Global Configuration
# =============================================================================

# Environment variables for configuration
HSYNC_SERVER_URL: Optional[str] = environ.get("HSYNC_SERVER_URL", None)
"""Server URL from environment variable."""

HSYNC_API_KEY: str = environ.get("HSYNC_API_KEY", "")
"""API key for authentication from environment variable."""

DEBUG: bool = False
"""Debug mode flag - enables verbose logging."""

CONFIG: dict = {}
"""Global configuration dictionary populated at runtime."""


def main() -> None:
    """
    Main entry point for the HSYNC client.

    Parses command-line arguments, validates configuration, sets up
    the global CONFIG dictionary, and starts either a single sync
    or continuous synchronization mode.
    """
    global CONFIG, DEBUG, LOG

    class SortedHelpFormatter(HelpFormatter):
        """Custom help formatter that sorts arguments alphabetically."""

        def add_arguments(self, actions):
            """Override to sort actions by their option strings."""
            actions = sorted(
                actions,
                key=lambda action: tuple(action.option_strings)
                if action.option_strings
                else (),
            )
            super(SortedHelpFormatter, self).add_arguments(actions)

    # =========================================================================
    # Argument Parser Setup
    # =========================================================================
    parser = ArgumentParser(
        prog="hsync",
        description="HSYNC - HTTP-based file synchronization client",
        formatter_class=SortedHelpFormatter,
        epilog="Environment: HSYNC_SERVER_URL, HSYNC_API_KEY",
    )

    # -------------------------------------------------------------------------
    # Required arguments
    # -------------------------------------------------------------------------

    # Remote bucket name
    parser.add_argument(
        "-b",
        "--bucket",
        type=valid_bucket,
        required=True,
        metavar="NAME",
        help="Remote bucket name.",
    )

    # -------------------------------------------------------------------------
    # Connection options
    # -------------------------------------------------------------------------

    # Server endpoint URL
    parser.add_argument(
        "-s",
        "--server",
        type=str,
        required=False if HSYNC_SERVER_URL else True,
        metavar="URL",
        dest="server_url",
        help="Server URL (or set HSYNC_SERVER_URL).",
    )

    # Disable SSL certificate verification
    parser.add_argument(
        "--insecure",
        action="store_false",
        dest="verify_ssl",
        help="Skip SSL certificate verification.",
    )

    # -------------------------------------------------------------------------
    # Sync options
    # -------------------------------------------------------------------------

    # Local directory to sync
    parser.add_argument(
        "-p",
        "--path",
        type=str,
        required=False,
        default=getcwd(),
        metavar="DIR",
        dest="local_path",
        help="Local directory path (default: cwd).",
    )

    # Watch mode (continuous sync)
    parser.add_argument(
        "-w",
        "--watch",
        action="store_true",
        dest="continuous",
        help="Watch for changes and sync continuously.",
    )

    # Sync order (mtime or ctime)
    parser.add_argument(
        "--order-by",
        type=str,
        required=False,
        default="mtime",
        choices=["mtime", "ctime"],
        metavar="FIELD",
        dest="sync_order",
        help="Compare files by: mtime, ctime (default: mtime).",
    )

    # Periodic resync interval
    parser.add_argument(
        "-i",
        "--interval",
        type=valid_resync,
        required=False,
        metavar="SEC",
        dest="resync",
        help="Force full sync every SEC seconds (requires -w).",
    )

    # Reconnection delay after disconnect
    parser.add_argument(
        "--retry-delay",
        type=valid_reconnect_delay,
        required=False,
        default=5,
        metavar="SEC",
        dest="reconnect_delay",
        help="Retry connection after SEC seconds (default: 5).",
    )

    # Dry-run mode (preview only)
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        dest="dryrun",
        help="Show what would be done without making changes.",
    )

    # -------------------------------------------------------------------------
    # Output options
    # -------------------------------------------------------------------------

    # Verbose mode (debug logging)
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        dest="debug",
        help="Enable verbose output.",
    )

    # Quiet mode (suppress output)
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress all output.",
    )

    # Log file path
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        required=False,
        metavar="FILE",
        dest="logfile",
        help="Write output to FILE.",
    )

    # Disable colored output
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output.",
    )

    # Display log legend
    parser.add_argument(
        "--help-symbols",
        action="store_true",
        dest="legend",
        help="Show output symbols legend.",
    )

    # Version display
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {HSYNC_VERSION}",
    )

    # =========================================================================
    # Argument Validation
    # =========================================================================
    args = parser.parse_args()

    # Initialize logging early so we can log validation errors
    LOG = setup_logging(
        debug=args.debug,
        quiet=args.quiet,
        logfile=args.logfile if hasattr(args, 'logfile') and args.logfile and is_valid_logfile(args.logfile) else None,
        no_color=args.no_color,
    )

    # Validate local directory exists
    if not exists(args.local_path):
        LOG.error(f"Directory '{args.local_path}' does not exist")
        return

    # Validate log file path
    if args.logfile and not is_valid_logfile(args.logfile):
        LOG.error(f"Invalid output file '{args.logfile}'")
        return

    # =========================================================================
    # Build Configuration
    # =========================================================================

    # Determine server URL (CLI argument takes precedence over env var)
    server_url = args.server_url or HSYNC_SERVER_URL

    # Validate server URL is provided
    if not server_url:
        log("[E] Server URL is required (use -s or set HSYNC_SERVER)")
        return

    # Convert HTTP URL to WebSocket URL for events endpoint
    ws_url = sub(r"^http:", "ws:", server_url)
    ws_url = sub(r"^https:", "wss:", ws_url)

    # Populate global configuration dictionary
    CONFIG = {
        # API endpoints (RESTful paths)
        "API": {
            "buckets": f"{server_url}/api/v1/buckets",
            "events": f"{ws_url}/api/v1/buckets",
        },
        # Unique client identifier for this session
        "CID": get_cid(),
        # Dry-run mode flag
        "DRYRUN": args.dryrun,
        # Local directory path
        "LOCAL_BUCKET": args.local_path,
        # Log file path
        "LOGFILE": args.logfile,
        # SSL verification flag (inverted: verify_ssl=False means NO_VERIFY_SSL=True)
        "NO_VERIFY_SSL": not args.verify_ssl,
        # Quiet mode flag
        "QUIET": args.quiet,
        # Remote bucket name
        "REMOTE_BUCKET": args.bucket,
        # HTTP request headers
        "REQUEST_HEADERS": {
            "accept": "application/json",
            "Authorization": f"Bearer {HSYNC_API_KEY}",
            "X-Client-Name": basename(__file__),
            "X-Client-Version": HSYNC_VERSION,
        },
        # Reconnection delay in seconds
        "RECONNECT_DELAY": args.reconnect_delay,
        # Periodic resync interval in seconds
        "RESYNC": args.resync,
        # Sync ordering (mtime or ctime)
        "SYNC_ORDER": args.sync_order,
    }

    # Set global debug flag
    DEBUG = args.debug

    # Log configuration in debug mode
    LOG.debug("Local configuration:", extra={'json_data': CONFIG})

    # =========================================================================
    # Display Legend (if requested)
    # =========================================================================
    if args.legend:
        LOG.info("Log legend:")
        log_symbols = {
            ACTION_PREFIX['skip']: "File not changed (no action required)",
            ACTION_PREFIX['download']: "Download file",
            ACTION_PREFIX['upload']: "Upload file",
            ACTION_PREFIX['move']: "Move/Rename file/dir",
            ACTION_PREFIX['remove']: "Remove file/dir",
            ACTION_PREFIX['create']: "Create directory",
            ACTION_PREFIX['permission']: "Set permission",
        }
        for symbol, description in log_symbols.items():
            LOG.info(f"    {symbol} {description}")

    # =========================================================================
    # Execute Sync
    # =========================================================================
    if CONFIG["DRYRUN"] and args.continuous:
        # Dry-run is incompatible with continuous mode
        LOG.warning("Continuous sync is disabled due to dryrun option set")
    elif not CONFIG["DRYRUN"] and args.continuous:
        # Start continuous synchronization with WebSocket events
        run(remote_connect())
    else:
        # Perform single sync operation
        run(sync())


# =============================================================================
# Script Entry Point
# =============================================================================
if __name__ == "__main__":
    main()
