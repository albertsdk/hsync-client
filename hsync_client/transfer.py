# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
File transfer operations for HSYNC Client.

This module provides upload and download functionality for
synchronizing files with the remote server.
"""

import hashlib
import hmac
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from os import close, fsync, remove, replace, stat_result, utime
from pathlib import Path
from tempfile import mkstemp
from time import monotonic
from time import sleep as time_sleep
from time import time as wall_time
from typing import Any, cast
from uuid import uuid4

import zstandard as zstd
from hsync_common.clock import clamp_incoming_corrected_mtime
from hsync_common.constants import (
    CHUNK_SIZE_SMALL,
    CONTENT_TYPE_OCTET_STREAM,
    HEADER_CHECKSUM_SHA256,
)
from hsync_common.disk import check_free_space
from hsync_common.format import format_bytes, format_duration
from hsync_common.hashing import adaptive_chunk_size, compute_sha256, is_sha256_hex
from hsync_common.models.file import File
from hsync_common.models.symlink import Symlink
from hsync_common.models.tombstone import relpath_in_bucket
from hsync_common.path_validation import (
    is_safe_symlink_target,
    resolve_within,
)
from hsync_common.permissions import mode_to_octal
from hsync_common.text import escape_loguru_markup, safe_json_for_log
from loguru import logger as log
from requests import RequestException
from requests.exceptions import SSLError

from hsync_client.compression import (
    CONTENT_ENCODING_ZSTD,
    compress_file_to_file,
    compression_level,
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
from hsync_client.events import UiEventName, emit_event, emit_warning
from hsync_client.exceptions import (
    DeclaredSizeMismatchError,
    PermanentDownloadError,
    PermanentUploadError,
    ServerError,
)
from hsync_client.p2p_inventory import NOOP_INVENTORY_OBSERVER, InventoryObserver
from hsync_client.p2p_sources import (
    P2PCandidateMap,
    P2PPeerSource,
    P2PSourceResolver,
    PeerFailureKind,
)
from hsync_client.progress import ProgressReporter
from hsync_client.retry import retry_on_transient_transfer_error
from hsync_client.sync_state import (
    abs_in_bucket,
    get_sync_state_store,
    local_version_for_compare,
    note_local_edit,
    resolve_upload_version_fields,
)
from hsync_client.tombstones import deletion_marker_snapshot, destination_mutation_since
from hsync_client.utils import (
    get_ssl_verify,
    get_throttle_delay,
    normalize_bucket_relpath,
    path_lock,
    register_inflight_partial,
    set_permissions,
    unregister_inflight_partial,
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
from hsync_client.utils import (
    upload_timeout as _upload_timeout,
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


def _emit_auth_error(action: str, op: str, filepath: str) -> None:
    """Log and surface an authentication failure for a transfer.

    ``action`` is the human reason (``"Authentication failed"``); ``op`` is the
    gerund direction (``"downloading"`` / ``"uploading"``). Both the log line and
    the ``auth_error`` event carry the same sentence so the GUI prompt and the
    log agree.
    """
    message = f'{action} {op} "{filepath}"'
    log.error(message)
    emit_event(UiEventName.auth_error, message=message)


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
    (convergence invariant, ADR-0022 §5). On download it is the remote-advertised clock;
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
                # Durable-persistence marker: emitted only after the flush
                # actually hit disk. Black-box offline-change tests wait for it
                # before SIGKILLing the process so the offline-deletion minting
                # on restart has a merge base to mint from (deterministic kill
                # point; see test_bb_server_offline_changes).
                log.debug(f'Merge base persisted for "{file.filepath}"')
    except Exception as e:  # pragma: no cover - bookkeeping must never break sync
        log.debug(f'Could not record sync-state for "{file.filepath}": {e}')


# Upload streaming chunk size (multipart bodies, plain and compressed spool).
# Derived from the shared small tier so the client and the master never drift on
# the per-chunk buffering; the download read tiers are chosen separately (see
# ``adaptive_chunk_size``).
_UPLOAD_CHUNK_SIZE = CHUNK_SIZE_SMALL

# Below this size a fresh upload is cheaper than the resumable-upload status
# probe (one extra HTTP round trip per file), so the probe is skipped and the
# transfer starts at offset 0. Correctness is unaffected: a stale server-side
# partial is discarded by the offset=0 POST (upload.py removes it), and an
# already-complete file is caught by the 409 dedup response. Resume is kept for
# larger files, where re-sending costs real bandwidth. Permission-only uploads
# never use this short-circuit (their fast path depends on probing).
UPLOAD_STATUS_PREFLIGHT_MIN_SIZE = 1024 * 1024


def _compress_to_temp_file(filepath: str) -> str:
    """Stream-compress *filepath* into a temporary file; return its path.

    Used by the compressed-upload branch so neither the original content nor
    the zstd frame is ever fully buffered in RAM: the previous implementation
    read the whole file and compressed it in memory, which delayed the first
    network byte by the full read+compress time and spiked memory on large
    files. The caller owns the returned path and must delete it; on
    compression failure the temp file is removed before raising.
    """
    fd, tmp_path = mkstemp(prefix="hsync-upload-", suffix=".zst")
    close(fd)
    try:
        compress_file_to_file(filepath, tmp_path, level=compression_level(transfer_type="upload"))
    except BaseException:
        with suppress(OSError):
            remove(tmp_path)
        raise
    return tmp_path


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
    on_progress: Callable[[int], None] | None = None,
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
    file_size = max(file_size, 0)

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
        sent = 0
        with open(file_path, "rb") as fh:
            if offset > 0:
                fh.seek(offset)
            while True:
                chunk = fh.read(_UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
                sent += len(chunk)
                if on_progress is not None:
                    on_progress(sent)
                if bandwidth_limit:
                    delay = get_throttle_delay(len(chunk), bandwidth_limit, "upload")
                    if delay > 0:
                        time_sleep(delay)
        yield file_trailer
        yield closing

    return _SizedStreamingBody(_generator(), content_length), content_type


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


_CONTENT_RANGE_RE = re.compile(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)\Z")


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` delay in seconds, or ``None`` when unusable.

    Only the delay-seconds form is honoured (HTTP-date is not used by the P2P
    seeder); unparseable or negative values fall back to the local default so a
    malformed header can never stall the requester.
    """
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return seconds


