# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Embedded HTTP Server for P2P Master Mode.

This module provides a lightweight HTTP server that runs when a client
becomes the master in P2P mode. It exposes the essential hsync API
endpoints so that other peers can sync with the master.

Endpoints:
- GET    /api/v1/health                          - Health check
- GET    /api/v1/buckets/{bucket}/files          - List files
- GET    /api/v1/buckets/{bucket}/files/download - Download file
- POST   /api/v1/buckets/{bucket}/files          - Upload file
- DELETE /api/v1/buckets/{bucket}/files          - Delete file (bidirectional sync)
- PATCH  /api/v1/buckets/{bucket}/files          - Rename file/dir (bidirectional sync)
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import hmac
import json
import os
import socket
import ssl
import stat as stat_module
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from enum import StrEnum
from http import HTTPStatus
from os import walk
from pathlib import Path
from shutil import disk_usage
from typing import IO, TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import zstandard as zstd
from aiohttp import web
from hsync_common.clock import clamp_incoming_corrected_mtime, uncorrect_mtime
from hsync_common.compression import compression_level
from hsync_common.constants import (
    CONTENT_TYPE_JSON,
    CONTENT_TYPE_OCTET_STREAM,
    HEADER_CHECKSUM_SHA256,
    MTIME_PROPAGATION_TOLERANCE_SECONDS,
    PARTIAL_DOWNLOAD_SUFFIX,
)
from hsync_common.hashing import compute_sha256, is_sha256_hex
from hsync_common.models.dir import Dir, is_hidden_dir
from hsync_common.models.event import is_valid_cid
from hsync_common.models.file import File
from hsync_common.models.tombstone import TOMBSTONE_MAX_ENTRIES
from hsync_common.path_validation import resolve_within, validate_remote_relpath
from hsync_common.protocol.p2p import P2P_FILES_SOURCES_SUFFIX, P2P_WS_EVENTS_PATH
from hsync_common.protocol.peer_download import (
    P2P_SOURCES_MAX_BODY_BYTES,
    build_p2p_sources_response,
    validate_p2p_sources_request,
)
from hsync_common.protocol.rest import (
    REST_BUCKETS_PREFIX,
    REST_DELTA_APPLY_SUFFIX,
    REST_DELTA_DATA_SUFFIX,
    REST_DELTA_SIGNATURES_SUFFIX,
    REST_DIRS_SUFFIX,
    REST_FILES_DOWNLOAD_SUFFIX,
    REST_FILES_PERMISSIONS_SUFFIX,
    REST_FILES_SUFFIX,
    REST_FILES_UPLOAD_STATUS_SUFFIX,
    REST_SYMLINKS_SUFFIX,
    REST_TOMBSTONES_SUFFIX,
)
from hsync_common.text import compact_json_bytes
from loguru import logger as log

from hsync_client.arbitration import conflicted_copy_name
from hsync_client.compression import (
    CONTENT_ENCODING_ZSTD,
    should_compress,
)
from hsync_client.config import CONFIG
from hsync_client.constants import HSYNCKEEP_FILENAME, MTIME_MAX_FUTURE_SKEW_SECONDS, ActionLabel
from hsync_client.multipart import (
    _MULTIPART_BOUNDARY_RE,
    _CappedBytesWriter,
    _StreamingMultipartParser,
)
from hsync_client.utils import cid_suffix, inbound_size_cap, open_partial_no_follow

if TYPE_CHECKING:
    from hsync_client.p2p_inventory import P2PInventoryStore
    from hsync_client.p2p_sources import P2PSourceService
    from hsync_client.p2p_websocket import SlaveCidRegistry

# =============================================================================
# Constants
# =============================================================================

# Maximum number of concurrent request threads the P2P master will spawn.
# Bounds resource consumption against a flood of peer connections. Sized
# ABOVE the sum of realistic per-slave connection pools: with HTTP/1.1
# keep-alive every connection — idle ones included — holds its thread and its
# semaphore slot for the whole connection lifetime, so the cap must
# accommodate the transfer workers of all peers plus headroom, not just
# in-flight requests.
P2P_SERVER_MAX_THREADS = 64

# Delay a saturated seeder asks a requester to wait (``Retry-After`` on the 503).
# It only has to cover the read that occupies a slot; the requester honours it as
# a scheduling deferral instead of a health failure (ADR-0030).
SEEDER_RETRY_AFTER_S = 0.25

# Seconds of socket-read silence after which an idle keep-alive connection is
# reaped. With HTTP/1.1 a connection is held open between requests, and a worker
# slot is occupied only while a request is actually being served: without this
# reap timeout, connections parked idle in the clients' pools would linger
# forever instead of being released.
# 300 s is deliberately large: the reap must NEVER fire on a connection that
# is merely throttled. A maximally-throttled upload (``MAX_BANDWIDTH_UPLOAD``
# at the 1 KB/s CLI floor) can legitimately sit silent between two 256 KiB
# chunk writes for ~262 s, so anything below that would kill a live transfer;
# it stays comfortably above the client HTTP timeout (30 s default) too.
P2P_HANDLER_IDLE_TIMEOUT_S = 300

# Chunk size (bytes) for streaming file reads on the download path. Mirrors
# the client transfer module so the master never reads more than necessary
# into RAM per iteration.
_P2P_UPLOAD_CHUNK_SIZE = 256 * 1024

# Cap (bytes) on the file size the compressed download branch accepts. The
# branch stages a bounded zstd frame in a temporary file before sending any
# headers; larger files use identity streaming so resume stays available and
# temporary-disk use remains bounded.
P2P_COMPRESSION_MAX_FILE_BYTES = 256 * 1024 * 1024


class P2PServerMode(StrEnum):
    """Route policy for the embedded P2P listener."""

    master = "master"
    seeder = "seeder"


def _stat_identity(stat_result: os.stat_result) -> tuple[int, int, int, int, int]:
    """Return the identity tuple shared by :func:`_same_open_file` and the cache."""
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
    )


def _same_open_file(before: os.stat_result, after: os.stat_result) -> bool:
    """Return whether an opened regular file retained identity and content metadata."""
    return _stat_identity(before) == _stat_identity(after)


class _ChecksumCache:
    """Bounded LRU of served-file checksums keyed by stat identity.

    A download would otherwise re-hash the whole file before serving it, costing
    a full extra disk pass per request on top of the (optional) compressed-staging
    read. The cache keys a checksum on the same ``(dev, ino, size, mtime_ns,
    ctime_ns)`` identity the engine already trusts for mid-serve consistency
    checks, so a served-unchanged file repeats its checksum for free and any
    observed content change necessarily evicts the stale entry. The trade-off
    is identical to the existing model: content rewritten without any stat
    field changing cannot be detected by the engine either.
    """

    def __init__(self, maxsize: int = 512) -> None:
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[int, int, int, int, int], str] = OrderedDict()

    def get(self, identity: tuple[int, int, int, int, int]) -> str | None:
        with self._lock:
            checksum = self._entries.get(identity)
            if checksum is not None:
                self._entries.move_to_end(identity)
            return checksum

    def put(self, identity: tuple[int, int, int, int, int], checksum: str) -> None:
        with self._lock:
            self._entries[identity] = checksum
            self._entries.move_to_end(identity)
            while len(self._entries) > self._maxsize:
                self._entries.popitem(last=False)


_CHECKSUM_CACHE = _ChecksumCache()


