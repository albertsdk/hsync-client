# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Delta transfer handlers for the stdlib P2P master server.

The P2P embedded server (``hsync_client.p2p_server``) cannot reuse the FastAPI
routers (separate repos, stdlib-only runtime), so these handlers re-implement
the four delta endpoints as thin shells over the *same* shared core
(``hsync_common.delta``): wire shapes, paths, status codes and JSON payloads
are identical to the FastAPI server, and parity is pinned by the cross-mode
fixtures/tests.

- ``handle_delta_signatures_get`` — serve base-file signatures (upload delta)
- ``handle_delta_signatures_post`` — match slave signatures (download delta)
- ``handle_delta_apply`` — multipart apply onto the master's base file
- ``handle_delta_data`` — single-use literal payload download (token-bound)

Signature/matching work is CPU-bound and runs under a dedicated sub-semaphore
so a burst of delta requests cannot starve the other handlers behind the
global 32-thread cap. Literal payloads are parked on disk (never in RAM)
under the system temp dir — inside LOCAL_BUCKET they would be synced.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

import zstandard as zstd
from hsync_common.constants import (
    CONTENT_TYPE_OCTET_STREAM,
    DELTA_MAX_LITERAL_SIZE,
    DELTA_TOKEN_TTL,
    PARTIAL_DELTA_SUFFIX,
)
from hsync_common.delta import (
    MISMATCH_HASH,
    BoundedLiteralSink,
    DeltaTokenStore,
    ReconstructionMismatch,
    TokenStoreFull,
    changed_block_ratio,
    compute_signatures,
    find_matches_file_to_sink,
    is_delta_worthwhile,
    optimal_block_size,
    reconstruct_and_verify,
)
from hsync_common.hashing import compute_sha256, is_sha256_hex
from hsync_common.ignore import should_skip
from hsync_common.models.delta import (
    BlockSignature,
    delta_download_payload,
    delta_signatures_payload,
    parse_op_payload,
    sanitize_signature_list,
    validate_op_bounds,
)
from hsync_common.models.file import File
from hsync_common.path_validation import resolve_within
from hsync_common.protocol.rest import rest_delta_data_path
from loguru import logger as log

from hsync_client.compression import (
    CONTENT_ENCODING_ZSTD,
    compress_file_to_file,
    compression_level,
    decompress_stream,
    should_compress,
)
from hsync_client.config import CONFIG
from hsync_client.constants import MTIME_MAX_FUTURE_SKEW_SECONDS
from hsync_client.progress import (
    emit_completed_transfer,
    progress_reporter,
)

# HTTP status codes used here (kept local to avoid a p2p_server import cycle).
_HTTP_OK = 200
_HTTP_BAD_REQUEST = 400
_HTTP_FORBIDDEN = 403
_HTTP_NOT_FOUND = 404
_HTTP_GONE = 410
_HTTP_UNPROCESSABLE = 422
_HTTP_CONFLICT = 409

# Cap on the POST signatures JSON body (DoS guard).
_MAX_SIG_BODY_BYTES = 50 * 1024 * 1024

# Sub-limit for CPU-bound delta work inside the global thread cap.
_delta_compute_sem = threading.Semaphore(4)

# Cap on concurrently live download tokens. The TTL is the shared SSOT
# (``hsync_common.constants.DELTA_TOKEN_TTL``) so a client and a server of the
# same deployment expire parked payloads at the same pace.
_MAX_TOKENS = 100


def _file_sha256(filepath: str) -> str:
    """Return the cached SHA-256 for *filepath*, recomputing it on a miss."""
    file_obj = File.get(filepath)
    return (file_obj.sha256sum if file_obj else None) or compute_sha256(filepath)


# Process-wide temp dir for parked payloads (outside LOCAL_BUCKET on purpose).
# It is created on demand and removed at interpreter exit by
# ``_discard_payload_dir`` (registered with :mod:`atexit`): any other
# process-scoped temporary directory added here MUST register the same way, or
# every run leaves an empty directory behind in the system temp dir.
_payload_dir: str | None = None