class _PeerSourceFailure(Exception):
    """A source-local failure that must advance to the next candidate.

    ``retry_after_s`` carries the delay a saturated seeder asked for
    (``Retry-After``): the source is deferred for that interval instead of being
    counted as a health failure (ADR-0030).
    """

    def __init__(
        self,
        kind: PeerFailureKind,
        message: str,
        *,
        discard_partial: bool = False,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.discard_partial = discard_partial
        self.retry_after_s = retry_after_s


class _RestartMasterDownload(Exception):
    """Retry the canonical master once without a stale partial prefix."""


class _CountingReader:
    """Count and throttle compressed wire bytes consumed by the zstd reader."""

    def __init__(self, stream: Any, bandwidth_limit: int | None = None) -> None:
        self._stream = stream
        self._bw_limit = bandwidth_limit
        self.bytes_read = 0

    def _throttle(self, nbytes: int) -> None:
        if not self._bw_limit:
            return
        delay = get_throttle_delay(nbytes, self._bw_limit, "download")
        if delay > 0:
            time_sleep(delay)

    def read(self, size: int = -1) -> bytes:
        data = self._stream.read(size)
        if data:
            self.bytes_read += len(data)
            self._throttle(len(data))
        return data

    def readinto(self, buffer: Any) -> int:
        readinto = getattr(self._stream, "readinto", None)
        if readinto is None:
            data = self.read(len(buffer))
            count = len(data)
            buffer[:count] = data
            return count
        count = readinto(buffer) or 0
        self.bytes_read += count
        if count:
            self._throttle(count)
        return count

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _normalized_download_path(file: File) -> str | None:
    """Return the canonical bucket-relative path used on P2P source requests."""
    return normalize_bucket_relpath(str(file.filepath), CONFIG["LOCAL_BUCKET"])


def _valid_content_range(value: str | None, *, start: int, total: int) -> bool:
    if value is None:
        return False
    match = _CONTENT_RANGE_RE.fullmatch(value.strip())
    if match is None:
        return False
    response_start, response_end, response_total = (int(part) for part in match.groups())
    return (
        response_start == start
        and response_total == total
        and response_end == total - 1
        and response_start <= response_end
    )


def _peer_transport_failure_kind(exc: RequestException) -> PeerFailureKind:
    if isinstance(exc, SSLError):
        detail = str(exc).lower()
        if "fingerprint" in detail or "certificate" in detail:
            return PeerFailureKind.pin_changed
        return PeerFailureKind.tls
    return PeerFailureKind.transport


def _discard_partial(path: str) -> bool:
    try:
        remove(path)
    except FileNotFoundError:
        return True
    except OSError as exc:
        log.error(f'{ActionLabel.CLEANUP} Could not remove partial file "{path}": {exc}')
        return False
    return True


def download_file(
    file: File,
    inventory_observer: InventoryObserver = NOOP_INVENTORY_OBSERVER,
    *,
    candidates: P2PCandidateMap | None = None,
    source_resolver: P2PSourceResolver | None = None,
    refresh_peer_sources: bool = False,
) -> None:
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
            attempt(
                file,
                inventory_observer=inventory_observer,
                candidates=candidates,
                source_resolver=source_resolver,
                refresh_peer_sources=refresh_peer_sources,
            )
    except (RequestException, ServerError) as exc:
        log.error(f'Download failed after retries for "{file.filepath}": {exc}')
        _emit_transfer_failed("download", file.filepath, str(exc))


def _build_download_request(
    file: File,
    normalized_path: str,
    source: P2PPeerSource | None,
    source_resolver: P2PSourceResolver | None,
    resume_offset: int,
    own_cid: Any,
) -> tuple[str, dict[str, str], dict[str, str]]:
    """Build ``(uri, headers, params)`` for a master or peer download."""
    if source is None:
        uri = f"{CONFIG['API']['buckets']}/{CONFIG['REMOTE_BUCKET']}/files/download"
        headers = dict(cast(dict[str, str], CONFIG["REQUEST_HEADERS"]))
        params: dict[str, str] = {"filename": normalized_path}
        if own_cid:
            params["cid"] = own_cid
    else:
        if source_resolver is None:
            raise RuntimeError("peer source requires a resolver")
        uri = source.download_url
        headers = source_resolver.request_headers(
            source,
            resumed=resume_offset > 0,
            compression_enabled=CONFIG.get("COMPRESSION_ENABLED", True),
        )
        params = {
            "filename": normalized_path,
            "sha256": file.sha256sum,
            "cid": own_cid or "",
        }
    if resume_offset > 0:
        headers["Range"] = f"bytes={resume_offset}-"
        headers["Accept-Encoding"] = "identity"
    return uri, headers, params


def _peer_only_failure(
    source: P2PPeerSource | None,
    kind: PeerFailureKind,
    message: str,
    *,
    discard_partial: bool = False,
) -> None:
    """Raise the peer-channel failure when a seeder served this download.

    Every integrity check runs on two channels: a peer failure is raised so the
    resolver ranks the next source, while the master channel logs and swallows
    the same condition. Centralizing the branch keeps the two channels from
    drifting — a missing ``if source`` test used to turn a peer defect into a
    silent success.
    """
    if source is not None:
        raise _PeerSourceFailure(kind, message, discard_partial=discard_partial)


@dataclass(frozen=True, slots=True)
class _DownloadPlan:
    """Validated response headers for one download attempt."""

    content_length: int | None
    expected_sha: str
    server_sha256: str | None
    is_compressed: bool


def _already_up_to_date(
    file: File, full_save_path: str, inventory_observer: InventoryObserver
) -> bool:
    """True when the local copy already holds the remote content.

    The mtime shortcut alone is not enough: an equal (or newer) local mtime with
    *different* content is exactly the conflict case — the caller has already
    preserved the local copy, and skipping here would prevent the master's
    version from ever arriving. Skip only when the content is genuinely
    identical, or when no well-formed hash is available to disprove the mtime
    shortcut (legacy behaviour preserved for metadata without a valid expected
    digest).
    """
    if not Path(full_save_path).exists():
        return False
    if file.mtime is None or Path(full_save_path).stat().st_mtime < file.mtime:
        return False
    expected = file.sha256sum or ""
    if (
        expected
        and is_sha256_hex(expected)
        and not hmac.compare_digest(compute_sha256(full_save_path), expected)
    ):
        return False
    nothing_to_do(file)
    inventory_observer.upsert(file)
    return True


def _apply_remote_metadata(file: File, path: str) -> None:
    """Apply the listing's mtime and mode to a downloaded file."""
    if file.mtime is not None:
        utime(path, (file.mtime, file.mtime))
    if file.permissions:
        set_permissions(path, file.permissions)


def _finalize_download(
    file: File,
    full_save_path: str,
    t0: float,
    *,
    source: P2PPeerSource | None = None,
    source_resolver: P2PSourceResolver | None = None,
    rtt_seconds: float = 0.0,
    wire_bytes: int = 0,
    body_seconds: float = 0.0,
    inventory_observer: InventoryObserver = NOOP_INVENTORY_OBSERVER,
    metadata_errors_are_fatal: bool = False,
) -> None:
    """Complete a committed download: log, event, metadata and merge base.

    Delta and full transfers share this post-condition so they cannot diverge on
    the recorded merge base. ``metadata_errors_are_fatal`` lets the delta path
    propagate a metadata failure into its caller's full-transfer fallback; the
    full path has already committed the destination and can only log it.
    """
    source_detail = f" source_cid={source.cid}" if source is not None else ""
    log.info(
        f'{ActionLabel.DOWNLOAD} "{full_save_path}" {format_bytes(file.size)} '
        f"{format_duration(monotonic() - t0, subsecond=True)}{source_detail}"
    )
    extra: dict[str, Any] = {"source_cid": source.cid} if source is not None else {}
    emit_event(
        UiEventName.transfer,
        op="download",
        file=full_save_path,
        size=file.size or 0,
        **extra,
    )
    if source is not None and source_resolver is not None:
        source_resolver.record_success(
            source,
            rtt_seconds=rtt_seconds,
            bytes_transferred=wire_bytes,
            body_seconds=body_seconds,
        )
    try:
        _apply_remote_metadata(file, full_save_path)
    except OSError as exc:
        if metadata_errors_are_fatal:
            raise
        log.error(f'Could not apply metadata to "{file.filepath}": {exc}')
    _record_synced_base(
        file,
        vclock=_remote_vclock(file),
        corrected_mtime=_remote_corrected_mtime(file),
    )
    inventory_observer.upsert(file)


def _try_delta_download(
    file: File,
    full_save_path: str,
    t0: float,
    inventory_observer: InventoryObserver,
) -> bool:
    """Rebuild the file from its recorded base; True when the delta succeeded.

    Any recoverable failure falls through to the full-download body (same
    attempt, retry policy unchanged).
    """
    ctx = prepare_delta_download(file, full_save_path)
    if ctx is None:
        return False
    try:
        download_file_delta(file, ctx)
        _finalize_download(
            file,
            full_save_path,
            t0,
            inventory_observer=inventory_observer,
            metadata_errors_are_fatal=True,
        )
    except (DeltaTransferError, OSError) as exc:
        log.warning(f'Delta download for "{file.filepath}" fell back to full transfer ({exc})')
        return False
    return True


def _resolve_resume_offset(
    file: File, partial_file_path: str, *, force_restart: bool
) -> int | None:
    """Offset to resume from, or ``None`` when the caller must skip the file."""
    if force_restart:
        return 0 if _discard_partial(partial_file_path) else None
    if not Path(partial_file_path).exists():
        return 0
    try:
        resume_offset = Path(partial_file_path).stat().st_size
    except OSError as exc:
        log.warning(f'Could not inspect partial file for "{file.filepath}": {exc}')
        _discard_partial(partial_file_path)
        return 0
    if resume_offset >= file.size:
        log.warning(f'Invalid full-size partial for "{file.filepath}", restarting download')
        return 0 if _discard_partial(partial_file_path) else None
    if resume_offset > 0:
        log.info(
            f'{ActionLabel.RESUME} "{file.filepath}" from '
            f"{format_bytes(resume_offset)}/{format_bytes(file.size)}"
        )
    return resume_offset


def _has_download_space(file: File, full_save_path: str, resume_offset: int) -> bool:
    """Size-aware disk-space guard: False when the file must be skipped.

    Skipping avoids a mid-transfer ENOSPC that would waste bandwidth and leave a
    ``.part`` behind. The rule itself lives in ``hsync-common``
    (``check_free_space``), shared with the server and the master upload guard.
    """
    space = check_free_space(
        str(Path(full_save_path).parent),
        reserve_bytes=CONFIG.get("MIN_FREE_DISK_SPACE", 0),
        incoming_bytes=max(file.size - resume_offset, 0),
    )
    if space.error is not None:
        log.warning(f"Could not check disk space: {space.error}")
        return True
    if space.ok:
        return True
    log.error(
        f'Insufficient disk space for "{file.filepath}": '
        f"{space.free_mb} MB available, {space.required_mb} MB required"
    )
    # A skipped download is a file that will not arrive: the terminal event the
    # GUI aggregates ("N transfers failed") is the only way it is not silent.
    emit_event(
        UiEventName.transfer_failed,
        op="download",
        file=file.filepath,
        message=(
            f"Insufficient disk space ({space.free_mb} MB available, "
            f"{space.required_mb} MB required)"
        ),
    )
    return False


def _accept_download_response(
    response,
    file: File,
    source: P2PPeerSource | None,
    resume_offset: int,
    partial_file_path: str,
) -> int | None:
    """Validate the response status; return the effective resume offset.

    ``None`` means the caller must skip the file. Retryable channels
    (``_PeerSourceFailure``, ``ServerError``, ``_RestartMasterDownload``) are
    raised so the caller's retry policy reacts. A server that ignores ``Range``
    is normalized to a full restart.
    """
    status = response.status_code
    if source is not None and status not in (200, 206):
        retry_after_s = _parse_retry_after(response.headers.get("Retry-After"))
        response.close()
        if status in (400, 401, 403):
            kind = PeerFailureKind.unauthorized
        elif status == 404:
            kind = PeerFailureKind.not_found
        elif status == 409:
            kind = PeerFailureKind.conflict
        elif status == 503:
            kind = PeerFailureKind.busy
        elif 400 <= status < 500:
            kind = PeerFailureKind.malformed
        else:
            kind = PeerFailureKind.transport
        raise _PeerSourceFailure(
            kind,
            f"Seeder returned HTTP {status}",
            discard_partial=status == 416,
            retry_after_s=retry_after_s,
        )

    if status in (401, 403):
        _emit_auth_error("Authentication failed", "downloading", file.filepath)
        response.close()
        return None
    if status == 404:
        log.error(f'File not found on server: "{file.filepath}"')
        response.close()
        return None
    if status == 416:
        response.close()
        if resume_offset:
            if not _discard_partial(partial_file_path):
                return None
            raise _RestartMasterDownload
        log.error(f'Unexpected range response downloading "{file.filepath}"')
        return None
    if status >= 500:
        response.close()
        raise ServerError(f'Server error downloading "{file.filepath}": {status}')
    if status not in (200, 206):
        log.error(f'Unexpected status downloading "{file.filepath}": {status}')
        response.close()
        return None

    if status == 206 and (
        resume_offset == 0
        or not _valid_content_range(
            response.headers.get("Content-Range"),
            start=resume_offset,
            total=file.size,
        )
    ):
        response.close()
        _discard_partial(partial_file_path)
        _peer_only_failure(
            source,
            PeerFailureKind.malformed,
            "Invalid Content-Range",
            discard_partial=True,
        )
        log.error(f'Invalid Content-Range downloading "{file.filepath}"')
        return None

    # A 200 for a ranged request means the server ignored Range: restart.
    if resume_offset > 0 and status == 200:
        log.debug("Server does not support Range requests, restarting download")
        if not _discard_partial(partial_file_path):
            response.close()
            return None
        return 0
    return resume_offset


def _validate_download_headers(
    response,
    file: File,
    source: P2PPeerSource | None,
    request_headers: dict[str, str],
) -> _DownloadPlan | None:
    """Extract and validate Content-Length, checksums and Content-Encoding.

    ``None`` means the caller must skip the file; the caller's ``finally`` owns
    the response close and the ``.part`` cleanup for those paths.
    """
    raw_content_length = response.headers.get("Content-Length")
    content_length: int | None = None
    if raw_content_length is not None:
        if not (
            isinstance(raw_content_length, str)
            and raw_content_length.isascii()
            and raw_content_length.isdigit()
        ):
            _peer_only_failure(
                source,
                PeerFailureKind.malformed,
                "Invalid Content-Length",
                discard_partial=True,
            )
            log.error(f'Invalid Content-Length downloading "{file.filepath}"')
            return None
        content_length = int(raw_content_length)

    expected_sha = file.sha256sum or ""
    if not is_sha256_hex(expected_sha):
        _peer_only_failure(
            source,
            PeerFailureKind.hash_mismatch,
            "Invalid listing checksum",
            discard_partial=True,
        )
        log.error(f'Invalid expected checksum downloading "{file.filepath}"')
        return None
    expected_sha = expected_sha.lower()

    server_sha256 = response.headers.get(HEADER_CHECKSUM_SHA256)
    if source is not None:
        if not is_sha256_hex(server_sha256):
            _peer_only_failure(
                source,
                PeerFailureKind.hash_mismatch,
                "Invalid source checksum",
                discard_partial=True,
            )
        source_sha256 = cast(str, server_sha256)
        # Uppercase is rejected explicitly: a seeder must advertise the same
        # canonical lowercase digest the listing carries.
        if source_sha256 != source_sha256.lower() or not hmac.compare_digest(
            source_sha256, expected_sha
        ):
            _peer_only_failure(
                source,
                PeerFailureKind.hash_mismatch,
                "Invalid source checksum",
                discard_partial=True,
            )

    content_encoding = response.headers.get("Content-Encoding", "").strip().lower()
    if content_encoding not in ("", "identity", CONTENT_ENCODING_ZSTD):
        _peer_only_failure(
            source,
            PeerFailureKind.malformed,
            f"Unsupported Content-Encoding: {content_encoding}",
            discard_partial=True,
        )
        log.error(f'Unsupported Content-Encoding downloading "{file.filepath}"')
        return None
    is_compressed = content_encoding == CONTENT_ENCODING_ZSTD
    if is_compressed and (
        response.status_code == 206
        or (source is not None and request_headers.get("Accept-Encoding") != CONTENT_ENCODING_ZSTD)
    ):
        _peer_only_failure(
            source,
            PeerFailureKind.malformed,
            "Compressed response was not negotiated",
            discard_partial=True,
        )
        log.error(f'Compressed range response downloading "{file.filepath}"')
        return None

    return _DownloadPlan(
        content_length=content_length,
        expected_sha=expected_sha,
        server_sha256=cast(str | None, server_sha256),
        is_compressed=is_compressed,
    )


def _download_from_source_attempt(
    file: File,
    inventory_observer: InventoryObserver = NOOP_INVENTORY_OBSERVER,
    *,
    source: P2PPeerSource | None = None,
    source_resolver: P2PSourceResolver | None = None,
    allow_delta: bool = True,
    delta_only: bool = False,
    force_restart: bool = False,
) -> bool:
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

    full_save_path = _local_dest_path(file)
    if full_save_path is None:
        # Already gated in download_file; stay defensive for direct callers.
        log.error(f'Refusing unsafe download path "{file.filepath}"')
        return True
    Path(full_save_path).parent.mkdir(parents=True, exist_ok=True)
    partial_file_path = f"{full_save_path}{PARTIAL_DOWNLOAD_SUFFIX}"
    # Snapshot the destination before the body streams: if it is deleted or
    # renamed away — or its deletion marker appears or changes — while the
    # transfer runs, the final move must not commit over it (it would resurrect
    # a removed name or clobber a file the user recreated in place).
    dest_existed = Path(full_save_path).exists()
    dest_marker = deletion_marker_snapshot(full_save_path)

    if _already_up_to_date(file, full_save_path, inventory_observer):
        return True

    if allow_delta and _try_delta_download(file, full_save_path, t0, inventory_observer):
        return True
    if delta_only:
        return False

    resume_offset = _resolve_resume_offset(file, partial_file_path, force_restart=force_restart)
    if resume_offset is None:
        return True

    if not _has_download_space(file, full_save_path, resume_offset):
        return True

    max_inbound_size = CONFIG.get("MAX_INBOUND_SIZE", 0)
    if max_inbound_size and file.size > max_inbound_size:
        raise PermanentDownloadError(
            f'"{file.filepath}" exceeds the maximum download size ({max_inbound_size} bytes)'
        )

    normalized_path = _normalized_download_path(file)
    if normalized_path is None:
        return True
    own_cid = CONFIG.get("CID")
    uri, headers, params = _build_download_request(
        file, normalized_path, source, source_resolver, resume_offset, own_cid
    )

    request_started = monotonic()
    try:
        if source is None:
            response = get(
                uri,
                headers=headers,
                params=params,
                stream=True,
                verify=get_ssl_verify(),
                timeout=CONFIG.get("HTTP_TIMEOUT", HTTP_TIMEOUT_DEFAULT),
            )
        else:
            response = get(
                uri,
                headers=headers,
                params=params,
                stream=True,
                timeout=(2.0, 30.0),
                verify=False,
                cert_fingerprint=source.cert_fingerprint,
            )
    except RequestException as exc:
        if source is not None:
            raise _PeerSourceFailure(_peer_transport_failure_kind(exc), str(exc)) from exc
        raise
    rtt_seconds = monotonic() - request_started

    log.debug(f"Request URI: {uri}")
    log.debug(f"Request Header: {safe_json_for_log(headers)}")
    log.debug(f"Response status: {response.status_code}")

    resume_offset = _accept_download_response(
        response, file, source, resume_offset, partial_file_path
    )
    if resume_offset is None:
        return True

    cleanup_partial = True
    # Mark the artifact in flight for the whole body: the sync cycle's
    # leftover sweep must never delete a ``.part`` a live transfer is still
    # writing (that sweep runs in the resync task, outside this transfer).
    register_inflight_partial(partial_file_path)
    sha256 = hashlib.sha256()
    written = resume_offset
    wire_bytes = 0
    counter: _CountingReader | None = None
    # Bound before the try: the zstd handler reads it, and header validation can
    # end the attempt before the body loop assigns it.
    content_length: int | None = None
    body_started = monotonic()
    try:
        plan = _validate_download_headers(response, file, source, headers)
        if plan is None:
            return True
        content_length = plan.content_length
        expected_sha = plan.expected_sha
        server_sha256 = plan.server_sha256
        is_compressed = plan.is_compressed

        chunk_size = adaptive_chunk_size(file.size)
        write_mode = "ab" if resume_offset > 0 else "wb"
        if resume_offset > 0:
            prefix_size = 0
            with _open_partial_no_follow(partial_file_path, "rb") as prefix:
                while prefix_chunk := prefix.read(chunk_size):
                    prefix_size += len(prefix_chunk)
                    sha256.update(prefix_chunk)
            if prefix_size != resume_offset:
                _peer_only_failure(
                    source,
                    PeerFailureKind.malformed,
                    "Partial prefix changed",
                    discard_partial=True,
                )
                log.error(f'Partial prefix changed for "{file.filepath}"')
                return True

        body_started = monotonic()
        # The remote listing is authoritative for downloads, including an
        # exact zero-byte file. MAX_INBOUND_SIZE is enforced above as a policy
        # ceiling; it must never turn size=0 into an "unknown" multi-GB cap.
        write_cap = file.size
        reporter = ProgressReporter(full_save_path, write_cap, "download")
        with _open_partial_no_follow(partial_file_path, write_mode) as output:
            if is_compressed:
                response.raw.decode_content = False
                counter = _CountingReader(
                    response.raw, bandwidth_limit=CONFIG.get("MAX_BANDWIDTH_DOWNLOAD")
                )
                for chunk in decompress_stream(
                    counter,
                    max_output_size=write_cap,
                    read_size=max(1, min(chunk_size, max(write_cap, 1))),
                ):
                    output.write(chunk)
                    sha256.update(chunk)
                    written += len(chunk)
                    reporter.update(written)
            else:
                dl_bw_limit = CONFIG.get("MAX_BANDWIDTH_DOWNLOAD")
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if not chunk:
                        continue
                    written += len(chunk)
                    wire_bytes += len(chunk)
                    reporter.update(written)
                    if written > write_cap:
                        _peer_only_failure(
                            source,
                            PeerFailureKind.malformed,
                            "Body exceeds listing size",
                            discard_partial=True,
                        )
                        # Distinct from the absolute-cap check above
                        # ("maximum download size"): here the body outgrew the
                        # size the remote listing declared, i.e. the remote
                        # copy changed while it was being transferred. The
                        # wording must say so, or the user reads the declared
                        # size as their --max-inbound-size limit.
                        raise DeclaredSizeMismatchError(
                            f'"{file.filepath}" received more bytes than the size '
                            f"declared by the remote ({write_cap} bytes); "
                            "the remote copy changed while transferring"
                        )
                    output.write(chunk)
                    sha256.update(chunk)
                    delay = get_throttle_delay(len(chunk), dl_bw_limit, "download")
                    if delay > 0:
                        time_sleep(delay)
            output.flush()
            fsync(output.fileno())
        if counter is not None:
            wire_bytes = counter.bytes_read
        body_seconds = monotonic() - body_started

        if content_length is not None and wire_bytes != content_length:
            if wire_bytes < content_length:
                cleanup_partial = False
                message = f"Incomplete wire body ({wire_bytes}/{content_length} bytes)"
                _peer_only_failure(source, PeerFailureKind.transport, message)
                raise RequestException(message)
            _peer_only_failure(
                source,
                PeerFailureKind.malformed,
                "Content-Length mismatch",
                discard_partial=True,
            )
            log.error(f'Content-Length mismatch downloading "{file.filepath}"')
            return True

        if written != file.size:
            if written < file.size:
                _peer_only_failure(
                    source,
                    PeerFailureKind.malformed,
                    f"Complete body has wrong size ({written}/{file.size} bytes)",
                    discard_partial=True,
                )
                log.error(f'Incomplete body downloading "{file.filepath}"')
                return True
            _peer_only_failure(
                source,
                PeerFailureKind.malformed,
                "Body exceeds listing size",
                discard_partial=True,
            )
            log.error(f'Body exceeds listing size downloading "{file.filepath}"')
            return True

        sha256sum = sha256.hexdigest()
        if is_sha256_hex(server_sha256):
            normalized_server_sha = cast(str, server_sha256).lower()
            if not hmac.compare_digest(sha256sum, normalized_server_sha):
                _peer_only_failure(
                    source,
                    PeerFailureKind.hash_mismatch,
                    "Source checksum mismatch",
                    discard_partial=True,
                )
                if resume_offset:
                    raise _RestartMasterDownload
                log.error(f'Server checksum mismatch downloading "{file.filepath}"')
                return True
        if not hmac.compare_digest(sha256sum, expected_sha):
            _peer_only_failure(
                source,
                PeerFailureKind.hash_mismatch,
                "Listing checksum mismatch",
                discard_partial=True,
            )
            if resume_offset:
                raise _RestartMasterDownload
            log.error(f'Invalid checksum downloading "{file.filepath}"')
            return True

        if destination_mutation_since(full_save_path, dest_existed, dest_marker):
            log.info(
                f'Not committing "{file.filepath}": the destination was deleted '
                "or renamed while downloading"
            )
            cleanup_partial = True
            return True
        replace(partial_file_path, full_save_path)
        cleanup_partial = False
        _finalize_download(
            file,
            full_save_path,
            t0,
            source=source,
            source_resolver=source_resolver,
            rtt_seconds=rtt_seconds,
            wire_bytes=wire_bytes,
            body_seconds=body_seconds,
            inventory_observer=inventory_observer,
        )
        return True

    except _PeerSourceFailure:
        raise
    except _RestartMasterDownload:
        raise
    except PermanentDownloadError:
        raise
    except RequestException as exc:
        cleanup_partial = False
        if source is not None:
            raise _PeerSourceFailure(_peer_transport_failure_kind(exc), str(exc)) from exc
        raise
    except PermissionError as exc:
        log.error(f'Permission denied saving "{file.filepath}": {exc}')
        return True
    except zstd.ZstdError as exc:
        if counter is not None:
            wire_bytes = counter.bytes_read
        wire_body_complete = content_length is not None and wire_bytes >= content_length
        if written < file.size and not wire_body_complete:
            cleanup_partial = False
            if source is not None:
                raise _PeerSourceFailure(PeerFailureKind.transport, str(exc)) from exc
            raise RequestException(str(exc)) from exc
        if source is not None:
            raise _PeerSourceFailure(
                PeerFailureKind.malformed,
                str(exc),
                discard_partial=True,
            ) from exc
        log.error(f'Decompression failed for "{file.filepath}": {exc}')
        return True
    except OSError as exc:
        log.error(f'Error saving file "{file.filepath}": {exc}')
        return True
    finally:
        response.close()
        if cleanup_partial and partial_file_path and Path(partial_file_path).exists():
            _discard_partial(partial_file_path)
            log.debug(f"Cleaned up partial file: {partial_file_path}")
        # Released last: while the artifact is still ours the sweep skips it,
        # so it cannot delete the leftover in the window between the two
        # statements (and a successful transfer already renamed it away).
        unregister_inflight_partial(partial_file_path)


def _download_file_attempt(
    file: File,
    inventory_observer: InventoryObserver = NOOP_INVENTORY_OBSERVER,
    *,
    candidates: P2PCandidateMap | None = None,
    source_resolver: P2PSourceResolver | None = None,
    refresh_peer_sources: bool = False,
) -> None:
    """Try delta, ranked exact-hash peers, then the canonical master."""
    if _download_from_source_attempt(
        file,
        inventory_observer,
        delta_only=True,
    ):
        return

    if source_resolver is not None:
        peer_sources = source_resolver.candidates_for(
            file,
            candidates,
            refresh=refresh_peer_sources,
        )
        source_limit = int(CONFIG.get("P2P_PEER_SOURCES_MAX", 3))
        for source in peer_sources[:source_limit]:
            if not source_resolver.authorize(source):
                continue
            # Admission control: never exceed the capacity the seeder advertised.
            # Waiting for a slot (bounded) keeps the peer path in use for a wide
            # worker pool; expiring it selects the master without ever producing
            # a refused attempt that would be counted against the seeder.
            if not source_resolver.acquire_slot(source):
                continue
            try:
                handled = _download_from_source_attempt(
                    file,
                    inventory_observer,
                    source=source,
                    source_resolver=source_resolver,
                    allow_delta=False,
                )
            except _PeerSourceFailure as exc:
                if exc.discard_partial:
                    dest = _local_dest_path(file)
                    if dest is None or not _discard_partial(f"{dest}{PARTIAL_DOWNLOAD_SUFFIX}"):
                        return
                source_resolver.record_failure(source, exc.kind, retry_after_s=exc.retry_after_s)
                log.debug(f'Peer source {source.cid} failed for "{file.filepath}": {exc}')
                continue
            finally:
                source_resolver.release_slot(source)
            if handled:
                return

    try:
        _download_from_source_attempt(
            file,
            inventory_observer,
            allow_delta=False,
        )
    except _RestartMasterDownload:
        _download_from_source_attempt(
            file,
            inventory_observer,
            allow_delta=False,
            force_restart=True,
        )


# =============================================================================
# File Stability Check
# =============================================================================


def _file_stats(path: str) -> tuple[int, float] | None:
    """``(size, mtime)`` used to detect an in-flight write; None when unreadable."""
    try:
        st = Path(path).stat()
    except OSError:
        # Deleted or unreadable: treated as stable, so the caller's open() reports it.
        return None
    return (st.st_size, st.st_mtime)


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
    if not file.filepath:
        return True

    before = _file_stats(file.filepath)
    if before is None:
        return True

    time_sleep(interval)

    after = _file_stats(file.filepath)
    if after is None:
        return True

    log.debug(
        f'Writing file: "{file.filepath}" '
        f"(size: {format_bytes(before[0])} -> {format_bytes(after[0])}, "
        f"mtime: {before[1]} -> {after[1]})"
    )

    return before == after


def _file_already_settled(file: File) -> bool:
    """True when *file* has not been written for at least ``STABILITY_DELAY``.

    Used by the conservative stability path to skip a full size+mtime poll
    when the first ``stat`` already shows an mtime older than the configured
    delay. A writer that is still appending would have refreshed ``mtime``.
    ``from_closed`` never uses this shortcut: the 0.3s re-check stays
    mandatory after ``IN_CLOSE_WRITE``.
    """
    if not file.filepath:
        return True
    try:
        mtime = Path(file.filepath).stat().st_mtime
    except OSError:
        # Deleted / unreadable: same degradation as ``is_file_stable``.
        return True
    delay = CONFIG.get("STABILITY_DELAY", FILE_STABILITY_INTERVAL_DEFAULT)
    return (wall_time() - mtime) >= delay


def _file_identity(path: str) -> tuple[int, int] | None:
    """Return ``(size, st_mtime_ns)`` for *path*, or ``None`` if unreadable."""
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def _apply_stat_metadata(file: File, st: stat_result) -> None:
    """Copy size, timestamps, and mode from a ``stat`` result onto *file*."""
    file.size = st.st_size
    file.mtime = int(st.st_mtime)
    file.ctime = int(st.st_ctime)
    file.permissions = mode_to_octal(st.st_mode)


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
    uploading immediately. When the trigger is not a close event, a file whose
    mtime is already older than ``STABILITY_DELAY`` is treated as settled
    without the 5s poll. Otherwise, or if the short re-check still sees the
    file changing, fall back to the conservative poll bounded by
    ``MAX_WAIT_TIMEOUT``.

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

    # Already-settled shortcut (conservative path only): if mtime is older
    # than STABILITY_DELAY the file has not been written recently, so the
    # 5s size+mtime poll would only confirm what we already know. Fresh
    # mtimes (atomic rename just landed, still-open writer) still poll.
    if not from_closed and _file_already_settled(file):
        return True

    # Conservative path: poll until size/mtime settle, bounded by the timeout.
    # ``is_file_stable`` itself sleeps ``STABILITY_DELAY``, so the budget is a
    # monotonic deadline: counting iterations would stretch the documented
    # "seconds" by that delay (120 iterations ≈ 12 minutes, not 2). The last
    # probe may overshoot the deadline by at most one stability interval.
    max_wait = CONFIG.get("MAX_WAIT_TIMEOUT", MAX_WAIT_TIMEOUT_DEFAULT)
    deadline = monotonic() + max_wait
    while not is_file_stable(file):
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        time_sleep(min(1.0, remaining))
    return True


def _reschedule_upload(
    file: File,
    reschedules_left: int | None = None,
    permissions_only: bool = False,
    inventory_observer: InventoryObserver = NOOP_INVENTORY_OBSERVER,
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
        # Self-healing (the next change or resync retries it), so it is a
        # transient notice rather than a persistent problem: the user usually
        # just has the file open in a program that keeps writing it.
        emit_warning(
            "A file kept changing while it was being sent and was skipped: it is "
            "retried on its next change or the next full sync.",
            source="upload",
            reason="transient",
        )
        return

    delay = cast(float, CONFIG.get("UPLOAD_RESCHEDULE_DELAY", UPLOAD_RESCHEDULE_DELAY_DEFAULT))
    log.info(
        f'File "{file.filepath}" still being written; rescheduling upload in '
        f"{delay}s ({reschedules_left} attempt(s) left)"
    )
    timer_kwargs: dict[str, object] = {
        "_reschedules_left": reschedules_left - 1,
        "permissions_only": permissions_only,
    }
    if inventory_observer is not NOOP_INVENTORY_OBSERVER:
        timer_kwargs["inventory_observer"] = inventory_observer
    timer = threading.Timer(delay, upload_file, args=(file,), kwargs=timer_kwargs)
    timer.daemon = True
    timer.start()


def wire_relpath(file: File) -> str:
    """Return *file*'s bucket-relative path for a peer mutation request.

    A ``File`` reaches the transfer layer either from a remote listing (already
    bucket-relative) or from a local scan (absolute): both are normalised
    against ``LOCAL_BUCKET`` so the peer only ever receives a path inside the
    bucket it is allowed to touch.
    """
    prefix = f"{CONFIG['LOCAL_BUCKET']}".rstrip("/")
    return str(file.filepath or "").removeprefix(prefix).lstrip("/")


def delete_file_remote(file: File) -> None:
    """Delete *file* on the server via the REST ``DELETE …/files`` endpoint.

    Invoked for ``SyncActionName.delete_remote`` actions: the file was deleted
    locally while this peer was offline and the (tombstone-verified) copy left
    on the server is stale, so it must be removed instead of being downloaded
    back. ``file.filepath`` is the bucket-relative path from the remote listing
    (it must not resolve to a local file).

    The two transports speak different contracts for this endpoint: the
    central server expects form fields (``filename`` + ``cid``) while the P2P
    master reads query parameters (``path`` + ``is_dir`` + ``cid``, see
    ``p2p_server._handle_delete``). The branch below picks the wire shape
    from ``CONFIG["P2P_MODE"]`` so the same action works in both modes.

    Raises:
        NetworkError: On a connection-level error.
        ServerError: On a server-side error response (except 404: the file is
            already gone, which is the desired end state).
    """
    from hsync_client.utils import get_ssl_verify, http_delete

    uri = f"{CONFIG['API']['buckets']}/{CONFIG['REMOTE_BUCKET']}/files"
    filename = wire_relpath(file)

    p2p_mode = bool(CONFIG.get("P2P_MODE", False))
    response = http_delete(
        uri,
        params=({"path": filename, "is_dir": "false", "cid": CONFIG["CID"]} if p2p_mode else None),
        data=None if p2p_mode else {"cid": CONFIG["CID"], "filename": filename},
        headers=dict(CONFIG["REQUEST_HEADERS"]),
        verify=get_ssl_verify(),
        timeout=CONFIG.get("HTTP_TIMEOUT", HTTP_TIMEOUT_DEFAULT),
    )
    if response.status_code == 404:
        log.debug(f'"{filename}" already absent on server')
        return
    if response.status_code not in (200, 204):
        raise ServerError(
            message=f"Remote delete failed for {filename}: status {response.status_code}",
            details={"status_code": response.status_code, "response": response.text},
        )
    log.info(f'{ActionLabel.DELETE} "{filename}" (on server)')
    emit_event(UiEventName.transfer, op="delete_remote", file=filename)


def rename_file_remote(old_file: File, new_file: File) -> dict | None:
    """Move *old_file* to *new_file* on the remote without transferring bytes.

    Invoked for the reconcile-time rename shortcut: a file renamed while this
    peer was offline arrives as a tombstone-confirmed deletion of the stale
    remote path plus an upload of the new one, and both sides already advertise
    the same content hash. Moving the remote copy yields exactly the same
    result as DELETE + re-upload, and no byte crosses the wire.

    The wire shape is the one the P2P master already exposes
    (``PATCH …/files?src=&dst=``), now served by the central server too, so one
    contract covers both transports. ``expected_sha256`` makes the peer verify
    the content it is about to move: a stale listing must not relocate bytes
    nobody checked. ``mtime``/``permissions`` carry the local metadata an
    upload would have applied.

    Args:
        old_file: The remote-listing entry being vacated.
        new_file: The local entry holding the same content at the new path.

    Returns:
        The decoded response body (``file`` and the carried ``version``), or
        ``None`` when the peer refused the move (destination appeared, source
        gone, checksum no longer matches, endpoint unavailable). The caller
        falls back to DELETE + upload in that case.

    Raises:
        NetworkError: On a connection-level error.
        ServerError: On any other unexpected response.
    """
    uri = f"{CONFIG['API']['buckets']}/{CONFIG['REMOTE_BUCKET']}/files"
    params: dict[str, str] = {
        "src": wire_relpath(old_file),
        "dst": wire_relpath(new_file),
        "cid": CONFIG["CID"],
    }
    if old_file.sha256sum:
        params["expected_sha256"] = old_file.sha256sum
    if new_file.mtime and int(new_file.mtime) > 0:
        params["mtime"] = str(int(new_file.mtime))
    if new_file.permissions:
        params["permissions"] = new_file.permissions

    response = patch(
        uri,
        params=params,
        headers=dict(CONFIG["REQUEST_HEADERS"]),
        verify=get_ssl_verify(),
        timeout=CONFIG.get("HTTP_TIMEOUT", HTTP_TIMEOUT_DEFAULT),
    )
    if response.status_code == 200:
        try:
            return dict(response.json())
        except ValueError:
            # A 200 without a decodable body still moved the file: the caller
            # records the base from the local hash and the next listing
            # supplies the version.
            return {}
    if response.status_code in (404, 405, 409):
        # 404/405: the peer does not serve the rename contract (or the source
        # is already gone). 409: the destination appeared or the content no
        # longer matches. Both converge through the ordinary delete + upload.
        log.debug(
            f'Remote rename "{params["src"]}" -> "{params["dst"]}" refused '
            f"(status {response.status_code}); falling back to delete + upload"
        )
        return None
    raise ServerError(
        message=f"Remote rename failed for {params['src']}: status {response.status_code}",
        details={"status_code": response.status_code, "response": response.text},
    )


def upload_file(
    file: File,
    check_size: bool = True,
    from_closed: bool = False,
    permissions_only: bool = False,
    _reschedules_left: int | None = None,
    inventory_observer: InventoryObserver = NOOP_INVENTORY_OBSERVER,
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
    if not file.filepath:
        log.error("Cannot upload file: missing filepath")
        return

    # Security: never dereference a symlink whose target escapes the bucket.
    # This covers the realtime (watch/P2P slave) upload path, which builds the
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
        pre_identity = _file_identity(file.filepath)
        if not _wait_for_stable_file(file, from_closed):
            # File never stopped being written within the wait window: defer the
            # upload instead of transferring a half-written file.
            if inventory_observer is NOOP_INVENTORY_OBSERVER:
                _reschedule_upload(file, _reschedules_left, permissions_only)
            else:
                _reschedule_upload(
                    file,
                    _reschedules_left,
                    permissions_only,
                    inventory_observer,
                )
            return

        # Refresh metadata after the write is complete. Reuse the digest
        # already on *file* (watcher / reconcile scan) only when size and
        # nanosecond mtime are unchanged — File.get's cache is keyed on
        # int(mtime)+size and would miss a same-second same-size rewrite.
        try:
            st = Path(file.filepath).stat()
        except OSError:
            log.warning(f'File "{file.filepath}" disappeared after stability wait')
            return
        post_identity = (st.st_size, st.st_mtime_ns)
        if file.sha256sum and pre_identity == post_identity:
            _apply_stat_metadata(file, st)
        else:
            file.sha256sum = compute_sha256(file.filepath)
            _apply_stat_metadata(file, st)

        log.debug(f'Written file: "{file.filepath}" ({format_bytes(file.size)})')
        inventory_observer.upsert(file)

        # Recursive call with check_size=False to do actual upload
        if inventory_observer is NOOP_INVENTORY_OBSERVER:
            upload_file(file, False, permissions_only=permissions_only)
        else:
            upload_file(
                file,
                False,
                permissions_only=permissions_only,
                inventory_observer=inventory_observer,
            )
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
        if not file.filepath:
            return None
        abs_path = abs_in_bucket(file.filepath)
        eff = local_version_for_compare(abs_path, file.sha256sum)
        if eff is not None:
            return eff

        relpath = relpath_in_bucket(abs_path, str(CONFIG["LOCAL_BUCKET"]))
        if relpath:
            return get_sync_state_store().get(relpath)
    except Exception as e:  # pragma: no cover - bookkeeping must never break upload
        log.debug(f'Could not resolve upload version for "{file.filepath}": {e}')
    return None


def _resolve_local_version_pair(file: File) -> tuple[dict[str, int] | None, int]:
    """Return ``(vclock, corrected_mtime)`` of the effective local version.

    One resolution serves both merge-base fields: resolving them separately ran
    ``_resolve_effective_version`` twice per completed upload, and that helper
    hits the version map twice (compare view + stored clock).
    """
    eff = _resolve_effective_version(file)
    if eff is None:
        return None, 0
    return eff["vclock"], int(eff.get("corrected_mtime", 0) or 0)


def _resolve_local_vclock(file: File) -> dict[str, int] | None:
    """Return the effective local vclock for *file*, or ``None``."""
    return _resolve_local_version_pair(file)[0]


def _remote_vclock(file: File) -> dict[str, int] | None:
    """Return the remote-advertised vclock attached to *file*, if any.

    A downloaded ``File`` carries the version metadata from the remote listing
    or event; recording it as the merge base keeps the clock honest on the
    receiving side (convergence invariant, ADR-0022 §5).
    """
    version = getattr(file, "version", None)
    if isinstance(version, dict):
        vclock = version.get("vclock")
        if isinstance(vclock, dict):
            return vclock
    return None


def patch_permissions_remote(
    filename: str,
    permissions: str,
    mtime: int = 0,
    vclock_json: str = "",
    version_id: str = "",
    expected_sha256: str = "",
) -> bool:
    """Apply a metadata-only change on the remote WITHOUT sending file bytes.

    Hits the server's metadata-only ``PATCH …/files/permissions`` endpoint
    (the same route the P2P master exposes), so a chmod or a plain ``touch`` on
    a large file does not re-upload its content. ``mtime`` is the ADR-0023
    *corrected* timestamp to apply remotely; ``vclock_json``/``version_id``
    carry this node's effective causal version so the remote records the
    metadata change as a real version (listings and events then advertise it,
    instead of the peer re-detecting the divergence every sync).
    ``expected_sha256`` makes the peer verify that the path still holds the
    content this node compared against — a concurrent write turns the call into
    a ``False`` (409) and the caller re-uploads. Best-effort by
    construction: any transport error or non-200 status degrades to ``False``
    and the caller falls back to the regular upload path.

    Args:
        filename: Bucket-relative remote path of the file.
        permissions: Octal permission string to apply (remote-sanitized).
        mtime: Corrected modification time to propagate (0 = leave unchanged).
        vclock_json: JSON-encoded effective vector clock ("" = none).
        version_id: Effective version id ("" = none).
        expected_sha256: Content hash this node compared against ("" = no guard).

    Returns:
        True when the remote applied the change, False otherwise.
    """
    uri = f"{CONFIG['API']['buckets']}/{CONFIG['REMOTE_BUCKET']}/files/permissions"
    params: dict[str, str] = {"filename": filename, "permissions": permissions}
    if mtime > 0:
        params["mtime"] = str(mtime)
    if vclock_json and vclock_json != "{}":
        params["vclock"] = vclock_json
    if version_id:
        params["version_id"] = version_id
    if expected_sha256:
        params["expected_sha256"] = expected_sha256
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
    """Resolve a content-unchanged metadata divergence through the PATCH endpoint.

    Covers every metadata-only change (permissions and/or mtime). Records the
    local touch first (so the advertised clock and corrected mtime are honest),
    sends them alongside the mode, and returns ``True`` when the upload is fully
    resolved (metadata applied remotely, merge base recorded, UI event
    emitted). Returns ``False`` when the endpoint is unavailable (older server)
    and the caller must fall back to the legacy full-upload same-SHA branch.
    """
    abs_path = abs_in_bucket(file.filepath) if file.filepath else ""
    if abs_path and file.sha256sum:
        try:
            note_local_edit(abs_path, file.sha256sum)
        except Exception as e:  # pragma: no cover - bookkeeping must never break a transfer
            log.debug(f'Could not self-record metadata edit for "{file.filepath}": {e}')
    vclock_json, version_id, corrected_mtime = resolve_upload_version_fields(file)
    if not patch_permissions_remote(
        filename,
        file.permissions or "",
        corrected_mtime,
        vclock_json,
        version_id,
        file.sha256sum or "",
    ):
        return False
    log.info(f'{ActionLabel.PERMISSION} "{file.filepath}" (metadata updated)')
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


def _record_201_upload(file: File, response) -> None:
    """Record the merge base after a 201 upload success.

    The server's 201 may carry a ``version`` field with the recorded causal
    version (merged vclock + ADR-0023 corrected mtime) to mirror locally
    (convergence invariant, ADR-0022 §5); when absent, the local effective clock is
    recorded via ``merge_and_record``.
    """
    record_vclock: dict[str, int] | None = None
    record_corrected: int = 0
    try:
        payload = response.json() if response.text else None
        if isinstance(payload, dict):
            # The canonical "version" object carries the recorded causal
            # version (merged vclock + ADR-0023 corrected mtime).
            version = payload.get("version")
            if isinstance(version, dict):
                vclock_raw = version.get("vclock")
                if isinstance(vclock_raw, dict) and record_vclock is None:
                    record_vclock = {
                        str(k): int(v)
                        for k, v in vclock_raw.items()
                        if isinstance(v, int) and not isinstance(v, bool)
                    }
                raw_corrected = version.get("corrected_mtime")
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
        vclock, corrected_mtime = _resolve_local_version_pair(file)
        _record_synced_base(file, vclock=vclock, corrected_mtime=corrected_mtime)


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


def _handle_upload_response(file: File, response, t0: float) -> None:
    """Handle the shared upload response cases (201/200/401/403/409/≥500/other).

    Used by both the compressed and the streaming-multipart upload branches;
    the streaming branch handles the ``202`` partial-upload case itself before
    delegating here.
    """
    status = response.status_code
    match status:
        case 201:
            log.info(
                f'{ActionLabel.UPLOAD} "{file.filepath}" {format_bytes(file.size)} '
                f"{format_duration(monotonic() - t0, subsecond=True)}"
            )
            emit_event(UiEventName.transfer, op="upload", file=file.filepath, size=file.size or 0)
            _record_201_upload(file, response)
        case 200:
            # Permissions updated on the same-SHA branch (permission-only
            # change): local and remote content agree; record the base.
            log.info(f'{ActionLabel.PERMISSION} "{file.filepath}" (permissions updated)')
            _record_synced_base(file, vclock=_resolve_local_vclock(file))
        case 401:
            _emit_auth_error("Authentication failed", "uploading", file.filepath)
        case 403:
            log.error(f'Access denied uploading "{file.filepath}"')
        case 409:
            _handle_upload_conflict_response(file, response)
        case code if code >= 500:
            # Transient server-side failure: raise so the wrapper backs off.
            raise ServerError(f'Server error uploading "{file.filepath}": {code}')
        case code:
            # 4xx (not 401/403/409): permanent — log the server's
            # diagnostic body and raise a non-retryable error. Escape
            # braces so loguru does not interpret JSON bodies (e.g.
            # {"error": "..."}) as format placeholders.
            body = (response.text or "")[:500]
            log.error(
                f'Upload rejected (HTTP {code}) for "{file.filepath}": {escape_loguru_markup(body)}'
            )
            raise PermanentUploadError(
                f'Upload rejected (HTTP {code}) for "{file.filepath}": {body}'
            )


def _post_streaming_body(uri: str, headers: dict[str, str], body: Any) -> Any:
    """POST a streamed multipart body with the transfer SSL/timeout policy."""
    return post(
        uri,
        headers=headers,
        data=body,
        verify=get_ssl_verify(),
        timeout=_upload_timeout(),
    )


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
        delta_ctx = None
        if not permissions_only:
            delta_ctx = prepare_delta_upload(file, filename)

        # Self-record the local content edit before advertising it (ADR-0022
        # §3.4.7). Capture the delta context FIRST: it needs the OLD base hash
        # for the signatures; the tick then advances the version the delta/full
        # upload advertises, so the server's causal arbitration accepts it
        # instead of rejecting it as superseded/conflict (409).
        try:
            from hsync_client.sync import is_sync_cycle_active
            from hsync_client.sync_state import flush, note_local_edit

            if file.sha256sum:
                note_local_edit(file.filepath, file.sha256sum)
                # Full sync cycles defer persistence to their single end flush;
                # real-time watcher uploads persist immediately.
                if not is_sync_cycle_active():
                    flush()
        except Exception as e:  # pragma: no cover - bookkeeping must never break upload
            log.debug(f'Could not self-record content edit for "{file.filepath}": {e}')

        if delta_ctx is not None:
            try:
                upload_file_delta(file, delta_ctx)
                log.info(
                    f'{ActionLabel.UPLOAD} "{file.filepath}" {format_bytes(file.size)} '
                    f"{format_duration(monotonic() - t0, subsecond=True)}"
                )
                emit_event(
                    UiEventName.transfer, op="upload", file=file.filepath, size=file.size or 0
                )
                vclock, corrected_mtime = _resolve_local_version_pair(file)
                _record_synced_base(file, vclock=vclock, corrected_mtime=corrected_mtime)
                return
            except (DeltaTransferError, OSError) as exc:
                log.warning(
                    f'Delta upload for "{file.filepath}" fell back to full transfer ({exc})'
                )

        # Check for resumable upload. Permission-only uploads always probe
        # (their fast path depends on learning the peer already holds the
        # content); small non-permission uploads skip the probe and start fresh
        # (see UPLOAD_STATUS_PREFLIGHT_MIN_SIZE).
        if file.sha256sum is None or (
            not permissions_only and (file.size or 0) < UPLOAD_STATUS_PREFLIGHT_MIN_SIZE
        ):
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

        vclock_json, version_id, corrected_mtime = resolve_upload_version_fields(file)
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
        # the Authorization header (API key / P2P key) must be masked.
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

        compressed_tmp: str | None = None
        compressed_size = 0
        compressed_partial = False
        if use_compression:
            # Compress STREAMING to a temporary file (memory stays bounded by
            # one chunk), then send it as a regular file part: a real file
            # object lets requests frame the multipart body with an exact
            # Content-Length while still streaming each chunk from disk.
            compressed_tmp = _compress_to_temp_file(file.filepath)
            compressed_size = Path(compressed_tmp).stat().st_size
            if file.size and compressed_size >= file.size:
                # Compressing did not help (e.g. already-compressed or random
                # data): sending the larger frame would waste bandwidth and
                # route the body through the whole-file reader. Send the
                # original instead. The compression CPU is already spent;
                # should_compress skips known formats beforehand.
                log.debug(
                    f'Compression not beneficial for "{file.filepath}" '
                    f"({format_bytes(compressed_size)} >= {format_bytes(file.size)}); "
                    "sending the original"
                )
                with suppress(OSError):
                    remove(compressed_tmp)
                compressed_tmp = None
                use_compression = False

        if use_compression:
            assert compressed_tmp is not None
            try:
                if file.size:
                    compression_ratio = compressed_size / file.size * 100
                    log.debug(
                        f"Compressed {format_bytes(file.size)} -> "
                        f"{format_bytes(compressed_size)} "
                        f"({compression_ratio:.1f}%)"
                    )

                # Do NOT send ``Content-Encoding: zstd``: only the multipart file
                # part is compressed, not the request body, and both servers key
                # decompression off the ``compressed`` form field. A
                # Content-Encoding on the multipart envelope makes aiohttp (the
                # p2p master) decode the whole body and fail with 400.
                data["compressed"] = "true"
                data["compressed_size"] = compressed_size

                ul_bw_limit = CONFIG.get("MAX_BANDWIDTH_UPLOAD")
                reporter = ProgressReporter(file.filepath, compressed_size, "upload")
                boundary = uuid4().hex
                body, content_type = _build_multipart_streaming(
                    compressed_tmp,
                    dict(data),
                    boundary=boundary,
                    filename=Path(file.filepath).name,
                    offset=0,
                    bandwidth_limit=ul_bw_limit,
                    on_progress=reporter.update,
                )
                headers["Content-Type"] = content_type
                response = _post_streaming_body(uri, headers, body)
                if response.status_code == 202:
                    # The peer stored only part of the compressed frame. A
                    # compressed upload is not resumable (the server refuses
                    # an offset for it), so retrying it would fail the same
                    # way; fall through to the standard, resumable upload.
                    # The standard body is NOT compressed: drop the form
                    # fields or the peer would try to zstd-decompress the
                    # raw bytes ("Unknown frame descriptor").
                    compressed_partial = True
                    data.pop("compressed", None)
                    data.pop("compressed_size", None)
                    log.warning(
                        f"Compressed upload was only partially received for "
                        f'"{file.filepath}"; retrying uncompressed'
                    )
                else:
                    _handle_upload_response(file, response, t0)
                    return
            finally:
                with suppress(OSError):
                    remove(compressed_tmp)
        if not use_compression or compressed_partial:
            # Standard upload: stream the multipart body directly from disk
            # so files of arbitrary size do not need to be loaded into RAM.
            ul_bw_limit = CONFIG.get("MAX_BANDWIDTH_UPLOAD")

            boundary = uuid4().hex
            reporter = ProgressReporter(file.filepath, file.size or 0, "upload")
            body, content_type = _build_multipart_streaming(
                file.filepath,
                dict(data),
                boundary=boundary,
                filename=Path(file.filepath).name,
                offset=offset,
                bandwidth_limit=ul_bw_limit,
                on_progress=lambda sent: reporter.update(offset + sent),
            )
            # Content-Length is derived by requests from body.__len__; setting it
            # here as well would coexist with the chunked framing requests adds
            # for generators, yielding a request h11/uvicorn rejects.
            headers["Content-Type"] = content_type
            response = _post_streaming_body(uri, headers, body)
            if response.status_code == 202:
                # Partial upload received - file still incomplete
                result = response.json()
                new_offset = result.get("offset", 0)
                remaining = result.get("remaining", 0)
                log.debug(
                    f"Partial upload: {new_offset} bytes uploaded, {remaining} bytes remaining"
                )
            else:
                _handle_upload_response(file, response, t0)
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
# event. Both server mode and P2P mode use the same HTTP surface: the central
# server exposes POST /symlinks; the P2P master exposes the same route
# (p2p_server.py) so the client code is transport-agnostic.


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
    P2P master in P2P mode — the same POST /symlinks surface)."""
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


def download_symlink(
    link: Symlink,
    inventory_observer: InventoryObserver = NOOP_INVENTORY_OBSERVER,
) -> None:
    """Materialise a remote symlink entry locally as a link."""
    with path_lock(str(Path(CONFIG["LOCAL_BUCKET"]) / (link.filepath or ""))):
        if materialize_symlink_local(link) and link.filepath:
            inventory_observer.remove_file(link.filepath)