@contextmanager
def _open_regular_beneath(bucket: str, relpath: str) -> Iterator[tuple[IO[bytes], os.stat_result]]:
    """Open one regular file beneath *bucket* without following symlink components."""
    normalized = validate_remote_relpath(relpath)
    if not bucket or normalized is None:
        raise PermissionError("unsafe file path")
    root = Path(bucket).resolve(strict=True)
    parts = Path(normalized).parts
    if not parts:
        raise PermissionError("unsafe file path")

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if nofollow and directory and os.open in os.supports_dir_fd:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        dir_fd = os.open(root, flags | directory)
        try:
            try:
                for component in parts[:-1]:
                    next_fd = os.open(component, flags | directory | nofollow, dir_fd=dir_fd)
                    os.close(dir_fd)
                    dir_fd = next_fd
                file_fd = os.open(parts[-1], flags | nofollow, dir_fd=dir_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise PermissionError("symlink components are not served") from exc
                raise
        finally:
            os.close(dir_fd)
    else:  # pragma: no cover - fallback for platforms without openat/O_NOFOLLOW
        confined = resolve_within(str(root), normalized)
        if confined is None:
            raise PermissionError("unsafe file path")
        before = os.lstat(confined)
        if stat_module.S_ISLNK(before.st_mode) or not stat_module.S_ISREG(before.st_mode):
            raise PermissionError("not a regular file")
        file_fd = os.open(confined, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        opened = os.fstat(file_fd)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            os.close(file_fd)
            raise PermissionError("file identity changed while opening")

    stream = os.fdopen(file_fd, "rb")
    try:
        opened = os.fstat(stream.fileno())
        if not stat_module.S_ISREG(opened.st_mode):
            raise PermissionError("not a regular file")
        yield stream, opened
    finally:
        stream.close()


def _hash_open_file(stream: IO[bytes]) -> str:
    """Hash an already-open descriptor and rewind it for serving."""
    digest = hashlib.sha256()
    while chunk := stream.read(_P2P_UPLOAD_CHUNK_SIZE):
        digest.update(chunk)
    stream.seek(0)
    return digest.hexdigest()


def _stage_compressed_body(
    stream: IO[bytes],
    opened_stat: os.stat_result,
    expected_sha256: str,
) -> IO[bytes] | None:
    """Stream one stable open-file snapshot into a bounded temporary zstd frame."""
    staged = tempfile.TemporaryFile(mode="w+b")
    digest = hashlib.sha256()
    total = 0
    try:
        stream.seek(0)
        compressor = zstd.ZstdCompressor(level=compression_level(is_p2p=True))
        with compressor.stream_writer(staged, closefd=False) as writer:
            while chunk := stream.read(_P2P_UPLOAD_CHUNK_SIZE):
                digest.update(chunk)
                total += len(chunk)
                writer.write(chunk)
        if (
            total != opened_stat.st_size
            or not _same_open_file(opened_stat, os.fstat(stream.fileno()))
            or not hmac.compare_digest(digest.hexdigest(), expected_sha256)
        ):
            staged.close()
            return None
        staged.flush()
        staged.seek(0)
        return staged
    except Exception:
        staged.close()
        raise


def _extract_multipart_boundary(content_type: str) -> bytes | None:
    """Return the multipart boundary bytes from a Content-Type header, or None.

    Strips the surrounding double quotes some clients add around the boundary
    value. Shared by the upload and delta-apply handlers.
    """
    match = _MULTIPART_BOUNDARY_RE.search(content_type)
    if not match:
        return None
    boundary = match.group(1).encode()
    if boundary.startswith(b'"') and boundary.endswith(b'"'):
        boundary = boundary[1:-1]
    return boundary


class _ServingContext:
    """Immutable per-request view of what the listener is currently serving.

    ``P2PServer`` swaps this atomically on a role transition; the endpoint
    mixin reads ``mode``/``bucket``/``peer_read_slots``/``cid_guard``/
    ``source_service``/``inventory_store`` through it. ``mode is None`` means
    the listener is up but not serving (yielded master): every request is
    refused with 503 before any endpoint logic runs (ADR-0027).
    """

    __slots__ = (
        "mode",
        "bucket",
        "peer_read_slots",
        "cid_guard",
        "source_service",
        "inventory_store",
    )

    def __init__(
        self,
        *,
        mode: P2PServerMode | None,
        bucket: str,
        peer_read_slots: threading.BoundedSemaphore | None,
        cid_guard: SlaveCidRegistry | None,
        source_service: P2PSourceService | None,
        inventory_store: P2PInventoryStore | None,
    ) -> None:
        self.mode = mode
        self.bucket = bucket
        self.peer_read_slots = peer_read_slots
        self.cid_guard = cid_guard
        self.source_service = source_service
        self.inventory_store = inventory_store


# API Configuration
API_VERSION = "v1"
API_PREFIX = f"/api/{API_VERSION}"

# API endpoint paths
ENDPOINT_HEALTH = f"{API_PREFIX}/health"
ENDPOINT_TIME = f"{API_PREFIX}/time"
# API endpoint paths. The peer-download suffixes are the shared SSOT
# (``hsync_common.protocol.p2p``); the local names stay for readability.
ENDPOINT_BUCKETS_PREFIX = REST_BUCKETS_PREFIX
ENDPOINT_FILES_SUFFIX = REST_FILES_SUFFIX
ENDPOINT_FILES_DOWNLOAD_SUFFIX = REST_FILES_DOWNLOAD_SUFFIX
ENDPOINT_FILES_UPLOAD_STATUS_SUFFIX = REST_FILES_UPLOAD_STATUS_SUFFIX
ENDPOINT_FILES_PERMISSIONS_SUFFIX = REST_FILES_PERMISSIONS_SUFFIX
ENDPOINT_FILES_SOURCES_SUFFIX = P2P_FILES_SOURCES_SUFFIX
ENDPOINT_DIRS_SUFFIX = REST_DIRS_SUFFIX
ENDPOINT_TOMBSTONES_SUFFIX = REST_TOMBSTONES_SUFFIX
ENDPOINT_SYMLINKS_SUFFIX = REST_SYMLINKS_SUFFIX
ENDPOINT_DELTA_SIGNATURES_SUFFIX = REST_DELTA_SIGNATURES_SUFFIX
ENDPOINT_DELTA_APPLY_SUFFIX = REST_DELTA_APPLY_SUFFIX
ENDPOINT_DELTA_DATA_MARKER = REST_DELTA_DATA_SUFFIX


def _parse_request_query(request_path: str) -> dict[str, list[str]]:
    """Parse a request-line query preserving blank values.

    ``keep_blank_values=True`` keeps the Python master and the Android mirror
    (``parseQueryParamLists``) in agreement: ``k=`` yields ``k -> ""`` on both
    instead of silently disappearing. Handlers read a missing key through
    ``query.get(key, [None])[0]``, so a blank is validated like any other value.
    """
    return parse_qs(urlparse(request_path).query, keep_blank_values=True)


# HTTP Status Codes
HTTP_OK = 200
HTTP_CREATED = 201
HTTP_ACCEPTED = 202
HTTP_PARTIAL_CONTENT = 206
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_METHOD_NOT_ALLOWED = 405
HTTP_CONFLICT = 409
HTTP_CONTENT_TOO_LARGE = 413
HTTP_RANGE_NOT_SATISFIABLE = 416
HTTP_INTERNAL_ERROR = 500
HTTP_SERVICE_UNAVAILABLE = 503
HTTP_INSUFFICIENT_STORAGE = 507

# Cap on tombstone-gossip POST bodies: the store itself is capped at
# TOMBSTONE_MAX_ENTRIES entries (at most ~165 bytes of JSON each with a
# content hash), so a larger payload can never be legitimate and would only
# waste memory.
MAX_TOMBSTONE_PAYLOAD_BYTES = TOMBSTONE_MAX_ENTRIES * 192


# Request Handler
# =============================================================================

if TYPE_CHECKING:
    # At type-check time the mixin is treated as a BaseHTTPRequestHandler so the
    # transport surface (``wfile``/``rfile``/``send_response``/...) is known to
    # mypy; at runtime it is a plain mixin so the concrete transport (the aiohttp
    # adapter) supplies its own surface.
    from http.server import BaseHTTPRequestHandler as _HTTPTransportBase

    from hsync_client.p2p_websocket import P2PWebSocketServer
else:  # pragma: no cover - runtime shim, never instantiated on its own
    _HTTPTransportBase = object


# Canonical realtime-events WebSocket endpoint (SSOT in
# ``hsync_common.protocol.p2p``), served by the unified listener on the same TLS
# port as the REST data plane.
WS_EVENTS_PATH = P2P_WS_EVENTS_PATH


class P2PEndpointMixin(_HTTPTransportBase):
    """Transport-agnostic P2P endpoint logic.

    Every endpoint handler (and the request dispatchers ``do_GET``/``do_POST``/
    ``do_DELETE``/``do_PATCH``/``do_PUT``/``do_HEAD``) lives here and depends
    only on the low-level HTTP surface supplied by the concrete transport:
    ``headers``, ``rfile``/``wfile``, ``send_response``/``send_header``/
    ``end_headers``, ``client_address``, ``close_connection`` and a ``server``
    object exposing ``mode``/``bucket``/``peer_read_slots``/``cid_guard``/
    ``source_service``/``inventory_store``. This is what lets the data plane run
    unchanged on the unified aiohttp listener.
    """

    # Reference to the serving context for accessing the active role config
    server: _ServingContext  # type: ignore[assignment]

    # HTTP/1.1 keep-alive: connections are reused across requests instead of
    # paying a TCP+TLS handshake per request (the dominant per-file cost for
    # many-small-files P2P syncs). Every response path MUST frame its body
    # with an accurate Content-Length (or close the connection) to stay
    # persistent-connection compliant — _send_json_response always sets it.
    # Idle connections are reaped by ``timeout`` below.
    protocol_version = "HTTP/1.1"

    # TCP_NODELAY on every accepted connection: with keep-alive the response
    # body is a second small write on a socket whose header segment is not
    # yet ACKed, so Nagle would park it behind the peer's delayed ACK — a
    # ~40 ms stall on EVERY small response (measured by
    # scripts/dev/benchmark_p2p_transfers.py: 24 req/s with Nagle vs hundreds
    # without). HTTP/1.0's close-per-request masked this because each request
    # started on a fresh socket with no unacknowledged data.
    disable_nagle_algorithm = True

    # Seconds of read-silence after which an idle keep-alive connection is
    # closed (see P2P_HANDLER_IDLE_TIMEOUT_S).
    timeout = P2P_HANDLER_IDLE_TIMEOUT_S

    def log_message(self, format: str, *args) -> None:
        """Override to use our logger instead of stderr."""
        log.debug(f"{self.address_string()} - {format % args}")

    def log_request(self, code: int | HTTPStatus | str = "-", size: int | str = "-") -> None:
        """Log one completed request line at DEBUG level.

        Mirrors ``BaseHTTPRequestHandler.log_request`` so every transport
        reports the request line (method, full path and query string) plus its
        status identically. A transport whose ``send_response`` cannot inherit
        the stdlib hook (the aiohttp bridge) calls this explicitly.
        """
        if isinstance(code, HTTPStatus):
            code = code.value
        self.log_message('"%s" %s %s', self.requestline, str(code), str(size))

    @staticmethod
    def _encode_json_response(data: dict | list) -> bytes:
        return compact_json_bytes(data)

    def _send_json_response(
        self,
        status: int,
        data: dict | list,
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        """Send an exactly framed compact JSON response."""
        body = self._encode_json_response(data)
        self.send_response(status)
        self.send_header("Content-Type", CONTENT_TYPE_JSON)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_error_response(
        self,
        status: int,
        message: str,
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        """Send an error response."""
        self._send_json_response(status, {"error": message}, extra_headers=extra_headers)

    def _send_error_response_safe(self, status: int, message: str) -> None:
        """Send an error response, silently swallowing socket/SSL failures.

        Used when the peer may have already closed the connection (e.g. a
        client read timeout during a large upload); without this guard the
        write would raise ``SSLEOFError``/``BAD_LENGTH`` and pollute the log
        with a secondary cascading error.
        """
        import ssl

        try:
            self._send_error_response(status, message)
        except (ssl.SSLError, OSError, BrokenPipeError, ConnectionError) as e:
            log.debug(f"Suppressed error response on closed socket: {e}")

    def _require_bucket(self) -> str | None:
        """Return the configured local bucket, or send 404 and return None.

        Every bucket-scoped endpoint repeats the same "is a local bucket
        configured?" gate; this helper is its single definition.
        """
        local_bucket = CONFIG.get("LOCAL_BUCKET", "")
        if not local_bucket:
            self._send_error_response(HTTP_NOT_FOUND, "Bucket not configured")
            return None
        return local_bucket

    def _check_auth(self) -> bool:
        """Check authentication if P2P_KEY is configured."""
        p2p_key = CONFIG.get("P2P_KEY")
        if not p2p_key:
            # No authentication required
            return True

        # Check Authorization header. Use a constant-time comparison so the
        # validity of the bearer token cannot be inferred from response timing.
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            if hmac.compare_digest(token, str(p2p_key)):
                return True

        if self.headers.get("Content-Length") or self.headers.get("Transfer-Encoding"):
            self._reject_unread_body(HTTP_UNAUTHORIZED, "Unauthorized")
        else:
            self._send_error_response(HTTP_UNAUTHORIZED, "Unauthorized")
        return False

    def _check_mutation_auth(self) -> bool:
        """Authorize a mutating endpoint under the explicit P2P write policy."""
        if CONFIG.get("P2P_KEY"):
            return self._check_auth()
        if CONFIG.get("P2P_ALLOW_UNAUTHENTICATED_WRITES", False):
            return True
        if self.headers.get("Content-Length") or self.headers.get("Transfer-Encoding"):
            self._reject_unread_body(
                HTTP_FORBIDDEN,
                "Unauthenticated P2P writes are disabled",
            )
        else:
            self._send_error_response(HTTP_FORBIDDEN, "Unauthenticated P2P writes are disabled")
        return False

    def _is_seeder(self) -> bool:
        return self.server.mode is P2PServerMode.seeder

    def _send_method_not_allowed(self) -> None:
        # Unsupported seeder requests are never parsed or drained. Closing the
        # connection prevents an unread mutation body becoming a second request.
        self.close_connection = True
        body = json.dumps({"error": "Method not allowed"}, separators=(",", ":")).encode()
        self.send_response(HTTP_METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET")
        self.send_header("Content-Type", CONTENT_TYPE_JSON)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        """Handle GET requests."""
        if not self._check_auth():
            return

        parsed = urlparse(self.path)
        path = parsed.path
        query = _parse_request_query(self.path)

        if self._is_seeder():
            if path == ENDPOINT_HEALTH:
                self._handle_health()
                return
            if path == ENDPOINT_TIME:
                self._handle_time()
                return
            download_path = (
                f"{ENDPOINT_BUCKETS_PREFIX}/{self.server.bucket}{ENDPOINT_FILES_DOWNLOAD_SUFFIX}"
            )
            if path != download_path:
                self._send_method_not_allowed()
                return
            required = {"filename", "sha256", "cid"}
            if set(query) != required or any(len(query[key]) != 1 for key in required):
                self._send_error_response(HTTP_BAD_REQUEST, "Invalid seeder query")
                return
            expected_sha = query["sha256"][0]
            requester_cid = query["cid"][0]
            if (
                not is_sha256_hex(expected_sha)
                or expected_sha != expected_sha.lower()
                or not is_valid_cid(requester_cid)
            ):
                self._send_error_response(HTTP_BAD_REQUEST, "Invalid seeder query")
                return
            slots = self.server.peer_read_slots
            if slots is None:
                raise RuntimeError("seeder server is missing its read-cap semaphore")
            if not slots.acquire(blocking=False):
                # Saturated, not broken: tell the requester how long to wait so it
                # defers this seeder instead of pricing the refusal as a failure.
                self._send_error_response(
                    HTTP_SERVICE_UNAVAILABLE,
                    "Seeder read cap reached",
                    extra_headers={"Retry-After": str(SEEDER_RETRY_AFTER_S)},
                )
                return
            try:
                self._handle_download(query, expected_sha256=expected_sha)
            finally:
                slots.release()
            return

        # Health check
        if path == ENDPOINT_HEALTH:
            self._handle_health()
            return

        # Time endpoint for clock synchronization
        if path == ENDPOINT_TIME:
            self._handle_time()
            return

        # List directory permissions: /api/v1/buckets/{bucket}/dirs
        if path.endswith(ENDPOINT_DIRS_SUFFIX) and ENDPOINT_BUCKETS_PREFIX in path:
            self._handle_list_dirs()
            return

        # List files: /api/v1/buckets/{bucket}/files
        if path.endswith(ENDPOINT_FILES_SUFFIX) and ENDPOINT_BUCKETS_PREFIX in path:
            self._handle_list_files(query)
            return

        # Download file: /api/v1/buckets/{bucket}/files/download
        if path.endswith(ENDPOINT_FILES_DOWNLOAD_SUFFIX) and ENDPOINT_BUCKETS_PREFIX in path:
            self._handle_download(query)
            return

        # Upload status: /api/v1/buckets/{bucket}/files/upload-status
        if path.endswith(ENDPOINT_FILES_UPLOAD_STATUS_SUFFIX) and ENDPOINT_BUCKETS_PREFIX in path:
            self._handle_upload_status(query)
            return

        # Delta signatures: /api/v1/buckets/{bucket}/files/delta/signatures
        if path.endswith(ENDPOINT_DELTA_SIGNATURES_SUFFIX) and ENDPOINT_BUCKETS_PREFIX in path:
            from hsync_client.p2p_delta import handle_delta_signatures_get

            handle_delta_signatures_get(self, query)
            return

        # Delta literal payload: /api/v1/buckets/{bucket}/files/delta/data/{token}
        if ENDPOINT_DELTA_DATA_MARKER in path and ENDPOINT_BUCKETS_PREFIX in path:
            from hsync_client.p2p_delta import handle_delta_data

            bucket = path.split(f"{ENDPOINT_BUCKETS_PREFIX}/", 1)[-1].split("/", 1)[0]
            token = path.rsplit(ENDPOINT_DELTA_DATA_MARKER, 1)[-1]
            handle_delta_data(self, bucket, token)
            return

        # Tombstones: /api/v1/buckets/{bucket}/tombstones
        if path.endswith(ENDPOINT_TOMBSTONES_SUFFIX) and ENDPOINT_BUCKETS_PREFIX in path:
            self._handle_tombstones()
            return

        self._send_error_response(HTTP_NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        """Handle POST requests."""
        if self._is_seeder():
            self._send_method_not_allowed()
            return
        parsed = urlparse(self.path)
        path = parsed.path

        source_service = self.server.source_service
        if path.endswith(ENDPOINT_FILES_SOURCES_SUFFIX):
            if source_service is None:
                self._reject_unread_body(HTTP_NOT_FOUND, "Not found")
                return
            sources_path = (
                f"{ENDPOINT_BUCKETS_PREFIX}/{source_service.bucket}{ENDPOINT_FILES_SOURCES_SUFFIX}"
            )
            if path != sources_path:
                self._reject_unread_body(HTTP_NOT_FOUND, "Not found")
                return
            if not self._check_auth():
                return
            self._handle_sources(source_service)
            return

        # Producing a delta from requester signatures is read-only: it exposes
        # no more content than GET /files/download and remains available in a
        # no-key download-only group.
        if path.endswith(ENDPOINT_DELTA_SIGNATURES_SUFFIX) and ENDPOINT_BUCKETS_PREFIX in path:
            if not self._check_auth():
                return
            from hsync_client.p2p_delta import handle_delta_signatures_post

            handle_delta_signatures_post(self)
            return

        if not self._check_mutation_auth():
            return

        # Tombstone gossip (slave -> master): /api/v1/buckets/{bucket}/tombstones
        if path.endswith(ENDPOINT_TOMBSTONES_SUFFIX) and ENDPOINT_BUCKETS_PREFIX in path:
            self._handle_tombstones_merge()
            return

        # Symlink creation (slave -> master): /api/v1/buckets/{bucket}/symlinks
        if path.endswith(ENDPOINT_SYMLINKS_SUFFIX) and ENDPOINT_BUCKETS_PREFIX in path:
            self._handle_symlink(_parse_request_query(self.path))
            return

        # Delta apply (upload delta): /api/v1/buckets/{bucket}/files/delta/apply
        if path.endswith(ENDPOINT_DELTA_APPLY_SUFFIX) and ENDPOINT_BUCKETS_PREFIX in path:
            self._handle_delta_apply()
            return

        # Create directory: /api/v1/buckets/{bucket}/dirs
        if path.endswith(ENDPOINT_DIRS_SUFFIX) and ENDPOINT_BUCKETS_PREFIX in path:
            query = _parse_request_query(self.path)
            self._handle_create_dir(query)
            return

        # Upload file: /api/v1/buckets/{bucket}/files
        if path.endswith(ENDPOINT_FILES_SUFFIX) and ENDPOINT_BUCKETS_PREFIX in path:
            self._handle_upload()
            return

        self._send_error_response(HTTP_NOT_FOUND, "Not found")

    def _handle_sources(self, source_service: P2PSourceService) -> None:
        """Validate and resolve one strict peer-source batch request."""
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != CONTENT_TYPE_JSON:
            self._reject_unread_body(HTTP_BAD_REQUEST, "Content-Type must be application/json")
            return
        if self.headers.get("Transfer-Encoding"):
            self._reject_unread_body(HTTP_BAD_REQUEST, "Chunked request bodies are unsupported")
            return

        raw_length = self.headers.get("Content-Length")
        content_length = (
            int(raw_length)
            if raw_length is not None and raw_length.isascii() and raw_length.isdigit()
            else -1
        )
        if content_length > P2P_SOURCES_MAX_BODY_BYTES:
            self._reject_unread_body(HTTP_CONTENT_TOO_LARGE, "Request body too large")
            return
        if content_length <= 0:
            self._reject_unread_body(HTTP_BAD_REQUEST, "Invalid Content-Length")
            return

        body = self.rfile.read(content_length)
        if len(body) != content_length:
            self.close_connection = True
            self._send_error_response(HTTP_BAD_REQUEST, "Incomplete request body")
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_response(HTTP_BAD_REQUEST, "Invalid JSON body")
            return

        request = validate_p2p_sources_request(payload)
        if request is None:
            self._send_error_response(HTTP_BAD_REQUEST, "Invalid sources request")
            return

        requester_cid = request["cid"]
        requester_address = (
            self.client_address[0]
            if isinstance(self.client_address, tuple)
            else str(self.client_address)
        )
        guard = getattr(self.server, "cid_guard", None)
        if guard is not None and guard.request_conflict(requester_cid, requester_address):
            if guard.should_report(requester_cid, requester_address):
                log.error(
                    f"{ActionLabel.SECURITY} P2P sources request from "
                    f"{requester_address} claims CID {requester_cid} owned by another host"
                )
            self._send_error_response(HTTP_FORBIDDEN, "Requester CID is owned by another host")
            return

        try:
            response = source_service.resolve(
                requester_cid=requester_cid,
                requester_address=requester_address,
                files=request["files"],
            )
        except Exception:
            log.exception("P2P source lookup failed; returning an empty source list")
            try:
                response = build_p2p_sources_response(
                    files=[
                        {"path": file["path"], "sha256": file["sha256"], "sources": []}
                        for file in request["files"]
                    ]
                )
            except ValueError:
                self._send_error_response(HTTP_BAD_REQUEST, "Sources response would exceed limit")
                return
        if len(self._encode_json_response(response)) > P2P_SOURCES_MAX_BODY_BYTES:
            response = build_p2p_sources_response(
                files=[
                    {"path": file["path"], "sha256": file["sha256"], "sources": []}
                    for file in request["files"]
                ]
            )
            if len(self._encode_json_response(response)) > P2P_SOURCES_MAX_BODY_BYTES:
                self._send_error_response(
                    HTTP_CONTENT_TOO_LARGE, "Sources response would exceed limit"
                )
                return
        self._send_json_response(HTTP_OK, response)

    def _reject_unread_body(self, status: int, message: str) -> None:
        """Reject a request before reading its body and disable connection reuse."""
        self.close_connection = True
        self._send_error_response(status, message)

    def do_DELETE(self) -> None:
        """Handle DELETE requests for bidirectional sync."""
        if self._is_seeder():
            self._send_method_not_allowed()
            return
        if not self._check_mutation_auth():
            return

        parsed = urlparse(self.path)
        path = parsed.path
        query = _parse_request_query(self.path)

        # Delete file: /api/v1/buckets/{bucket}/files?path=...
        if path.endswith(ENDPOINT_FILES_SUFFIX) and ENDPOINT_BUCKETS_PREFIX in path:
            self._handle_delete(query)
            return

        self._send_error_response(HTTP_NOT_FOUND, "Not found")

    def do_PATCH(self) -> None:
        """Handle PATCH requests for rename operations."""
        if self._is_seeder():
            self._send_method_not_allowed()
            return
        if not self._check_mutation_auth():
            return

        parsed = urlparse(self.path)
        path = parsed.path
        query = _parse_request_query(self.path)

        # Permission-only change: /api/v1/buckets/{bucket}/files/permissions
        if path.endswith(ENDPOINT_FILES_PERMISSIONS_SUFFIX) and ENDPOINT_BUCKETS_PREFIX in path:
            self._handle_patch_permissions(query)
            return

        # Rename file/dir: /api/v1/buckets/{bucket}/files?src=...&dst=...
        if path.endswith(ENDPOINT_FILES_SUFFIX) and ENDPOINT_BUCKETS_PREFIX in path:
            self._handle_rename(query)
            return

        self._send_error_response(HTTP_NOT_FOUND, "Not found")

    def do_PUT(self) -> None:
        """Reject unsupported writes consistently in read-only seeder mode."""
        if self._is_seeder():
            self._send_method_not_allowed()
            return
        self._send_error_response(HTTP_NOT_FOUND, "Not found")

    def do_HEAD(self) -> None:
        """Reject unsupported HEAD requests with the seeder's route policy."""
        self._send_method_not_allowed()

    def _reject_duplicate_cid(self, origin_cid) -> bool:
        """Refuse a write claiming a duplicated device identity (409).

        A request whose ``cid`` is this master's own, or is bound to a
        *different* source address in the shared slave-CID registry, comes from
        a cloned ``HSYNC_DEVICE_DIR``: two live peers sharing a CID would
        corrupt the causal vector-clock and tombstone authorship. The check
        runs after ``_check_auth``, so unauthenticated callers cannot probe
        which CIDs are in use.

        Returns True (and sends the 409 response) on conflict.
        """
        guard = getattr(self.server, "cid_guard", None)
        if guard is None or not origin_cid or not is_valid_cid(origin_cid):
            return False
        client_ip = (
            self.client_address[0]
            if isinstance(self.client_address, tuple)
            else str(self.client_address)
        )
        if not guard.request_conflict(origin_cid, client_ip):
            return False
        if guard.should_report(origin_cid, client_ip):
            log.error(
                f"{ActionLabel.SECURITY} Duplicate device identity: write from "
                f"{self.address_string()} claims CID {origin_cid} which is already in use "
                f"(this master or another host). Refusing the request."
            )
        self._send_error_response(HTTP_CONFLICT, "Duplicate device identity")
        return True

    def _handle_patch_permissions(self, query: dict) -> None:
        """Apply a content-unchanged metadata change (chmod / touch) from a slave.

        Metadata-only convergence: the slave declares the bucket-relative
        ``filename``, the desired octal ``permissions`` and/or a corrected
        ``mtime``; no file bytes cross the wire. Only REGULAR files are handled
        (directory modes go through the ``/dirs`` surface — a directory chmod
        must keep the owner rwx floor, which the file setter does not apply).
        The new mode is sanitized (special bits stripped) and the mtime applied
        when either diverges. The master's own watcher then observes the
        IN_ATTRIB change and broadcasts the ``modified`` event to the other
        slaves, exactly like the central server's same-SHA branch.
        """
        if self._reject_duplicate_cid(query.get("cid", [None])[0]):
            return

        rel_path = query.get("filename", [None])[0]
        permissions = query.get("permissions", [None])[0]
        mtime_raw = query.get("mtime", [None])[0]
        mtime = 0
        if mtime_raw is not None:
            try:
                mtime = int(mtime_raw)
            except (TypeError, ValueError, OverflowError):
                self._send_error_response(HTTP_BAD_REQUEST, "Invalid 'mtime' parameter")
                return
            if mtime < 0:
                self._send_error_response(HTTP_BAD_REQUEST, "Invalid 'mtime' parameter")
                return
            # ADR-0023 receiver-side guard: a far-future peer value must not win
            # every tie-break.
            mtime = int(clamp_incoming_corrected_mtime(mtime))

        if not rel_path or (not permissions and mtime <= 0):
            self._send_error_response(HTTP_BAD_REQUEST, "Missing 'filename' or metadata to apply")
            return

        from hsync_common.permissions import is_valid_octal_permissions

        if permissions and not is_valid_octal_permissions(permissions):
            self._send_error_response(HTTP_BAD_REQUEST, "Invalid 'permissions' parameter")
            return

        local_bucket = self._require_bucket()
        if not local_bucket:
            return

        full_path = resolve_within(local_bucket, rel_path, resolve_final=False)
        if not full_path:
            self._send_error_response(HTTP_BAD_REQUEST, "Invalid path")
            return

        target = Path(full_path)
        if target.is_symlink() or not target.is_file():
            # Symlinks are replicated structurally (their POSIX mode is always
            # 0777 anyway); directories go through the /dirs surface.
            self._send_error_response(HTTP_NOT_FOUND, "File not found")
            return

        origin_cid = query.get("cid", [None])[0]
        valid_origin_cid = (
            origin_cid if isinstance(origin_cid, str) and is_valid_cid(origin_cid) else None
        )
        authenticated = bool(CONFIG.get("P2P_KEY"))
        try:
            # Attribute the resulting IN_ATTRIB event to the slave that sent it
            # so its watcher can suppress its own echo (only honoured on
            # authenticated requests).
            if valid_origin_cid:
                from hsync_client.p2p_websocket import mark_inbound_origin

                mark_inbound_origin(full_path, valid_origin_cid, authenticated=authenticated)

            from hsync_client.utils import set_permissions

            if permissions:
                set_permissions(full_path, permissions)
            if mtime > 0:
                current_mtime = int(target.stat().st_mtime)
                if abs(current_mtime - mtime) > MTIME_PROPAGATION_TOLERANCE_SECONDS:
                    # ``mtime`` is an ADR-0023 corrected time: convert it back
                    # to the master's raw clock before writing it, or the next
                    # correction re-adds the offset and the value never matches.
                    raw_mtime = uncorrect_mtime(mtime)
                    os.utime(full_path, (raw_mtime, raw_mtime))
        except (OSError, ValueError, OverflowError) as e:
            log.warning(f'Metadata update failed for "{full_path}": {e}')
            self._send_error_response(HTTP_INTERNAL_ERROR, "Metadata update failed")
            return

        log.info(
            f'{ActionLabel.PERMISSION} Metadata updated: "{full_path}" '
            f"(permissions={permissions}, mtime={mtime})"
        )
        self._send_json_response(HTTP_OK, {"info": "Metadata updated"})

    def _handle_rename(self, query: dict) -> None:
        """Handle file/directory rename from client."""
        if self._reject_duplicate_cid(query.get("cid", [None])[0]):
            return

        from os import rename

        src_rel = query.get("src", [None])[0]
        dst_rel = query.get("dst", [None])[0]

        if not src_rel or not dst_rel:
            self._send_error_response(HTTP_BAD_REQUEST, "Missing 'src' or 'dst' parameter")
            return

        local_bucket = self._require_bucket()
        if not local_bucket:
            return

        # Peer-supplied paths are untrusted: validate the relative form and
        # confine both endpoints to the bucket (rejects traversal, absolute
        # paths, backslashes, sibling-prefix confusion and engine artifacts).
        # resolve_final=False on BOTH endpoints: renaming a link must move the
        # LINK entry — the default full resolve dereferences it and would
        # rename the target file instead.
        src_path = resolve_within(local_bucket, src_rel, resolve_final=False)
        dst_path = resolve_within(local_bucket, dst_rel, resolve_final=False)
        if not src_path or not dst_path:
            self._send_error_response(HTTP_BAD_REQUEST, "Invalid path")
            return

        # Dangling links are real entries: Path.exists() follows the link and
        # would report a dangling rename source as missing. Recognize both.
        src = Path(src_path)
        if not (src.is_symlink() or src.exists()):
            self._send_error_response(HTTP_NOT_FOUND, "Source not found")
            return

        try:
            # Create parent directory if needed
            dst_parent = str(Path(dst_path).parent)
            if dst_parent and not Path(dst_parent).exists():
                Path(dst_parent).mkdir(parents=True, exist_ok=True)

            # Mark both paths as inbound so the master's filesystem watcher
            # broadcasts the resulting `renamed` event with the originator
            # CID, allowing the sender to discard its own echo.  The CID is
            # only honoured on authenticated requests: without P2P_KEY any P2P
            # peer could spoof another peer's CID and suppress its events.
            origin_cid = query.get("cid", [None])[0]
            if origin_cid and is_valid_cid(origin_cid):
                from hsync_client.p2p_websocket import mark_inbound_origin

                authenticated = bool(CONFIG.get("P2P_KEY"))
                mark_inbound_origin(src_path, origin_cid, authenticated=authenticated)
                mark_inbound_origin(dst_path, origin_cid, authenticated=authenticated)

            rename(src_path, dst_path)

            self._send_json_response(HTTP_OK, {"info": "Renamed successfully"})

        except Exception as e:
            log.error(f"Error renaming {src_path} -> {dst_path}: {e}")
            self._send_error_response(HTTP_INTERNAL_ERROR, f"Rename error: {e}")

    def _handle_delete(self, query: dict) -> None:
        """Handle file/directory deletion from client."""
        if self._reject_duplicate_cid(query.get("cid", [None])[0]):
            return

        from shutil import rmtree

        rel_path = query.get("path", [None])[0]
        is_dir = query.get("is_dir", ["false"])[0].lower() == "true"

        if not rel_path:
            self._send_error_response(HTTP_BAD_REQUEST, "Missing 'path' parameter")
            return

        local_bucket = self._require_bucket()
        if not local_bucket:
            return

        # Peer-supplied path is untrusted: validate and confine to the bucket
        # (rejects traversal, absolute paths and sibling-prefix confusion).
        # resolve_final=False: a symlink AT this path must be deleted as the
        # LINK entry — the default full resolve dereferences it and turns
        # "delete the link" into "delete the target file" (data loss that
        # propagates to every peer).
        full_path = resolve_within(local_bucket, rel_path, resolve_final=False)
        if not full_path:
            self._send_error_response(HTTP_BAD_REQUEST, "Invalid path")
            return

        if not (Path(full_path).is_symlink() or Path(full_path).exists()):
            # Already deleted, return success. No mark_inbound_origin is
            # recorded here, so if another concurrent request's rmtree fires
            # a `deleted` event for this path it will broadcast with the
            # master CID (echo not suppressed on the originator).
            log.debug(
                f"[DELETE-API] already-deleted branch: path={full_path} is_dir={is_dir} "
                f"origin_cid={query.get('cid', [None])[0]}"
            )
            self._send_json_response(HTTP_OK, {"info": "File already deleted"})
            return

        # Mark the path as inbound so the resulting `deleted` event from the
        # filesystem watcher is broadcast with the originator's CID.
        # The CID is always recorded for accurate logging; only authenticated
        # CIDs are used for echo suppression (consume_inbound_origin gate).
        origin_cid = query.get("cid", [None])[0]
        marked = False
        if origin_cid and is_valid_cid(origin_cid):
            from hsync_client.p2p_websocket import mark_inbound_origin

            authenticated = bool(CONFIG.get("P2P_KEY"))
            mark_inbound_origin(full_path, origin_cid, authenticated=authenticated)
            marked = True
        log.debug(
            f"[DELETE-API] delete branch: path={full_path} is_dir={is_dir} "
            f"origin_cid={origin_cid} marked={marked} p2p_key_set={bool(CONFIG.get('P2P_KEY'))}"
        )

        try:
            if Path(full_path).is_symlink():
                # Remove the LINK entry, never its target: the is_dir branch's
                # rmtree would dereference a dir-symlink and destroy the target
                # tree. A file-symlink is structurally a link too.
                os.remove(full_path)
            elif is_dir:
                rmtree(full_path, ignore_errors=False)
            else:
                os.remove(full_path)
                parent = str(Path(full_path).parent)
                if Path(full_path).name == HSYNCKEEP_FILENAME:
                    # Sentinel removed — clean up the now-empty directory
                    self._cleanup_empty_dir(parent)
                else:
                    # Regular file removed — preserve directory with sentinel
                    self._ensure_sentinel_after_delete(full_path)

            self._send_json_response(HTTP_OK, {"info": "Deleted successfully"})

        except Exception as e:
            log.error(f"Error deleting {full_path}: {e}")
            self._send_error_response(HTTP_INTERNAL_ERROR, f"Delete error: {e}")

    @staticmethod
    def _ensure_sentinel_after_delete(deleted_file: str) -> None:
        """Create a .hsynckeep sentinel if the parent directory is now empty.

        After a file is deleted the parent directory may become empty.
        Placing a sentinel preserves the directory structure during sync.
        """
        from hsync_client.sync import ensure_sentinel

        parent = str(Path(deleted_file).parent)
        if not parent or not Path(parent).exists() or not Path(parent).is_dir():
            return
        ensure_sentinel(parent)

    @staticmethod
    def _cleanup_empty_dir(dir_path: str) -> None:
        """Remove *dir_path* if it is completely empty.

        Called after deleting a sentinel file so that the directory
        does not linger on disk once it has no real content. Never
        removes the LOCAL_BUCKET root itself.
        """
        from hsync_client.utils import safe_rmdir_if_empty

        if not dir_path or not Path(dir_path).exists() or not Path(dir_path).is_dir():
            return
        if safe_rmdir_if_empty(dir_path):
            log.debug(f'Removed empty directory "{dir_path}"')

    def _handle_create_dir(self, query: dict) -> None:
        """Handle directory creation from a slave peer.

        Creates the directory (and any missing parents) inside the local
        bucket, applies optional permissions, records the originator CID for
        echo suppression, and creates a ``.hsynckeep`` sentinel if the new
        directory is empty so the sync engine preserves it.
        """
        if self._reject_duplicate_cid(query.get("cid", [None])[0]):
            return

        rel_path = query.get("path", [None])[0]
        if not rel_path:
            self._send_error_response(HTTP_BAD_REQUEST, "Missing 'path' parameter")
            return

        local_bucket = self._require_bucket()
        if not local_bucket:
            return

        # Peer-supplied path is untrusted: validate and confine to the bucket
        # (rejects traversal, absolute paths and sibling-prefix confusion).
        full_path = resolve_within(local_bucket, rel_path)
        if not full_path:
            self._send_error_response(HTTP_BAD_REQUEST, "Invalid path")
            return

        origin_cid = query.get("cid", [None])[0]
        valid_origin_cid = (
            origin_cid if isinstance(origin_cid, str) and is_valid_cid(origin_cid) else None
        )
        authenticated = bool(CONFIG.get("P2P_KEY"))
        try:
            # Only mark inbound origin if the directory doesn't already
            # exist: an already-existing directory means the slave is
            # echoing a directory it received from us via broadcast, so
            # the mark must not be recorded (it would paint the slave's
            # CID over a genuinely local path, causing later local
            # events to be misattributed as REMOTE).
            #
            # mkdir(parents=True) below also creates every missing ancestor,
            # and the master's watcher fires a create event for each of them;
            # those events must carry the slave's CID too, otherwise they
            # would be logged/broadcast as local creates (no echo
            # suppression possible on the slaves). Mark every missing
            # ancestor BEFORE the mkdir.
            if valid_origin_cid:
                from hsync_client.p2p_websocket import mark_inbound_origin

                bucket_path = Path(local_bucket)
                for candidate in Path(full_path).parents:
                    # Only candidates that are the bucket itself or live
                    # inside it are eligible; ancestors above the bucket
                    # root are never touched by the mkdir below.
                    if not (candidate == bucket_path or bucket_path in candidate.parents):
                        continue
                    if not candidate.exists():
                        mark_inbound_origin(
                            str(candidate), valid_origin_cid, authenticated=authenticated
                        )
                if not Path(full_path).exists():
                    # A symlink *inside* the bucket pointing outside it must not
                    # redirect the mkdir into the out-of-bucket location (the
                    # path was already confined via resolve_within, which resolves
                    # existing symlink components — a symlink that would escape is
                    # rejected there). Nothing extra is needed here: resolve_within
                    # returns None when the path crosses an escaping symlink.
                    mark_inbound_origin(full_path, valid_origin_cid, authenticated=authenticated)

            Path(full_path).mkdir(parents=True, exist_ok=True)

            # Apply optional permissions if provided. The directory-specific
            # setter keeps the owner rwx floor so a peer-supplied mode like
            # ``000`` can never lock the master out of its own storage.
            permissions = query.get("permissions", [None])[0]
            if permissions:
                from hsync_client.utils import set_permissions_on_dir

                set_permissions_on_dir(full_path, permissions)

            self._send_json_response(HTTP_CREATED, {"info": "Directory created successfully"})

        except Exception as e:
            log.error(f"Error creating directory {full_path}: {e}")
            self._send_error_response(HTTP_INTERNAL_ERROR, f"Create dir error: {e}")

    def _handle_symlink(self, query: dict) -> None:
        """Handle symlink creation from a slave peer (structural replication).

        The peer sends the (path, target) pair; the master materialises the
        LINK itself — never the target content. A target escaping the bucket
        is rejected; an existing file/link at the destination is replaced.
        Mirrors the central server's ``POST /symlinks`` semantics so remote
        mode and P2P mode behave identically.
        """
        if self._reject_duplicate_cid(query.get("cid", [None])[0]):
            return

        from os import symlink as os_symlink

        from hsync_common.path_validation import is_safe_symlink_target

        rel_path = query.get("path", [None])[0]
        target = query.get("target", [None])[0]

        if not rel_path or not target:
            self._send_error_response(HTTP_BAD_REQUEST, "Missing 'path' or 'target' parameter")
            return

        local_bucket = self._require_bucket()
        if not local_bucket:
            return

        # Peer-supplied link path is untrusted: validate and confine to the
        # bucket WITHOUT dereferencing the final component — an existing link
        # at the destination must be replaced as the LINK, never its target.
        full_path = resolve_within(local_bucket, rel_path, resolve_final=False)
        if not full_path:
            self._send_error_response(HTTP_BAD_REQUEST, "Invalid path")
            return

        # The target must stay inside the bucket (an escaping target would
        # plant a symlink to arbitrary local files on the master).
        if not is_safe_symlink_target(full_path, target, local_bucket):
            log.warning(
                f"{ActionLabel.SECURITY} Symlink target escapes bucket, "
                f'not creating: "{full_path}" -> "{target}"'
            )
            self._send_error_response(HTTP_FORBIDDEN, "Target escapes bucket")
            return

        try:
            # Idempotency: a link already present with the SAME target is a
            # converged echo (the master's own broadcast looped back). Do not
            # unlink + recreate it: every mutation emits a deleted+created
            # pair on the master's watcher, and the single inbound-origin mark
            # would be consumed by the first event — the second would be
            # rebroadcast with the master CID and re-trigger the whole cycle
            # on every peer (infinite SYMLINK/DELETE oscillation).
            link_path = Path(full_path)
            if link_path.is_symlink():
                try:
                    if os.readlink(link_path) == target:
                        self._send_json_response(HTTP_OK, {"status": "already exists"})
                        return
                except OSError:
                    pass

            # Mark the path as inbound so the master's watcher rebroadcasts
            # the symlink event with the originator CID (echo suppression).
            origin_cid = query.get("cid", [None])[0]
            if origin_cid and is_valid_cid(origin_cid):
                from hsync_client.p2p_websocket import mark_inbound_origin

                authenticated = bool(CONFIG.get("P2P_KEY"))
                mark_inbound_origin(full_path, origin_cid, authenticated=authenticated)

            parent = str(Path(full_path).parent)
            if not Path(parent).exists():
                Path(parent).mkdir(parents=True, exist_ok=True)
            if link_path.is_dir() and not link_path.is_symlink():
                try:
                    link_path.rmdir()
                except OSError:
                    log.warning(
                        f"{ActionLabel.SECURITY} Refusing to replace non-empty directory "
                        f'with symlink: "{full_path}"'
                    )
                    self._send_error_response(
                        HTTP_BAD_REQUEST, "Cannot replace non-empty directory"
                    )
                    return
            elif link_path.is_symlink() or link_path.exists():
                os.remove(full_path)

            os_symlink(target, full_path)
            self._send_json_response(HTTP_CREATED, {"status": "created"})
        except Exception as e:
            log.error(f"Error creating symlink {full_path}: {e}")
            self._send_error_response(HTTP_INTERNAL_ERROR, f"Symlink error: {e}")

    def _handle_health(self) -> None:
        """Handle health check endpoint."""
        self._send_json_response(HTTP_OK, {"status": "ok", "mode": "p2p_master"})

    def _handle_time(self) -> None:
        """Handle time endpoint for clock synchronization.

        Returns the master's current timestamp so slaves can calculate
        clock offset using round-trip measurement (NTP-like).
        """
        self._send_json_response(HTTP_OK, {"timestamp": time.time()})

    def _handle_list_dirs(self) -> None:
        """List directory metadata (path + permissions) inside the bucket.

        Mirrors the central server's ``GET …/dirs`` endpoint so the client
        download-only directory-permission converge works identically in
        remote and P2P mode.
        """
        local_bucket = CONFIG.get("LOCAL_BUCKET", "")
        if not local_bucket or not Path(local_bucket).exists():
            self._send_error_response(HTTP_NOT_FOUND, "Bucket not found")
            return

        entries: list[dict] = []
        for root, dirs, _files in walk(local_bucket):
            dirs[:] = [d for d in dirs if not is_hidden_dir(f"{root}/{d}")]
            for name in dirs:
                full = f"{root}/{name}"
                if Path(full).is_symlink():
                    continue
                dir_obj = Dir.get(full)
                if dir_obj is None or dir_obj.permissions is None:
                    continue
                rel = os.path.relpath(full, local_bucket)
                if rel == ".":
                    continue
                entries.append({"path": rel, "permissions": dir_obj.permissions})

        entries.sort(key=lambda x: x.get("path", ""))
        self._send_json_response(HTTP_OK, entries)

    def _handle_list_files(self, query: dict) -> None:
        """Handle list files endpoint."""
        filter_path = query.get("path", [None])[0]

        local_bucket = CONFIG.get("LOCAL_BUCKET", "")
        if not local_bucket or not Path(local_bucket).exists():
            self._send_error_response(HTTP_NOT_FOUND, "Bucket not found")
            return

        from hsync_client.sync import iter_bucket_entries
        from hsync_client.sync_state import flush as _flush_state

        file_list = []
        self_recorded = False
        for file_path, link in iter_bucket_entries(local_bucket):
            # Symlinks are emitted as structural entries (path + target),
            # never dereferenced: File.get() (exists/stat/sha256) would
            # follow the link and the listing would either exfiltrate an
            # out-of-bucket target (e.g. ``secret -> /etc/passwd``) to every
            # slave or advertise the target content instead of the link.
            if link is not None:
                entry = link.to_dict()
                entry["filepath"] = str(Path(file_path).relative_to(local_bucket))
                file_list.append(entry)
                continue

            file_obj = File.get(file_path)
            if file_obj:
                file_dict = file_obj.to_dict()
                # Normalize path to be relative to bucket (like server does)
                file_normalized = File.from_dict(file_dict, local_bucket).to_dict()

                # Attach the effective causal version so slaves can run the
                # causal matrix on the listing (ADR-0022). The listing is the
                # master's *publication point*: before advertising, persist
                # the self-record for a dirty/edited file (catch-all of the
                # anti-aliasing invariant, §3.4.7) so two distinct contents
                # can never share the same advertised clock — a pure tick
                # without persistence would alias {master:1} across edits,
                # letting a slave silently dominate a genuine divergence.
                from hsync_client.sync_state import (
                    get_base_hash,
                    local_version_for_compare,
                    note_local_edit,
                )

                prev_hash = get_base_hash(file_path)
                note_local_edit(file_path, file_obj.sha256sum or "")
                if get_base_hash(file_path) != prev_hash:
                    self_recorded = True
                eff = local_version_for_compare(file_path, file_obj.sha256sum)
                if eff is not None:
                    file_normalized["version"] = {
                        "vclock": eff["vclock"],
                        "version_id": eff.get("version_id", ""),
                        "origin_cid": eff.get("origin_cid"),
                        "ts": eff.get("ts", 0),
                        "corrected_mtime": eff.get("corrected_mtime", 0),
                    }

                # Apply path filter if specified
                if filter_path:
                    rel_path = file_normalized.get("filepath", "")
                    if not rel_path.startswith(filter_path):
                        continue
                file_list.append(file_normalized)

        # Sort by mtime ascending (oldest first) to prioritize updating older files
        file_list.sort(key=lambda x: x.get("mtime", 0))

        # The master runs no sync cycle of its own, so any self-record made
        # while publishing must be persisted immediately: the advertised clock
        # has to survive a master restart (same guarantee as the server's
        # arbitration self-record).
        if self_recorded:
            _flush_state()

        self._send_json_response(HTTP_OK, file_list)

    def _handle_tombstones(self) -> None:
        """Serve this peer's deletion markers so slaves avoid resurrection."""
        from hsync_client.tombstones import get_payload

        try:
            self._send_json_response(HTTP_OK, get_payload())
        except Exception as e:  # pragma: no cover - defensive
            log.error(f"Error serving tombstones: {e}")
            self._send_error_response(HTTP_INTERNAL_ERROR, "Error reading tombstones")

    def _handle_tombstones_merge(self) -> None:
        """Merge a slave-pushed tombstone payload into the local store (gossip up).

        Lets a peer that holds deletion markers the current master lacks (e.g.
        accumulated while it was a former master) propagate them, so a freshly
        elected master converges to the union of known deletions.
        """
        from hsync_client.tombstones import merge_payload

        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._send_error_response(HTTP_BAD_REQUEST, "Invalid Content-Length")
            return
        if content_length <= 0:
            self._send_json_response(HTTP_OK, {"merged": 0})
            return
        # Gossip payloads are untrusted: reject bodies that could never be a
        # legitimate payload before buffering them in memory.
        if content_length > MAX_TOMBSTONE_PAYLOAD_BYTES:
            self._send_error_response(HTTP_BAD_REQUEST, "Tombstone payload too large")
            return

        try:
            raw = self.rfile.read(content_length)
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError):
            self._send_error_response(HTTP_BAD_REQUEST, "Invalid JSON body")
            return
        if not isinstance(payload, dict):
            self._send_error_response(HTTP_BAD_REQUEST, "Invalid tombstone payload")
            return

        merged = merge_payload(payload)
        self._send_json_response(HTTP_OK, {"merged": merged})

    def _parse_range_header(self, range_header: str, file_size: int) -> tuple[int, int] | None:
        """
        Parse HTTP Range header.

        Args:
            range_header: The Range header value (e.g., "bytes=1000-" or "bytes=1000-2000").
            file_size: Total size of the file.

        Returns:
            Tuple of (start, end) byte positions, or None if invalid.
        """
        if not range_header or not range_header.startswith("bytes="):
            return None

        try:
            range_spec = range_header[6:]  # Remove "bytes="
            if "-" not in range_spec:
                return None

            start_str, end_str = range_spec.split("-", 1)

            if start_str == "":
                # Suffix range: "-500" means last 500 bytes
                suffix_length = int(end_str)
                start = max(0, file_size - suffix_length)
                end = file_size
            elif end_str == "":
                # Range from start to end: "1000-"
                start = int(start_str)
                end = file_size
            else:
                # Explicit range: "1000-2000"
                start = int(start_str)
                end = int(end_str) + 1  # HTTP ranges are inclusive

            # Validate range
            if start < 0 or start >= file_size:
                return None
            end = min(end, file_size)
            if start >= end:
                return None

            return (start, end)
        except (ValueError, IndexError):
            return None

    def _handle_download(self, query: dict, expected_sha256: str | None = None) -> None:
        """Serve one safely-opened regular file, optionally bound to an expected hash."""
        filename = query.get("filename", [None])[0]
        if not filename:
            self._send_error_response(HTTP_BAD_REQUEST, "Missing filename parameter")
            return
        origin_cid = query.get("cid", [None])[0]
        log.info(f'{ActionLabel.REMOTE_DOWNLOAD} "{filename}"{cid_suffix(origin_cid)}')

        local_bucket = self._require_bucket()
        if not local_bucket:
            return
        normalized = validate_remote_relpath(filename)
        if normalized is None:
            self._send_error_response(HTTP_FORBIDDEN, "Access denied")
            return

        from hsync_common.ignore import should_skip

        display_path = str(Path(local_bucket) / normalized)
        if should_skip(display_path):
            self._send_error_response(HTTP_FORBIDDEN, "Access denied")
            return

        response_started = False
        try:
            with _open_regular_beneath(local_bucket, normalized) as (stream, opened):
                file_size = opened.st_size
                identity = _stat_identity(opened)
                checksum = _CHECKSUM_CACHE.get(identity)
                if checksum is None:
                    checksum = _hash_open_file(stream)
                    _CHECKSUM_CACHE.put(identity, checksum)
                if not _same_open_file(opened, os.fstat(stream.fileno())):
                    self._send_error_response(HTTP_CONFLICT, "File changed while opening")
                    return
                if expected_sha256 and not hmac.compare_digest(checksum, expected_sha256.lower()):
                    self._send_error_response(HTTP_CONFLICT, "File checksum no longer matches")
                    return

                range_header = self.headers.get("Range")
                range_tuple = (
                    self._parse_range_header(range_header, file_size) if range_header else None
                )
                if range_header and range_tuple is None:
                    self.send_response(HTTP_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"*/{file_size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                permissions = f"{stat_module.S_IMODE(opened.st_mode):o}"
                if range_tuple:
                    start, end = range_tuple
                    stream.seek(start)
                    range_len = end - start
                    self.send_response(HTTP_PARTIAL_CONTENT)
                    self.send_header("Content-Type", CONTENT_TYPE_OCTET_STREAM)
                    self.send_header("Content-Length", str(range_len))
                    self.send_header("Content-Range", f"bytes {start}-{end - 1}/{file_size}")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header(HEADER_CHECKSUM_SHA256, checksum)
                    self.send_header("X-Permissions", permissions)
                    self.end_headers()
                    response_started = True
                    remaining = range_len
                    while remaining > 0:
                        chunk = stream.read(min(_P2P_UPLOAD_CHUNK_SIZE, remaining))
                        if not chunk:
                            self.close_connection = True
                            return
                        if not _same_open_file(opened, os.fstat(stream.fileno())):
                            self.close_connection = True
                            return
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                    return

                accept_encoding = self.headers.get("Accept-Encoding", "").lower()
                identity_only = "identity" in accept_encoding and "zstd" not in accept_encoding
                use_compression = (
                    CONFIG.get("COMPRESSION_ENABLED", False)
                    and should_compress(display_path, file_size, is_p2p=True)
                    and file_size <= P2P_COMPRESSION_MAX_FILE_BYTES
                    and not identity_only
                    and (not self._is_seeder() or "zstd" in accept_encoding)
                )
                if use_compression:
                    try:
                        compressed_body = _stage_compressed_body(stream, opened, checksum)
                    except (OSError, zstd.ZstdError) as exc:
                        log.warning(
                            f'Could not stage compressed P2P download "{display_path}": {exc}; '
                            "serving identity encoding"
                        )
                        stream.seek(0)
                    else:
                        if compressed_body is None:
                            self._send_error_response(HTTP_CONFLICT, "File changed while reading")
                            return
                        with compressed_body:
                            compressed_size = os.fstat(compressed_body.fileno()).st_size
                            self.send_response(HTTP_OK)
                            self.send_header("Content-Type", CONTENT_TYPE_OCTET_STREAM)
                            self.send_header("Content-Length", str(compressed_size))
                            self.send_header("Content-Encoding", CONTENT_ENCODING_ZSTD)
                            self.send_header(HEADER_CHECKSUM_SHA256, checksum)
                            self.send_header("X-Permissions", permissions)
                            self.end_headers()
                            response_started = True
                            remaining = compressed_size
                            while remaining:
                                chunk = compressed_body.read(min(_P2P_UPLOAD_CHUNK_SIZE, remaining))
                                if not chunk:
                                    self.close_connection = True
                                    return
                                self.wfile.write(chunk)
                                remaining -= len(chunk)
                        return
                    if not _same_open_file(opened, os.fstat(stream.fileno())):
                        self._send_error_response(HTTP_CONFLICT, "File changed while reading")
                        return

                self.send_response(HTTP_OK)
                self.send_header("Content-Type", CONTENT_TYPE_OCTET_STREAM)
                self.send_header("Content-Length", str(file_size))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header(HEADER_CHECKSUM_SHA256, checksum)
                self.send_header("X-Permissions", permissions)
                self.end_headers()
                response_started = True
                while True:
                    chunk = stream.read(_P2P_UPLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    if not _same_open_file(opened, os.fstat(stream.fileno())):
                        self.close_connection = True
                        return
                    self.wfile.write(chunk)
                if not _same_open_file(opened, os.fstat(stream.fileno())):
                    self.close_connection = True
                return
        except FileNotFoundError:
            self._send_error_response(HTTP_NOT_FOUND, "File not found")
        except PermissionError as exc:
            log.warning(f'{ActionLabel.SECURITY} Refusing file serve "{display_path}": {exc}')
            self._send_error_response(HTTP_FORBIDDEN, "Access denied")
        except OSError as exc:
            log.error(f"Error reading file {display_path}: {exc}")
            if response_started:
                self.close_connection = True
            else:
                self._send_error_response(HTTP_INTERNAL_ERROR, "Error reading file")

    def _handle_upload_status(self, query: dict) -> None:
        """Handle GET /api/v1/buckets/{bucket}/files/upload-status.

        Reports whether an interrupted upload can be resumed from a partial
        ``.part`` file, mirroring the central server's endpoint so the same
        client resume logic works in P2P mode.

        Query params:
            filename:  Path to the file within the bucket.
            sha256sum: Expected SHA-256 of the complete file (optional).
            size:      Expected total size in bytes (optional).
        """
        filename = query.get("filename", [""])[0]
        if not filename:
            self._send_error_response(HTTP_BAD_REQUEST, "Missing 'filename' parameter")
            return

        sha256sum = query.get("sha256sum", [""])[0]
        try:
            size = int(query.get("size", ["0"])[0])
        except (TypeError, ValueError):
            size = 0

        local_bucket = self._require_bucket()
        if not local_bucket:
            return

        file_path = resolve_within(local_bucket, filename)
        if file_path is None:
            self._send_error_response(HTTP_FORBIDDEN, "Access denied")
            return

        partial_path = f"{file_path}{PARTIAL_DOWNLOAD_SUFFIX}"

        # Check if complete file already exists with same SHA-256
        if sha256sum and is_sha256_hex(sha256sum) and Path(file_path).exists():
            actual_sha = compute_sha256(file_path)
            if hmac.compare_digest(actual_sha, sha256sum):
                self._send_json_response(
                    HTTP_OK,
                    {
                        "status": "complete",
                        "offset": 0,
                        "message": "File already exists with same checksum",
                    },
                )
                return

        # Check for partial file
        if Path(partial_path).exists():
            partial_size = Path(partial_path).stat().st_size
            if size > 0 and partial_size >= size:
                # Partial file is invalid, remove it
                try:
                    os.remove(partial_path)
                except OSError:
                    pass
                self._send_json_response(
                    HTTP_OK,
                    {
                        "status": "new",
                        "offset": 0,
                        "message": "Previous partial file was invalid, starting fresh",
                    },
                )
                return
            self._send_json_response(
                HTTP_OK,
                {
                    "status": "resumable",
                    "offset": partial_size,
                    "message": f"Can resume from byte {partial_size}",
                },
            )
            return

        self._send_json_response(
            HTTP_OK,
            {
                "status": "new",
                "offset": 0,
                "message": "No partial file exists",
            },
        )

    def _handle_upload(self) -> None:
        """Handle file upload endpoint (streaming).

        The request body is parsed incrementally so files of arbitrary size
        can be received without buffering them entirely in RAM. The file part
        is written directly to a temporary ``.part`` file and atomically
        renamed on success.

        Supports resumable uploads: if the ``offset`` form field is > 0, the
        incoming data is appended to an existing ``.part`` file whose size
        must match the declared offset. Compressed uploads are not resumable.
        """

        content_type = self.headers.get("Content-Type", "")

        if "multipart/form-data" not in content_type:
            self._send_error_response(HTTP_BAD_REQUEST, "Expected multipart/form-data")
            return

        boundary = _extract_multipart_boundary(content_type)
        if boundary is None:
            self._send_error_response(HTTP_BAD_REQUEST, "Missing multipart boundary")
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._send_error_response(HTTP_BAD_REQUEST, "Invalid Content-Length")
            return
        if content_length <= 0:
            self._send_error_response(HTTP_BAD_REQUEST, "No content")
            return

        from hsync_client.utils import set_permissions

        local_bucket = CONFIG.get("LOCAL_BUCKET", "")

        # Size-aware disk space guard: reject before writing if the target
        # filesystem cannot hold the incoming bytes plus the configured reserve,
        # avoiding a mid-transfer ENOSPC that leaves a .part behind.
        reserve_mb = CONFIG.get("MIN_FREE_DISK_SPACE", 0)
        if reserve_mb and reserve_mb > 0:
            try:
                free = disk_usage(local_bucket or ".").free
                required = reserve_mb * 1024 * 1024 + content_length
                if free < required:
                    log.error(
                        f"Insufficient disk space for upload: "
                        f"{free // (1024 * 1024)} MB available, "
                        f"{required // (1024 * 1024)} MB required"
                    )
                    self._send_error_response(HTTP_INSUFFICIENT_STORAGE, "Insufficient disk space")
                    return
            except OSError as e:
                log.warning(f"Could not check disk space: {e}")

        parser = _StreamingMultipartParser(self.rfile, content_length, boundary)

        # Mutable state shared with the file_open callback (set by _file_open)
        dest_path: str | None = None
        tmp_path: str | None = None
        raw_file: IO[bytes] | None = None
        out_file: IO[bytes] | None = None
        response_sent = False

        def _file_open(form_fields, field_name, filename):
            nonlocal dest_path, tmp_path, raw_file, out_file
            path = form_fields.get("path", "")
            is_compressed_local = form_fields.get("compressed", "false").lower() == "true"

            # Peer-supplied destination is untrusted: validate the relative
            # directory and the filename (rejecting traversal, absolute
            # paths, backslashes and internal engine artifacts) and confine
            # the result to the bucket. ``resolve_within`` also resolves
            # existing symlink components, so a path crossing an escaping
            # in-bucket symlink is rejected here.
            rel_dest = f"{path.rstrip('/')}/{filename}" if path else filename
            dest_path_local = resolve_within(local_bucket, rel_dest)
            if dest_path_local is None:
                raise PermissionError("Access denied")
            Path(dest_path_local).parent.mkdir(parents=True, exist_ok=True)

            dest_path = dest_path_local
            tmp_path = dest_path_local + PARTIAL_DOWNLOAD_SUFFIX

            # Resumable upload: if offset > 0, append to an existing
            # .part file whose size must match the declared offset.
            # Compressed uploads are not resumable (the resumable path
            # never decompresses, mirroring the central server).
            try:
                offset_val = int(form_fields.get("offset", "0") or "0")
            except (TypeError, ValueError):
                offset_val = 0
            is_resumable = offset_val > 0
            if is_resumable and is_compressed_local:
                raise ValueError("Compressed uploads are not resumable")
            if is_resumable:
                if not Path(tmp_path).exists():
                    raise ValueError("Cannot resume: partial file not found")
                current_size = Path(tmp_path).stat().st_size
                if current_size != offset_val:
                    raise ValueError(f"Offset mismatch: expected {current_size}, got {offset_val}")
                raw = open_partial_no_follow(tmp_path, "ab")
            else:
                # O_NOFOLLOW where supported: a pre-existing/raced .part symlink
                # must not redirect the streamed write outside the bucket.
                raw = open_partial_no_follow(tmp_path, "wb")
            raw_file = raw
            # Cap the bytes landing on disk (decompression bomb containment
            # on the receiving side). The peer-declared size alone is not a
            # safe bound: a hostile peer could declare an arbitrary size and
            # expand a bomb up to it before the SHA-256 check discards the
            # content. The shared inbound_size_cap policy applies
            # min(declared, MAX_INBOUND_SIZE), with MAX_INBOUND_SIZE as the
            # only bound when no size is declared at all.
            try:
                declared_size = int(form_fields.get("size", ""))
            except (TypeError, ValueError):
                declared_size = -1
            cap = inbound_size_cap(
                declared_size if declared_size > 0 else None,
                CONFIG.get("MAX_INBOUND_SIZE", 0),
            )
            capped = _CappedBytesWriter(raw.write, cap) if cap is not None else raw
            if is_compressed_local:
                import zstandard as zstd

                if declared_size <= 0:
                    raise ValueError("Missing/invalid 'size' for compressed upload")

                # closefd=False so closing the decompressor flushes the frame
                # into ``raw`` without closing it, allowing an explicit fsync.
                writer = zstd.ZstdDecompressor().stream_writer(capped, closefd=False)
                out_file = writer
                return writer.write
            out_file = capped
            return capped.write

        try:
            form_fields, file_meta = parser.parse(_file_open)

            if file_meta is None or out_file is None:
                self._send_error_response(HTTP_BAD_REQUEST, "No file uploaded")
                return

            # _file_open succeeded, so the destination paths are set
            assert dest_path is not None
            assert tmp_path is not None

            # Close the writer (flushes zstd frame if used), then fsync the
            # underlying file so the bytes are durable before the atomic rename.
            try:
                if out_file is not raw_file:
                    out_file.close()
            finally:
                if raw_file is not None:
                    try:
                        raw_file.flush()
                        os.fsync(raw_file.fileno())
                    except OSError:
                        pass
                    raw_file.close()
            out_file = None
            raw_file = None

            # Resumable upload: check if the file is complete. If the
            # declared size has not been reached, keep the .part file and
            # return 202 so the client can resume from the new offset.
            try:
                declared_size = int(form_fields.get("size", "0") or "0")
            except (TypeError, ValueError):
                declared_size = 0
            if declared_size > 0:
                written_bytes = Path(tmp_path).stat().st_size
                if written_bytes < declared_size:
                    self._send_json_response(
                        HTTP_ACCEPTED,
                        {
                            "info": "Partial upload received",
                            "offset": written_bytes,
                            "remaining": declared_size - written_bytes,
                        },
                    )
                    response_sent = True
                    # Prevent the finally block from deleting the .part
                    # file (it is needed for the next resume attempt).
                    tmp_path = None
                    return

            cid = form_fields.get("cid", "")
            if self._reject_duplicate_cid(cid):
                return
            mtime = form_fields.get("mtime", "")
            ctime = form_fields.get("ctime", "")
            permissions = form_fields.get("permissions", "664")

            # Integrity is mandatory: the received bytes must hash to the
            # declared SHA-256 before they are committed over the destination
            # (a hostile or corrupt peer must not propagate content to slaves).
            # The declared digest must be well-formed: compare_digest raises
            # TypeError on non-ASCII input.
            expected_sha = form_fields.get("sha256sum", "")
            if not expected_sha:
                self._send_error_response(HTTP_BAD_REQUEST, "Missing 'sha256sum' field")
                return
            if not is_sha256_hex(expected_sha):
                self._send_error_response(HTTP_BAD_REQUEST, "Invalid 'sha256sum' format")
                return
            actual_sha = compute_sha256(tmp_path)
            if not hmac.compare_digest(actual_sha, expected_sha):
                log.warning(
                    f'Rejected upload "{dest_path}": checksum mismatch '
                    f"(declared {expected_sha}, computed {actual_sha})"
                )
                self._send_error_response(HTTP_BAD_REQUEST, "Checksum mismatch")
                return

            # Record the CID for accurate logging.  Only authenticated CIDs
            # are used for echo suppression (consume_inbound_origin gate).
            if cid and is_valid_cid(cid):
                from hsync_client.p2p_websocket import mark_inbound_origin

                authenticated = bool(CONFIG.get("P2P_KEY"))
                mark_inbound_origin(dest_path, cid, authenticated=authenticated)

            # Causal arbitration (ADR-0022): the master is a transport hub,
            # not an authority. Compare the incoming version against the stored
            # version for this path and decide accept / supersede / conflict.
            incoming_vclock_raw = form_fields.get("vclock", "")
            incoming_version_id = form_fields.get("version_id", "")
            try:
                parsed_vclock = json.loads(incoming_vclock_raw) if incoming_vclock_raw else None
            except (ValueError, TypeError):
                parsed_vclock = None
            # ADR-0023: the uploader's corrected mtime (0 ⇒ unknown ⇒ hash
            # fallback). Receiver-side guard: a far-future peer value is
            # clamped against the master's corrected clock so no device can
            # win every tie-break (conflict engineering).
            raw_corrected = form_fields.get("corrected_mtime", "0")
            try:
                incoming_corrected = int(raw_corrected)
                incoming_corrected = max(incoming_corrected, 0)
            except (ValueError, TypeError):
                incoming_corrected = 0
            if incoming_corrected > 0:
                incoming_corrected = int(clamp_incoming_corrected_mtime(float(incoming_corrected)))

            disk_hash = compute_sha256(dest_path) if Path(dest_path).exists() else None

            from hsync_client.arbitration import ArbitrationOutcome, arbitrate_upload

            arb = arbitrate_upload(
                abs_path=dest_path,
                incoming_hash=expected_sha,
                incoming_vclock_raw=parsed_vclock,
                incoming_version_id=incoming_version_id,
                incoming_origin_cid=cid if is_valid_cid(cid) else None,
                disk_hash=disk_hash,
                incoming_corrected_mtime=incoming_corrected,
            )

            if arb.outcome is ArbitrationOutcome.superseded:
                # Incoming is causally dominated (stale): reject, no copy.
                log.info(
                    f'{ActionLabel.CONFLICT} Upload of "{dest_path}" superseded '
                    f"by a newer local version"
                )
                self._send_error_response(HTTP_CONFLICT, "superseded")
                response_sent = True
                return

            if arb.outcome is ArbitrationOutcome.conflict_lost:
                # Concurrent: incoming loses the tie-break. Preserve the
                # incoming bytes as a conflicted copy, keep the local winner.
                from hsync_common.time_utils import conflict_timestamp

                ts = conflict_timestamp()
                loser_cid = arb.loser_cid
                conflict_path = conflicted_copy_name(dest_path, loser_cid, ts)
                try:
                    os.replace(tmp_path, conflict_path)
                    tmp_path = None
                    log.warning(
                        f'{ActionLabel.CONFLICT} "{dest_path}" concurrent edit; '
                        f'incoming version saved as "{conflict_path}"'
                    )
                except OSError as e:
                    log.error(f'Could not save conflict copy for "{dest_path}": {e}')
                self._send_error_response(HTTP_CONFLICT, "conflict_lost")
                response_sent = True
                return

            # accepted / accepted_conflict_won: commit the incoming bytes.
            if arb.outcome is ArbitrationOutcome.accepted_conflict_won and disk_hash is not None:
                # Preserve the displaced local content as a conflict copy
                # before overwriting it with the incoming winner.
                from hsync_common.time_utils import conflict_timestamp

                ts = conflict_timestamp()
                conflict_path = conflicted_copy_name(dest_path, arb.loser_cid, ts)
                try:
                    Path(conflict_path).parent.mkdir(parents=True, exist_ok=True)
                    os.replace(dest_path, conflict_path)
                    log.warning(
                        f'{ActionLabel.CONFLICT} "{dest_path}" concurrent edit; '
                        f'local version saved as "{conflict_path}"'
                    )
                except OSError as e:
                    log.error(f'Could not save conflict copy for "{dest_path}": {e}')

            os.replace(tmp_path, dest_path)
            tmp_path = None

            # Record the agreed version with the merged clock (ADR-0022 §5).
            from hsync_common.models.tombstone import relpath_in_bucket

            rel = relpath_in_bucket(dest_path, str(CONFIG.get("LOCAL_BUCKET", "")))
            if rel:
                from hsync_client.sync_state import get_sync_state_store

                get_sync_state_store().record(
                    rel,
                    base_hash=expected_sha,
                    vclock=arb.record_vclock,
                    version_id=incoming_version_id or None,
                    origin_cid=cid if is_valid_cid(cid) else None,
                    corrected_mtime=arb.record_corrected_mtime,
                )

            if mtime or ctime:
                try:
                    max_ts = time.time() + MTIME_MAX_FUTURE_SKEW_SECONDS
                    mtime_val = float(mtime) if mtime else None
                    atime_val = mtime_val or (float(ctime) if ctime else None)
                    if mtime_val:
                        # Clamp far-future peer-supplied timestamps (conflict
                        # engineering would favour the newest mtime).
                        mtime_val = min(mtime_val, max_ts)
                        atime_val = min(atime_val, max_ts) if atime_val else mtime_val
                        os.utime(dest_path, (atime_val, mtime_val))
                except (ValueError, OSError):
                    pass

            if permissions:
                set_permissions(dest_path, permissions)

            self._send_json_response(
                HTTP_CREATED,
                {"info": "File uploaded successfully", "file": dest_path},
            )
            response_sent = True

        except PermissionError:
            self._discard_temp_artifact(tmp_path, out_file, raw_file)
            self._send_error_response_safe(HTTP_FORBIDDEN, "Access denied")
            response_sent = True
        except ValueError as e:
            # Malformed upload metadata (e.g. missing size on a compressed
            # upload) or decompressed bytes beyond the declared size.
            self._discard_temp_artifact(tmp_path, out_file, raw_file)
            self._send_error_response_safe(HTTP_BAD_REQUEST, f"Invalid upload: {e}")
            response_sent = True
        except Exception as e:
            log.error(f"Error handling upload: {e}")
            self._discard_temp_artifact(tmp_path, out_file, raw_file)
            if not response_sent:
                self._send_error_response_safe(HTTP_INTERNAL_ERROR, f"Upload error: {e}")
        finally:
            # Idempotent safety net for paths that did not go through an
            # error branch (on success tmp_path/raw_file/out_file are None).
            self._discard_temp_artifact(tmp_path, out_file, raw_file)

    @staticmethod
    def _discard_temp_artifact(path: str | None, *files) -> None:
        """Remove a failed upload's temp artifact and close its handles.

        Every upload/delta-apply error path calls this BEFORE the error
        response is sent: with a persistent connection the client can observe
        the response the moment it arrives, so the artifact must already be
        gone — a cleanup that only runs after the send would race the client
        and leave a torn artifact visible. Safe to call twice (idempotent);
        double closes are swallowed.
        """
        if path and Path(path).exists():
            try:
                os.remove(path)
            except OSError:
                pass
        for fh in files:
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass

    def _handle_delta_apply(self) -> None:
        """Handle a delta apply request (upload delta, streaming multipart).

        The literal payload is streamed to a temp file (never buffered in
        RAM); the JSON instructions travel in the ``metadata`` form field and
        are applied by :func:`hsync_client.p2p_delta.handle_delta_apply`.
        """
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_error_response(HTTP_BAD_REQUEST, "Expected multipart/form-data")
            return
        boundary = _extract_multipart_boundary(content_type)
        if boundary is None:
            self._send_error_response(HTTP_BAD_REQUEST, "Missing multipart boundary")
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._send_error_response(HTTP_BAD_REQUEST, "Invalid Content-Length")
            return
        if content_length <= 0:
            self._send_error_response(HTTP_BAD_REQUEST, "No content")
            return

        from hsync_common.constants import DELTA_MAX_LITERAL_SIZE

        from hsync_client.p2p_delta import handle_delta_apply

        parser = _StreamingMultipartParser(self.rfile, content_length, boundary)

        literal_path: str | None = None
        raw_file: IO[bytes] | None = None

        def _file_open(form_fields, field_name, filename):
            nonlocal literal_path, raw_file
            # Park the literal payload in the system temp dir (outside the
            # bucket so it is never picked up by the sync engine), capped at
            # the protocol literal limit (decompression-bomb containment).
            import tempfile

            fd, literal_path = tempfile.mkstemp(prefix="hsync-delta-lit-")
            raw = os.fdopen(fd, "wb")
            raw_file = raw
            capped = _CappedBytesWriter(raw.write, DELTA_MAX_LITERAL_SIZE)
            return capped.write

        try:
            form_fields, file_meta = parser.parse(_file_open)
            if raw_file is not None:
                try:
                    raw_file.flush()
                    os.fsync(raw_file.fileno())
                except OSError:
                    pass
                raw_file.close()
                raw_file = None

            if file_meta is None or literal_path is None:
                self._send_error_response(HTTP_BAD_REQUEST, "No literal payload uploaded")
                return

            # A delta upload is a write: refuse a duplicated device identity.
            cid = form_fields.get("cid", "")
            if self._reject_duplicate_cid(cid):
                return

            handle_delta_apply(self, form_fields, literal_path)
        except ValueError as e:
            self._discard_temp_artifact(literal_path, raw_file)
            self._send_error_response_safe(HTTP_BAD_REQUEST, f"Invalid delta apply: {e}")
        except Exception as e:
            log.error(f"Error handling delta apply: {e}")
            self._discard_temp_artifact(literal_path, raw_file)
            self._send_error_response_safe(HTTP_INTERNAL_ERROR, "Delta apply error")
        finally:
            self._discard_temp_artifact(literal_path, raw_file)


class _AiohttpTransport(P2PEndpointMixin):
    """aiohttp transport for the P2P data plane.

    Runs the endpoint mixin synchronously on a worker thread (the data plane is
    blocking: file IO, sha256, zstd) and bridges its ``BaseHTTPRequestHandler``
    surface to aiohttp: ``send_response``/``send_header``/``end_headers`` build
    and prepare a :class:`aiohttp.web.StreamResponse`; ``wfile.write`` streams
    the body; ``rfile.read`` pulls from the request body. Every bridge call
    blocks the worker thread while the event loop performs the async operation,
    so the endpoint bodies keep their exact per-request semantics.
    """

    server: _ServingContext  # type: ignore[assignment]

    def __init__(
        self, request: web.Request, loop: asyncio.AbstractEventLoop, context: _ServingContext
    ) -> None:
        self._request = request
        self._loop = loop
        self.server = context
        self.path = request.rel_url.raw_path_qs
        version = request.version
        self.requestline = f"{request.method} {self.path} HTTP/{version.major}.{version.minor}"
        self.headers = request.headers  # type: ignore[assignment]
        remote = request.remote or ""
        self.client_address = (remote, 0)
        self.close_connection = False
        self._status = 0
        self._out_headers: list[tuple[str, str]] = []
        self._response: web.StreamResponse | None = None
        self._prepared = False
        self._written = 0
        self._declared_length: int | None = None

    # -- BaseHTTPRequestHandler surface ------------------------------------

    def address_string(self) -> str:
        return self.client_address[0]

    def send_response(self, code: int, message: str | None = None) -> None:
        self._status = int(code)
        # BaseHTTPRequestHandler.send_response() logs every response through
        # log_request(); the aiohttp bridge must call it explicitly so the
        # unified transport keeps the per-request DEBUG line (full path and
        # query string) the former ThreadingHTTPServer emitted.
        self.log_request(code)

    def send_header(self, keyword: str, value: str) -> None:
        self._out_headers.append((str(keyword), str(value)))
        if str(keyword).lower() == "content-length":
            with contextlib.suppress(ValueError):
                self._declared_length = int(value)

    def end_headers(self) -> None:
        response = web.StreamResponse(status=self._status or HTTP_OK)
        for name, value in self._out_headers:
            response.headers[name] = value
        if self.close_connection:
            response.force_close()
        self._loop_run(response.prepare(self._request))
        self._response = response
        self._prepared = True

    @property
    def wfile(self) -> _AiohttpTransport:  # type: ignore[override]
        return self

    def write(self, data: bytes) -> None:
        if self._response is None:
            raise RuntimeError("response body written before end_headers()")
        self._loop_run(self._response.write(bytes(data)))
        self._written += len(data)

    def flush(self) -> None:
        pass

    @property
    def rfile(self) -> _AiohttpTransport:  # type: ignore[override]
        return self

    def read(self, size: int = -1) -> bytes:
        """Read exactly *size* bytes (or until EOF), like ``rfile.read``.

        aiohttp's ``StreamReader.read(n)`` returns as soon as *some* data is
        buffered, while the endpoint mixin is written against
        ``BaseHTTPRequestHandler.rfile`` and relies on an exact read; looping
        keeps the framing semantics identical and avoids spurious "incomplete
        request body" failures on a segmented transfer.
        """
        if size is None or size < 0:
            return self._loop_run(self._request.content.read())
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = self._loop_run(self._request.content.read(remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    # -- Driver -------------------------------------------------------------

    def _loop_run(self, coro, timeout: float | None = None):
        """Run *coro* on the event loop from this worker thread and block.

        Bounded by ``P2P_HANDLER_IDLE_TIMEOUT_S`` (the shared per-request idle
        timeout) so a peer that stalls mid-body or stops reading a response
        cannot pin a worker slot forever (slowloris). On timeout the coroutine is
        cancelled and ``TimeoutError`` propagates, aborting the request and
        closing the connection.
        """
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(
                timeout=float(P2P_HANDLER_IDLE_TIMEOUT_S if timeout is None else timeout)
            )
        except FutureTimeoutError:
            future.cancel()
            raise TimeoutError("P2P request I/O timed out") from None

    def dispatch(self) -> None:
        """Run the mixin dispatcher for this request's method (worker thread)."""
        handler = getattr(self, f"do_{self._request.method}", None)
        if handler is None:
            self._send_method_not_allowed()
            return
        handler()

    async def finalize(self) -> web.StreamResponse:
        """Complete the bridged response once the worker thread returned."""
        if not self._prepared:
            raise web.HTTPInternalServerError(text="P2P handler produced no response")
        response = self._response
        assert response is not None
        # Use the length the handler actually declared: a response that framed
        # itself with a length and wrote fewer bytes is a mid-stream truncation
        # (e.g. the file changed while serving) and must close, never be left
        # framed as if complete. A response without an explicit length is
        # aiohttp-framed and finalized normally.
        declared = self._declared_length
        try:
            if declared is not None and self._written != declared:
                response.force_close()
                return response
            await response.write_eof()
        except (ConnectionResetError, RuntimeError, TimeoutError):
            response.force_close()
        return response


# =============================================================================
# P2P Server
# =============================================================================


class P2PServer:
    """Unified HTTPS/WSS listener for the P2P serving role.

    One TLS listener serves the whole data plane: a single aiohttp application
    runs the endpoint mixin on a bounded worker pool. The serving role
    (``master``/``seeder``) is installed at runtime with :meth:`configure_role`
    and retired with :meth:`stop_serving`, without closing or rebinding the
    socket.
    """

    def __init__(
        self,
        port: int,
        use_https: bool = False,
        cert_file: str | None = None,
        key_file: str | None = None,
        cid_guard: SlaveCidRegistry | None = None,
        inventory_store: P2PInventoryStore | None = None,
        source_service: P2PSourceService | None = None,
        mode: P2PServerMode = P2PServerMode.master,
        bucket: str | None = None,
        peer_read_cap: int = 2,
        websocket_server: P2PWebSocketServer | None = None,
    ) -> None:
        """
        Initialize the P2P server.

        Args:
            port: Port to listen on (``0`` = OS-assigned; read it back from
                :attr:`port` after :meth:`start`).
            use_https: Whether to use HTTPS instead of HTTP.
            cert_file: Path to SSL certificate file (required if use_https=True).
            key_file: Path to SSL private key file (required if use_https=True).
                The persistent master key is stored unencrypted and protected by
                filesystem permissions (see ``load_or_create_master_cert``).
            cid_guard: Shared :class:`SlaveCidRegistry` used to refuse writes
                claiming a duplicated device identity. ``None`` (standalone /
                test usage) disables the check.
        """
        self._port = port
        self._actual_port = port
        self._use_https = use_https
        self._cert_file = cert_file
        self._key_file = key_file
        self._cid_guard = cid_guard
        # Stable reference kept across role changes: the seeder installs
        # ``cid_guard=None`` in its context, but the shared registry must survive
        # so a later master role still enforces duplicate-identity checks.
        self._registry = cid_guard
        self._inventory_store = inventory_store
        self._source_service = source_service
        if peer_read_cap < 1:
            raise ValueError("peer_read_cap must be at least 1")
        self._peer_read_cap = peer_read_cap
        self._bucket = bucket or str(CONFIG.get("REMOTE_BUCKET", ""))
        self._websocket_server = websocket_server
        self._peer_read_slots = (
            threading.BoundedSemaphore(peer_read_cap) if mode is P2PServerMode.seeder else None
        )
        # The serving context is swapped as a single object, so a request always
        # reads one coherent role (mode + its bucket/guard/service/semaphore).
        self._serving_context: _ServingContext | None = _ServingContext(
            mode=mode,
            bucket=self._bucket,
            peer_read_slots=self._peer_read_slots,
            cid_guard=self._cid_guard,
            source_service=self._source_service,
            inventory_store=self._inventory_store,
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner: web.AppRunner | None = None
        self._executor: ThreadPoolExecutor | None = None
        # In-flight request bound (created on the listener loop); see _serve.
        self._dispatch_slots: asyncio.Semaphore | None = None
        self._stop_event: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._running = False

        # Validate HTTPS configuration
        if use_https and (not cert_file or not key_file):
            raise ValueError("cert_file and key_file are required when use_https=True")

    @property
    def port(self) -> int:
        """The actual bound port (resolved from the OS when ``port=0``)."""
        return self._actual_port

    @property
    def use_https(self) -> bool:
        """Check if HTTPS is enabled."""
        return self._use_https

    @property
    def cid_guard(self) -> SlaveCidRegistry | None:
        """The shared duplicate-identity guard (``None`` when disabled)."""
        return self._cid_guard

    @property
    def registry(self) -> SlaveCidRegistry | None:
        """The stable duplicate-identity registry, unchanged across roles."""
        return self._registry

    @property
    def inventory_store(self) -> P2PInventoryStore | None:
        """Role-owned inventory store shared with the WSS server."""
        return self._inventory_store

    @property
    def source_service(self) -> P2PSourceService | None:
        """Batch source resolver exposed by this master."""
        return self._source_service

    @property
    def mode(self) -> P2PServerMode | None:
        """The current serving role, or ``None`` when the listener is not serving.

        Derived from the serving context so a yielded master can never report a
        stale role (the context is the single source of truth, ADR-0027).
        """
        context = self._serving_context
        return context.mode if context is not None else None

    @property
    def websocket_server(self) -> P2PWebSocketServer | None:
        """The realtime control plane sharing this listener (``None`` if none)."""
        return self._websocket_server

    def configure_role(
        self,
        mode: P2PServerMode,
        *,
        bucket: str,
        cid_guard: SlaveCidRegistry | None,
        source_service: P2PSourceService | None,
        inventory_store: P2PInventoryStore | None,
        peer_read_cap: int,
    ) -> None:
        """Install a serving role and its context atomically.

        The listener is bound once for the process lifetime; a role change only
        swaps the serving context. The whole context (mode + fields) is built
        first and installed with a single assignment, so a concurrent request
        reads either the old role or the new one — never a torn mix (ADR-0027).
        The seeder rebuilds its own bounded read semaphore; the master has none.
        """
        if peer_read_cap < 1:
            raise ValueError("peer_read_cap must be at least 1")
        self._bucket = bucket
        self._cid_guard = cid_guard
        self._source_service = source_service
        self._inventory_store = inventory_store
        self._peer_read_cap = peer_read_cap
        self._peer_read_slots = (
            threading.BoundedSemaphore(peer_read_cap) if mode is P2PServerMode.seeder else None
        )
        self._serving_context = _ServingContext(
            mode=mode,
            bucket=bucket,
            peer_read_slots=self._peer_read_slots,
            cid_guard=cid_guard,
            source_service=source_service,
            inventory_store=inventory_store,
        )
        # A role that serves events re-opens the control plane's push gate
        # (symmetric with stop_serving): the hub may have been left gated by an
        # earlier yield on this same reused listener.
        if mode is P2PServerMode.master and self._websocket_server is not None:
            self._websocket_server.set_publishing(True)

    def stop_serving(self) -> Future[None] | None:
        """Retire the serving role without touching the listening socket.

        The listener stays bound but stops serving: the control plane's publish
        gate closes **first** (no event slips out during the yield window), every
        new request is refused with 503, and **live WebSocket clients are
        closed** (ADR-0027). The swap is a single assignment of the serving
        context, so a request either sees the old role or gets refused, never a
        mix. Roles are installed with :meth:`configure_role`; this only retires
        one. Returns the client-close completion future so a caller can wait for
        the consumers to be gone before releasing shared state.
        """
        close_future: Future[None] | None = None
        if self._websocket_server is not None:
            # Close the push gate before retiring the role: a broadcast already
            # queued by the watcher must not reach a live client.
            self._websocket_server.set_publishing(False)
        self._serving_context = None
        if self._websocket_server is not None:
            close_future = self._websocket_server.close_clients()
        return close_future

    def _context(self) -> _ServingContext | None:
        """The current serving context, or ``None`` when the node is not serving."""
        return self._serving_context

    def close_inventory(self) -> None:
        """Close SQLite after HTTP and WSS consumers have stopped."""
        store = self._inventory_store
        self._inventory_store = None
        if store is not None:
            store.close()

    def reset_delta_state(self) -> None:
        """Drop parked delta payloads/tokens (called on every role yield, ADR-0027).

        The listener is reused across roles, so the previous role's delta tokens
        and temporary payloads must not survive into the next one.
        """
        from hsync_client.p2p_delta import reset_delta_p2p_state

        reset_delta_p2p_state()

    # -- Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Bind the TLS listener and start serving in a background thread.

        Returns once the socket is bound: :attr:`port` is the real port. A bind
        failure propagates synchronously instead of leaving a half-started
        server (no silent degraded serving).
        """
        if self._running:
            log.warning("P2P server already running")
            return

        self._ready.clear()
        self._start_error = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._start_error is not None:
            error = self._start_error
            self._thread.join(timeout=5)
            self._loop = None
            self._thread = None
            raise error

        self._running = True
        protocol = "HTTPS" if self._use_https else "HTTP"
        role = "seeder" if self.mode is P2PServerMode.seeder else "master server"
        log.info(f"P2P {role} started on port {self._actual_port} ({protocol})")

    def _run(self) -> None:
        """Event-loop thread entry point."""
        loop = self._loop
        assert loop is not None
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except BaseException as exc:  # bind/setup failure before readiness
            if not self._ready.is_set():
                self._start_error = exc
                self._ready.set()
        finally:
            # The listener thread owns its loop: close it here, after _serve has
            # drained the worker pool, and only then drop the reference.
            with contextlib.suppress(Exception):
                loop.close()
            if self._loop is loop:
                self._loop = None

    async def _serve(self) -> None:
        assert self._loop is not None
        app = web.Application(client_max_size=0)
        # Always register the canonical realtime route: when no control plane is
        # attached (a read-only seeder) or the node is not serving, it answers
        # 503 rather than falling through to the REST router as a 404.
        app.router.add_get(WS_EVENTS_PATH, self._handle_websocket)
        app.router.add_route("*", "/{tail:.*}", self._handle_http)
        executor = ThreadPoolExecutor(
            max_workers=P2P_SERVER_MAX_THREADS, thread_name_prefix="p2p-http"
        )
        self._executor = executor
        # Bound in-flight requests, not just worker threads: aiohttp accepts
        # connections eagerly while the executor's queue is unbounded, so excess
        # requests wait here instead of growing memory and sockets without limit.
        self._dispatch_slots = asyncio.Semaphore(P2P_SERVER_MAX_THREADS)
        # Access logging stays off (the endpoint layer logs what matters) and the
        # idle keep-alive timeout is the shared handler reap window.
        self._runner = web.AppRunner(
            app, access_log=None, keepalive_timeout=float(P2P_HANDLER_IDLE_TIMEOUT_S)
        )
        try:
            await self._runner.setup()

            ssl_context: ssl.SSLContext | None = None
            if self._use_https:
                ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ssl_context.load_cert_chain(
                    self._cert_file,  # type: ignore[arg-type]
                    self._key_file,  # type: ignore[arg-type]
                )

            # The WebSocket control plane shares this listener's loop.
            if self._websocket_server is not None:
                self._websocket_server.set_loop(self._loop)

            # Bind first and read the port back: an OS-assigned (port 0) or a
            # configured port is resolved from the real socket, never guessed.
            sock = socket.create_server(("0.0.0.0", self._port))
            self._actual_port = int(sock.getsockname()[1])
            if self._websocket_server is not None:
                self._websocket_server.set_port(self._actual_port)
            site = web.SockSite(self._runner, sock, ssl_context=ssl_context)
            await site.start()
        except BaseException:
            # Bind/setup failed before readiness: release the runner (which
            # closes the site socket) and the worker pool so start() raising
            # leaks nothing.
            with contextlib.suppress(Exception):
                await self._runner.cleanup()
            self._runner = None
            executor.shutdown(wait=False)
            if self._executor is executor:
                self._executor = None
            self._dispatch_slots = None
            raise

        self._stop_event = asyncio.Event()
        self._ready.set()
        await self._stop_event.wait()
        await self._runner.cleanup()
        self._runner = None
        # Drain in-flight workers WITHOUT parking the event loop: a worker
        # blocked in ``_loop_run`` needs this same loop to run its coroutine, so
        # a blocking ``shutdown(wait=True)`` here would stall it until its I/O
        # timeout. The reference is cleared only once the drain completed.
        await asyncio.to_thread(executor.shutdown, True)
        if self._executor is executor:
            self._executor = None
        self._dispatch_slots = None

    def stop(self) -> None:
        """Stop serving and close the listener."""
        if not self._running:
            return

        # Retire the serving role first: new requests are refused (503) and live
        # control-plane clients are closed, so nothing is served while the
        # listener is torn down.
        self.stop_serving()

        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=10)

        # Reap any parked delta literal payloads tied to the listener lifetime
        # (idempotent; also covers a node that yielded before stopping).
        from hsync_client.p2p_delta import reset_delta_p2p_state

        reset_delta_p2p_state()

        self._running = False
        role = "seeder" if self.mode is P2PServerMode.seeder else "master server"
        log.info(f"P2P {role} stopped")

        if thread is not None and thread.is_alive():
            # The listener thread is still draining an in-flight worker and owns
            # its loop/runner/executor: let it finish its own teardown instead of
            # nulling the references it still needs.
            log.warning("P2P server thread is still draining; teardown left to the listener")
            return

        self._loop = None
        self._runner = None
        self._executor = None
        self._dispatch_slots = None
        self._stop_event = None
        self._thread = None

    # -- Request handling ---------------------------------------------------

    async def _handle_http(self, request: web.Request) -> web.StreamResponse:
        """Bridge one aiohttp request onto the endpoint mixin (worker thread)."""
        context = self._context()
        executor = self._executor
        slots = self._dispatch_slots
        if context is None or executor is None or slots is None:
            raise web.HTTPServiceUnavailable(text="P2P server is not serving")
        loop = asyncio.get_running_loop()
        transport = _AiohttpTransport(request, loop, context)
        try:
            # Wait for a dispatch slot before enqueuing: the executor's queue is
            # unbounded, so this is what caps in-flight work under a peer flood.
            async with slots:
                await loop.run_in_executor(executor, transport.dispatch)
        except (ConnectionError, TimeoutError) as exc:
            # Routine peer teardown (reset/broken pipe) or an I/O timeout is not
            # a handler crash: log it at debug and let finalize() end the request.
            log.debug(f"P2P request ended early ({request.method} {request.path}): {exc}")
        except Exception:
            log.exception("P2P request handler crashed")
        return await transport.finalize()

    async def _handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        """Serve the canonical realtime-events channel on the unified listener.

        Only the ``master`` serving state exposes the control plane; a yielded
        master (or a seeder) refuses the upgrade so no peer can keep receiving
        events through a retired role (ADR-0027).
        """
        context = self._context()
        if (
            context is None
            or context.mode is not P2PServerMode.master
            or self._websocket_server is None
        ):
            raise web.HTTPServiceUnavailable(text="P2P realtime channel is not available")
        return await self._websocket_server.handle(request)
