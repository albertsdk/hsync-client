# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
File transfer operations for HSYNC Client.

This module provides upload and download functionality for
synchronizing files with the remote server.
"""

from os import makedirs, remove, utime
from typing import cast
from os.path import basename, dirname, exists, getmtime, getsize, join
from requests import get, post, RequestException
from shutil import move
from tempfile import NamedTemporaryFile
from time import sleep as time_sleep

from loguru import logger as LOG

from hsync_client.compression import compress_data, decompress_data, should_compress
from hsync_client.config import CONFIG
from hsync_client.constants import FILE_STABILITY_INTERVAL_DEFAULT
from hsync_client.models.file import File
from hsync_client.utils import (
    bytes_to_human_readable,
    compute_md5,
    get_chunk_size,
    get_ssl_verify,
    set_permissions,
    verify_master_fingerprint,
)


# Content-Encoding header value for zstd compression
CONTENT_ENCODING_ZSTD = "zstd"


# =============================================================================
# Download Operations
# =============================================================================


def download_file(file: File) -> None:
    """
    Download a file from the remote server with resume support.

    Streams the file to a partial file location (.part extension), verifies
    MD5 checksum, then moves it to the final destination. Preserves timestamps
    and permissions from the remote file.

    If a partial file exists from a previous interrupted download, resumes
    from where it left off using HTTP Range requests.

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

    # Prepare destination directory and partial file path
    relative_dir = dirname(file.filepath)
    dest_dir = join(CONFIG["LOCAL_BUCKET"], relative_dir.lstrip("/"))
    makedirs(dest_dir, exist_ok=True)

    full_save_path = join(dest_dir, file.name.lstrip("/"))
    partial_file_path = f"{full_save_path}.part"

    # Check if local file is already up-to-date
    if exists(full_save_path):
        local_mtime = getmtime(full_save_path)
        if file.mtime is not None and local_mtime >= file.mtime:
            nothing_to_do(file)
            return

    # Check for existing partial file and determine resume offset
    resume_offset = 0
    if exists(partial_file_path):
        resume_offset = getsize(partial_file_path)
        # Don't resume if partial file is larger than expected
        if resume_offset >= file.size:
            LOG.warning(
                f'Partial file larger than expected for "{file.filepath}", '
                f"restarting download"
            )
            remove(partial_file_path)
            resume_offset = 0
        elif resume_offset > 0:
            LOG.info(
                f'[RESUME    ] "{file.filepath}" from '
                f"{bytes_to_human_readable(resume_offset)}/{bytes_to_human_readable(file.size)}"
            )

    # Prepare request headers with Range if resuming
    uri = f"{CONFIG['API']['buckets']}/{CONFIG['REMOTE_BUCKET']}/files/download"
    headers = dict(cast(dict[str, str], CONFIG["REQUEST_HEADERS"]))
    if resume_offset > 0:
        headers["Range"] = f"bytes={resume_offset}-"

    # Verify master certificate fingerprint for LAN HTTPS mode
    if not verify_master_fingerprint(uri):
        LOG.error(f'Certificate fingerprint verification failed for "{file.filepath}"')
        return

    try:
        response = get(
            uri,
            headers=headers,
            params={"filename": file.filepath},
            stream=True,
            verify=get_ssl_verify(),
            timeout=CONFIG.get("HTTP_TIMEOUT", 30),
        )
    except RequestException as e:
        LOG.error(f'Network error downloading "{file.filepath}": {e}')
        return

    LOG.debug(f"Request URI: {uri}")
    LOG.debug(f"Request Header: {headers}")
    LOG.debug(f"Response status: {response.status_code}")

    if response.status_code == 401:
        LOG.error(f'Authentication failed downloading "{file.filepath}"')
        return
    if response.status_code == 404:
        LOG.error(f'File not found on server: "{file.filepath}"')
        return
    if response.status_code == 416:
        # Range Not Satisfiable - partial file is invalid, restart
        LOG.warning(f'Range not satisfiable for "{file.filepath}", restarting download')
        if exists(partial_file_path):
            remove(partial_file_path)
        # Retry without Range header
        download_file(file)
        return
    if response.status_code not in (200, 206):
        LOG.error(f'Server error downloading "{file.filepath}": {response.status_code}')
        return

    # If server returned 200 instead of 206, it doesn't support Range - restart
    if resume_offset > 0 and response.status_code == 200:
        LOG.debug(f'Server does not support Range requests, restarting download')
        resume_offset = 0
        if exists(partial_file_path):
            remove(partial_file_path)

    try:
        # Check if response is compressed
        content_encoding = response.headers.get("Content-Encoding", "")
        is_compressed = content_encoding.lower() == CONTENT_ENCODING_ZSTD

        # Calculate optimal chunk size based on file size
        chunk_size = get_chunk_size(file.size)

        if is_compressed and resume_offset == 0:
            # Compressed response - download all and decompress
            # (compression is not used with resume)
            compressed_data = response.content
            LOG.debug(
                f"Decompressing {bytes_to_human_readable(len(compressed_data))} "
                f"-> ~{bytes_to_human_readable(file.size)}"
            )
            decompressed_data = decompress_data(compressed_data)
            with open(partial_file_path, "wb") as f:
                f.write(decompressed_data)
        else:
            # Stream file content to partial file (append if resuming)
            write_mode = "ab" if resume_offset > 0 and response.status_code == 206 else "wb"
            with open(partial_file_path, write_mode) as f:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if not chunk:
                        break
                    f.write(chunk)

        # Verify MD5 checksum of complete file
        md5sum = compute_md5(partial_file_path)
        if md5sum != file.md5sum:
            LOG.error(
                f'"{file.filepath}" '
                f"(Invalid checksum: expected {file.md5sum}, "
                f"{md5sum} given)"
            )
            # Keep partial file for potential manual inspection, but rename it
            return

        # Move partial file to final destination
        move(partial_file_path, full_save_path)
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


