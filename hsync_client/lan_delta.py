# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Delta transfer handlers for the stdlib LAN master server.

The LAN embedded server (``hsync_client.lan_server``) cannot reuse the FastAPI
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

import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import zstandard as zstd
from hsync_common.constants import (
    CONTENT_TYPE_OCTET_STREAM,
    DELTA_MAX_LITERAL_SIZE,
    PARTIAL_DELTA_SUFFIX,
)
from hsync_common.delta import (
    changed_block_ratio,
    compute_signatures,
    find_matches_file,
    is_delta_worthwhile,
    optimal_block_size,
)
from hsync_common.hashing import compute_sha256, is_sha256_hex
from hsync_common.ignore import should_skip
from hsync_common.models.delta import (
    BlockSignature,
    DeltaOp,
    sanitize_op_list,
    sanitize_signature_list,
)
from hsync_common.models.file import File
from hsync_common.path_validation import resolve_within
from loguru import logger as log

from hsync_client.compression import (
    CONTENT_ENCODING_ZSTD,
    compress_data,
    compression_level,
    decompress_stream,
    should_compress,
)
from hsync_client.config import CONFIG

# HTTP status codes used here (kept local to avoid a lan_server import cycle).
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

# Token TTL (seconds) and the cap on concurrently live tokens.
_TOKEN_TTL = 300
_MAX_TOKENS = 100


def _file_sha256(filepath: str) -> str:
    """Return the cached SHA-256 for *filepath*, recomputing it on a miss."""
    file_obj = File.get(filepath)
    return (file_obj.sha256sum if file_obj else None) or compute_sha256(filepath)


@dataclass
class _LanToken:
    bucket: str
    relpath: str
    payload_file: str
    compressed: bool
    created: float


_tokens: dict[str, _LanToken] = {}
_tokens_lock = threading.Lock()

# Process-wide temp dir for parked payloads (outside LOCAL_BUCKET on purpose).
_payload_dir: str | None = None


def _get_payload_dir() -> str:
    """Return (creating once) the temp dir holding literal payloads."""
    global _payload_dir
    if _payload_dir is None:
        _payload_dir = tempfile.mkdtemp(prefix="hsync-delta-lan-")
    return _payload_dir


def _cleanup_tokens(now: float | None = None) -> None:
    """Drop expired tokens and their payload files (lazy reaper)."""
    cutoff = (now if now is not None else time.time()) - _TOKEN_TTL
    with _tokens_lock:
        expired = [t for t, e in _tokens.items() if e.created < cutoff]
        for token in expired:
            entry = _tokens.pop(token)
            try:
                os.remove(entry.payload_file)
            except OSError:
                pass


def _issue_token(*, bucket: str, relpath: str, payload: bytes, compressed: bool) -> str | None:
    """Park *payload* on disk and return its download token (None when full)."""
    _cleanup_tokens()
    token = uuid.uuid4().hex
    payload_file = os.path.join(_get_payload_dir(), f"delta_{token}.bin")
    try:
        with open(payload_file, "wb") as f:
            f.write(payload)
    except OSError as exc:
        log.warning(f"Cannot park delta payload: {exc}")
        return None
    with _tokens_lock:
        if len(_tokens) >= _MAX_TOKENS:
            os.remove(payload_file)
            return None
        _tokens[token] = _LanToken(
            bucket=bucket,
            relpath=relpath,
            payload_file=payload_file,
            compressed=compressed,
            created=time.time(),
        )
    return token


def reset_delta_lan_state() -> None:
    """Drop every token and payload (tests + LANServer lifecycle hook)."""
    with _tokens_lock:
        entries = list(_tokens.values())
        _tokens.clear()
    for entry in entries:
        try:
            os.remove(entry.payload_file)
        except OSError:
            pass


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
        {
            "block_size": block_size,
            "file_size": file_size,
            "sha256": sha256sum,
            "signatures": [s.to_dict() for s in sigs],
        },
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

    with _delta_compute_sem:
        ops, literals = find_matches_file(filepath, sigs, block_size)

    base_size = max((s.offset + s.length for s in sigs), default=0)
    ratio = changed_block_ratio(ops, base_size, block_size)
    if not is_delta_worthwhile(ops, base_size, len(literals), block_size):
        reason = (
            "literal_payload_too_large"
            if len(literals) > DELTA_MAX_LITERAL_SIZE
            else "too_many_changed_blocks"
        )
        handler._send_json_response(
            _HTTP_OK, {"delta": False, "reason": reason, "changed_percentage": round(ratio, 4)}
        )
        return

    compressed = False
    payload = literals
    if (
        literals
        and CONFIG.get("COMPRESSION_ENABLED", False)
        and should_compress(filepath, len(literals), is_lan=True)
    ):
        payload = compress_data(literals, level=compression_level(is_lan=True))
        compressed = True

    bucket = CONFIG.get("REMOTE_BUCKET", "")
    token = _issue_token(bucket=bucket, relpath=relpath, payload=payload, compressed=compressed)
    if token is None:
        handler._send_error_response(_HTTP_CONFLICT, "Too many active delta tokens")
        return
    handler._send_json_response(
        _HTTP_OK,
        {
            "delta": True,
            "block_size": block_size,
            "new_sha256": new_sha256,
            "instructions": {
                "new_size": new_size,
                "ops": [op.to_dict() for op in ops],
            },
            "data_url": f"/api/v1/buckets/{bucket}/files/delta/data/{token}",
            "data_size": len(literals),
            "compressed": compressed,
        },
    )


