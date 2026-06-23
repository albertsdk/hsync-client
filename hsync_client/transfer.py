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

import threading
from os import fsync, remove, replace, utime
from pathlib import Path
from shutil import disk_usage
from time import sleep as time_sleep
from typing import cast

from hsync_common.text import safe_json_for_log
from loguru import logger as log
from requests import RequestException, get, post

from hsync_client.compression import compress_data, decompress_data, should_compress
from hsync_client.config import CONFIG
from hsync_client.constants import (
    FILE_STABILITY_INTERVAL_DEFAULT,
    MAX_WAIT_TIMEOUT_DEFAULT,
    TRANSFER_RETRY_ATTEMPTS_DEFAULT,
    TRANSFER_RETRY_MAX_WAIT_DEFAULT,
    TRANSFER_RETRY_MIN_WAIT_DEFAULT,
    UPLOAD_MAX_RESCHEDULES_DEFAULT,
    UPLOAD_RESCHEDULE_DELAY_DEFAULT,
)
from hsync_client.events import UiEventName, emit_event
from hsync_client.exceptions import ServerError
from hsync_client.models.file import File
from hsync_client.retry import retry_on_transient_transfer_error
from hsync_client.utils import (
    bytes_to_human_readable,
    compute_sha256,
    get_chunk_size,
    get_ssl_verify,
    get_throttle_delay,
    path_lock,
    set_permissions,
    verify_master_fingerprint,
)

# Content-Encoding header value for zstd compression
CONTENT_ENCODING_ZSTD = "zstd"


def _transfer_retry_params() -> tuple[int, float, float]:
    """Return ``(max_attempts, min_wait, max_wait)`` for per-file retries (§11)."""
    return (
        CONFIG.get("TRANSFER_RETRY_ATTEMPTS", TRANSFER_RETRY_ATTEMPTS_DEFAULT),
        CONFIG.get("TRANSFER_RETRY_MIN_WAIT", TRANSFER_RETRY_MIN_WAIT_DEFAULT),
        CONFIG.get("TRANSFER_RETRY_MAX_WAIT", TRANSFER_RETRY_MAX_WAIT_DEFAULT),
    )


def _emit_transfer_failed(op: str, filepath: str, message: str) -> None:
    """Surface a permanent/exhausted transfer failure as a structured event (§11)."""
    emit_event(UiEventName.transfer_failed, op=op, file=filepath, message=message)


def _record_synced_base(file: File) -> None:
    """Record the just-synced content hash as the new common-ancestor base.

    After a successful transfer (or a no-op because the server already held the
    file), local and remote agree on ``file.sha256sum``; persisting it lets the
    next reconciliation use a clock-independent three-way merge (see ``§7``).
    Bookkeeping failures are swallowed so they never abort a successful transfer.
    """
    try:
        from hsync_client.sync_state import flush, record_base

        if file.filepath and file.sha256sum:
            record_base(file.filepath, file.sha256sum)
            flush()
    except Exception as e:  # pragma: no cover - bookkeeping must never break sync
        log.debug(f'Could not record sync-state for "{file.filepath}": {e}')


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
        filename = Path(file_path).name

    file_size = Path(file_path).stat().st_size - offset
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


def _local_dest_path(file: File) -> str:
    """Return the absolute local destination path for a downloaded *file*.

    The remote ``file.filepath`` is relative to the bucket; the local copy lives
    under ``LOCAL_BUCKET``. Shared by the downloader and the per-path lock key so
    a download serializes against any other operation on the same local path.
    """
    relative_dir = str(Path(file.filepath).parent)
    dest_dir = str(Path(CONFIG["LOCAL_BUCKET"]) / relative_dir.lstrip("/"))
    return str(Path(dest_dir) / file.name.lstrip("/"))