def _get_payload_dir() -> str:
    """Return (creating on demand) the temp dir holding literal payloads.

    The directory is process-wide because a payload must survive across the
    requests of one delta round trip. It is created on first use and dropped by
    :func:`_discard_payload_dir`, which also resets this module global, so the
    next request after a teardown gets a fresh directory.
    """
    global _payload_dir
    if _payload_dir is None:
        _payload_dir = tempfile.mkdtemp(prefix="hsync-delta-lan-")
    return _payload_dir


def _discard_payload_dir() -> None:
    """Remove the parked-payload directory and anything left inside it.

    Payload files are reachable only through in-process tokens (TTL
    ``DELTA_TOKEN_TTL``, single use), so at interpreter exit whatever remains is
    garbage by construction and the whole tree can go. Registered with
    :mod:`atexit` so no process that ever issued a delta token leaves an empty
    ``hsync-delta-lan-*`` directory behind; failures are swallowed because a
    cleanup error must never abort shutdown.
    """
    global _payload_dir
    path, _payload_dir = _payload_dir, None
    if path:
        shutil.rmtree(path, ignore_errors=True)


atexit.register(_discard_payload_dir)

# Single-use download tokens: the registry mechanics (TTL, caps, lazy expiry,
# single use) are shared with the server; the TTL is the common SSOT.
_token_store = DeltaTokenStore(
    ttl=DELTA_TOKEN_TTL,
    max_tokens=_MAX_TOKENS,
    tmp_dir=lambda: Path(_get_payload_dir()),
)


def _issue_token_from_file(
    *, bucket: str, relpath: str, payload_file: str, size: int, compressed: bool
) -> str | None:
    """Adopt an already-written payload file as a token (None when full).

    The payload is *moved* into place (same directory, so ``os.replace`` is
    atomic) instead of being written from a bytes buffer, so a multi-GB literal
    payload produced by the matcher never has to sit in RAM. The caller owns
    *payload_file* and must remove it when this returns None.
    """
    try:
        return _token_store.issue_from_file(
            bucket=bucket,
            payload_file=payload_file,
            size=size,
            compressed=compressed,
            meta={"relpath": relpath},
        )
    except TokenStoreFull:
        return None
    except OSError as exc:
        # A vanished/unwritable payload only skips the delta: the caller answers
        # with the full file instead of failing the request.
        log.warning(f"Cannot park delta payload: {exc}")
        return None


def reset_delta_p2p_state() -> None:
    """Drop every token and payload (tests + P2PServer lifecycle hook).

    The payload *directory* is deliberately left in place: it is process-scoped
    and removed at interpreter exit (:func:`_discard_payload_dir`), so clearing
    ``_payload_dir`` here would orphan it for that handler.
    """
    _token_store.reset()


def _delta_disabled(handler) -> bool:
    """Respond 404 (pre-delta peer shape) when the feature is off."""
    if CONFIG.get("DELTA_ENABLED", False):
        return False
    handler._send_error_response(_HTTP_NOT_FOUND, "Not found")
    return True


def _resolve_servable(handler, relpath: str | None) -> str | None:
    """Resolve and gate a peer-supplied relpath for delta operations."""
    if not relpath:
        handler._send_error_response(_HTTP_BAD_REQUEST, "Missing path parameter")
        return None
    filepath = resolve_within(CONFIG.get("LOCAL_BUCKET", ""), relpath)
    if filepath is None:
        handler._send_error_response(_HTTP_FORBIDDEN, "Access denied")
        return None
    if should_skip(filepath):
        handler._send_error_response(_HTTP_FORBIDDEN, "Access denied")
        return None
    if not Path(filepath).is_file():
        handler._send_error_response(_HTTP_NOT_FOUND, "File not found")
        return None
    return filepath


def handle_delta_signatures_get(handler, query: dict) -> None:
    """Serve block signatures of the master's base file (upload delta)."""
    if _delta_disabled(handler):
        return
    filepath = _resolve_servable(handler, query.get("path", [None])[0])
    if filepath is None:
        return
    file_size = Path(filepath).stat().st_size
    block_size = optimal_block_size(file_size)
    sha256sum = _file_sha256(filepath)
    with _delta_compute_sem:
        sigs = compute_signatures(filepath, block_size)
    handler._send_json_response(
        _HTTP_OK,
        delta_signatures_payload(
            block_size=block_size,
            file_size=file_size,
            sha256=sha256sum,
            signatures=sigs,
        ),
    )


