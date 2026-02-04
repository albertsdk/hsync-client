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
File transfer operations for HSYNC Client.

This module provides upload and download functionality for
synchronizing files with the remote server.
"""

import logging

from os import makedirs, remove, utime
from typing import cast
from os.path import basename, dirname, exists, getmtime, getsize, join
from requests import get, post, RequestException
from shutil import move
from tempfile import NamedTemporaryFile
from time import sleep as time_sleep

from hsync_client.config import CONFIG
from hsync_client.constants import FILE_STABILITY_INTERVAL
from hsync_client.models.file import File
from hsync_client.utils import (
    bytes_to_human_readable,
    compute_md5,
    get_chunk_size,
    get_ssl_verify,
    set_permissions,
)


# Logger instance for this module
LOG = logging.getLogger(__name__)


# =============================================================================
# Download Operations
# =============================================================================


def download_file(file: File) -> None:
    """
    Download a file from the remote server.

    Streams the file to a temporary location, verifies MD5 checksum,
    then moves it to the final destination. Preserves timestamps and
    permissions from the remote file.

    Args:
        file: File object with remote metadata (filepath, md5sum, etc.).

    Raises:
        DownloadError: If download fails.
        ChecksumError: If MD5 checksum doesn't match.
        FileWriteError: If file cannot be saved locally.
    """
    # Validate required file attributes
    if not file.filepath or not file.name:
        LOG.error("Invalid file metadata: missing filepath or name")
        return

    uri = f"{CONFIG['API']['buckets']}/{CONFIG['REMOTE_BUCKET']}/files/download"
    try:
        response = get(
            uri,
            headers=cast(dict[str, str], CONFIG["REQUEST_HEADERS"]),
            params={"filename": file.filepath},
            stream=True,
            verify=get_ssl_verify(),
        )
    except RequestException as e:
        LOG.error(f'Network error downloading "{file.filepath}": {e}')
        return

    LOG.debug(f"Request URI: {uri}")
    LOG.debug(f"Request Header: {CONFIG['REQUEST_HEADERS']}")

    if response.status_code == 401:
        LOG.error(f'Authentication failed downloading "{file.filepath}"')
        return
    if response.status_code == 404:
        LOG.error(f'File not found on server: "{file.filepath}"')
        return
    if response.status_code != 200:
        LOG.error(f'Server error downloading "{file.filepath}": {response.status_code}')
        return

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
            LOG.error(
                f'"{file.filepath}" '
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
        LOG.info(f'[DOWNLOAD  ] "{full_save_path}" {bytes_to_human_readable(file.size)}')

        # Preserve timestamps from remote file
        if file.ctime is not None and file.mtime is not None:
            utime(full_save_path, (file.ctime, file.mtime))

        # Apply permissions
        if file.permissions:
            set_permissions(full_save_path, file.permissions)

    except PermissionError as e:
        LOG.error(f'Permission denied saving "{file.filepath}": {e}')
    except OSError as e:
        LOG.error(f'Error saving file "{file.filepath}": {e}')
    finally:
        # Clean up temporary file
        if exists(temp_file_path):
            remove(temp_file_path)


# =============================================================================
# File Stability Check
# =============================================================================


def is_file_stable(file: File, interval: int = FILE_STABILITY_INTERVAL) -> bool:
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

    LOG.debug(
        f'Writing file: "{file.filepath}" '
        f"({bytes_to_human_readable(last_size)} >"
        f" {bytes_to_human_readable(current_size)})"
    )

    return last_size == current_size


# =============================================================================
# Upload Operations
# =============================================================================


def upload_file(file: File, check_size: bool = True) -> None:
    """
    Upload a file to the remote server.

    If check_size is True, waits for the file to stop being written
    to before uploading. Sends file with metadata including timestamps,
    MD5 checksum, and permissions.

    Args:
        file: File object to upload.
        check_size: If True, wait for file writes to complete first.

    Raises:
        FileReadError: If file cannot be read.
        NetworkError: If upload request fails.
        AuthenticationError: If authentication fails.
    """
    # Validate required file attributes
    if not file.filepath:
        LOG.error("Cannot upload file: missing filepath")
        return

    if check_size:
        # Wait for file to stop being written
        while not is_file_stable(file):
            time_sleep(1)

        # Refresh file metadata after write is complete
        file.size = getsize(file.filepath)
        file.md5sum = compute_md5(file.filepath)

        LOG.debug(
            f'Written file: "{file.filepath}" '
            f"({bytes_to_human_readable(file.size)})"
        )

        # Recursive call with check_size=False to do actual upload
        upload_file(file, False)
        return

    try:
        # Prepare upload request
        uri = f"{CONFIG['API']['buckets']}/{CONFIG['REMOTE_BUCKET']}/files"
        prefix = f"{CONFIG['LOCAL_BUCKET']}".rstrip("/")
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

        LOG.debug(f"Upload file: {file.filepath}")
        LOG.debug(f"        URI: {uri}")
        LOG.debug(f"     Header: {CONFIG['REQUEST_HEADERS']}")
        LOG.debug(f"       data: {data}")

        with open(file.filepath, "rb") as upload:
            files = {"file": (basename(file.filepath), upload)}
            response = post(
                uri,
                headers=cast(dict[str, str], CONFIG["REQUEST_HEADERS"]),
                data=data,
                files=files,
                verify=get_ssl_verify(),
            )
            if response.status_code == 201:
                LOG.info(f'[UPLOAD    ] "{file.filepath}" {bytes_to_human_readable(file.size)}')
            elif response.status_code == 401:
                LOG.error(f'Authentication failed uploading "{file.filepath}"')
            elif response.status_code == 403:
                LOG.error(f'Access denied uploading "{file.filepath}"')
            elif response.status_code == 409:
                LOG.debug(f'"{file.filepath}" (already present)')
            elif response.status_code >= 500:
                LOG.error(f'Server error uploading "{file.filepath}": {response.status_code}')
            else:
                response.raise_for_status()
    except FileNotFoundError:
        LOG.error(f'File not found: "{file.filepath}"')
    except PermissionError:
        LOG.error(f'Permission denied reading: "{file.filepath}"')
    except RequestException as e:
        LOG.error(f'Network error uploading "{file.filepath}": {e}')


# =============================================================================
# No-Op Logging
# =============================================================================


def nothing_to_do(file: File) -> None:
    """
    Log that a file requires no action (already in sync).

    Args:
        file: File that is already synchronized.
    """
    LOG.debug(f'"{file.filepath}"')