def download_file(file: File) -> None:
    """
    Download a file from the remote server with per-file retry/backoff (§11).

    Wraps :func:`_download_file_attempt` in an exponential-backoff retry policy
    so a single transient network blip (timeout, connection reset, 5xx) does not
    leave the file unsynced until the next full sync. Permanent errors (4xx) are
    never retried. When every attempt is exhausted the failure is surfaced as a
    ``transfer_failed`` event for the UI.

    The whole download holds the per-path lock (§14) so a concurrent upload or
    inbound delete of the same local path cannot interleave and lose the update.

    Args:
        file: File object with remote metadata (filepath, sha256sum, etc.).
    """
    # Validate required file attributes
    if not file.filepath or not file.name:
        log.error("Invalid file metadata: missing filepath or name")
        return

    max_attempts, min_wait, max_wait = _transfer_retry_params()
    attempt = retry_on_transient_transfer_error(max_attempts, min_wait, max_wait)(
        _download_file_attempt
    )
    try:
        with path_lock(_local_dest_path(file)):
            attempt(file)
    except (RequestException, ServerError) as exc:
        log.error(f'Download failed after retries for "{file.filepath}": {exc}')
        _emit_transfer_failed("download", file.filepath, str(exc))


def _download_file_attempt(file: File) -> None:
    """
    Perform a single download attempt with resume support (§11).

    Streams the file to a partial file location (.part extension), verifies
    SHA-256 checksum, then moves it to the final destination. Preserves timestamps
    and permissions from the remote file.

    If a partial file exists from a previous interrupted download, resumes
    from where it left off using HTTP Range requests.

    Transient failures are raised (``RequestException`` for connection-level
    errors, ``ServerError`` for 5xx) so the caller's retry policy can react;
    permanent errors (4xx, checksum mismatch) are logged and swallowed.

    Args:
        file: File object with remote metadata (filepath, sha256sum, etc.).

    Raises:
        RequestException: On a connection-level error (retryable).
        ServerError: On a server-side 5xx response (retryable).
    """
    # Prepare destination directory and partial file path
    full_save_path = _local_dest_path(file)
    Path(full_save_path).parent.mkdir(parents=True, exist_ok=True)
    partial_file_path = f"{full_save_path}.part"

    # Check if local file is already up-to-date
    if Path(full_save_path).exists():
        local_mtime = Path(full_save_path).stat().st_mtime
        if file.mtime is not None and local_mtime >= file.mtime:
            nothing_to_do(file)
            return

    # Check for existing partial file and determine resume offset
    resume_offset = 0
    if Path(partial_file_path).exists():
        resume_offset = Path(partial_file_path).stat().st_size
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

    # Size-aware disk space guard: skip the file if the target filesystem
    # cannot hold the remaining bytes plus the configured reserve, avoiding a
    # mid-transfer ENOSPC that would waste bandwidth and leave a .part behind.
    reserve_mb = CONFIG.get("MIN_FREE_DISK_SPACE", 0)
    if reserve_mb and reserve_mb > 0:
        try:
            free = disk_usage(str(Path(full_save_path).parent)).free
            required = reserve_mb * 1024 * 1024 + max(file.size - resume_offset, 0)
            if free < required:
                log.error(
                    f'Insufficient disk space for "{file.filepath}": '
                    f"{free // (1024 * 1024)} MB available, "
                    f"{required // (1024 * 1024)} MB required"
                )
                return
        except OSError as e:
            log.warning(f"Could not check disk space: {e}")

    # Prepare request headers with Range if resuming
    uri = f"{CONFIG['API']['buckets']}/{CONFIG['REMOTE_BUCKET']}/files/download"
    headers = dict(cast(dict[str, str], CONFIG["REQUEST_HEADERS"]))
    if resume_offset > 0:
        headers["Range"] = f"bytes={resume_offset}-"

    # Verify master certificate fingerprint for LAN HTTPS mode
    if not verify_master_fingerprint(uri):
        log.error(f'Certificate fingerprint verification failed for "{file.filepath}"')
        return

    # A connection-level error here propagates to the retry wrapper (§11).
    response = get(
        uri,
        headers=headers,
        params={"filename": file.filepath},
        stream=True,
        verify=get_ssl_verify(),
        timeout=CONFIG.get("HTTP_TIMEOUT", 30),
    )

    log.debug(f"Request URI: {uri}")
    log.debug(f"Request Header: {safe_json_for_log(headers)}")
    log.debug(f"Response status: {response.status_code}")

    if response.status_code == 401:
        log.error(f'Authentication failed downloading "{file.filepath}"')
        emit_event(
            UiEventName.auth_error, message=f'Authentication failed downloading "{file.filepath}"'
        )
        return
    if response.status_code == 404:
        log.error(f'File not found on server: "{file.filepath}"')
        return
    if response.status_code == 416:
        # Range Not Satisfiable - partial file is invalid, restart
        log.warning(f'Range not satisfiable for "{file.filepath}", restarting download')
        if Path(partial_file_path).exists():
            remove(partial_file_path)
        # Retry without Range header. Recurse into the attempt (not the public
        # download_file) so we stay inside the per-path lock (§14) already held
        # by the caller; re-locking the non-reentrant lock would deadlock.
        _download_file_attempt(file)
        return
    if response.status_code >= 500:
        # Transient server-side failure: raise so the retry wrapper backs off.
        raise ServerError(f'Server error downloading "{file.filepath}": {response.status_code}')
    if response.status_code not in (200, 206):
        log.error(f'Unexpected status downloading "{file.filepath}": {response.status_code}')
        return

    # If server returned 200 instead of 206, it doesn't support Range - restart
    if resume_offset > 0 and response.status_code == 200:
        log.debug("Server does not support Range requests, restarting download")
        resume_offset = 0
        if Path(partial_file_path).exists():
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
                # Flush OS buffers to disk before the file is considered written
                f.flush()
                fsync(f.fileno())
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
                # Flush OS buffers to disk before the file is considered written
                f.flush()
                fsync(f.fileno())

        # Verify SHA-256 checksum of complete file
        sha256sum = compute_sha256(partial_file_path)
        server_sha256 = response.headers.get("X-Checksum-SHA256")

        # Check 1: Client-computed hash vs. server-asserted current hash
        if server_sha256 and sha256sum != server_sha256:
            log.error(
                f'"{file.filepath}" '
                f"(Server checksum mismatch: expected {server_sha256}, computed {sha256sum})"
            )
            cleanup_partial = False
            return

        # Check 2: Client-computed hash vs. listing metadata hash (stale-safe fallback)
        if sha256sum != file.sha256sum:
            log.error(
                f'"{file.filepath}" (Invalid checksum: expected {file.sha256sum}, {sha256sum} given)'
            )
            cleanup_partial = False
            return

        # Atomically move partial file to final destination (same-filesystem
        # rename) so a partially written file is never visible at the target path
        replace(partial_file_path, full_save_path)
        cleanup_partial = False
        log.info(f'[DOWNLOAD  ] "{full_save_path}" {bytes_to_human_readable(file.size)}')
        emit_event(UiEventName.transfer, op="download", file=full_save_path, size=file.size or 0)

        # Restore the modification time from the remote file. POSIX ctime is
        # not settable, so it is intentionally not restored; atime mirrors mtime.
        if file.mtime is not None:
            utime(full_save_path, (file.mtime, file.mtime))

        # Apply permissions
        if file.permissions:
            set_permissions(full_save_path, file.permissions)

        # Local now matches the remote content: remember it as the merge base.
        _record_synced_base(file)

    except PermissionError as e:
        log.error(f'Permission denied saving "{file.filepath}": {e}')
    except OSError as e:
        log.error(f'Error saving file "{file.filepath}": {e}')
    finally:
        if cleanup_partial and partial_file_path and Path(partial_file_path).exists():
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
        last_stat = Path(file.filepath).stat()
        last_size = last_stat.st_size
        last_mtime = last_stat.st_mtime
    except OSError:
        # File may have been deleted
        return True

    time_sleep(interval)

    try:
        current_stat = Path(file.filepath).stat()
        current_size = current_stat.st_size
        current_mtime = current_stat.st_mtime
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

        params: dict[str, str | int] = {
            "filename": filename,
            "sha256sum": sha256sum,
            "size": size,
        }
        response = get(
            uri,
            headers=cast(dict[str, str], CONFIG["REQUEST_HEADERS"]),
            params=params,
            verify=get_ssl_verify(),
            timeout=CONFIG.get("HTTP_TIMEOUT", 30),
        )
        if response.status_code == 200:
            data = response.json()
            return data.get("status", "new"), data.get("offset", 0)
    except RequestException:
        pass
    return "new", 0


