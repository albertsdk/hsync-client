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

import hmac
import threading
from collections.abc import Iterator
from os import fsync, remove, replace, utime
from pathlib import Path
from shutil import disk_usage
from time import monotonic
from time import sleep as time_sleep
from typing import cast

import zstandard as zstd
from hsync_common.clock import clamp_incoming_corrected_mtime
from hsync_common.constants import CONTENT_TYPE_OCTET_STREAM
from hsync_common.format import format_bytes, format_duration
from hsync_common.hashing import adaptive_chunk_size, compute_sha256, is_sha256_hex
from hsync_common.models.file import File
from hsync_common.models.symlink import Symlink
from hsync_common.path_validation import is_safe_symlink_target, resolve_within
from hsync_common.text import escape_loguru_markup, safe_json_for_log
from loguru import logger as log
from requests import RequestException

from hsync_client.compression import (
    CONTENT_ENCODING_ZSTD,
    compress_data,
    decompress_stream,
    should_compress,
)
from hsync_client.config import CONFIG
from hsync_client.constants import (
    FILE_STABILITY_INTERVAL_DEFAULT,
    HTTP_TIMEOUT_DEFAULT,
    MAX_WAIT_TIMEOUT_DEFAULT,
    PARTIAL_DOWNLOAD_SUFFIX,
    STABILITY_DELAY_ON_CLOSED,
    TRANSFER_RETRY_ATTEMPTS_DEFAULT,
    TRANSFER_RETRY_MAX_WAIT_DEFAULT,
    TRANSFER_RETRY_MIN_WAIT_DEFAULT,
    UPLOAD_MAX_RESCHEDULES_DEFAULT,
    UPLOAD_RESCHEDULE_DELAY_DEFAULT,
    ActionLabel,
)
from hsync_client.delta_transfer import (
    DeltaTransferError,
    download_file_delta,
    prepare_delta_download,
    prepare_delta_upload,
    upload_file_delta,
)
from hsync_client.events import UiEventName, emit_event
from hsync_client.exceptions import (
    PermanentDownloadError,
    PermanentUploadError,
    ServerError,
)
from hsync_client.retry import retry_on_transient_transfer_error
from hsync_client.utils import (
    get_ssl_verify,
    get_throttle_delay,
    inbound_size_cap,
    path_lock,
    set_permissions,
)
from hsync_client.utils import (
    http_get as get,
)
from hsync_client.utils import (
    http_patch as patch,
)
from hsync_client.utils import (
    http_post as post,
)
from hsync_client.utils import (
    open_partial_no_follow as _open_partial_no_follow,
)


def _transfer_retry_params() -> tuple[int, float, float]:
    """Return ``(max_attempts, min_wait, max_wait)`` for per-file retries."""
    return (
        cast(int, CONFIG.get("TRANSFER_RETRY_ATTEMPTS", TRANSFER_RETRY_ATTEMPTS_DEFAULT)),
        cast(float, CONFIG.get("TRANSFER_RETRY_MIN_WAIT", TRANSFER_RETRY_MIN_WAIT_DEFAULT)),
        cast(float, CONFIG.get("TRANSFER_RETRY_MAX_WAIT", TRANSFER_RETRY_MAX_WAIT_DEFAULT)),
    )


def _emit_transfer_failed(op: str, filepath: str, message: str) -> None:
    """Surface a permanent/exhausted transfer failure as a structured event."""
    emit_event(UiEventName.transfer_failed, op=op, file=filepath, message=message)


def _record_synced_base(
    file: File,
    vclock: dict[str, int] | None = None,
    version_id: str | None = None,
    corrected_mtime: int = 0,
) -> None:
    """Record the just-synced content hash as the new common-ancestor base.

    After a successful transfer (or a no-op because the server already held the
    file), local and remote agree on ``file.sha256sum``; persisting it lets the
    next reconciliation use a clock-independent three-way merge.
    Bookkeeping failures are swallowed so they never abort a successful transfer.

    ``file.filepath`` may be bucket-relative (files parsed from a remote listing)
    or absolute (real-time events). ``record_base`` keys by absolute path, so a
    relative path is resolved against ``LOCAL_BUCKET`` first — a raw relative
    path would silently produce a no-op and the merge base would never exist,
    degrading every later reconciliation to the legacy mtime heuristic.

    ``vclock`` is the merged clock the receiver should record on accept
    (convergence invariant D11). On download it is the remote-advertised clock;
    on upload it is the local effective clock the client just pushed. When
    absent, ``merge_and_record`` falls back to merging the stored clock.

    Persistence: during a full reconcile cycle the record is held in memory and
    flushed once by the single ``sync_state.flush()`` at the end of the cycle
    (``sync.py``), avoiding redundant per-file rewrites of the entire version
    map. For **real-time** transfers (watcher uploads / inbound event downloads,
    detected via ``is_sync_cycle_active`` being False) the merge base is
    persisted immediately instead: a process kill between a watcher upload and
    the next cycle flush would otherwise lose the in-memory base, and a file
    then deleted while the process was dead would be resurrected on restart
    (the offline-deletion minting in ``sync()`` has no base to mint from). The
    tombstone store already persists every marker immediately (same pattern).
    """
    try:
        from hsync_client.sync_state import flush, merge_and_record, record_base

        if file.filepath and file.sha256sum:
            if Path(file.filepath).is_absolute():
                abs_path = file.filepath
            else:
                abs_path = _local_dest_path(file) or str(
                    Path(CONFIG["LOCAL_BUCKET"]) / file.filepath
                )
            if vclock is not None:
                merge_and_record(
                    abs_path, file.sha256sum, vclock, remote_corrected_mtime=corrected_mtime
                )
            else:
                record_base(
                    abs_path,
                    file.sha256sum,
                    version_id=version_id,
                    corrected_mtime=corrected_mtime,
                )
            # Real-time transfers (not inside a full-sync cycle) persist the
            # merge base durably so a crash cannot lose it. Full-sync transfers
            # defer to the single end-of-cycle flush (batch).
            from hsync_client.sync import is_sync_cycle_active

            if not is_sync_cycle_active():
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