def handle_delta_signatures_post(handler) -> None:
    """Match slave base signatures against the master file (download delta)."""
    if _delta_disabled(handler):
        return
    try:
        content_length = int(handler.headers.get("Content-Length", 0))
    except ValueError:
        content_length = 0
    if content_length <= 0 or content_length > _MAX_SIG_BODY_BYTES:
        handler._send_error_response(_HTTP_BAD_REQUEST, "Invalid request body")
        return
    try:
        body = json.loads(handler.rfile.read(content_length))
    except ValueError:
        handler._send_error_response(_HTTP_BAD_REQUEST, "Invalid JSON body")
        return
    if not isinstance(body, dict):
        handler._send_error_response(_HTTP_BAD_REQUEST, "Invalid request body")
        return

    relpath = body.get("path")
    if not isinstance(relpath, str):
        relpath = None
    block_size = body.get("block_size")
    filepath = _resolve_servable(handler, relpath)
    if filepath is None or relpath is None:
        return
    if not isinstance(block_size, int) or block_size <= 0:
        handler._send_error_response(_HTTP_BAD_REQUEST, "Invalid block_size")
        return
    clean_sigs = sanitize_signature_list(body.get("signatures"))
    if clean_sigs is None:
        handler._send_error_response(_HTTP_BAD_REQUEST, "Invalid signatures")
        return
    sigs = [BlockSignature(**s) for s in clean_sigs]

    new_size = Path(filepath).stat().st_size
    new_sha256 = _file_sha256(filepath)

    literal_path = os.path.join(_get_payload_dir(), f"delta_literals_{uuid.uuid4().hex}.bin")

    def _match() -> tuple[list, int, bool]:
        # Literals stream straight to the temp file (never a RAM buffer) and the
        # data-op offsets come from the bounded sink, which reports the true
        # payload size and whether the cap was hit.
        with open(literal_path, "wb") as sink_file:
            sink = BoundedLiteralSink(sink_file, DELTA_MAX_LITERAL_SIZE)
            ops = find_matches_file_to_sink(filepath, sigs, block_size, sink)
        return ops, sink.total, sink.over_limit

    with _delta_compute_sem:
        ops, literal_size, capped = _match()

    if capped:
        os.remove(literal_path)
        handler._send_json_response(
            _HTTP_OK, {"delta": False, "reason": "literal_payload_too_large"}
        )
        return

    base_size = max((s.offset + s.length for s in sigs), default=0)
    ratio = changed_block_ratio(ops, base_size, block_size)
    if not is_delta_worthwhile(ops, base_size, literal_size, block_size):
        # Only the change-ratio branch can trigger here: the literal cap is
        # enforced by the bounded sink while the match runs.
        os.remove(literal_path)
        handler._send_json_response(
            _HTTP_OK,
            {
                "delta": False,
                "reason": "too_many_changed_blocks",
                "changed_percentage": round(ratio, 4),
            },
        )
        return

    compressed = False
    payload_path = literal_path
    if (
        literal_size
        and CONFIG.get("COMPRESSION_ENABLED", False)
        and should_compress(filepath, literal_size, is_p2p=True)
    ):
        # Second pass: the sink must hold raw literals (the data ops reference
        # its offsets), so compression streams the temp file into a new one.
        compressed_path = os.path.join(_get_payload_dir(), f"delta_literals_{uuid.uuid4().hex}.zst")
        try:
            compress_file_to_file(
                literal_path, compressed_path, level=compression_level(is_p2p=True)
            )
        except Exception:
            os.remove(literal_path)
            if os.path.exists(compressed_path):
                os.remove(compressed_path)
            raise
        os.remove(literal_path)
        payload_path = compressed_path
        compressed = True

    bucket = CONFIG.get("REMOTE_BUCKET", "")
    token = _issue_token_from_file(
        bucket=bucket,
        relpath=relpath,
        payload_file=payload_path,
        size=literal_size,
        compressed=compressed,
    )
    if token is None:
        try:
            os.remove(payload_path)
        except OSError:
            pass
        handler._send_error_response(_HTTP_CONFLICT, "Too many active delta tokens")
        return
    handler._send_json_response(
        _HTTP_OK,
        delta_download_payload(
            block_size=block_size,
            new_sha256=new_sha256,
            new_size=new_size,
            ops=ops,
            data_url=rest_delta_data_path(bucket, token),
            data_size=literal_size,
            compressed=compressed,
        ),
    )