def _reschedule_upload(file: File, reschedules_left: int | None = None) -> None:
    """
    Defer an upload whose file never stabilized within MAX_WAIT_TIMEOUT (§15).

    A file still being written is not uploaded ("torn" upload); instead a new
    attempt is scheduled after ``UPLOAD_RESCHEDULE_DELAY`` seconds via a daemon
    ``threading.Timer``, which keeps the worker/observer thread free. The number
    of deferrals is bounded by ``UPLOAD_MAX_RESCHEDULES``: once exhausted the
    file is skipped and left to the next file-system event (``on_closed``) or the
    periodic resync.

    Args:
        file: File object whose upload is being deferred.
        reschedules_left: Remaining deferrals; resolved from CONFIG on the first
                          call (None) and decremented on each subsequent one.
    """
    if reschedules_left is None:
        reschedules_left = CONFIG.get("UPLOAD_MAX_RESCHEDULES", UPLOAD_MAX_RESCHEDULES_DEFAULT)

    if reschedules_left <= 0:
        log.warning(
            f'File "{file.filepath}" did not stabilize after max reschedules; '
            "skipping (will retry on next file-system event or periodic resync)"
        )
        return

    delay = CONFIG.get("UPLOAD_RESCHEDULE_DELAY", UPLOAD_RESCHEDULE_DELAY_DEFAULT)
    log.info(
        f'File "{file.filepath}" still being written; rescheduling upload in '
        f"{delay}s ({reschedules_left} attempt(s) left)"
    )
    timer = threading.Timer(delay, upload_file, args=(file, True, reschedules_left - 1))
    timer.daemon = True
    timer.start()


