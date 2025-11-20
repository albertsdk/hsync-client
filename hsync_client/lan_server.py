# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Embedded HTTP Server for LAN Master Mode.

This module provides a lightweight HTTP server that runs when a client
becomes the master in LAN P2P mode. It exposes the essential hsync API
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

import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from os import walk
from pathlib import Path
from shutil import disk_usage
from typing import IO
from urllib.parse import parse_qs, urlparse

from hsync_common.clock import clamp_incoming_corrected_mtime
from hsync_common.constants import PARTIAL_DOWNLOAD_SUFFIX
from hsync_common.hashing import compute_sha256, is_sha256_hex
from hsync_common.models.dir import is_hidden_dir
from hsync_common.models.event import is_valid_cid
from hsync_common.models.file import File, is_hidden_file
from hsync_common.models.tombstone import TOMBSTONE_MAX_ENTRIES
from hsync_common.path_validation import resolve_within
from loguru import logger as log

from hsync_client.arbitration import conflicted_copy_name
from hsync_client.compression import (
    CONTENT_ENCODING_ZSTD,
    compress_data,
    should_compress,
)
from hsync_client.config import CONFIG
from hsync_client.constants import HSYNCKEEP_FILENAME, MAX_INBOUND_SIZE_MB_DEFAULT, ActionLabel
from hsync_client.utils import inbound_size_cap, open_partial_no_follow

# =============================================================================
# Constants
# =============================================================================

# Maximum number of concurrent request threads the LAN master will spawn.
# Bounds resource consumption against a flood of peer connections.
LAN_SERVER_MAX_THREADS = 32