class _SizedStreamingBody:
    """Streaming request body that advertises its exact length.

    ``requests`` frames a bare generator body with ``Transfer-Encoding:
    chunked`` because it cannot measure it; paired with a manual
    ``Content-Length`` that produces a request carrying both framing headers,
    which strict HTTP/1.1 parsers (h11/uvicorn) reject as malformed ("Invalid
    HTTP request received"). Exposing ``__len__`` lets ``requests`` frame the
    request with a single ``Content-Length`` while still streaming each chunk
    from disk.
    """

    def __init__(self, chunks: Iterator[bytes], length: int) -> None:
        self._chunks = chunks
        self._length = length

    def __len__(self) -> int:
        return self._length

    def __iter__(self) -> Iterator[bytes]:
        return iter(self._chunks)


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

    Returns ``(body, content_type)`` where ``body`` is a
    :class:`_SizedStreamingBody` streaming the multipart payload from disk
    (never loaded into memory) and advertising its exact length via ``__len__``
    so ``requests`` frames the request with ``Content-Length`` instead of
    chunked transfer-encoding.
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

    # Sanitize the filename for use in the multipart header: a double quote
    # would close the quoted-string early and CRLF sequences would inject
    # arbitrary headers into the body. Replace them with underscores so the
    # header stays well-formed even with a hostile local filename.
    safe_filename = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
    file_header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{file_field_name}"; '
        f'filename="{safe_filename}"\r\n'
        f"Content-Type: {CONTENT_TYPE_OCTET_STREAM}\r\n\r\n"
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

    return _SizedStreamingBody(_generator(), content_length), content_type


def _upload_timeout() -> tuple[float, float | None]:
    """Return (connect_timeout, read_timeout) for upload requests.

    Read timeout is disabled because large file uploads can legitimately
    keep the socket busy for many minutes; the connect timeout still
    catches unreachable servers quickly.
    """
    connect = float(CONFIG.get("HTTP_TIMEOUT", HTTP_TIMEOUT_DEFAULT))
    return connect, None


# =============================================================================
# Download Operations
# =============================================================================


def _local_dest_path(file: File) -> str | None:
    """Return the absolute local destination path for a downloaded *file*.

    The remote ``file.filepath`` is relative to the bucket; the local copy lives
    under ``LOCAL_BUCKET``. Shared by the downloader and the per-path lock key so
    a download serializes against any other operation on the same local path.

    The remote path is untrusted: it is validated (no traversal, absolute
    paths, backslashes or internal engine artifacts) and confined to
    ``LOCAL_BUCKET`` even against symlink escapes. Returns ``None`` when the
    remote path is unsafe and must be rejected.
    """
    return resolve_within(CONFIG["LOCAL_BUCKET"], file.filepath)


def download_file(file: File) -> None:
    """
    Download a file from the remote server with per-file retry/backoff.

    Wraps :func:`_download_file_attempt` in an exponential-backoff retry policy
    so a single transient network blip (timeout, connection reset, 5xx) does not
    leave the file unsynced until the next full sync. Permanent errors (4xx) are
    never retried. When every attempt is exhausted the failure is surfaced as a
    ``transfer_failed`` event for the UI.

    The whole download holds the per-path lock so a concurrent upload or
    inbound delete of the same local path cannot interleave and lose the update.

    Args:
        file: File object with remote metadata (filepath, sha256sum, etc.).
    """
    # Validate required file attributes
    if not file.filepath or not file.name:
        log.error("Invalid file metadata: missing filepath or name")
        return

    # The remote path is untrusted: refuse anything that would escape the
    # local bucket (traversal, absolute paths, symlink hops, engine files).
    dest = _local_dest_path(file)
    if dest is None:
        log.warning(f'{ActionLabel.SECURITY} Rejected unsafe download path "{file.filepath}"')
        emit_event(
            UiEventName.security_rejected,
            op="download",
            path=file.filepath,
            message="unsafe remote path rejected",
        )
        return

    max_attempts, min_wait, max_wait = _transfer_retry_params()
    attempt = retry_on_transient_transfer_error(max_attempts, min_wait, max_wait)(
        _download_file_attempt
    )
    try:
        with path_lock(dest):
            attempt(file)
    except (RequestException, ServerError) as exc:
        log.error(f'Download failed after retries for "{file.filepath}": {exc}')
        _emit_transfer_failed("download", file.filepath, str(exc))