def upload_file(file: File, check_size: bool = True, _reschedules_left: int | None = None) -> None:
    """
    Upload a file to the remote server with per-file retry/backoff (§11).

    If check_size is True, waits for the file to stop being written to before
    uploading. The actual transfer is wrapped in an exponential-backoff retry
    policy so a single transient blip (timeout, connection reset, 5xx) does not
    leave the file unsynced; permanent errors (4xx) are never retried. When every
    attempt is exhausted the failure is surfaced as a ``transfer_failed`` event.

    A file that does not stabilize within ``MAX_WAIT_TIMEOUT`` is not uploaded in
    a half-written ("torn") state: the upload is rescheduled for a later attempt
    via :func:`_reschedule_upload`, bounded by ``UPLOAD_MAX_RESCHEDULES`` (§15).

    If a previous upload was interrupted, resumes from where it left off.

    Args:
        file: File object to upload.
        check_size: If True, wait for file writes to complete first.
        _reschedules_left: Internal; remaining deferred attempts when this call
                           originates from a rescheduled Timer (§15).
    """
    # Validate required file attributes
    if not file.filepath:
        log.error("Cannot upload file: missing filepath")
        return

    if check_size:
        # Wait for file to stop being written, with timeout
        max_wait = CONFIG.get("MAX_WAIT_TIMEOUT", MAX_WAIT_TIMEOUT_DEFAULT)
        elapsed = 0
        stabilized = True
        while not is_file_stable(file):
            if elapsed >= max_wait:
                stabilized = False
                break
            time_sleep(1)
            elapsed += 1

        if not stabilized:
            # File never stopped being written within the wait window: defer the
            # upload instead of transferring a half-written file (§15).
            _reschedule_upload(file, _reschedules_left)
            return

        # Refresh file metadata after write is complete
        file.size = Path(file.filepath).stat().st_size
        file.sha256sum = compute_sha256(file.filepath)

        log.debug(f'Written file: "{file.filepath}" ({bytes_to_human_readable(file.size)})')

        # Recursive call with check_size=False to do actual upload
        upload_file(file, False)
        return

    max_attempts, min_wait, max_wait = _transfer_retry_params()
    attempt = retry_on_transient_transfer_error(max_attempts, min_wait, max_wait)(
        _upload_file_attempt
    )
    try:
        # Serialize on the local path (§14) so the read does not race a
        # concurrent download/delete of the same file.
        with path_lock(file.filepath):
            attempt(file)
    except (RequestException, ServerError) as exc:
        log.error(f'Upload failed after retries for "{file.filepath}": {exc}')
        _emit_transfer_failed("upload", file.filepath, str(exc))


