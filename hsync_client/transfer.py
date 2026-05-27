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

from json import dumps as _json_dumps
from os import makedirs, remove, utime


def _safe_json(obj: object) -> str:
    """Serialize object to JSON and escape braces for loguru."""
    return _json_dumps(obj).replace("{", "{{").replace("}", "}}")


from os.path import basename, dirname, exists, getmtime, getsize, join
from shutil import move
from time import sleep as time_sleep
from typing import cast

from loguru import logger as log
from requests import RequestException, get, post

from hsync_client.compression import compress_data, decompress_data, should_compress
from hsync_client.config import CONFIG
from hsync_client.constants import FILE_STABILITY_INTERVAL_DEFAULT
from hsync_client.models.file import File
from hsync_client.utils import (
    bytes_to_human_readable,
    compute_sha256,
    get_chunk_size,
    get_ssl_verify,
    get_throttle_delay,
    set_permissions,
    verify_master_fingerprint,
)

# Content-Encoding header value for zstd compression
CONTENT_ENCODING_ZSTD = "zstd"


class _ThrottledFileReader:
    """Wraps a file object and applies bandwidth throttling on read().

    The ``requests`` library calls ``read()`` internally when streaming
    a multipart upload, so wrapping the file handle lets us inject a
    delay after each chunk without modifying the upload flow.
    """

    def __init__(self, fp, bandwidth_limit: int | None, transfer_key: str = "upload"):
        self._fp = fp
        self._bw_limit = bandwidth_limit
        self._key = transfer_key

    # Proxy every attribute to the inner file …
    def __getattr__(self, name: str):
        return getattr(self._fp, name)

    def read(self, size: int = -1) -> bytes:
        data = self._fp.read(size)
        if data and self._bw_limit:
            delay = get_throttle_delay(len(data), self._bw_limit, self._key)
            if delay > 0:
                time_sleep(delay)
        return data


# Upload streaming chunk size
_UPLOAD_CHUNK_SIZE = 64 * 1024