def _download_file_attempt(file: File) -> None:
    """
    Perform a single download attempt with resume support.

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
    t0 = monotonic()

    # Prepare destination directory and partial file path
    full_save_path = _local_dest_path(file)
    if full_save_path is None:
        # Already gated in download_file; stay defensive for direct callers.
        log.error(f'Refusing unsafe download path "{file.filepath}"')
        return
    Path(full_save_path).parent.mkdir(parents=True, exist_ok=True)
    partial_file_path = f"{full_save_path}{PARTIAL_DOWNLOAD_SUFFIX}"

    # Check if local file is already up-to-date. The mtime shortcut alone is
    # not enough: an equal (or newer) local mtime with *different* content is
    # exactly the conflict case — the caller has already preserved the local
    # copy, and skipping here would prevent the master's version from ever
    # arriving. Skip only when the content is genuinely identical, or when no
    # well-formed hash is available to disprove the mtime shortcut (legacy
    # behaviour preserved for metadata without a valid expected digest).
    if Path(full_save_path).exists():
        local_mtime = Path(full_save_path).stat().st_mtime
        if file.mtime is not None and local_mtime >= file.mtime:
            expected = file.sha256sum or ""
            if (
                not expected
                or not is_sha256_hex(expected)
                or hmac.compare_digest(compute_sha256(full_save_path), expected)
            ):
                nothing_to_do(file)
                return

    # Delta branch: when the local file IS the recorded base, only the changed
    # blocks need to cross the wire. Any recoverable failure falls through to
    # the full-download body below (same attempt, retry policy unchanged).
    if delta_ctx := prepare_delta_download(file, full_save_path):
        try:
            download_file_delta(file, delta_ctx)
            log.info(
                f'{ActionLabel.DOWNLOAD} "{full_save_path}" {format_bytes(file.size)} '
                f"{format_duration(monotonic() - t0, subsecond=True)}"
            )
            emit_event(
                UiEventName.transfer, op="download", file=full_save_path, size=file.size or 0
            )
            if file.mtime is not None:
                utime(full_save_path, (file.mtime, file.mtime))
            if file.permissions:
                set_permissions(full_save_path, file.permissions)
            _record_synced_base(
                file,
                vclock=_remote_vclock(file),
                corrected_mtime=_remote_corrected_mtime(file),
            )
            return
        except (DeltaTransferError, OSError) as exc:
            log.warning(f'Delta download for "{file.filepath}" fell back to full transfer ({exc})')

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
                f'{ActionLabel.RESUME} "{file.filepath}" from '
                f"{format_bytes(resume_offset)}/{format_bytes(file.size)}"
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

    # Include our CID so the server can attribute the download to us in its
    # logs. The CID is not a secret (it is already in the WS URL and upload
    # form data), so sending it as a query param is acceptable.
    params: dict[str, str] = {"filename": file.filepath}
    own_cid = CONFIG.get("CID")
    if own_cid:
        params["cid"] = own_cid

    # A connection-level error here propagates to the retry wrapper. In LAN
    # mode the session pins the master cert fingerprint on this very connection.
    response = get(
        uri,
        headers=headers,
        params=params,
        stream=True,
        verify=get_ssl_verify(),
        timeout=CONFIG.get("HTTP_TIMEOUT", HTTP_TIMEOUT_DEFAULT),
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
        # download_file) so we stay inside the per-path lock already held
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
        chunk_size = adaptive_chunk_size(file.size)

        if is_compressed:
            # A compressed response cannot be resumed with a Range
            # request — the zstd frame would be appended raw to the partial,
            # corrupting it. If we have a partial from a previous attempt,
            # discard it and restart the decompression from byte 0.
            if resume_offset > 0:
                log.debug("Compressed download with resume offset — restarting from scratch")
                resume_offset = 0
                if Path(partial_file_path).exists():
                    remove(partial_file_path)
            # Compressed response — stream-decompress straight to disk with a
            # hard output cap (anti decompression-bomb): the payload comes
            # from a remote peer, so neither the compressed body nor the
            # expanded content is ever fully buffered in memory. The cap is
            # min(declared size, MAX_INBOUND_SIZE): the real content must
            # match the declared size exactly (the SHA-256 check below
            # verifies integrity against the listing), and the absolute cap
            # keeps a hostile peer from expanding a bomb up to an arbitrarily
            # large declaration.
            response.raw.decode_content = False
            with _open_partial_no_follow(partial_file_path, "wb") as f:
                write_cap = inbound_size_cap(file.size, CONFIG.get("MAX_INBOUND_SIZE", 0))
                for chunk in decompress_stream(
                    response.raw, max_output_size=write_cap if write_cap is not None else 0
                ):
                    f.write(chunk)
                # Flush OS buffers to disk before the file is considered written
                f.flush()
                fsync(f.fileno())
        else:
            # Stream file content to partial file (append if resuming)
            write_mode = "ab" if resume_offset > 0 and response.status_code == 206 else "wb"
            dl_bw_limit = CONFIG.get("MAX_BANDWIDTH_DOWNLOAD")
            write_cap = inbound_size_cap(file.size, CONFIG.get("MAX_INBOUND_SIZE", 0))
            written = resume_offset
            with _open_partial_no_follow(partial_file_path, write_mode) as f:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if not chunk:
                        break
                    written += len(chunk)
                    if write_cap is not None and written > write_cap:
                        # Over-cap body (hostile or simply larger than the
                        # configured maximum): permanent failure — the
                        # partial file is dropped in ``finally``.
                        # Raise a PermanentDownloadError so the caller
                        # can add the filepath to a skip-set and stop
                        # retrying this file on every resync cycle.
                        log.error(
                            f'"{file.filepath}" exceeds the maximum download size '
                            f"({write_cap} bytes) — download aborted"
                        )
                        raise PermanentDownloadError(
                            f'"{file.filepath}" exceeds the maximum download size '
                            f"({write_cap} bytes)"
                        )
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

        # Check 1: Client-computed hash vs. server-asserted current hash.
        # The header is untrusted: only digests with a valid shape are
        # compared (hmac.compare_digest raises TypeError on non-ASCII).
        if server_sha256 and is_sha256_hex(server_sha256):
            if not hmac.compare_digest(sha256sum, server_sha256):
                log.error(
                    f'"{file.filepath}" '
                    f"(Server checksum mismatch: expected {server_sha256}, computed {sha256sum})"
                )
                cleanup_partial = False
                return

        # Check 2: Client-computed hash vs. listing metadata hash (stale-safe fallback)
        expected_sha = file.sha256sum or ""
        if not is_sha256_hex(expected_sha) or not hmac.compare_digest(sha256sum, expected_sha):
            log.error(
                f'"{file.filepath}" (Invalid checksum: expected {expected_sha}, {sha256sum} given)'
            )
            cleanup_partial = False
            return

        # Atomically move partial file to final destination (same-filesystem
        # rename) so a partially written file is never visible at the target path
        replace(partial_file_path, full_save_path)
        cleanup_partial = False
        log.info(
            f'{ActionLabel.DOWNLOAD} "{full_save_path}" {format_bytes(file.size)} '
            f"{format_duration(monotonic() - t0, subsecond=True)}"
        )
        emit_event(UiEventName.transfer, op="download", file=full_save_path, size=file.size or 0)

        # Restore the modification time from the remote file. POSIX ctime is
        # not settable, so it is intentionally not restored; atime mirrors mtime.
        if file.mtime is not None:
            utime(full_save_path, (file.mtime, file.mtime))

        # Apply permissions
        if file.permissions:
            set_permissions(full_save_path, file.permissions)

        # Local now matches the remote content: remember it as the merge base.
        _record_synced_base(
            file,
            vclock=_remote_vclock(file),
            corrected_mtime=_remote_corrected_mtime(file),
        )

    except PermissionError as e:
        log.error(f'Permission denied saving "{file.filepath}": {e}')
    except zstd.ZstdError as e:
        # Hostile/corrupt compressed payload (e.g. decompression bomb):
        # permanent failure — the partial file is dropped in ``finally``.
        log.error(f'Decompression failed for "{file.filepath}": {e}')
    except OSError as e:
        log.error(f'Error saving file "{file.filepath}": {e}')
    finally:
        if cleanup_partial and partial_file_path and Path(partial_file_path).exists():
            remove(partial_file_path)
            log.debug(f"Cleaned up partial file: {partial_file_path}")


# =============================================================================
# File Stability Check
# =============================================================================


def is_file_stable(file: File, interval: float | None = None) -> bool:
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
        f"(size: {format_bytes(last_size)} -> "
        f"{format_bytes(current_size)}, "
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
            timeout=CONFIG.get("HTTP_TIMEOUT", HTTP_TIMEOUT_DEFAULT),
        )
        if response.status_code == 200:
            data = response.json()
            status, offset = data.get("status", "new"), data.get("offset", 0)
            # The server is untrusted: a negative, non-integer or oversized
            # offset would corrupt the resumed stream (mismatched
            # Content-Length) — clamp anything suspicious back to a fresh
            # upload. (bool is an int subclass: exclude it explicitly.)
            offset_is_int = isinstance(offset, int) and not isinstance(offset, bool)
            if status == "resumable" and (
                not offset_is_int or offset < 0 or (size > 0 and offset > size)
            ):
                log.warning(f"Ignoring invalid resumable offset from server: {offset!r}")
                return "new", 0
            return status, offset if offset_is_int else 0
    except RequestException:
        pass
    return "new", 0


def _wait_for_stable_file(file: File, from_closed: bool = False) -> bool:
    """
    Block until *file* has stopped being written to, or give up.

    On Linux an ``on_closed`` (``IN_CLOSE_WRITE``) event already signals that
    the writer closed the descriptor, so a single short re-check
    (``STABILITY_DELAY_ON_CLOSED``) is enough to absorb edge cases (NFS chunked
    writes, rapid multi-close, non-atomic saves) before trusting the close and
    uploading immediately. When the trigger is not a close event, or the short
    re-check still sees the file changing, fall back to the conservative poll
    bounded by ``MAX_WAIT_TIMEOUT``.

    Args:
        file: File object to check.
        from_closed: True when the upload was triggered by an ``on_closed``
                     (``IN_CLOSE_WRITE``) event, enabling the fast re-check.

    Returns:
        True if the file stabilized, False if it never settled within the wait
        window (the caller should defer the upload).
    """
    # Fast path: trust the close after a single short re-check.
    if from_closed and is_file_stable(file, STABILITY_DELAY_ON_CLOSED):
        return True

    # Conservative path: poll until size/mtime settle, bounded by the timeout.
    max_wait = CONFIG.get("MAX_WAIT_TIMEOUT", MAX_WAIT_TIMEOUT_DEFAULT)
    elapsed = 0
    while not is_file_stable(file):
        if elapsed >= max_wait:
            return False
        time_sleep(1)
        elapsed += 1
    return True


def _reschedule_upload(
    file: File, reschedules_left: int | None = None, permissions_only: bool = False
) -> None:
    """
    Defer an upload whose file never stabilized within the configured
    "MAX_WAIT_TIMEOUT".

    A file still being written is not uploaded ("torn" upload); instead a new
    attempt is scheduled after the configured "UPLOAD_RESCHEDULE_DELAY" seconds
    via a daemon ``threading.Timer``, which keeps the worker/observer thread
    free. The number of deferrals is bounded by the configured
    "UPLOAD_MAX_RESCHEDULES": once exhausted the
    file is skipped and left to the next file-system event (``on_closed``) or the
    periodic resync.

    Args:
        file: File object whose upload is being deferred.
        reschedules_left: Remaining deferrals; resolved from CONFIG on the first
                          call (None) and decremented on each subsequent one.
    """
    if reschedules_left is None:
        reschedules_left = cast(
            int, CONFIG.get("UPLOAD_MAX_RESCHEDULES", UPLOAD_MAX_RESCHEDULES_DEFAULT)
        )

    if reschedules_left <= 0:
        log.warning(
            f'File "{file.filepath}" did not stabilize after max reschedules; '
            "skipping (will retry on next file-system event or periodic resync)"
        )
        return

    delay = cast(float, CONFIG.get("UPLOAD_RESCHEDULE_DELAY", UPLOAD_RESCHEDULE_DELAY_DEFAULT))
    log.info(
        f'File "{file.filepath}" still being written; rescheduling upload in '
        f"{delay}s ({reschedules_left} attempt(s) left)"
    )
    timer = threading.Timer(
        delay,
        upload_file,
        args=(file,),
        kwargs={
            "_reschedules_left": reschedules_left - 1,
            "permissions_only": permissions_only,
        },
    )
    timer.daemon = True
    timer.start()


def delete_file_remote(file: File) -> None:
    """Delete *file* on the server via the REST ``DELETE …/files`` endpoint.

    Invoked for ``SyncActionName.delete_remote`` actions: the file was deleted
    locally while this peer was offline and the (tombstone-verified) copy left
    on the server is stale, so it must be removed instead of being downloaded
    back. ``file.filepath`` is the bucket-relative path from the remote listing
    (it must not resolve to a local file).

    The two transports speak different contracts for this endpoint: the
    central server expects form fields (``filename`` + ``cid``) while the LAN
    master reads query parameters (``path`` + ``is_dir`` + ``cid``, see
    ``lan_server._handle_delete``). The branch below picks the wire shape
    from ``CONFIG["LAN_MODE"]`` so the same action works in both modes.

    Raises:
        NetworkError: On a connection-level error.
        ServerError: On a server-side error response (except 404: the file is
            already gone, which is the desired end state).
    """
    from hsync_client.utils import get_ssl_verify, http_delete

    uri = f"{CONFIG['API']['buckets']}/{CONFIG['REMOTE_BUCKET']}/files"
    # file.filepath here is the remote bucket-relative path (the File was built
    # from the remote listing against LOCAL_BUCKET, so strip the local prefix).
    prefix = f"{CONFIG['LOCAL_BUCKET']}".rstrip("/")
    filename = str(file.filepath).removeprefix(prefix).lstrip("/")

    lan_mode = bool(CONFIG.get("LAN_MODE", False))
    response = http_delete(
        uri,
        params=({"path": filename, "is_dir": "false", "cid": CONFIG["CID"]} if lan_mode else None),
        data=None if lan_mode else {"cid": CONFIG["CID"], "filename": filename},
        headers=dict(CONFIG["REQUEST_HEADERS"]),
        verify=get_ssl_verify(),
        timeout=CONFIG.get("HTTP_TIMEOUT", HTTP_TIMEOUT_DEFAULT),
    )
    if response.status_code == 404:
        log.debug(f'"{filename}" already absent on server')
        return
    if response.status_code not in (200, 204):
        from hsync_client.exceptions import ServerError

        raise ServerError(
            message=f"Remote delete failed for {filename}: status {response.status_code}",
            details={"status_code": response.status_code, "response": response.text},
        )
    log.info(f'{ActionLabel.DELETE} "{filename}" (on server)')
    emit_event(UiEventName.transfer, op="delete_remote", file=filename)


def upload_file(
    file: File,
    check_size: bool = True,
    from_closed: bool = False,
    permissions_only: bool = False,
    _reschedules_left: int | None = None,
) -> None:
    """
    Upload a file to the remote server with per-file retry/backoff.

    If check_size is True, waits for the file to stop being written to before
    uploading. When ``from_closed`` is True the trigger was an ``on_closed``
    (``IN_CLOSE_WRITE``) event, so a single short re-check
    (``STABILITY_DELAY_ON_CLOSED``) replaces the conservative poll, cutting the
    Slave -> Master latency to sub-second; if that re-check still sees the file
    changing it degrades to the full guard. The actual transfer is wrapped in an
    exponential-backoff retry policy so a single transient blip (timeout,
    connection reset, 5xx) does not leave the file unsynced; permanent errors
    (4xx) are never retried. When every attempt is exhausted the failure is
    surfaced as a ``transfer_failed`` event.

    A file that does not stabilize within the configured "MAX_WAIT_TIMEOUT" is
    not uploaded in a half-written ("torn") state: the upload is rescheduled for
    a later attempt via :func:`_reschedule_upload`, bounded by the configured
    "UPLOAD_MAX_RESCHEDULES".

    If a previous upload was interrupted, resumes from where it left off.

    A symlink *inside* the bucket whose target escapes ``LOCAL_BUCKET`` must
    never be dereferenced: the guard below refuses the upload entirely, so an
    out-of-bucket target such as ``secret -> /etc/passwd`` is never hashed,
    read or exfiltrated to the peer (matches the ``get_files_local`` scan
    guard in ``sync.py``). Symlinks whose target stays inside the bucket keep
    their current semantic (treated as a regular file, i.e. the link itself,
    not the target), consistent with the periodic reconcile.

    Args:
        file: File object to upload.
        check_size: If True, wait for file writes to complete first.
        from_closed: If True, the upload was triggered by an ``on_closed``
                     (``IN_CLOSE_WRITE``) event; enables the fast re-check.
        _reschedules_left: Internal; remaining deferred attempts when this call
                           originates from a rescheduled Timer.
    """
    # Validate required file attributes
    if not file.filepath:
        log.error("Cannot upload file: missing filepath")
        return

    # Security: never dereference a symlink whose target escapes the bucket.
    # This covers the realtime (watch/LAN slave) upload path, which builds the
    # ``File`` directly from the watchdog event without the ``get_files_local``
    # scan guard. A symlink that stays inside the bucket is uploaded as the
    # link entry itself (its dereferenced metadata), not as a symlink object.
    if Path(file.filepath).is_symlink():
        try:
            link = Symlink.get(file.filepath)
            if link is not None and not is_safe_symlink_target(
                file.filepath, link.target, CONFIG["LOCAL_BUCKET"]
            ):
                log.warning(
                    f"{ActionLabel.SECURITY} Symlink target escapes bucket, "
                    f'not uploading: "{file.filepath}" -> "{link.target}"'
                )
                emit_event(
                    UiEventName.security_rejected,
                    op="upload",
                    path=file.filepath,
                    message="symlink target escapes bucket",
                )
                return
        except OSError:
            log.warning(
                f'{ActionLabel.SECURITY} Cannot inspect symlink, not uploading: "{file.filepath}"'
            )
            return

    if check_size:
        if not _wait_for_stable_file(file, from_closed):
            # File never stopped being written within the wait window: defer the
            # upload instead of transferring a half-written file.
            _reschedule_upload(file, _reschedules_left, permissions_only)
            return

        # Refresh file metadata after write is complete
        file.size = Path(file.filepath).stat().st_size
        file.sha256sum = compute_sha256(file.filepath)

        log.debug(f'Written file: "{file.filepath}" ({format_bytes(file.size)})')

        # Recursive call with check_size=False to do actual upload
        upload_file(file, False, permissions_only=permissions_only)
        return

    max_attempts, min_wait, max_wait = _transfer_retry_params()
    attempt = retry_on_transient_transfer_error(max_attempts, min_wait, max_wait)(
        _upload_file_attempt
    )
    try:
        # Serialize on the local path so the read does not race a
        # concurrent download/delete of the same file.
        with path_lock(file.filepath):
            attempt(file, permissions_only=permissions_only)
    except (RequestException, ServerError, PermanentUploadError) as exc:
        log.error(
            f'Upload failed after retries for "{file.filepath}": {escape_loguru_markup(str(exc))}'
        )
        _emit_transfer_failed("upload", file.filepath, str(exc))


def _resolve_effective_version(file: File) -> dict | None:
    """Return the effective local version dict for *file* (pure, no mutation).

    Used to record the merge base after a successful transfer. Mirrors
    ``sync_state.local_version_for_compare`` but returns ``None`` (rather than
    a fresh tick) when no version is recorded, so callers can fall back to the
    stored clock.
    """
    try:
        from hsync_client.sync_state import get_sync_state_store, local_version_for_compare

        if not file.filepath:
            return None
        abs_path = (
            file.filepath
            if Path(file.filepath).is_absolute()
            else str(Path(CONFIG["LOCAL_BUCKET"]) / file.filepath)
        )
        eff = local_version_for_compare(abs_path, file.sha256sum)
        if eff is not None:
            return eff
        from hsync_common.models.tombstone import relpath_in_bucket

        relpath = relpath_in_bucket(abs_path, str(CONFIG["LOCAL_BUCKET"]))
        if relpath:
            return get_sync_state_store().get(relpath)
    except Exception as e:  # pragma: no cover - bookkeeping must never break upload
        log.debug(f'Could not resolve upload version for "{file.filepath}": {e}')
    return None


def _resolve_local_vclock(file: File) -> dict[str, int] | None:
    """Return the effective local vclock for *file*, or ``None``."""
    eff = _resolve_effective_version(file)
    return eff["vclock"] if eff else None


def _resolve_local_corrected_mtime(file: File) -> int:
    """Return the effective local corrected mtime for *file*, or 0."""
    eff = _resolve_effective_version(file)
    return int(eff.get("corrected_mtime", 0) or 0) if eff else 0


def _remote_vclock(file: File) -> dict[str, int] | None:
    """Return the remote-advertised vclock attached to *file*, if any.

    A downloaded ``File`` carries the version metadata from the remote listing
    or event; recording it as the merge base keeps the clock honest on the
    receiving side (convergence invariant D11).
    """
    version = getattr(file, "version", None)
    if isinstance(version, dict):
        vclock = version.get("vclock")
        if isinstance(vclock, dict):
            return vclock
    return None


def patch_permissions_remote(filename: str, permissions: str) -> bool:
    """Apply a permission-only change on the remote WITHOUT sending file bytes.

    Hits the server's metadata-only ``PATCH …/files/permissions`` endpoint
    (the same route the LAN master exposes), so a chmod on a large file does
    not re-upload its content. Best-effort by construction: any transport
    error or non-200 status degrades to ``False`` and the caller falls back
    to the regular upload path (whose same-SHA branch applies the mode).

    Args:
        filename: Bucket-relative remote path of the file.
        permissions: Octal permission string to apply (remote-sanitized).

    Returns:
        True when the remote applied the change, False otherwise.
    """
    uri = f"{CONFIG['API']['buckets']}/{CONFIG['REMOTE_BUCKET']}/files/permissions"
    params: dict[str, str] = {"filename": filename, "permissions": permissions}
    own_cid = CONFIG.get("CID")
    if own_cid:
        params["cid"] = own_cid
    try:
        response = patch(
            uri,
            params=params,
            headers=cast(dict[str, str], CONFIG["REQUEST_HEADERS"]),
            verify=get_ssl_verify(),
            timeout=CONFIG.get("HTTP_TIMEOUT", HTTP_TIMEOUT_DEFAULT),
        )
    except RequestException as e:
        log.debug(f'Permission PATCH failed for "{filename}": {e}')
        return False
    if response.status_code == 200:
        return True
    log.debug(f'Permission PATCH for "{filename}": status {response.status_code}')
    return False


def _patch_permissions_only(file: File, filename: str) -> bool:
    """Resolve a ``permissions_only`` upload through the metadata-only endpoint.

    Returns ``True`` when the upload is fully resolved (mode applied remotely,
    merge base recorded, UI event emitted). Returns ``False`` when the endpoint
    is unavailable (older server) and the caller must fall back to the legacy
    full-upload same-SHA branch.
    """
    if not patch_permissions_remote(filename, file.permissions or ""):
        return False
    log.info(f'{ActionLabel.PERMISSION} "{file.filepath}" (permissions updated)')
    _record_synced_base(file, vclock=_resolve_local_vclock(file))
    emit_event(UiEventName.transfer, op="upload", file=file.filepath, size=file.size or 0)
    return True


def _remote_corrected_mtime(file: File) -> int:
    """Return the remote-advertised corrected mtime for *file*, or 0.

    The receiver-side guard of ADR-0023 applies: a far-future peer value is
    clamped against this process's corrected clock, so a malicious peer cannot
    win every tie-break (conflict engineering).
    """
    version = getattr(file, "version", None)
    if isinstance(version, dict):
        raw = version.get("corrected_mtime")
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
            return int(clamp_incoming_corrected_mtime(float(raw)))
    return 0


def _upload_version_fields(file: File) -> tuple[str, str, int]:
    """Return ``(vclock_json, version_id, corrected_mtime)`` upload form values.

    The uploaded version is the *effective* local version: the stored clock
    ticked by this node when the content differs from the recorded base (the
    same honest value ``sync_state.local_version_for_compare`` produces), plus
    the ADR-0023 corrected mtime. The server arbitrates this against its stored
    version (ADR-0022).
    """
    try:
        from hsync_client.sync_state import upload_version_fields

        if file.filepath:
            abs_path = (
                file.filepath
                if Path(file.filepath).is_absolute()
                else str(Path(CONFIG["LOCAL_BUCKET"]) / file.filepath)
            )
            return upload_version_fields(abs_path, file.sha256sum)
    except Exception as e:  # pragma: no cover - bookkeeping must never break upload
        log.debug(f'Could not resolve upload version for "{file.filepath}": {e}')
    return "{}", "", 0


def _record_201_upload(file: File, response) -> None:
    """Record the merge base after a 201 upload success.

    The server's 201 may carry a ``version`` field with the recorded causal
    version (merged vclock + ADR-0023 corrected mtime) to mirror locally
    (convergence invariant D11); when absent, the local effective clock is
    recorded via ``merge_and_record``.
    """
    record_vclock: dict[str, int] | None = None
    record_corrected: int = 0
    try:
        payload = response.json() if response.text else None
        if isinstance(payload, dict):
            # Both shapes are accepted: the canonical "version" object, and the
            # legacy "resolution" dict some hubs used to return.
            version = payload.get("version")
            resolution = payload.get("resolution")
            for container in (version, resolution):
                if not isinstance(container, dict):
                    continue
                vclock_raw = container.get("vclock")
                if isinstance(vclock_raw, dict) and record_vclock is None:
                    record_vclock = {
                        str(k): int(v)
                        for k, v in vclock_raw.items()
                        if isinstance(v, int) and not isinstance(v, bool)
                    }
                raw_corrected = container.get("corrected_mtime")
                if (
                    isinstance(raw_corrected, int)
                    and not isinstance(raw_corrected, bool)
                    and not record_corrected
                ):
                    record_corrected = raw_corrected
    except (ValueError, TypeError):
        pass
    if record_vclock is not None:
        _record_synced_base(file, vclock=record_vclock, corrected_mtime=record_corrected)
    else:
        # Fall back to the local effective clock (merge with stored).
        _record_synced_base(
            file,
            vclock=_resolve_local_vclock(file),
            corrected_mtime=_resolve_local_corrected_mtime(file),
        )


def _handle_upload_conflict_response(file: File, response) -> bool:
    """Handle a 409 upload response.

    Returns ``True`` when the 409 is a causal supersede/conflict-lost signal
    (the caller should trigger a download of the current remote version on the
    next cycle); ``False`` when the 409 is the legacy "already present" no-op
    (the base is recorded as in-sync).
    """
    body = (response.text or "")[:500]
    lower = body.lower()
    if "superseded" in lower or "conflict_lost" in lower:
        log.info(
            f'{ActionLabel.CONFLICT} Upload of "{file.filepath}" was '
            f"superseded by a newer remote version; download needed ({body})"
        )
        emit_event(
            UiEventName.transfer_failed,
            op="upload",
            file=file.filepath,
            message=f"superseded by remote version: {body}",
        )
        return True
    # Legacy / duplicate "already present": the server already holds this
    # exact content, so local and remote agree — record the base.
    log.debug(f'"{file.filepath}" (already present)')
    _record_synced_base(file)
    return False


def _upload_file_attempt(file: File, permissions_only: bool = False) -> None:
    """
    Perform a single upload attempt with resume support.

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
    t0 = monotonic()
    try:
        # Prepare upload request
        uri = f"{CONFIG['API']['buckets']}/{CONFIG['REMOTE_BUCKET']}/files"
        prefix = f"{CONFIG['LOCAL_BUCKET']}".rstrip("/")
        path = str(Path(file.filepath).parent).rstrip("/").removeprefix(prefix).lstrip("/")
        filename = f"{path}/{Path(file.filepath).name}" if path else Path(file.filepath).name

        # Delta branch: when the peer still holds the recorded base, only the
        # changed blocks need to cross the wire. Any recoverable failure falls
        # through to the full-upload body below (same attempt, retry unchanged).
        # A permission-only upload carries no content change, so it skips the
        # delta path entirely (the same-SHA branch applies the mode server-side).
        if not permissions_only and (delta_ctx := prepare_delta_upload(file, filename)):
            try:
                upload_file_delta(file, delta_ctx)
                log.info(
                    f'{ActionLabel.UPLOAD} "{file.filepath}" {format_bytes(file.size)} '
                    f"{format_duration(monotonic() - t0, subsecond=True)}"
                )
                emit_event(
                    UiEventName.transfer, op="upload", file=file.filepath, size=file.size or 0
                )
                _record_synced_base(
                    file,
                    vclock=_resolve_local_vclock(file),
                    corrected_mtime=_resolve_local_corrected_mtime(file),
                )
                return
            except (DeltaTransferError, OSError) as exc:
                log.warning(
                    f'Delta upload for "{file.filepath}" fell back to full transfer ({exc})'
                )

        # Check for resumable upload
        if file.sha256sum is None:
            status, offset = "new", 0
        else:
            status, offset = _get_upload_status(filename, file.sha256sum, file.size)

        if status == "complete":
            if not permissions_only:
                # Identical content is already on the peer and no permission
                # change was requested: it is a genuine no-op upload.
                log.debug(f'"{file.filepath}" (already present on server)')
                _record_synced_base(file, vclock=_resolve_local_vclock(file))
                return
            # A permission-only chmod: try the metadata-only endpoint first
            # (no bytes cross the wire). When the peer does not expose it,
            # fall through with a full upload so the same-SHA branch applies
            # the new mode (and re-broadcasts); the bytes are skipped there.
            if _patch_permissions_only(file, filename):
                return
            log.debug(f'"{file.filepath}" (already present — converging metadata)')
            offset = 0

        if status == "resumable" and offset > 0:
            log.info(
                f'{ActionLabel.RESUME} "{file.filepath}" from '
                f"{format_bytes(offset)}/{format_bytes(file.size)}"
            )

        # Build form data with file metadata
        vclock_json, version_id, corrected_mtime = _upload_version_fields(file)
        data = {
            "cid": CONFIG["CID"],
            "path": path,
            "ctime": file.ctime,
            "mtime": file.mtime,
            "size": file.size,
            "sha256sum": file.sha256sum,
            "permissions": file.permissions,
            "offset": offset,
            "vclock": vclock_json,
            "version_id": version_id,
            "corrected_mtime": corrected_mtime,
        }

        log.debug(f"Upload file: {file.filepath}")
        log.debug(f"        URI: {uri}")
        # safe_json_for_log escapes loguru markup but does NOT redact secrets:
        # the Authorization header (API key / HSYNC_LAN_KEY) must be masked.
        from hsync_client.utils import redact_headers

        log.debug(f"     Header: {safe_json_for_log(redact_headers(CONFIG['REQUEST_HEADERS']))}")
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
                f"Compressed {format_bytes(len(file_data))} -> "
                f"{format_bytes(len(compressed_data))} "
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
                log.info(
                    f'{ActionLabel.UPLOAD} "{file.filepath}" {format_bytes(file.size)} '
                    f"{format_duration(monotonic() - t0, subsecond=True)}"
                )
                emit_event(
                    UiEventName.transfer, op="upload", file=file.filepath, size=file.size or 0
                )
                _record_201_upload(file, response)
            elif response.status_code == 200:
                # Permissions updated on the same-SHA branch (permission-only
                # change): local and remote content agree; record the base.
                log.info(f'{ActionLabel.PERMISSION} "{file.filepath}" (permissions updated)')
                _record_synced_base(file, vclock=_resolve_local_vclock(file))
            elif response.status_code == 401:
                log.error(f'Authentication failed uploading "{file.filepath}"')
                emit_event(
                    UiEventName.auth_error,
                    message=f'Authentication failed uploading "{file.filepath}"',
                )
            elif response.status_code == 403:
                log.error(f'Access denied uploading "{file.filepath}"')
            elif response.status_code == 409:
                _handle_upload_conflict_response(file, response)
            elif response.status_code >= 500:
                # Transient server-side failure: raise so the wrapper backs off.
                raise ServerError(
                    f'Server error uploading "{file.filepath}": {response.status_code}'
                )
            else:
                # 4xx (not 401/403/409): permanent — log the server's
                # diagnostic body and raise a non-retryable error. Escape
                # braces so loguru does not interpret JSON bodies (e.g.
                # {"error": "..."}) as format placeholders.
                body = (response.text or "")[:500]
                log.error(
                    f"Upload rejected (HTTP {response.status_code}) for "
                    f'"{file.filepath}": {escape_loguru_markup(body)}'
                )
                raise PermanentUploadError(
                    f'Upload rejected (HTTP {response.status_code}) for "{file.filepath}": {body}'
                )
        else:
            # Standard upload: stream the multipart body directly from disk
            # so files of arbitrary size do not need to be loaded into RAM.
            ul_bw_limit = CONFIG.get("MAX_BANDWIDTH_UPLOAD")
            import uuid

            boundary = uuid.uuid4().hex
            body, content_type = _build_multipart_streaming(
                file.filepath,
                dict(data),
                boundary=boundary,
                filename=Path(file.filepath).name,
                offset=offset,
                bandwidth_limit=ul_bw_limit,
            )
            # Content-Length is derived by requests from body.__len__; setting it
            # here as well would coexist with the chunked framing requests adds
            # for generators, yielding a request h11/uvicorn rejects.
            headers["Content-Type"] = content_type
            response = post(
                uri,
                headers=headers,
                data=body,
                verify=get_ssl_verify(),
                timeout=_upload_timeout(),
            )
            if response.status_code == 201:
                log.info(
                    f'{ActionLabel.UPLOAD} "{file.filepath}" {format_bytes(file.size)} '
                    f"{format_duration(monotonic() - t0, subsecond=True)}"
                )
                emit_event(
                    UiEventName.transfer, op="upload", file=file.filepath, size=file.size or 0
                )
                _record_201_upload(file, response)
            elif response.status_code == 200:
                # Permissions updated on the same-SHA branch (permission-only
                # change): local and remote content agree; record the base.
                log.info(f'{ActionLabel.PERMISSION} "{file.filepath}" (permissions updated)')
                _record_synced_base(file, vclock=_resolve_local_vclock(file))
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
                _handle_upload_conflict_response(file, response)
            elif response.status_code >= 500:
                # Transient server-side failure: raise so the wrapper backs off.
                raise ServerError(
                    f'Server error uploading "{file.filepath}": {response.status_code}'
                )
            else:
                # 4xx (not 401/403/409): permanent — log the server's
                # diagnostic body and raise a non-retryable error. Escape
                # braces so loguru does not interpret JSON bodies (e.g.
                # {"error": "..."}) as format placeholders.
                body = (response.text or "")[:500]
                log.error(
                    f"Upload rejected (HTTP {response.status_code}) for "
                    f'"{file.filepath}": {escape_loguru_markup(body)}'
                )
                raise PermanentUploadError(
                    f'Upload rejected (HTTP {response.status_code}) for "{file.filepath}": {body}'
                )
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