# =============================================================================
# File Stability Check
# =============================================================================


def is_file_stable(file: File, interval: int | None = None) -> bool:
    """
    Check if a file has stopped being written to.

    Compares file size and modification time at two points in time to detect
    if the file is still being modified. Used to wait for file writes to
    complete before uploading.

    Args:
        file: File object to check.
        interval: Seconds to wait between checks. If None, uses the configured
                  STABILITY_DELAY from CONFIG, or falls back to the default.

    Returns:
        True if file size and mtime are stable, False if still changing.
    """
    # Determine interval: explicit parameter > CONFIG > default
    if interval is None:
        interval = CONFIG.get("STABILITY_DELAY", FILE_STABILITY_INTERVAL_DEFAULT)
    # Validate filepath before checking
    if not file.filepath:
        return True

    try:
        last_size = getsize(file.filepath)
        last_mtime = getmtime(file.filepath)
    except OSError:
        # File may have been deleted
        return True

    time_sleep(interval)

    try:
        current_size = getsize(file.filepath)
        current_mtime = getmtime(file.filepath)
    except OSError:
        # File may have been deleted
        return True

    LOG.debug(
        f'Writing file: "{file.filepath}" '
        f"(size: {bytes_to_human_readable(last_size)} -> "
        f"{bytes_to_human_readable(current_size)}, "
        f"mtime: {last_mtime} -> {current_mtime})"
    )

    return last_size == current_size and last_mtime == current_mtime


# =============================================================================
# Upload Operations
# =============================================================================


def _get_upload_status(filename: str, md5sum: str, size: int) -> tuple[str, int]:
    """
    Check upload status for resumable uploads.

    Args:
        filename: Path to the file within the bucket.
        md5sum: Expected MD5 checksum of the complete file.
        size: Total file size.

    Returns:
        Tuple of (status, offset) where:
        - status: "complete", "resumable", or "new"
        - offset: Number of bytes already uploaded (0 if new)
    """
    try:
        uri = f"{CONFIG['API']['buckets']}/{CONFIG['REMOTE_BUCKET']}/files/upload-status"

        # Verify master certificate fingerprint for LAN HTTPS mode
        if not verify_master_fingerprint(uri):
            return "new", 0

        response = get(
            uri,
            headers=cast(dict[str, str], CONFIG["REQUEST_HEADERS"]),
            params={"filename": filename, "md5sum": md5sum, "size": size},
            verify=get_ssl_verify(),
            timeout=CONFIG.get("HTTP_TIMEOUT", 30),
        )
        if response.status_code == 200:
            data = response.json()
            return data.get("status", "new"), data.get("offset", 0)
    except RequestException:
        pass
    return "new", 0