def _build_multipart_streaming(
    file_path: str,
    fields: dict,
    boundary: str,
    file_field_name: str = "file",
    filename: str | None = None,
    offset: int = 0,
    bandwidth_limit: int | None = None,
):
    """Construct a streaming multipart/form-data body.

    Returns ``(generator, content_length, content_type)`` where ``generator``
    yields the body in chunks (file part streamed from disk, never loaded
    into memory) and ``content_length`` is the exact total byte count for
    the HTTP Content-Length header.
    """
    if filename is None:
        filename = basename(file_path)

    file_size = getsize(file_path) - offset
    if file_size < 0:
        file_size = 0

    # Pre-compute body parts so Content-Length is exact
    field_parts: list[bytes] = []
    for name, value in fields.items():
        field_parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )

    file_header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{file_field_name}"; '
        f'filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    file_trailer = b"\r\n"
    closing = f"--{boundary}--\r\n".encode()

    content_length = (
        sum(len(p) for p in field_parts)
        + len(file_header)
        + file_size
        + len(file_trailer)
        + len(closing)
    )
    content_type = f"multipart/form-data; boundary={boundary}"

    def _generator():
        yield from field_parts
        yield file_header
        with open(file_path, "rb") as fh:
            if offset > 0:
                fh.seek(offset)
            while True:
                chunk = fh.read(_UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
                if bandwidth_limit:
                    delay = get_throttle_delay(len(chunk), bandwidth_limit, "upload")
                    if delay > 0:
                        time_sleep(delay)
        yield file_trailer
        yield closing

    return _generator(), content_length, content_type


def _upload_timeout() -> tuple[float, float | None]:
    """Return (connect_timeout, read_timeout) for upload requests.

    Read timeout is disabled because large file uploads can legitimately
    keep the socket busy for many minutes; the connect timeout still
    catches unreachable servers quickly.
    """
    connect = float(CONFIG.get("HTTP_TIMEOUT", 30))
    return connect, None


# =============================================================================
# Download Operations
# =============================================================================


def download_file(file: File) -> None:
    """
    Download a file from the remote server with resume support.

    Streams the file to a partial file location (.part extension), verifies
    SHA-256 checksum, then moves it to the final destination. Preserves timestamps
    and permissions from the remote file.

    If a partial file exists from a previous interrupted download, resumes
    from where it left off using HTTP Range requests.

    Args:
        file: File object with remote metadata (filepath, sha256sum, etc.).

    Raises:
        DownloadError: If download fails.
        ChecksumError: If SHA-256 checksum doesn't match.
        FileWriteError: If file cannot be saved locally.
    """
    # Validate required file attributes
    if not file.filepath or not file.name:
        log.error("Invalid file metadata: missing filepath or name")
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
            log.warning(
                f'Partial file larger than expected for "{file.filepath}", restarting download'
            )
            remove(partial_file_path)
            resume_offset = 0
        elif resume_offset > 0:
            log.info(
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
        log.error(f'Certificate fingerprint verification failed for "{file.filepath}"')
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
        log.error(f'Network error downloading "{file.filepath}": {e}')
        return

    log.debug(f"Request URI: {uri}")
    log.debug(f"Request Header: {_safe_json(headers)}")
    log.debug(f"Response status: {response.status_code}")

    if response.status_code == 401:
        log.error(f'Authentication failed downloading "{file.filepath}"')
        return
    if response.status_code == 404:
        log.error(f'File not found on server: "{file.filepath}"')
        return
    if response.status_code == 416:
        # Range Not Satisfiable - partial file is invalid, restart
        log.warning(f'Range not satisfiable for "{file.filepath}", restarting download')
        if exists(partial_file_path):
            remove(partial_file_path)
        # Retry without Range header
        download_file(file)
        return
    if response.status_code not in (200, 206):
        log.error(f'Server error downloading "{file.filepath}": {response.status_code}')
        return

    # If server returned 200 instead of 206, it doesn't support Range - restart
    if resume_offset > 0 and response.status_code == 200:
        log.debug("Server does not support Range requests, restarting download")
        resume_offset = 0
        if exists(partial_file_path):
            remove(partial_file_path)

    cleanup_partial = True
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
            log.debug(
                f"Decompressing {bytes_to_human_readable(len(compressed_data))} "
                f"-> ~{bytes_to_human_readable(file.size)}"
            )
            decompressed_data = decompress_data(compressed_data)
            with open(partial_file_path, "wb") as f:
                f.write(decompressed_data)
        else:
            # Stream file content to partial file (append if resuming)
            write_mode = "ab" if resume_offset > 0 and response.status_code == 206 else "wb"
            dl_bw_limit = CONFIG.get("MAX_BANDWIDTH_DOWNLOAD")
            with open(partial_file_path, write_mode) as f:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if not chunk:
                        break
                    f.write(chunk)
                    # Throttle download if bandwidth limit is set
                    delay = get_throttle_delay(len(chunk), dl_bw_limit, "download")
                    if delay > 0:
                        time_sleep(delay)

        # Verify SHA-256 checksum of complete file
        sha256sum = compute_sha256(partial_file_path)
        if sha256sum != file.sha256sum:
            log.error(
                f'"{file.filepath}" (Invalid checksum: expected {file.sha256sum}, {sha256sum} given)'
            )
            # Keep partial file for potential manual inspection
            cleanup_partial = False
            return

        # Move partial file to final destination
        move(partial_file_path, full_save_path)
        cleanup_partial = False
        log.info(f'[DOWNLOAD  ] "{full_save_path}" {bytes_to_human_readable(file.size)}')

        # Preserve timestamps from remote file
        if file.ctime is not None and file.mtime is not None:
            utime(full_save_path, (file.ctime, file.mtime))

        # Apply permissions
        if file.permissions:
            set_permissions(full_save_path, file.permissions)

    except PermissionError as e:
        log.error(f'Permission denied saving "{file.filepath}": {e}')
    except OSError as e:
        log.error(f'Error saving file "{file.filepath}": {e}')
    finally:
        if cleanup_partial and partial_file_path and exists(partial_file_path):
            remove(partial_file_path)
            log.debug(f"Cleaned up partial file: {partial_file_path}")


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

    log.debug(
        f'Writing file: "{file.filepath}" '
        f"(size: {bytes_to_human_readable(last_size)} -> "
        f"{bytes_to_human_readable(current_size)}, "
        f"mtime: {last_mtime} -> {current_mtime})"
    )

    return last_size == current_size and last_mtime == current_mtime


# =============================================================================
# Upload Operations
# =============================================================================


def _get_upload_status(filename: str, sha256sum: str, size: int) -> tuple[str, int]:
    """
    Check upload status for resumable uploads.

    Args:
        filename: Path to the file within the bucket.
        sha256sum: Expected SHA-256 checksum of the complete file.
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
            params={"filename": filename, "sha256sum": sha256sum, "size": size},
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
    SHA-256 checksum, and permissions.

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
        log.error("Cannot upload file: missing filepath")
        return

    if check_size:
        # Wait for file to stop being written, with timeout
        max_wait = CONFIG.get("MAX_WAIT_TIMEOUT", 120)
        elapsed = 0
        while not is_file_stable(file):
            if elapsed >= max_wait:
                log.warning(
                    f'File "{file.filepath}" did not stabilize within {max_wait}s, uploading anyway'
                )
                break
            time_sleep(1)
            elapsed += 1

        # Refresh file metadata after write is complete
        file.size = getsize(file.filepath)
        file.sha256sum = compute_sha256(file.filepath)

        log.debug(f'Written file: "{file.filepath}" ({bytes_to_human_readable(file.size)})')

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
            log.error(f'Certificate fingerprint verification failed for "{file.filepath}"')
            return

        # Check for resumable upload
        if file.sha256sum is None:
            status, offset = "new", 0
        else:
            status, offset = _get_upload_status(filename, file.sha256sum, file.size)

        if status == "complete":
            log.debug(f'"{file.filepath}" (already present on server)')
            return

        if status == "resumable" and offset > 0:
            log.info(
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
            "sha256sum": file.sha256sum,
            "permissions": file.permissions,
            "offset": offset,
        }

        log.debug(f"Upload file: {file.filepath}")
        log.debug(f"        URI: {uri}")
        log.debug(f"     Header: {_safe_json(CONFIG['REQUEST_HEADERS'])}")
        log.debug(f"       data: {_safe_json(data)}")

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
            log.debug(
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
            ul_bw_limit = CONFIG.get("MAX_BANDWIDTH_UPLOAD")
            fh = (
                _ThrottledFileReader(compressed_file, ul_bw_limit)
                if ul_bw_limit
                else compressed_file
            )
            files = {"file": (basename(file.filepath), fh)}
            response = post(
                uri,
                headers=headers,
                data=data,
                files=files,
                verify=get_ssl_verify(),
                timeout=_upload_timeout(),
            )
            if response.status_code == 201:
                log.info(f'[UPLOAD    ] "{file.filepath}" {bytes_to_human_readable(file.size)}')
            elif response.status_code == 401:
                log.error(f'Authentication failed uploading "{file.filepath}"')
            elif response.status_code == 403:
                log.error(f'Access denied uploading "{file.filepath}"')
            elif response.status_code == 409:
                log.debug(f'"{file.filepath}" (already present)')
            elif response.status_code >= 500:
                log.error(f'Server error uploading "{file.filepath}": {response.status_code}')
            else:
                response.raise_for_status()
        else:
            # Standard upload: stream the multipart body directly from disk
            # so files of arbitrary size do not need to be loaded into RAM.
            ul_bw_limit = CONFIG.get("MAX_BANDWIDTH_UPLOAD")
            import uuid

            boundary = uuid.uuid4().hex
            body_iter, content_length, content_type = _build_multipart_streaming(
                file.filepath,
                dict(data),
                boundary=boundary,
                filename=basename(file.filepath),
                offset=offset,
                bandwidth_limit=ul_bw_limit,
            )
            headers["Content-Type"] = content_type
            headers["Content-Length"] = str(content_length)
            response = post(
                uri,
                headers=headers,
                data=body_iter,
                verify=get_ssl_verify(),
                timeout=_upload_timeout(),
            )
            if response.status_code == 201:
                log.info(f'[UPLOAD    ] "{file.filepath}" {bytes_to_human_readable(file.size)}')
            elif response.status_code == 202:
                # Partial upload received - file still incomplete
                result = response.json()
                new_offset = result.get("offset", 0)
                remaining = result.get("remaining", 0)
                log.debug(
                    f"Partial upload: {new_offset} bytes uploaded, {remaining} bytes remaining"
                )
            elif response.status_code == 401:
                log.error(f'Authentication failed uploading "{file.filepath}"')
            elif response.status_code == 403:
                log.error(f'Access denied uploading "{file.filepath}"')
            elif response.status_code == 409:
                log.debug(f'"{file.filepath}" (already present)')
            elif response.status_code >= 500:
                log.error(f'Server error uploading "{file.filepath}": {response.status_code}')
            else:
                response.raise_for_status()
    except FileNotFoundError:
        log.error(f'File not found: "{file.filepath}"')
    except PermissionError:
        log.error(f'Permission denied reading: "{file.filepath}"')
    except RequestException as e:
        log.error(f'Network error uploading "{file.filepath}": {e}')


# =============================================================================
# No-Op Logging
# =============================================================================


def nothing_to_do(file: File) -> None:
    """
    Log that a file requires no action (already in sync).

    Args:
        file: File that is already synchronized.
    """
    log.debug(f'"{file.filepath}"')