# =============================================================================
# Symlink Structural Replication
# =============================================================================
#
# Symlinks are replicated STRUCTURALLY: the (path, target) pair travels the
# sync channel and the receiver recreates the link. Never the target content.
# This is the reconcile (full-sync) counterpart of the realtime WS `symlink`
# event. Both server mode and LAN mode use the same HTTP surface: the central
# server exposes POST /symlinks; the LAN master exposes the same route
# (lan_server.py) so the client code is transport-agnostic.


def _local_symlink_relpath(link: Symlink) -> str | None:
    """Return the bucket-relative path of *link*'s local filepath.

    The link produced by the reconcile is already bucket-relative (built via
    ``Symlink.from_dict`` with prefix strip); realtime watchers may pass an
    absolute path. Both forms are normalized to the wire ``path`` parameter.
    """
    if not link.filepath:
        return None
    fp = str(link.filepath)
    bucket = CONFIG.get("LOCAL_BUCKET", "")
    if bucket and Path(fp).is_absolute():
        try:
            fp = str(Path(fp).relative_to(bucket))
        except ValueError:
            return None
    return fp.lstrip("/") or None


def materialize_symlink_local(link: Symlink) -> bool:
    """Recreate *link* locally as a symlink; return True on success.

    The link location is confined to LOCAL_BUCKET WITHOUT dereferencing the
    final component; the target must stay inside the bucket (``is_safe_symlink_target``).
    An existing file/link at the path is replaced; a non-empty directory is
    refused. Mirrors the inbound-event materialisation (event_dispatch._apply_symlink).
    """
    bucket = CONFIG.get("LOCAL_BUCKET", "")
    if not bucket or not link.filepath:
        return False
    local = resolve_within(bucket, link.filepath, resolve_final=False)
    if local is None:
        log.warning(f'{ActionLabel.SECURITY} Rejected unsafe symlink path: "{link.filepath}"')
        return False
    if not link.target:
        log.warning(f'{ActionLabel.SECURITY} Refusing symlink with empty target: "{local}"')
        return False
    if not is_safe_symlink_target(local, link.target, bucket):
        log.warning(
            f"{ActionLabel.SECURITY} Symlink target escapes bucket, not creating: "
            f'"{local}" -> "{link.target}"'
        )
        return False
    parent = str(Path(local).parent)
    if not Path(parent).exists():
        Path(parent).mkdir(parents=True, exist_ok=True)
    if Path(local).is_dir() and not Path(local).is_symlink():
        try:
            Path(local).rmdir()
        except OSError:
            log.warning(
                f"{ActionLabel.SECURITY} Refusing to replace non-empty directory with "
                f'symlink: "{local}"'
            )
            return False
    elif Path(local).is_symlink() or Path(local).exists():
        Path(local).unlink()
    from os import symlink as os_symlink

    os_symlink(link.target, local)
    log.info(f'{ActionLabel.SYMLINK} Created symlink: "{local}" -> "{link.target}"')
    return True