def upload_file(file: File, check_size: bool = True) -> None:
    """
    Upload a file to the remote server with resume support.

    If check_size is True, waits for the file to stop being written
    to before uploading. Sends file with metadata including timestamps,
    MD5 checksum, and permissions.

    If a previous upload was interrupted, resumes from where it left off.

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
        # Wait for file to stop being written, with timeout
        max_wait = CONFIG.get("MAX_WAIT_TIMEOUT", 120)
        elapsed = 0
        while not is_file_stable(file):
            if elapsed >= max_wait:
                LOG.warning(
                    f'File "{file.filepath}" did not stabilize within {max_wait}s, '
                    f"uploading anyway"
                )
                break
            time_sleep(1)
            elapsed += 1

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
        filename = f"{path}/{basename(file.filepath)}" if path else basename(file.filepath)

        # Verify master certificate fingerprint for LAN HTTPS mode
        if not verify_master_fingerprint(uri):
            LOG.error(f'Certificate fingerprint verification failed for "{file.filepath}"')
            return

        # Check for resumable upload
        if file.md5sum is None:
            status, offset = "new", 0
        else:
            status, offset = _get_upload_status(filename, file.md5sum, file.size)

        if status == "complete":
            LOG.debug(f'"{file.filepath}" (already present on server)')
            return

        if status == "resumable" and offset > 0:
            LOG.info(
                f'[RESUME    ] "{file.filepath}" from '
                f"{bytes_to_human_readable(offset)}/{bytes_to_human_readable(file.size)}"
            )

        # Build form data with file metadata
        data = {
            "cid": CONFIG["CID"],
            "path": path,
            "ctime": file.ctime,
            "mtime": file.mtime,
            "size": file.size,
            "md5sum": file.md5sum,
            "permissions": file.permissions,
            "offset": offset,
        }

        LOG.debug(f"Upload file: {file.filepath}")
        LOG.debug(f"        URI: {uri}")
        LOG.debug(f"     Header: {CONFIG['REQUEST_HEADERS']}")
        LOG.debug(f"       data: {data}")

        # Determine if compression should be used
        # Don't compress if: resuming, client disabled, server disabled, or file not compressible
        use_compression = (
            offset == 0
            and CONFIG.get("COMPRESSION_ENABLED", False)
            and CONFIG.get("SERVER_COMPRESSION_ENABLED", False)
            and should_compress(file.filepath, file.size, transfer_type="upload")
        )

        headers = dict(cast(dict[str, str], CONFIG["REQUEST_HEADERS"]))

        if use_compression:
            # Read and compress file data
            with open(file.filepath, "rb") as f:
                file_data = f.read()
            compressed_data = compress_data(file_data)
            compression_ratio = len(compressed_data) / len(file_data) * 100
            LOG.debug(
                f"Compressed {bytes_to_human_readable(len(file_data))} -> "
                f"{bytes_to_human_readable(len(compressed_data))} "
                f"({compression_ratio:.1f}%)"
            )

            # Add compression header and flag
            headers["Content-Encoding"] = CONTENT_ENCODING_ZSTD
            data["compressed"] = "true"
            data["compressed_size"] = len(compressed_data)

            # Send compressed data as file
            from io import BytesIO
            compressed_file = BytesIO(compressed_data)
            files = {"file": (basename(file.filepath), compressed_file)}
            response = post(
                uri,
                headers=headers,
                data=data,
                files=files,
                verify=get_ssl_verify(),
                timeout=CONFIG.get("HTTP_TIMEOUT", 30),
            )
        else:
            # Standard upload (streaming, supports resume)
            with open(file.filepath, "rb") as upload:
                # Seek to offset if resuming
                if offset > 0:
                    upload.seek(offset)

                files = {"file": (basename(file.filepath), upload)}
                response = post(
                    uri,
                    headers=headers,
                    data=data,
                    files=files,
                    verify=get_ssl_verify(),
                    timeout=CONFIG.get("HTTP_TIMEOUT", 30),
                )
            if response.status_code == 201:
                LOG.info(f'[UPLOAD    ] "{file.filepath}" {bytes_to_human_readable(file.size)}')
            elif response.status_code == 202:
                # Partial upload received - file still incomplete
                result = response.json()
                new_offset = result.get("offset", 0)
                remaining = result.get("remaining", 0)
                LOG.debug(
                    f'Partial upload: {new_offset} bytes uploaded, '
                    f'{remaining} bytes remaining'
                )
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