def handle_delta_apply(handler, form_fields: dict, literal_path: str) -> None:
    """Apply a slave-computed delta onto the master's base file.

    ``form_fields`` carries the multipart ``path`` and ``metadata`` (JSON)
    fields; ``literal_path`` is the temp file where the streamed literal
    payload was parked by the multipart parser.
    """
    if _delta_disabled(handler):
        return
    relpath = form_fields.get("path", "")
    filepath = resolve_within(CONFIG.get("LOCAL_BUCKET", ""), relpath)
    if filepath is None or should_skip(filepath):
        handler._send_error_response(_HTTP_FORBIDDEN, "Access denied")
        return
    if not Path(filepath).is_file():
        handler._send_error_response(_HTTP_NOT_FOUND, "File not found")
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
    clean_ops = sanitize_op_list(meta.get("ops"))
    if clean_ops is None:
        handler._send_error_response(_HTTP_BAD_REQUEST, "Invalid ops")
        return
    ops = [
        DeltaOp(
            type=o["type"], offset=o["offset"], length=o["length"], data_offset=o["data_offset"]
        )
        for o in clean_ops
    ]

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
    total = 0
    invalid: str | None = None
    for op in ops:
        if op.type == "copy" and op.offset + op.length > base_size:
            invalid = "copy_op_out_of_bounds"
            break
        if op.type == "data" and op.data_offset + op.length > literal_size:
            invalid = "data_op_out_of_bounds"
            break
        total += op.length
    if invalid is None and total != new_size:
        invalid = "ops_coverage_mismatch"
    if invalid is not None:
        handler._send_json_response(_HTTP_UNPROCESSABLE, {"detail": invalid})
        return

    part_path = f"{filepath}{PARTIAL_DELTA_SUFFIX}"
    # Cleanup tracker: cleared once the rebuilt file has been renamed over the
    # base, so the finally block removes only genuine leftovers.
    cleanup_path: str | None = part_path
    try:
        with _delta_compute_sem, open(literal_file, "rb") as literal_stream:
            from hsync_common.delta import apply_delta

            apply_delta(filepath, ops, literal_stream, part_path)
        rebuilt = compute_sha256(part_path)
        if rebuilt != new_sha256:
            handler._send_json_response(
                _HTTP_UNPROCESSABLE,
                {
                    "detail": "reconstructed_file_hash_mismatch",
                    "expected_sha256": new_sha256,
                    "actual_sha256": rebuilt,
                },
            )
            return
        if Path(part_path).stat().st_size != new_size:
            handler._send_json_response(
                _HTTP_UNPROCESSABLE, {"detail": "reconstructed_file_size_mismatch"}
            )
            return

        cid = meta.get("cid", "")
        if cid:
            from hsync_common.models.event import is_valid_cid

            from hsync_client.lan_websocket import mark_inbound_origin

            if is_valid_cid(cid) and CONFIG.get("LAN_KEY"):
                mark_inbound_origin(filepath, cid)

        os.replace(part_path, filepath)
        cleanup_path = None

        mtime = meta.get("mtime")
        if mtime:
            try:
                mtime_val = min(float(mtime), time.time() + 300)
                os.utime(filepath, (mtime_val, mtime_val))
            except (ValueError, OSError):
                pass
        permissions = meta.get("permissions")
        if permissions:
            from hsync_client.utils import set_permissions

            set_permissions(filepath, permissions)

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
    _cleanup_tokens()
    with _tokens_lock:
        entry = _tokens.get(token)
    if entry is None:
        handler._send_json_response(_HTTP_NOT_FOUND, {"detail": "token_not_found"})
        return
    if entry.created < time.time() - _TOKEN_TTL:
        with _tokens_lock:
            _tokens.pop(token, None)
        handler._send_json_response(_HTTP_GONE, {"detail": "token_expired"})
        return
    # The token is bound to the bucket it was issued for.
    if entry.bucket != bucket:
        handler._send_json_response(_HTTP_NOT_FOUND, {"detail": "token_not_found"})
        return

    # Single-use: the entry leaves the registry before streaming starts.
    with _tokens_lock:
        _tokens.pop(token, None)
    payload_file = entry.payload_file
    if not Path(payload_file).is_file():
        handler._send_json_response(_HTTP_GONE, {"detail": "token_expired"})
        return

    try:
        payload_size = Path(payload_file).stat().st_size
        handler.send_response(_HTTP_OK)
        handler.send_header("Content-Type", CONTENT_TYPE_OCTET_STREAM)
        handler.send_header("Content-Length", str(payload_size))
        handler.send_header("Content-Disposition", 'attachment; filename="delta-data.bin"')
        if entry.compressed:
            handler.send_header("Content-Encoding", CONTENT_ENCODING_ZSTD)
        handler.end_headers()
        with open(payload_file, "rb") as f:
            while chunk := f.read(64 * 1024):
                handler.wfile.write(chunk)
    finally:
        try:
            os.remove(payload_file)
        except OSError:
            pass