def handle_delta_apply(handler, form_fields: dict, literal_path: str) -> None:
    """Apply a slave-computed delta onto the master's base file.

    ``form_fields`` carries the multipart ``path`` and ``metadata`` (JSON)
    fields; ``literal_path`` is the temp file where the streamed literal
    payload was parked by the multipart parser.
    """
    if _delta_disabled(handler):
        return
    filepath = _resolve_servable(handler, form_fields.get("path", ""))
    if filepath is None:
        return

    try:
        meta = json.loads(form_fields.get("metadata", ""))
    except ValueError:
        handler._send_error_response(_HTTP_BAD_REQUEST, "Invalid metadata JSON")
        return
    if not isinstance(meta, dict):
        handler._send_error_response(_HTTP_BAD_REQUEST, "Invalid metadata")
        return

    base_sha256 = meta.get("base_sha256")
    new_sha256 = meta.get("new_sha256")
    new_size = meta.get("new_size")
    if (
        not is_sha256_hex(base_sha256)
        or not is_sha256_hex(new_sha256)
        or not isinstance(new_size, int)
        or new_size < 0
    ):
        handler._send_error_response(_HTTP_BAD_REQUEST, "Invalid metadata fields")
        return
    ops = parse_op_payload(meta.get("ops"))
    if ops is None:
        handler._send_error_response(_HTTP_BAD_REQUEST, "Invalid ops")
        return

    # Race check: the master's base must still be what the slave saw.
    current_base = _file_sha256(filepath)
    if current_base != base_sha256:
        handler._send_json_response(
            _HTTP_CONFLICT,
            {
                "detail": "base_file_changed",
                "expected_base_sha256": base_sha256,
                "actual_base_sha256": current_base,
            },
        )
        return

    # Decompress a zstd literal payload (anti-bomb capped).
    literal_file = literal_path
    decompressed_path: str | None = None
    if meta.get("compressed") is True:
        decompressed_path = f"{literal_path}.decompressed"
        try:
            with open(literal_path, "rb") as src, open(decompressed_path, "wb") as out:
                for chunk in decompress_stream(src, DELTA_MAX_LITERAL_SIZE):
                    out.write(chunk)
        except (zstd.ZstdError, ValueError) as exc:
            handler._send_error_response(_HTTP_BAD_REQUEST, f"Invalid compressed payload: {exc}")
            return
        literal_file = decompressed_path

    # Validate ops against real bounds before touching anything.
    base_size = Path(filepath).stat().st_size
    literal_size = Path(literal_file).stat().st_size
    invalid = validate_op_bounds(
        ops,
        base_size=base_size,
        literal_size=literal_size,
        new_size=new_size,
    )
    if invalid is not None:
        handler._send_json_response(_HTTP_UNPROCESSABLE, {"detail": invalid})
        return

    part_path = f"{filepath}{PARTIAL_DELTA_SUFFIX}"
    # Cleanup tracker: cleared once the rebuilt file has been renamed over the
    # base, so the finally block removes only genuine leftovers.
    cleanup_path: str | None = part_path
    try:
        with _delta_compute_sem, open(literal_file, "rb") as literal_stream:
            try:
                rebuilt = reconstruct_and_verify(
                    filepath,
                    ops,
                    literal_stream,
                    part_path,
                    expected_sha256=new_sha256,
                    expected_size=new_size,
                )
            except ReconstructionMismatch as mismatch:
                details = dict(mismatch.details)
                reason = str(details.pop("reason", MISMATCH_HASH))
                handler._send_json_response(_HTTP_UNPROCESSABLE, {"detail": reason, **details})
                return

        cid = meta.get("cid", "")
        if cid:
            from hsync_common.models.event import is_valid_cid

            from hsync_client.p2p_websocket import mark_inbound_origin

            if is_valid_cid(cid) and CONFIG.get("P2P_KEY"):
                mark_inbound_origin(filepath, cid)

        os.replace(part_path, filepath)
        cleanup_path = None

        mtime = meta.get("mtime")
        if mtime:
            try:
                mtime_val = min(float(mtime), time.time() + MTIME_MAX_FUTURE_SKEW_SECONDS)
                os.utime(filepath, (mtime_val, mtime_val))
            except (ValueError, OSError):
                pass
        permissions = meta.get("permissions")
        if permissions:
            from hsync_client.utils import set_permissions

            set_permissions(filepath, permissions)

        # The rebuilt file is committed: publish the terminal transfer (the file
        # the peer reports as an upload, received here as a download) so the
        # Desktop closes the row and counts it as received.
        emit_completed_transfer("download", filepath, new_size)

        handler._send_json_response(_HTTP_OK, {"status": "ok", "sha256": rebuilt, "size": new_size})
    finally:
        for leftover in (cleanup_path, decompressed_path):
            if leftover and Path(leftover).exists():
                try:
                    os.remove(leftover)
                except OSError:
                    pass