# Chunk size (bytes) for streaming file reads on the download path. Mirrors
# the client transfer module so the master never reads more than necessary
# into RAM per iteration.
_LAN_UPLOAD_CHUNK_SIZE = 64 * 1024


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server with a hard cap on concurrent request threads.

    ``ThreadingHTTPServer`` spawns an unbounded thread per request: a flood of
    peer connections (or slow clients holding sockets open) can exhaust file
    descriptors and memory on the master. This subclass guards every
    ``process_request`` with a :class:`threading.BoundedSemaphore` so at most
    ``max_threads`` requests are served concurrently; excess connections wait
    in the (bounded) socket backlog and are picked up as slots free up.
    """

    def __init__(self, *args, max_threads: int = LAN_SERVER_MAX_THREADS, **kwargs):
        self._thread_semaphore = threading.BoundedSemaphore(max_threads)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address) -> None:
        # Acquire a slot before spawning a thread. If the cap is reached the
        # call blocks until a running request finishes, keeping the backlog
        # bounded by the socket's request_queue_size.
        self._thread_semaphore.acquire()
        try:
            super().process_request(request, client_address)
        except Exception:
            # Release on failure so the slot is not leaked.
            self._thread_semaphore.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            # Release the slot held for the duration of the request.
            self._thread_semaphore.release()


# API Configuration
API_VERSION = "v1"
API_PREFIX = f"/api/{API_VERSION}"

# API endpoint paths
ENDPOINT_HEALTH = f"{API_PREFIX}/health"
ENDPOINT_TIME = f"{API_PREFIX}/time"
ENDPOINT_BUCKETS_PREFIX = f"{API_PREFIX}/buckets"
ENDPOINT_FILES_SUFFIX = "/files"
ENDPOINT_FILES_DOWNLOAD_SUFFIX = "/files/download"
ENDPOINT_FILES_UPLOAD_STATUS_SUFFIX = "/files/upload-status"
ENDPOINT_DIRS_SUFFIX = "/dirs"
ENDPOINT_TOMBSTONES_SUFFIX = "/tombstones"
ENDPOINT_SYMLINKS_SUFFIX = "/symlinks"
ENDPOINT_DELTA_SIGNATURES_SUFFIX = "/files/delta/signatures"
ENDPOINT_DELTA_APPLY_SUFFIX = "/files/delta/apply"
ENDPOINT_DELTA_DATA_MARKER = "/files/delta/data/"

# Content Types
CONTENT_TYPE_JSON = "application/json"
CONTENT_TYPE_OCTET_STREAM = "application/octet-stream"

# HTTP Status Codes
HTTP_OK = 200
HTTP_CREATED = 201
HTTP_ACCEPTED = 202
HTTP_PARTIAL_CONTENT = 206
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_RANGE_NOT_SATISFIABLE = 416
HTTP_INTERNAL_ERROR = 500
HTTP_INSUFFICIENT_STORAGE = 507

# Cap on tombstone-gossip POST bodies: the store itself is capped at
# TOMBSTONE_MAX_ENTRIES entries (at most ~165 bytes of JSON each with a
# content hash), so a larger payload can never be legitimate and would only
# waste memory.
MAX_TOMBSTONE_PAYLOAD_BYTES = TOMBSTONE_MAX_ENTRIES * 192

# Wire-supplied mtimes/ctimes may not exceed now + this skew: a peer
# asserting far-future timestamps would win every mtime-based conflict
# resolution on every other peer (conflict engineering).
MTIME_MAX_FUTURE_SKEW_SECONDS = 300

# Multipart parsing bounds: form-field values and part-header lines are
# buffered in memory, so they need a hard cap — an unbounded buffer lets a
# peer exhaust master RAM with a single giant "field" (the file part itself
# is streamed to disk and needs no such cap).
MAX_FORM_FIELD_BYTES = 64 * 1024
MAX_PART_HEADER_LINE_BYTES = 8 * 1024


class _CappedBytesWriter:
    """File-like wrapper that aborts once more than *cap* bytes are written.

    Placed between a decompressor and the destination file so a hostile
    upload cannot expand a small compressed payload past the effective cap
    — min(declared size, ``MAX_INBOUND_SIZE``) — (decompression bomb
    containment on the receiving side).
    """

    def __init__(self, write, cap: int) -> None:
        self._write = write
        self._cap = cap
        self.written = 0

    def write(self, data: bytes):
        self.written += len(data)
        if self.written > self._cap:
            raise ValueError(f"Upload exceeds the maximum allowed size ({self._cap} bytes)")
        return self._write(data)

    def close(self) -> None:
        """No-op: the underlying file is owned and closed by the caller."""


# =============================================================================
# Multipart Parser
# =============================================================================


class _StreamingMultipartParser:
    """
    Streaming multipart/form-data parser.

    Reads small form fields into memory and streams the file part body via
    a writer callback (bytes -> None), avoiding loading huge uploads into RAM.

    Usage:
        parser = _StreamingMultipartParser(rfile, content_length, boundary)
        form_fields, file_meta = parser.parse(file_writer)

    Where file_meta is (field_name, filename) or None if no file part was found.
    """

    _READ_CHUNK = 64 * 1024

    def __init__(self, rfile, content_length: int, boundary: bytes) -> None:
        self._rfile = rfile
        self._remaining = content_length
        # Boundary lines in multipart are prefixed with "--"
        self._boundary = b"--" + boundary
        # Mutable buffer: ``del buf[:emit]`` trims consumed bytes in-place
        # (O(1) amortized) instead of re-copying the whole tail per flush.
        self._buf = bytearray()
        # Scan restart offset after a flush. A terminator can straddle the
        # flush cut; because the kept safety window (tlen+2 bytes) is larger
        # than a terminator, only the last tlen-1 kept bytes can ever complete
        # a straddling match, so the scan restarts there instead of at 0.
        self._search_from = 0

    def _read_more(self, n: int = _READ_CHUNK) -> bytes:
        n = min(n, self._remaining)
        if n <= 0:
            return b""
        chunk = self._rfile.read(n)
        if chunk:
            self._remaining -= len(chunk)
            self._buf += chunk
        return chunk

    def _read_line(self) -> bytes | None:
        """Read one CRLF-terminated line, replenishing the buffer as needed.

        Returns the line without the trailing CRLF, or None on EOF.
        Raises ValueError if the line grows past the header-line cap.
        """
        while True:
            idx = self._buf.find(b"\r\n")
            if idx != -1:
                line = bytes(self._buf[:idx])
                del self._buf[: idx + 2]
                return line
            # No CRLF in the buffer yet: if the accumulated bytes (which form
            # a single unterminated line) exceed the cap, reject. Only check
            # when there is no CRLF, because a buffer WITH a CRLF contains a
            # complete (possibly short) line plus lookahead for the next read.
            if len(self._buf) > MAX_PART_HEADER_LINE_BYTES:
                log.warning(
                    f"multipart header line too long ({len(self._buf)} bytes, "
                    f"cap {MAX_PART_HEADER_LINE_BYTES}): "
                    f"buf_head={bytes(self._buf[:256])!r}"
                )
                raise ValueError("multipart header line too long")
            if not self._read_more():
                break
        return None

    def _read_until_boundary(self, writer) -> str:
        """Write body bytes to ``writer`` until the next boundary line.

        ``writer`` is a callable accepting bytes (or None to discard).
        Returns "next" if another part follows, "end" for the closing boundary,
        or "eof" if the stream ended before any boundary was found.
        """
        terminator = b"\r\n" + self._boundary
        tlen = len(terminator)
        # Need at least tlen+2 bytes to safely emit, to disambiguate the
        # trailing CRLF before the boundary from in-body content.
        safety = tlen + 2

        while True:
            idx = self._buf.find(terminator, self._search_from)
            if idx != -1:
                if writer is not None and idx > 0:
                    writer(bytes(self._buf[:idx]))
                # Consume up to and including the boundary line
                after_boundary = idx + tlen
                # The two bytes after the boundary are either b"\r\n" (next
                # part) or b"--" (final boundary).
                while len(self._buf) < after_boundary + 2 and self._read_more():
                    pass
                marker = bytes(self._buf[after_boundary : after_boundary + 2])
                del self._buf[: after_boundary + 2]
                self._search_from = 0
                if marker == b"--":
                    # Final boundary; discard the optional epilogue CRLF
                    if self._buf.startswith(b"\r\n"):
                        del self._buf[:2]
                    return "end"
                # Next part: consume past CRLF
                return "next"

            # No boundary yet: flush everything except the trailing safety window
            if len(self._buf) > safety:
                emit = len(self._buf) - safety
                if writer is not None:
                    writer(bytes(self._buf[:emit]))
                del self._buf[:emit]
                # A terminator may straddle the flush cut: restart the scan at
                # most tlen-1 bytes from the end (the kept window is >= that).
                self._search_from = max(0, len(self._buf) - (tlen - 1))

            if not self._read_more():
                # EOF: flush whatever remains
                if writer is not None and self._buf:
                    writer(bytes(self._buf))
                self._buf.clear()
                return "eof"

    def parse(self, file_open) -> tuple[dict[str, str], tuple[str, str] | None]:
        """Parse the multipart body.

        ``file_open`` is a callable invoked once when the file part header
        is decoded, with signature
        ``file_open(form_fields, field_name, filename) -> writer``
        where ``writer`` is a callable ``bytes -> None`` receiving successive
        chunks of the file body. ``form_fields`` is the dict of form fields
        decoded so far (all fields preceding the file part are guaranteed to
        be present). Pass ``None`` to ignore the file body.

        Returns ``(form_fields, file_meta)`` where ``file_meta`` is
        ``(field_name, filename)`` if a file part was seen.
        """
        import re

        form_fields: dict[str, str] = {}
        file_meta: tuple[str, str] | None = None

        # Skip preamble: read lines until we hit the first boundary line
        while True:
            line = self._read_line()
            if line is None:
                return form_fields, file_meta
            if line == self._boundary:
                break
            if line == self._boundary + b"--":
                return form_fields, file_meta

        while True:
            # Parse part headers
            headers: dict[bytes, bytes] = {}
            while True:
                line = self._read_line()
                if line is None:
                    return form_fields, file_meta
                if line == b"":
                    break
                if b":" in line:
                    k, _, v = line.partition(b":")
                    headers[k.strip().lower()] = v.strip()

            cd = headers.get(b"content-disposition", b"").decode("utf-8", "replace")
            name_m = re.search(r'name="([^"]*)"', cd)
            filename_m = re.search(r'filename="([^"]*)"', cd)

            if filename_m and name_m and file_open is not None and file_meta is None:
                field_name = name_m.group(1)
                filename = filename_m.group(1)
                file_meta = (field_name, filename)
                writer = file_open(form_fields, field_name, filename)
                marker = self._read_until_boundary(writer)
            elif name_m:
                # Form field: buffered in memory, so hard-capped (the file
                # part is the only part allowed to be large, and it streams).
                field_buf = bytearray()

                def _field_writer(data: bytes, _buf=field_buf) -> None:
                    if len(_buf) + len(data) > MAX_FORM_FIELD_BYTES:
                        raise ValueError(
                            f"multipart form field exceeds {MAX_FORM_FIELD_BYTES} bytes"
                        )
                    _buf.extend(data)

                marker = self._read_until_boundary(_field_writer)
                form_fields[name_m.group(1)] = bytes(field_buf).decode("utf-8", "replace")
            else:
                marker = self._read_until_boundary(None)

            if marker in ("end", "eof"):
                return form_fields, file_meta


# =============================================================================
# Request Handler
# =============================================================================


class LANRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for LAN master server."""

    # Reference to the server instance for accessing config
    server: LANServer  # type: ignore[assignment]

    def log_message(self, format: str, *args) -> None:
        """Override to use our logger instead of stderr."""
        log.debug(f"{self.address_string()} - {format % args}")

    def _send_json_response(self, status: int, data: dict | list) -> None:
        """Send a JSON response."""
        self.send_response(status)
        self.send_header("Content-Type", CONTENT_TYPE_JSON)
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _send_error_response(self, status: int, message: str) -> None:
        """Send an error response."""
        self._send_json_response(status, {"error": message})

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

    def _check_auth(self) -> bool:
        """Check authentication if LAN_KEY is configured."""
        lan_key = CONFIG.get("LAN_KEY")
        if not lan_key:
            # No authentication required
            return True

        # Check Authorization header. Use a constant-time comparison so the
        # validity of the bearer token cannot be inferred from response timing.
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            if hmac.compare_digest(token, str(lan_key)):
                return True

        self._send_error_response(HTTP_UNAUTHORIZED, "Unauthorized")
        return False

    def do_GET(self) -> None:
        """Handle GET requests."""
        if not self._check_auth():
            return

        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # Health check
        if path == ENDPOINT_HEALTH:
            self._handle_health()
            return

        # Time endpoint for clock synchronization
        if path == ENDPOINT_TIME:
            self._handle_time()
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
            from hsync_client.lan_delta import handle_delta_signatures_get

            handle_delta_signatures_get(self, query)
            return

        # Delta literal payload: /api/v1/buckets/{bucket}/files/delta/data/{token}
        if ENDPOINT_DELTA_DATA_MARKER in path and ENDPOINT_BUCKETS_PREFIX in path:
            from hsync_client.lan_delta import handle_delta_data

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
        if not self._check_auth():
            return

        parsed = urlparse(self.path)
        path = parsed.path

        # Tombstone gossip (slave -> master): /api/v1/buckets/{bucket}/tombstones
        if path.endswith(ENDPOINT_TOMBSTONES_SUFFIX) and ENDPOINT_BUCKETS_PREFIX in path:
            self._handle_tombstones_merge()
            return

        # Symlink creation (slave -> master): /api/v1/buckets/{bucket}/symlinks
        if path.endswith(ENDPOINT_SYMLINKS_SUFFIX) and ENDPOINT_BUCKETS_PREFIX in path:
            self._handle_symlink(parse_qs(urlparse(self.path).query))
            return

        # Delta signatures (download delta): /api/v1/buckets/{bucket}/files/delta/signatures
        if path.endswith(ENDPOINT_DELTA_SIGNATURES_SUFFIX) and ENDPOINT_BUCKETS_PREFIX in path:
            from hsync_client.lan_delta import handle_delta_signatures_post

            handle_delta_signatures_post(self)
            return

        # Delta apply (upload delta): /api/v1/buckets/{bucket}/files/delta/apply
        if path.endswith(ENDPOINT_DELTA_APPLY_SUFFIX) and ENDPOINT_BUCKETS_PREFIX in path:
            self._handle_delta_apply()
            return

        # Create directory: /api/v1/buckets/{bucket}/dirs
        if path.endswith(ENDPOINT_DIRS_SUFFIX) and ENDPOINT_BUCKETS_PREFIX in path:
            query = parse_qs(urlparse(self.path).query)
            self._handle_create_dir(query)
            return

        # Upload file: /api/v1/buckets/{bucket}/files
        if path.endswith(ENDPOINT_FILES_SUFFIX) and ENDPOINT_BUCKETS_PREFIX in path:
            self._handle_upload()
            return

        self._send_error_response(HTTP_NOT_FOUND, "Not found")

    def do_DELETE(self) -> None:
        """Handle DELETE requests for bidirectional sync."""
        if not self._check_auth():
            return

        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # Delete file: /api/v1/buckets/{bucket}/files?path=...
        if path.endswith(ENDPOINT_FILES_SUFFIX) and ENDPOINT_BUCKETS_PREFIX in path:
            self._handle_delete(query)
            return

        self._send_error_response(HTTP_NOT_FOUND, "Not found")

    def do_PATCH(self) -> None:
        """Handle PATCH requests for rename operations."""
        if not self._check_auth():
            return

        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # Rename file/dir: /api/v1/buckets/{bucket}/files?src=...&dst=...
        if path.endswith(ENDPOINT_FILES_SUFFIX) and ENDPOINT_BUCKETS_PREFIX in path:
            self._handle_rename(query)
            return

        self._send_error_response(HTTP_NOT_FOUND, "Not found")

    def _handle_rename(self, query: dict) -> None:
        """Handle file/directory rename from client."""
        from os import rename

        src_rel = query.get("src", [None])[0]
        dst_rel = query.get("dst", [None])[0]

        if not src_rel or not dst_rel:
            self._send_error_response(HTTP_BAD_REQUEST, "Missing 'src' or 'dst' parameter")
            return

        local_bucket = CONFIG.get("LOCAL_BUCKET", "")
        if not local_bucket:
            self._send_error_response(HTTP_NOT_FOUND, "Bucket not configured")
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
            # only honoured on authenticated requests: without LAN_KEY any LAN
            # peer could spoof another peer's CID and suppress its events.
            origin_cid = query.get("cid", [None])[0]
            if origin_cid and is_valid_cid(origin_cid):
                from hsync_client.lan_websocket import mark_inbound_origin

                authenticated = bool(CONFIG.get("LAN_KEY"))
                mark_inbound_origin(src_path, origin_cid, authenticated=authenticated)
                mark_inbound_origin(dst_path, origin_cid, authenticated=authenticated)

            rename(src_path, dst_path)

            self._send_json_response(HTTP_OK, {"info": "Renamed successfully"})

        except Exception as e:
            log.error(f"Error renaming {src_path} -> {dst_path}: {e}")
            self._send_error_response(HTTP_INTERNAL_ERROR, f"Rename error: {e}")

    def _handle_delete(self, query: dict) -> None:
        """Handle file/directory deletion from client."""
        from shutil import rmtree

        rel_path = query.get("path", [None])[0]
        is_dir = query.get("is_dir", ["false"])[0].lower() == "true"

        if not rel_path:
            self._send_error_response(HTTP_BAD_REQUEST, "Missing 'path' parameter")
            return

        local_bucket = CONFIG.get("LOCAL_BUCKET", "")
        if not local_bucket:
            self._send_error_response(HTTP_NOT_FOUND, "Bucket not configured")
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
            from hsync_client.lan_websocket import mark_inbound_origin

            authenticated = bool(CONFIG.get("LAN_KEY"))
            mark_inbound_origin(full_path, origin_cid, authenticated=authenticated)
            marked = True
        log.debug(
            f"[DELETE-API] delete branch: path={full_path} is_dir={is_dir} "
            f"origin_cid={origin_cid} marked={marked} lan_key_set={bool(CONFIG.get('LAN_KEY'))}"
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
        parent = str(Path(deleted_file).parent)
        if not parent or not Path(parent).exists() or not Path(parent).is_dir():
            return
        try:
            entries = os.listdir(parent)
        except OSError:
            return
        has_syncable = any(not is_hidden_file(e) for e in entries)
        if has_syncable:
            return
        sentinel = str(Path(parent) / HSYNCKEEP_FILENAME)
        if not Path(sentinel).exists():
            try:
                open(sentinel, "w").close()  # noqa: SIM115
                log.debug(f'Created sentinel "{sentinel}"')
            except OSError as exc:
                log.warning(f'Cannot create sentinel "{sentinel}": {exc}')

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
        rel_path = query.get("path", [None])[0]
        if not rel_path:
            self._send_error_response(HTTP_BAD_REQUEST, "Missing 'path' parameter")
            return

        local_bucket = CONFIG.get("LOCAL_BUCKET", "")
        if not local_bucket:
            self._send_error_response(HTTP_NOT_FOUND, "Bucket not configured")
            return

        # Peer-supplied path is untrusted: validate and confine to the bucket
        # (rejects traversal, absolute paths and sibling-prefix confusion).
        full_path = resolve_within(local_bucket, rel_path)
        if not full_path:
            self._send_error_response(HTTP_BAD_REQUEST, "Invalid path")
            return

        try:
            # Only mark inbound origin if the directory doesn't already
            # exist: an already-existing directory means the slave is
            # echoing a directory it received from us via broadcast, so
            # the mark must not be recorded (it would paint the slave's
            # CID over a genuinely local path, causing later local
            # events to be misattributed as REMOTE).
            if not Path(full_path).exists():
                # A symlink *inside* the bucket pointing outside it must not
                # redirect the mkdir into the out-of-bucket location (the
                # path was already confined via resolve_within, which resolves
                # existing symlink components — a symlink that would escape is
                # rejected there). Nothing extra is needed here: resolve_within
                # returns None when the path crosses an escaping symlink.
                origin_cid = query.get("cid", [None])[0]
                if origin_cid and is_valid_cid(origin_cid):
                    from hsync_client.lan_websocket import mark_inbound_origin

                    authenticated = bool(CONFIG.get("LAN_KEY"))
                    mark_inbound_origin(full_path, origin_cid, authenticated=authenticated)

            Path(full_path).mkdir(parents=True, exist_ok=True)

            # Apply optional permissions if provided.
            permissions = query.get("permissions", [None])[0]
            if permissions:
                from hsync_client.utils import set_permissions

                set_permissions(full_path, permissions)

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
        mode and LAN mode behave identically.
        """
        from os import symlink as os_symlink

        from hsync_common.path_validation import is_safe_symlink_target

        rel_path = query.get("path", [None])[0]
        target = query.get("target", [None])[0]

        if not rel_path or not target:
            self._send_error_response(HTTP_BAD_REQUEST, "Missing 'path' or 'target' parameter")
            return

        local_bucket = CONFIG.get("LOCAL_BUCKET", "")
        if not local_bucket:
            self._send_error_response(HTTP_NOT_FOUND, "Bucket not configured")
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
                from hsync_client.lan_websocket import mark_inbound_origin

                authenticated = bool(CONFIG.get("LAN_KEY"))
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
        self._send_json_response(HTTP_OK, {"status": "ok", "mode": "lan_master"})

    def _handle_time(self) -> None:
        """Handle time endpoint for clock synchronization.

        Returns the master's current timestamp so slaves can calculate
        clock offset using round-trip measurement (NTP-like).
        """
        self._send_json_response(HTTP_OK, {"timestamp": time.time()})

    def _handle_list_files(self, query: dict) -> None:
        """Handle list files endpoint."""
        filter_path = query.get("path", [None])[0]

        local_bucket = CONFIG.get("LOCAL_BUCKET", "")
        if not local_bucket or not Path(local_bucket).exists():
            self._send_error_response(HTTP_NOT_FOUND, "Bucket not found")
            return

        from hsync_client.sync_state import flush as _flush_state

        file_list = []
        self_recorded = False
        for root, dirs, files in walk(local_bucket):
            # Skip hidden directories
            dirs[:] = [d for d in dirs if not is_hidden_dir(d)]

            for file in files:
                # Skip hidden files
                if is_hidden_file(file):
                    continue

                file_path = str(Path(root) / file)
                # Symlinks are emitted as structural entries (path + target),
                # never dereferenced: File.get() (exists/stat/sha256) would
                # follow the link and the listing would either exfiltrate an
                # out-of-bucket target (e.g. ``secret -> /etc/passwd``) to every
                # slave or advertise the target content instead of the link.
                if Path(file_path).is_symlink():
                    from hsync_common.models.symlink import Symlink
                    from hsync_common.path_validation import is_safe_symlink_target

                    try:
                        link = Symlink.get(file_path)
                    except OSError:
                        link = None
                    if link is not None and not is_safe_symlink_target(
                        file_path, link.target, local_bucket
                    ):
                        log.warning(
                            f"{ActionLabel.SECURITY} Symlink target escapes bucket, "
                            f'skipping: "{file_path}" -> "{link.target}"'
                        )
                        continue
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
            if end > file_size:
                end = file_size
            if start >= end:
                return None

            return (start, end)
        except (ValueError, IndexError):
            return None

    def _handle_download(self, query: dict) -> None:
        """Handle file download endpoint with Range request support."""
        filename = query.get("filename", [None])[0]
        if not filename:
            self._send_error_response(HTTP_BAD_REQUEST, "Missing filename parameter")
            return
        origin_cid = query.get("cid", [None])[0]
        cid_suffix = f" (CID: {origin_cid})" if origin_cid else ""
        log.info(f'{ActionLabel.REMOTE_DOWNLOAD} "{filename}"{cid_suffix}')

        local_bucket = CONFIG.get("LOCAL_BUCKET", "")
        # Peer-supplied path is untrusted: validate and confine to the bucket
        # (rejects traversal, absolute paths and sibling-prefix confusion).
        filepath = resolve_within(local_bucket, filename)
        if not filepath:
            self._send_error_response(HTTP_FORBIDDEN, "Access denied")
            return

        # A peer can request a hidden/ignored file by its explicit
        # path. Reject it so the master never serves files the local
        # user excluded from sync (e.g. via .hsyncignore or dot-files).
        from hsync_common.ignore import should_skip

        if should_skip(filepath):
            self._send_error_response(HTTP_FORBIDDEN, "Access denied")
            return

        if not Path(filepath).exists():
            self._send_error_response(HTTP_NOT_FOUND, "File not found")
            return

        # A symlink inside the bucket whose target escapes it must never be
        # dereferenced: File.get() (exists/stat/sha256) and the read below
        # would follow it and serve the out-of-bucket target (e.g.
        # ``secret -> /etc/passwd``) to the requesting peer.
        if Path(filepath).is_symlink():
            from hsync_common.models.symlink import Symlink
            from hsync_common.path_validation import is_safe_symlink_target

            try:
                link = Symlink.get(filepath)
            except OSError:
                link = None
            if link is not None and not is_safe_symlink_target(filepath, link.target, local_bucket):
                log.warning(
                    f"{ActionLabel.SECURITY} Symlink target escapes bucket, "
                    f'not serving: "{filepath}" -> "{link.target}"'
                )
                self._send_error_response(HTTP_FORBIDDEN, "Access denied")
                return

        try:
            # Get file metadata
            file_obj = File.get(filepath)
            if not file_obj:
                self._send_error_response(HTTP_NOT_FOUND, "File not found")
                return

            file_size = file_obj.size or 0

            # Check for Range header
            range_header = self.headers.get("Range")
            range_tuple = (
                self._parse_range_header(range_header, file_size) if range_header else None
            )

            if range_header and range_tuple is None:
                # Invalid range requested
                self.send_response(HTTP_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"*/{file_size}")
                self.end_headers()
                return

            with open(filepath, "rb") as f:
                if range_tuple:
                    # Partial content — stream in chunks, never load the
                    # whole range into RAM (master LAN RAM exhaustion).
                    start, end = range_tuple
                    f.seek(start)
                    range_len = end - start

                    self.send_response(HTTP_PARTIAL_CONTENT)
                    self.send_header("Content-Type", CONTENT_TYPE_OCTET_STREAM)
                    self.send_header("Content-Length", str(range_len))
                    self.send_header("Content-Range", f"bytes {start}-{end - 1}/{file_size}")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("X-SHA256", file_obj.sha256sum or "")
                    self.send_header("X-Permissions", file_obj.permissions or "")
                    self.end_headers()
                    remaining = range_len
                    while remaining > 0:
                        chunk = f.read(min(_LAN_UPLOAD_CHUNK_SIZE, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                else:
                    # Full file — stream in chunks, never load the whole
                    # file into RAM (master LAN RAM exhaustion).
                    # Use compression if enabled and beneficial.
                    # In LAN mode, use higher threshold (100KB) since local network is fast
                    use_compression = CONFIG.get("COMPRESSION_ENABLED", False) and should_compress(
                        filepath, file_size, is_lan=True
                    )

                    if use_compression:
                        # Compression requires the full content to compute the
                        # zstd frame; cap the in-memory size to MAX_INBOUND_SIZE
                        # so a hostile huge file cannot OOM the master. When the
                        # cap is not configured (0), fall back to the default
                        # max download size so an unbounded f.read() is never
                        # possible on the compressed branch.
                        max_inbound = CONFIG.get("MAX_INBOUND_SIZE", 0) or (
                            MAX_INBOUND_SIZE_MB_DEFAULT * 1024 * 1024
                        )
                        if max_inbound and file_size > max_inbound:
                            self._send_error_response(
                                HTTP_FORBIDDEN, "File too large for compressed transfer"
                            )
                            return
                        content = f.read(max_inbound)
                        compressed_content = compress_data(content)
                        log.debug(
                            f"Compressed download: {len(content)} -> "
                            f"{len(compressed_content)} bytes"
                        )
                        self.send_response(HTTP_OK)
                        self.send_header("Content-Type", CONTENT_TYPE_OCTET_STREAM)
                        self.send_header("Content-Length", str(len(compressed_content)))
                        self.send_header("Content-Encoding", CONTENT_ENCODING_ZSTD)
                        self.send_header("X-SHA256", file_obj.sha256sum or "")
                        self.send_header("X-Permissions", file_obj.permissions or "")
                        self.end_headers()
                        self.wfile.write(compressed_content)
                    else:
                        self.send_response(HTTP_OK)
                        self.send_header("Content-Type", CONTENT_TYPE_OCTET_STREAM)
                        self.send_header("Content-Length", str(file_size))
                        self.send_header("Accept-Ranges", "bytes")
                        self.send_header("X-SHA256", file_obj.sha256sum or "")
                        self.send_header("X-Permissions", file_obj.permissions or "")
                        self.end_headers()
                        while True:
                            chunk = f.read(_LAN_UPLOAD_CHUNK_SIZE)
                            if not chunk:
                                break
                            self.wfile.write(chunk)

        except OSError as e:
            log.error(f"Error reading file {filepath}: {e}")
            self._send_error_response(HTTP_INTERNAL_ERROR, "Error reading file")

    def _handle_upload_status(self, query: dict) -> None:
        """Handle GET /api/v1/buckets/{bucket}/files/upload-status.

        Reports whether an interrupted upload can be resumed from a partial
        ``.part`` file, mirroring the central server's endpoint so the same
        client resume logic works in LAN P2P mode.

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

        local_bucket = CONFIG.get("LOCAL_BUCKET", "")
        if not local_bucket:
            self._send_error_response(HTTP_NOT_FOUND, "Bucket not configured")
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

        import re

        content_type = self.headers.get("Content-Type", "")

        if "multipart/form-data" not in content_type:
            self._send_error_response(HTTP_BAD_REQUEST, "Expected multipart/form-data")
            return

        match = re.search(r"boundary=([^\s;]+)", content_type)
        if not match:
            self._send_error_response(HTTP_BAD_REQUEST, "Missing multipart boundary")
            return
        boundary = match.group(1).encode()
        if boundary.startswith(b'"') and boundary.endswith(b'"'):
            boundary = boundary[1:-1]

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
                from hsync_client.lan_websocket import mark_inbound_origin

                authenticated = bool(CONFIG.get("LAN_KEY"))
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
                if incoming_corrected < 0:
                    incoming_corrected = 0
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

            # Record the agreed version with the merged clock (D11).
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
            self._send_error_response_safe(HTTP_FORBIDDEN, "Access denied")
            response_sent = True
        except ValueError as e:
            # Malformed upload metadata (e.g. missing size on a compressed
            # upload) or decompressed bytes beyond the declared size.
            self._send_error_response_safe(HTTP_BAD_REQUEST, f"Invalid upload: {e}")
            response_sent = True
        except Exception as e:
            log.error(f"Error handling upload: {e}")
            if not response_sent:
                self._send_error_response_safe(HTTP_INTERNAL_ERROR, f"Upload error: {e}")
        finally:
            # Cleanup tmp file on failure
            if tmp_path and Path(tmp_path).exists():
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            # Close any still-open file handle from a failed stream
            if out_file is not None:
                try:
                    out_file.close()
                except Exception:
                    pass
            if raw_file is not None and raw_file is not out_file:
                try:
                    raw_file.close()
                except Exception:
                    pass

    def _handle_delta_apply(self) -> None:
        """Handle a delta apply request (upload delta, streaming multipart).

        The literal payload is streamed to a temp file (never buffered in
        RAM); the JSON instructions travel in the ``metadata`` form field and
        are applied by :func:`hsync_client.lan_delta.handle_delta_apply`.
        """
        import re

        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_error_response(HTTP_BAD_REQUEST, "Expected multipart/form-data")
            return
        match = re.search(r"boundary=([^\s;]+)", content_type)
        if not match:
            self._send_error_response(HTTP_BAD_REQUEST, "Missing multipart boundary")
            return
        boundary = match.group(1).encode()
        if boundary.startswith(b'"') and boundary.endswith(b'"'):
            boundary = boundary[1:-1]

        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._send_error_response(HTTP_BAD_REQUEST, "Invalid Content-Length")
            return
        if content_length <= 0:
            self._send_error_response(HTTP_BAD_REQUEST, "No content")
            return

        from hsync_common.constants import DELTA_MAX_LITERAL_SIZE

        from hsync_client.lan_delta import handle_delta_apply

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

            handle_delta_apply(self, form_fields, literal_path)
        except ValueError as e:
            self._send_error_response_safe(HTTP_BAD_REQUEST, f"Invalid delta apply: {e}")
        except Exception as e:
            log.error(f"Error handling delta apply: {e}")
            self._send_error_response_safe(HTTP_INTERNAL_ERROR, "Delta apply error")
        finally:
            if raw_file is not None:
                try:
                    raw_file.close()
                except Exception:
                    pass
            if literal_path and Path(literal_path).exists():
                try:
                    os.remove(literal_path)
                except OSError:
                    pass


# =============================================================================
# LAN Server
# =============================================================================


class LANServer:
    """
    Embedded HTTP/HTTPS server for LAN master mode.

    Runs in a background thread and serves the hsync API to peer clients.
    Supports both HTTP and HTTPS (with self-signed certificates).
    """

    def __init__(
        self,
        port: int,
        use_https: bool = False,
        cert_file: str | None = None,
        key_file: str | None = None,
    ) -> None:
        """
        Initialize the LAN server.

        Args:
            port: Port to listen on.
            use_https: Whether to use HTTPS instead of HTTP.
            cert_file: Path to SSL certificate file (required if use_https=True).
            key_file: Path to SSL private key file (required if use_https=True).
                The persistent master key is stored unencrypted and protected by
                filesystem permissions (see ``load_or_create_master_cert``).
        """
        self._port = port
        self._use_https = use_https
        self._cert_file = cert_file
        self._key_file = key_file
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._running = False

        # Validate HTTPS configuration
        if use_https and (not cert_file or not key_file):
            raise ValueError("cert_file and key_file are required when use_https=True")

    @property
    def port(self) -> int:
        """Get the server port."""
        return self._port

    @property
    def use_https(self) -> bool:
        """Check if HTTPS is enabled."""
        return self._use_https

    def start(self) -> None:
        """Start the server in a background thread."""
        import ssl

        if self._running:
            log.warning("LAN server already running")
            return

        self._server = BoundedThreadingHTTPServer(
            ("0.0.0.0", self._port), LANRequestHandler, max_threads=LAN_SERVER_MAX_THREADS
        )
        # Daemonize per-request threads so they don't block process shutdown
        self._server.daemon_threads = True
        # Cap the socket backlog so excess connections queue (bounded) instead
        # of being accepted immediately and consuming a thread slot.
        self._server.request_queue_size = LAN_SERVER_MAX_THREADS

        # Wrap socket with SSL if HTTPS is enabled
        if self._use_https:
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(
                self._cert_file,  # type: ignore[arg-type]
                self._key_file,  # type: ignore[arg-type]
            )
            self._server.socket = ssl_context.wrap_socket(
                self._server.socket,
                server_side=True,
            )
            protocol = "HTTPS"
        else:
            protocol = "HTTP"

        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self._running = True
        log.info(f"LAN master server started on port {self._port} ({protocol})")

    def _serve(self) -> None:
        """Server loop (runs in background thread)."""
        if self._server:
            self._server.serve_forever()

    def stop(self) -> None:
        """Stop the server."""
        if not self._running:
            return

        if self._server:
            self._server.shutdown()
            self._server.server_close()

        # Reap any parked delta literal payloads tied to this server lifetime.
        from hsync_client.lan_delta import reset_delta_lan_state

        reset_delta_lan_state()

        self._running = False
        log.info("LAN master server stopped")