def _upload_file_attempt(file: File) -> None:
    """
    Perform a single upload attempt with resume support (§11).

    Transient failures are raised (``RequestException`` for connection-level
    errors, ``ServerError`` for 5xx) so the caller's retry policy can react;
    permanent local errors (missing file, permission denied) are logged and
    swallowed.

    Args:
        file: File object to upload.

    Raises:
        RequestException: On a connection-level error (retryable).
        ServerError: On a server-side 5xx response (retryable).
    """
    try:
        # Prepare upload request
        uri = f"{CONFIG['API']['buckets']}/{CONFIG['REMOTE_BUCKET']}/files"
        prefix = f"{CONFIG['LOCAL_BUCKET']}".rstrip("/")
        path = str(Path(file.filepath).parent).rstrip("/").removeprefix(prefix).lstrip("/")
        filename = f"{path}/{Path(file.filepath).name}" if path else Path(file.filepath).name

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
            _record_synced_base(file)
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
        log.debug(f"     Header: {safe_json_for_log(CONFIG['REQUEST_HEADERS'])}")
        log.debug(f"       data: {safe_json_for_log(data)}")

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
            files = {"file": (Path(file.filepath).name, fh)}
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
                emit_event(
                    UiEventName.transfer, op="upload", file=file.filepath, size=file.size or 0
                )
                _record_synced_base(file)
            elif response.status_code == 401:
                log.error(f'Authentication failed uploading "{file.filepath}"')
                emit_event(
                    UiEventName.auth_error,
                    message=f'Authentication failed uploading "{file.filepath}"',
                )
            elif response.status_code == 403:
                log.error(f'Access denied uploading "{file.filepath}"')
            elif response.status_code == 409:
                log.debug(f'"{file.filepath}" (already present)')
                _record_synced_base(file)
            elif response.status_code >= 500:
                # Transient server-side failure: raise so the wrapper backs off.
                raise ServerError(
                    f'Server error uploading "{file.filepath}": {response.status_code}'
                )
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
                filename=Path(file.filepath).name,
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
                emit_event(
                    UiEventName.transfer, op="upload", file=file.filepath, size=file.size or 0
                )
                _record_synced_base(file)
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
                emit_event(
                    UiEventName.auth_error,
                    message=f'Authentication failed uploading "{file.filepath}"',
                )
            elif response.status_code == 403:
                log.error(f'Access denied uploading "{file.filepath}"')
            elif response.status_code == 409:
                log.debug(f'"{file.filepath}" (already present)')
                _record_synced_base(file)
            elif response.status_code >= 500:
                # Transient server-side failure: raise so the wrapper backs off.
                raise ServerError(
                    f'Server error uploading "{file.filepath}": {response.status_code}'
                )
            else:
                response.raise_for_status()
    except FileNotFoundError:
        log.error(f'File not found: "{file.filepath}"')
    except PermissionError:
        log.error(f'Permission denied reading: "{file.filepath}"')


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