def handle_delta_data(handler, bucket: str, token: str) -> None:
    """Stream the parked literal payload for a valid download token."""
    if _delta_disabled(handler):
        return
    _token_store.cleanup()
    entry = _token_store.lookup(token)
    if entry is None:
        handler._send_json_response(_HTTP_NOT_FOUND, {"detail": "token_not_found"})
        return
    if _token_store.is_expired(entry):
        _token_store.discard(token)
        handler._send_json_response(_HTTP_GONE, {"detail": "token_expired"})
        return
    # The token is bound to the bucket it was issued for.
    if entry.bucket != bucket:
        handler._send_json_response(_HTTP_NOT_FOUND, {"detail": "token_not_found"})
        return

    # Single-use: the entry leaves the registry before streaming starts.
    _token_store.consume(token)
    payload_file = entry.payload_file
    if not Path(payload_file).is_file():
        handler._send_json_response(_HTTP_GONE, {"detail": "token_expired"})
        return

    try:
        payload_size = Path(payload_file).stat().st_size
        # The token carries the file it was issued for: the payload is that
        # file's wire representation, so the serving side can show it as the
        # file's upload progress. An unresolvable relpath (or a sentinel) just
        # streams without reporting.
        relpath = str(entry.meta.get("relpath", "") or "")
        served_path = str(Path(CONFIG.get("LOCAL_BUCKET", "")) / relpath) if relpath else ""
        reporter = progress_reporter(served_path, payload_size, "upload") if served_path else None
        handler.send_response(_HTTP_OK)
        handler.send_header("Content-Type", CONTENT_TYPE_OCTET_STREAM)
        handler.send_header("Content-Length", str(payload_size))
        handler.send_header("Content-Disposition", 'attachment; filename="delta-data.bin"')
        if entry.compressed:
            handler.send_header("Content-Encoding", CONTENT_ENCODING_ZSTD)
        handler.end_headers()
        sent = 0
        with open(payload_file, "rb") as f:
            while chunk := f.read(64 * 1024):
                handler.wfile.write(chunk)
                sent += len(chunk)
                if reporter is not None:
                    reporter.update(sent)
        # Payload fully served: the peer can rebuild the file, so publish the
        # logical transfer — the file size, like the full-file serve — which is
        # what closes the Desktop row and counts the file as sent.
        if served_path:
            try:
                served_size = Path(served_path).stat().st_size
            except OSError:
                served_size = 0
            emit_completed_transfer("upload", served_path, served_size)
    finally:
        try:
            os.remove(payload_file)
        except OSError:
            pass
