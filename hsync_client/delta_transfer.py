# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Client-side delta transfer orchestration (rsync-style block sync).

This module implements the client half of the delta protocol against the
shared core in ``hsync_common.delta``:

- :func:`prepare_delta_upload` / :func:`prepare_delta_download` — the
  direction-aware gate (capability, size floor, recorded base hash). No extra
  disk I/O here: the local hash comes from the transfer engine / the shared
  SHA-256 LRU cache, the remote one from the event/listing metadata.
- :func:`upload_file_delta` — GET signatures (early-out when the remote is no
  longer the recorded base) → rolling match → TOCTOU re-check → POST apply.
- :func:`download_file_delta` — POST signatures → GET literal payload →
  rebuild into ``.delta.part`` → SHA-256 verify → atomic rename.

Every operational failure raises :class:`DeltaTransferError` and the caller
falls back to the full transfer within the same attempt; transient transport
errors (``RequestException``/``ServerError``) propagate to the existing retry
policy unchanged. This module works against both the FastAPI server and the
stdlib P2P master: the wire contract is identical (``hsync_common.delta``).
"""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import dataclass
from os import close, fdopen, fsync, remove, replace
from pathlib import Path
from tempfile import mkstemp
from time import monotonic
from typing import cast
from uuid import uuid4

import zstandard as zstd
from hsync_common.constants import PARTIAL_DELTA_SUFFIX
from hsync_common.delta import (
    apply_delta,
    compute_signatures,
    find_matches_file_to_sink,
    is_delta_worthwhile,
    optimal_block_size,
)
from hsync_common.format import format_bytes, format_duration
from hsync_common.hashing import compute_sha256, is_sha256_hex
from hsync_common.models.delta import (
    BlockSignature,
    DeltaOp,
    sanitize_op_list,
    sanitize_signature_list,
)
from hsync_common.models.file import File
from loguru import logger as log

from hsync_client.compression import (
    CONTENT_ENCODING_ZSTD,
    compress_file_to_file,
    compression_level,
    decompress_stream,
    should_compress,
)
from hsync_client.config import (
    CONFIG,
    get_delta_min_file_size,
    get_delta_min_file_size_p2p,
)
from hsync_client.exceptions import ServerError
from hsync_client.progress import ProgressReporter
from hsync_client.sync_state import get_base_hash, resolve_upload_version_fields
from hsync_client.utils import (
    get_ssl_verify,
    inbound_size_cap,
    register_inflight_partial,
    unregister_inflight_partial,
)
from hsync_client.utils import (
    http_connect_timeout as _timeout,
)
from hsync_client.utils import (
    http_get as get,
)
from hsync_client.utils import (
    http_post as post,
)
from hsync_client.utils import (
    upload_timeout as _upload_timeout,
)


def _resolve_delta_version_fields(file: File) -> tuple[str, str, int]:
    """Return ``(vclock_json, version_id, corrected_mtime)`` delta-apply fields.

    Delegates to :func:`sync_state.resolve_upload_version_fields` (the SSOT
    shared with the full-upload path) so both transfer channels advertise the
    same effective causal version and the ADR-0023 corrected mtime.
    """
    return resolve_upload_version_fields(file)


class DeltaTransferError(Exception):
    """Recoverable delta failure: the caller falls back to a full transfer."""


@dataclass(frozen=True)
class DeltaUploadContext:
    """Pre-computed state for an upload-delta attempt."""

    relpath: str  # bucket-relative remote path of the file
    base_hash: str  # recorded common-ancestor hash (== remote base, to confirm)


@dataclass(frozen=True)
class DeltaDownloadContext:
    """Pre-computed state for a download-delta attempt."""

    base_hash: str  # recorded common-ancestor hash (== local content)
    local_path: str  # absolute path of the local base file
    local_sha256: str  # hash of the local base (from the shared cache)


def _peer_supports_delta() -> bool:
    """True when delta is enabled locally AND advertised by the peer."""
    return bool(CONFIG.get("DELTA_ENABLED", False)) and bool(
        CONFIG.get("SERVER_DELTA_ENABLED", False)
    )


def _delta_min_size() -> int:
    """Size floor for delta, higher on P2P (CPU-bound, bandwidth-rich).

    CLI flags (--delta-min-file-size / --delta-min-file-size-p2p) take
    precedence over the HSYNC_DELTA_MIN_FILE_SIZE[_P2P] env settings.
    """
    if CONFIG.get("P2P_MODE", False):
        override = CONFIG.get("DELTA_MIN_FILE_SIZE_P2P")
        return override if override is not None else get_delta_min_file_size_p2p()
    override = CONFIG.get("DELTA_MIN_FILE_SIZE")
    return override if override is not None else get_delta_min_file_size()


def prepare_delta_upload(file: File, relpath: str) -> DeltaUploadContext | None:
    """Decide whether an upload-delta is applicable (no extra disk I/O).

    The local file is the *new* version; the server must still hold the
    recorded base (confirmed server-side by the signatures GET early-out).
    """
    if not _peer_supports_delta():
        return None
    if not file.filepath or not file.sha256sum or not relpath:
        return None
    if file.size < _delta_min_size():
        return None
    base_hash = get_base_hash(file.filepath)
    if not base_hash or file.sha256sum == base_hash:
        return None
    return DeltaUploadContext(relpath=relpath, base_hash=base_hash)


def prepare_delta_download(file: File, local_path: str) -> DeltaDownloadContext | None:
    """Decide whether a download-delta is applicable (no extra disk I/O).

    The local file on disk must BE the recorded base (only the remote side
    changed); its hash comes from the shared SHA-256 LRU cache, warm after
    the reconciliation pass — never re-hashed just for this decision.
    """
    if not _peer_supports_delta():
        return None
    if not file.filepath or not file.sha256sum or not local_path:
        return None
    if file.size < _delta_min_size():
        return None
    if not Path(local_path).is_file():
        return None
    base_hash = get_base_hash(local_path)
    if not base_hash or file.sha256sum == base_hash:
        return None
    local_file = File.get(local_path)
    if local_file is None or local_file.sha256sum != base_hash:
        return None
    return DeltaDownloadContext(
        base_hash=base_hash, local_path=local_path, local_sha256=local_file.sha256sum
    )


def upload_file_delta(file: File, ctx: DeltaUploadContext) -> None:
    """Perform an upload-delta against the peer's base file.

    Raises:
        DeltaTransferError: Recoverable condition → full-upload fallback.
        RequestException: Connection-level failure (retryable by the caller).
        ServerError: Server-side 5xx (retryable by the caller).
    """
    t0 = monotonic()
    headers = dict(cast(dict[str, str], CONFIG["REQUEST_HEADERS"]))
    sig_uri = f"{CONFIG['API']['buckets']}/{CONFIG['REMOTE_BUCKET']}/files/delta/signatures"

    response = get(
        sig_uri,
        headers=headers,
        params={"path": ctx.relpath},
        verify=get_ssl_verify(),
        timeout=_timeout(),
    )
    if response.status_code >= 500:
        raise ServerError(f"Server error on delta signatures: {response.status_code}")
    if response.status_code != 200:
        # 404 covers both "file missing" and "peer without delta endpoints".
        raise DeltaTransferError(f"signatures unavailable ({response.status_code})")

    body = response.json()
    remote_sha = body.get("sha256")
    if remote_sha != ctx.base_hash:
        raise DeltaTransferError("remote base changed (conflict or rescan needed)")
    block_size = body.get("block_size")
    file_size = body.get("file_size")
    if not isinstance(block_size, int) or not isinstance(file_size, int):
        raise DeltaTransferError("malformed signatures response")
    if block_size != optimal_block_size(file_size):
        raise DeltaTransferError("peer block size mismatch")
    clean_sigs = sanitize_signature_list(body.get("signatures"))
    if clean_sigs is None:
        raise DeltaTransferError("invalid signatures from peer")
    sigs = [BlockSignature(**s) for s in clean_sigs]

    # TOCTOU: snapshot before the rolling scan, re-check after — a file that
    # changes mid-scan would produce a delta describing a torn read whose
    # server-side hash check would still pass, poisoning the merge base.
    #
    # Literals are streamed into a temp file and the multipart body is streamed
    # from disk: requests' ``files=`` encoder reads a file object fully into
    # RAM (measured 2x the payload), so it is not used here.
    raw_path: str | None = None
    compressed_path: str | None = None
    try:
        raw_fd, raw_path = mkstemp(prefix="hsync-delta-", suffix=".bin")
        with fdopen(raw_fd, "w+b") as raw_literals:
            pre_stat = Path(file.filepath).stat()
            ops = find_matches_file_to_sink(file.filepath, sigs, block_size, raw_literals)
            literal_size = raw_literals.tell()
            post_stat = Path(file.filepath).stat()
            if (pre_stat.st_mtime, pre_stat.st_size) != (post_stat.st_mtime, post_stat.st_size):
                raise DeltaTransferError("file changed during delta scan")

        # For small files, also do a full re-hash to catch content changes that
        # mtime+size alone miss (e.g., rapid edits within the same second). The
        # check runs exactly once: the duplicated block wasted a full extra
        # SHA-256 pass over every small file on each delta upload.
        from hsync_common.constants import DELTA_TOCTOU_REHASH_THRESHOLD

        if file.size < DELTA_TOCTOU_REHASH_THRESHOLD:
            post_sha256 = compute_sha256(file.filepath)
            if post_sha256 != file.sha256sum:
                raise DeltaTransferError("file content changed during delta scan")

        if not is_delta_worthwhile(ops, file_size, literal_size, block_size):
            raise DeltaTransferError("delta not worthwhile (too many changed blocks)")

        new_sha256 = file.sha256sum
        if not is_sha256_hex(new_sha256):
            raise DeltaTransferError("missing local content hash")

        compressed = False
        payload_path = raw_path
        if (
            literal_size
            and CONFIG.get("COMPRESSION_ENABLED", False)
            and CONFIG.get("SERVER_COMPRESSION_ENABLED", False)
            and should_compress(file.filepath, literal_size, transfer_type="upload")
        ):
            # Stream-compress the literal file into a second temp file: neither
            # the raw literals nor the zstd frame are ever fully buffered.
            comp_fd, compressed_path = mkstemp(prefix="hsync-delta-", suffix=".zst")
            close(comp_fd)
            compress_file_to_file(
                raw_path, compressed_path, level=compression_level(transfer_type="upload")
            )
            payload_path = compressed_path
            compressed = True

        # Attach the effective causal version (vclock + version_id + the ADR-0023
        # corrected mtime) so the server/master can arbitrate the delta-apply
        # against its stored version (ADR-0022/ADR-0023). The server's
        # delta/apply.py reads meta["vclock"] / meta["version_id"] /
        # meta["corrected_mtime"].
        vclock_json, version_id, corrected_mtime = _resolve_delta_version_fields(file)
        metadata = {
            "cid": CONFIG["CID"],
            "base_sha256": ctx.base_hash,
            "new_sha256": new_sha256,
            "new_size": file.size,
            "block_size": block_size,
            "compressed": compressed,
            "mtime": file.mtime,
            "permissions": file.permissions,
            "ops": [op.to_dict() for op in ops],
            "vclock": vclock_json,
            "version_id": version_id,
            "corrected_mtime": corrected_mtime,
        }
        apply_uri = f"{CONFIG['API']['buckets']}/{CONFIG['REMOTE_BUCKET']}/files/delta/apply"

        # Stream the multipart body from disk (see _build_multipart_streaming):
        # the form fields are small and the literal payload is yielded in chunks,
        # so requests never builds the body in RAM.
        from hsync_client.transfer import _build_multipart_streaming

        body, content_type = _build_multipart_streaming(
            payload_path,
            {"path": ctx.relpath, "metadata": json.dumps(metadata)},
            boundary=uuid4().hex,
            filename="delta.bin",
        )
        headers["Content-Type"] = content_type
        response = post(
            apply_uri,
            headers=headers,
            data=body,
            verify=get_ssl_verify(),
            timeout=_upload_timeout(),
        )

        if response.status_code >= 500:
            raise ServerError(f"Server error on delta apply: {response.status_code}")
        if response.status_code != 200:
            raise DeltaTransferError(f"apply rejected ({response.status_code})")

        result = response.json()
        if result.get("sha256") != new_sha256:
            raise DeltaTransferError("apply response hash mismatch")
        log.info(
            f'[DELTA ↑] "{file.filepath}" {format_bytes(literal_size)} literal '
            f"(saved {format_bytes(file.size - literal_size)}) "
            f"{format_duration(monotonic() - t0, subsecond=True)}"
        )
    finally:
        for path in (raw_path, compressed_path):
            if path is not None:
                with suppress(OSError):
                    remove(path)


def _parse_data_url(data_url: object) -> str:
    """Validate a peer-supplied data_url and extract its token.

    Must be a relative path of the form
    ``/api/v1/buckets/<bucket>/files/delta/data/<token>`` — anything else
    (absolute URLs, other hosts, traversal) is rejected.
    """
    if not isinstance(data_url, str) or not data_url.startswith("/api/v1/buckets/"):
        raise DeltaTransferError("invalid data_url from peer")
    marker = "/files/delta/data/"
    if marker not in data_url:
        raise DeltaTransferError("invalid data_url from peer")
    token = data_url.rsplit(marker, 1)[-1]
    if not token or "/" in token or token in (".", ".."):
        raise DeltaTransferError("invalid data_url token from peer")
    return token


def download_file_delta(file: File, ctx: DeltaDownloadContext) -> None:
    """Perform a download-delta, rebuilding the new file from the local base.

    The rebuilt file is verified against the peer-declared ``new_sha256``
    before the atomic rename; on any mismatch the ``.delta.part`` is dropped
    and the caller falls back to a full download.

    Raises:
        DeltaTransferError: Recoverable condition → full-download fallback.
        RequestException: Connection-level failure (retryable by the caller).
        ServerError: Server-side 5xx (retryable by the caller).
    """
    t0 = monotonic()
    headers = dict(cast(dict[str, str], CONFIG["REQUEST_HEADERS"]))
    sig_uri = f"{CONFIG['API']['buckets']}/{CONFIG['REMOTE_BUCKET']}/files/delta/signatures"

    base_size = Path(ctx.local_path).stat().st_size
    block_size = optimal_block_size(base_size)
    sigs = compute_signatures(ctx.local_path, block_size)

    response = post(
        sig_uri,
        headers=headers,
        json={
            "path": file.filepath,
            "block_size": block_size,
            "signatures": [s.to_dict() for s in sigs],
        },
        verify=get_ssl_verify(),
        timeout=_timeout(),
    )
    if response.status_code >= 500:
        raise ServerError(f"Server error on delta signatures: {response.status_code}")
    if response.status_code != 200:
        raise DeltaTransferError(f"signatures rejected ({response.status_code})")

    body = response.json()
    if not body.get("delta"):
        raise DeltaTransferError(str(body.get("reason") or "not worthwhile"))

    instructions = body.get("instructions")
    if not isinstance(instructions, dict):
        raise DeltaTransferError("malformed instructions from peer")
    new_sha256 = body.get("new_sha256")
    new_size = instructions.get("new_size")
    if not is_sha256_hex(new_sha256) or not isinstance(new_size, int) or new_size < 0:
        raise DeltaTransferError("invalid instructions metadata from peer")
    clean_ops = sanitize_op_list(instructions.get("ops"))
    if clean_ops is None:
        raise DeltaTransferError("invalid ops from peer")
    ops = [
        DeltaOp(
            type=o["type"], offset=o["offset"], length=o["length"], data_offset=o["data_offset"]
        )
        for o in clean_ops
    ]
    if sum(op.length for op in ops) != new_size:
        raise DeltaTransferError("ops coverage mismatch from peer")

    token = _parse_data_url(body.get("data_url"))
    data_uri = f"{CONFIG['API']['buckets']}/{CONFIG['REMOTE_BUCKET']}/files/delta/data/{token}"

    part_path = f"{ctx.local_path}{PARTIAL_DELTA_SUFFIX}"
    payload_path = f"{part_path}.lit"
    # Both artifacts are written over the network below: mark them in flight so
    # the sync cycle's leftover sweep cannot delete a rebuild mid-apply.
    register_inflight_partial(part_path)
    register_inflight_partial(payload_path)
    try:
        with get(
            data_uri,
            headers=headers,
            stream=True,
            verify=get_ssl_verify(),
            timeout=_timeout(),
        ) as response:
            if response.status_code >= 500:
                raise ServerError(f"Server error on delta data: {response.status_code}")
            if response.status_code != 200:
                # 404/410: token lost or expired → full-download fallback.
                raise DeltaTransferError(f"payload unavailable ({response.status_code})")

            is_compressed = (
                response.headers.get("Content-Encoding", "").lower() == CONTENT_ENCODING_ZSTD
            )
            # Anti decompression-bomb: expansion capped at the peer-declared
            # literal size (it must match exactly) plus the configured cap.
            declared_size = body.get("data_size")
            write_cap = inbound_size_cap(
                declared_size if isinstance(declared_size, int) else 0,
                CONFIG.get("MAX_INBOUND_SIZE", 0),
            )
            # Progress tracks the literal payload crossing the wire (the
            # peer-declared data_size), not the reconstructed file size: a delta
            # only transfers the changed blocks.
            reporter = ProgressReporter(
                ctx.local_path,
                declared_size if isinstance(declared_size, int) else 0,
                "download",
            )
            response.raw.decode_content = False
            try:
                with open(payload_path, "wb") as payload_file:
                    written = 0
                    if is_compressed:
                        for chunk in decompress_stream(
                            response.raw, max_output_size=write_cap if write_cap is not None else 0
                        ):
                            written += len(chunk)
                            payload_file.write(chunk)
                            reporter.update(written)
                    else:
                        for chunk in response.iter_content(chunk_size=256 * 1024):
                            if not chunk:
                                break
                            written += len(chunk)
                            if write_cap is not None and written > write_cap:
                                raise DeltaTransferError("payload exceeds size cap")
                            payload_file.write(chunk)
                            reporter.update(written)
                    payload_file.flush()
                    fsync(payload_file.fileno())
            except zstd.ZstdError as exc:
                raise DeltaTransferError(f"payload decompression failed: {exc}") from exc

        with open(payload_path, "rb") as literal_stream:
            apply_delta(ctx.local_path, ops, literal_stream, part_path)

        rebuilt = compute_sha256(part_path)
        if rebuilt != new_sha256:
            raise DeltaTransferError("reconstructed file hash mismatch")
        if Path(part_path).stat().st_size != new_size:
            raise DeltaTransferError("reconstructed file size mismatch")

        replace(part_path, ctx.local_path)
        log.info(
            f'[DELTA ↓] "{file.filepath}" {format_bytes(Path(payload_path).stat().st_size)} literal '
            f"(saved {format_bytes(max(new_size - Path(payload_path).stat().st_size, 0))}) "
            f"{format_duration(monotonic() - t0, subsecond=True)}"
        )
    finally:
        for leftover in (part_path, payload_path):
            if Path(leftover).exists():
                remove(leftover)
        # Released last: while the artifacts are still ours the sweep skips
        # them, so it cannot delete a leftover we are about to remove (or a
        # rebuild we just renamed) in the window between the two statements.
        unregister_inflight_partial(part_path)
        unregister_inflight_partial(payload_path)