def upload_symlink(link: Symlink) -> None:
    """Upload a symlink structurally to the remote (server in remote mode,
    LAN master in LAN mode — the same POST /symlinks surface)."""
    rel = _local_symlink_relpath(link)
    if rel is None or not link.target:
        log.warning(
            f"{ActionLabel.SECURITY} Cannot derive bucket-relative path for symlink "
            f'"{link.filepath}"'
        )
        return
    # An absolute target refers to THIS host's layout and is not portable:
    # relativize it against the link's directory (rsync munge semantics) so
    # every peer mounts the same reference. Escaping targets yield None.
    # ``wire_symlink_target`` requires the link's ABSOLUTE path: the reconcile
    # builds ``link.filepath`` bucket-relative, so resolve it first (a bare
    # relative path would be resolved against the process cwd, producing a
    # bogus ``../../…`` wire target).
    from hsync_common.path_validation import wire_symlink_target

    if Path(link.filepath).is_absolute():
        abs_link_path = link.filepath
    else:
        abs_link_path = str(Path(CONFIG["LOCAL_BUCKET"]) / link.filepath)
    wire_target = wire_symlink_target(abs_link_path, link.target, CONFIG["LOCAL_BUCKET"])
    if wire_target is None:
        log.warning(
            f"{ActionLabel.SECURITY} Symlink target escapes bucket, "
            f'not uploading: "{link.filepath}" -> "{link.target}"'
        )
        emit_event(
            UiEventName.security_rejected,
            op="upload_symlink",
            path=rel,
            message="symlink target escapes bucket",
        )
        return
    uri = f"{CONFIG['API']['buckets']}/{CONFIG['REMOTE_BUCKET']}/symlinks"
    params = {"path": rel, "target": wire_target, "cid": CONFIG.get("CID", "")}
    try:
        response = post(
            uri,
            params=params,
            headers=cast(dict[str, str], CONFIG["REQUEST_HEADERS"]),
            verify=get_ssl_verify(),
            timeout=CONFIG.get("HTTP_TIMEOUT", HTTP_TIMEOUT_DEFAULT),
        )
    except RequestException as e:
        log.error(f'Upload symlink failed for "{rel}": {e}')
        emit_event(UiEventName.transfer_failed, op="upload_symlink", file=rel, message=str(e))
        return
    if response.status_code in (200, 201):
        emit_event(UiEventName.transfer, op="upload_symlink", file=rel)
    elif response.status_code in (400, 403):
        log.warning(
            f'{ActionLabel.SECURITY} Remote refused symlink "{rel}" -> '
            f'"{wire_target}" (status {response.status_code})'
        )
        emit_event(
            UiEventName.security_rejected,
            op="upload_symlink",
            path=rel,
            message=f"remote refused symlink: {response.status_code}",
        )
    else:
        log.error(f'Upload symlink "{rel}": unexpected status {response.status_code}')


def download_symlink(link: Symlink) -> None:
    """Materialise a remote symlink entry locally as a link."""
    with path_lock(str(Path(CONFIG["LOCAL_BUCKET"]) / (link.filepath or ""))):
        materialize_symlink_local(link)
