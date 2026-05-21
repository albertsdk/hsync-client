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

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from os import walk
from os.path import exists, join
from urllib.parse import parse_qs, urlparse

from loguru import logger as log

from hsync_client.compression import (
    CONTENT_ENCODING_ZSTD,
    compress_data,
    should_compress,
)
from hsync_client.config import CONFIG
from hsync_client.constants import HSYNCKEEP_FILENAME
from hsync_client.models.dir import is_hidden_dir
from hsync_client.models.file import File, is_hidden_file

# =============================================================================
# Constants
# =============================================================================

# API Configuration
API_VERSION = "v1"
API_PREFIX = f"/api/{API_VERSION}"

# API endpoint paths
ENDPOINT_HEALTH = f"{API_PREFIX}/health"
ENDPOINT_TIME = f"{API_PREFIX}/time"
ENDPOINT_BUCKETS_PREFIX = f"{API_PREFIX}/buckets"
ENDPOINT_FILES_SUFFIX = "/files"
ENDPOINT_FILES_DOWNLOAD_SUFFIX = "/files/download"

# Content Types
CONTENT_TYPE_JSON = "application/json"
CONTENT_TYPE_OCTET_STREAM = "application/octet-stream"

# HTTP Status Codes
HTTP_OK = 200
HTTP_CREATED = 201
HTTP_PARTIAL_CONTENT = 206
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_RANGE_NOT_SATISFIABLE = 416
HTTP_INTERNAL_ERROR = 500


# =============================================================================
# Multipart Parser
# =============================================================================


def _parse_multipart(
    body: bytes, content_type: str
) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    """
    Parse multipart/form-data request body.

    This is a simple parser that handles the format sent by requests library.
    It replaces the deprecated cgi.FieldStorage.

    Args:
        body: Raw request body bytes.
        content_type: Content-Type header value.

    Returns:
        Tuple of (form_fields, file_fields) where:
        - form_fields: dict of field_name -> value
        - file_fields: dict of field_name -> (filename, content)
    """
    import re

    # Extract boundary from content-type
    match = re.search(r"boundary=([^\s;]+)", content_type)
    if not match:
        raise ValueError("No boundary found in content-type")

    boundary = match.group(1).encode()
    # Handle quoted boundary
    if boundary.startswith(b'"') and boundary.endswith(b'"'):
        boundary = boundary[1:-1]

    form_data: dict[str, str] = {}
    file_data: dict[str, tuple[str, bytes]] = {}

    # Split by boundary
    parts = body.split(b"--" + boundary)

    for part in parts:
        # Skip empty parts and closing boundary
        if not part or part.strip() in (b"", b"--", b"--\r\n"):
            continue

        # Remove leading CRLF if present
        if part.startswith(b"\r\n"):
            part = part[2:]

        # Remove trailing CRLF and -- if present
        if part.endswith(b"--\r\n"):
            part = part[:-4]
        elif part.endswith(b"\r\n"):
            part = part[:-2]

        # Split headers from content
        if b"\r\n\r\n" in part:
            headers_raw, content = part.split(b"\r\n\r\n", 1)
        else:
            continue

        headers_str = headers_raw.decode("utf-8", errors="replace")

        # Parse Content-Disposition header
        name_match = re.search(r'name="([^"]*)"', headers_str)
        if not name_match:
            continue
        field_name = name_match.group(1)

        filename_match = re.search(r'filename="([^"]*)"', headers_str)
        if filename_match:
            # This is a file field
            filename = filename_match.group(1)
            file_data[field_name] = (filename, content)
        else:
            # This is a regular form field
            form_data[field_name] = content.decode("utf-8", errors="replace")

    return form_data, file_data


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
        self._buf = b""

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
        """
        while b"\r\n" not in self._buf:
            if not self._read_more():
                break
        if b"\r\n" not in self._buf:
            return None
        line, self._buf = self._buf.split(b"\r\n", 1)
        return line

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
            idx = self._buf.find(terminator)
            if idx != -1:
                if writer is not None and idx > 0:
                    writer(self._buf[:idx])
                # Consume up to and including the boundary line
                after_boundary = idx + tlen
                # The two bytes after the boundary are either b"\r\n" (next
                # part) or b"--" (final boundary).
                while len(self._buf) < after_boundary + 2 and self._read_more():
                    pass
                marker = self._buf[after_boundary : after_boundary + 2]
                if marker == b"--":
                    # Final boundary; advance past trailing CRLF too
                    self._buf = self._buf[after_boundary + 2 :]
                    # Discard the optional epilogue CRLF
                    if self._buf.startswith(b"\r\n"):
                        self._buf = self._buf[2:]
                    return "end"
                # Next part: advance past CRLF
                self._buf = self._buf[after_boundary + 2 :]
                return "next"

            # No boundary yet: flush everything except the trailing safety window
            if len(self._buf) > safety:
                emit = len(self._buf) - safety
                if writer is not None:
                    writer(self._buf[:emit])
                self._buf = self._buf[emit:]

            if not self._read_more():
                # EOF: flush whatever remains
                if writer is not None and self._buf:
                    writer(self._buf)
                self._buf = b""
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
                buf = bytearray()
                marker = self._read_until_boundary(buf.extend)
                form_fields[name_m.group(1)] = bytes(buf).decode("utf-8", "replace")
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

        # Check Authorization header
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            if token == lan_key:
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

        self._send_error_response(HTTP_NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        """Handle POST requests."""
        if not self._check_auth():
            return

        parsed = urlparse(self.path)
        path = parsed.path

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
        from os import makedirs, rename
        from os.path import dirname

        src_rel = query.get("src", [None])[0]
        dst_rel = query.get("dst", [None])[0]

        if not src_rel or not dst_rel:
            self._send_error_response(HTTP_BAD_REQUEST, "Missing 'src' or 'dst' parameter")
            return

        local_bucket = CONFIG.get("LOCAL_BUCKET", "")
        if not local_bucket:
            self._send_error_response(HTTP_NOT_FOUND, "Bucket not configured")
            return

        # Build full paths
        src_path = join(local_bucket, src_rel)
        dst_path = join(local_bucket, dst_rel)

        # Security check: ensure paths are within bucket
        try:
            real_bucket = os.path.realpath(local_bucket)
            real_src = os.path.realpath(src_path)
            # For dst, check parent since it doesn't exist yet
            real_dst_parent = os.path.realpath(dirname(dst_path) or local_bucket)
            if not real_src.startswith(real_bucket) or not real_dst_parent.startswith(real_bucket):
                self._send_error_response(HTTP_BAD_REQUEST, "Invalid path")
                return
        except Exception:
            self._send_error_response(HTTP_BAD_REQUEST, "Invalid path")
            return

        if not exists(src_path):
            self._send_error_response(HTTP_NOT_FOUND, "Source not found")
            return

        try:
            # Create parent directory if needed
            dst_parent = dirname(dst_path)
            if dst_parent and not exists(dst_parent):
                makedirs(dst_parent, exist_ok=True)

            # Mark both paths as inbound so the master's filesystem watcher
            # broadcasts the resulting `renamed` event with the originator
            # CID, allowing the sender to discard its own echo.
            origin_cid = query.get("cid", [None])[0]
            if origin_cid:
                from hsync_client.lan_websocket import mark_inbound_origin

                mark_inbound_origin(src_path, origin_cid)
                mark_inbound_origin(dst_path, origin_cid)

            log.info(f"Renaming from client request: {src_path} -> {dst_path}")
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

        # Build full path
        full_path = join(local_bucket, rel_path)

        # Security check: ensure path is within bucket
        try:
            real_bucket = os.path.realpath(local_bucket)
            real_path = os.path.realpath(full_path)
            if not real_path.startswith(real_bucket):
                self._send_error_response(HTTP_BAD_REQUEST, "Invalid path")
                return
        except Exception:
            self._send_error_response(HTTP_BAD_REQUEST, "Invalid path")
            return

        if not exists(full_path):
            # Already deleted, return success
            self._send_json_response(HTTP_OK, {"info": "File already deleted"})
            return

        # Mark the path as inbound so the resulting `deleted` event from the
        # filesystem watcher is broadcast with the originator's CID.
        origin_cid = query.get("cid", [None])[0]
        if origin_cid:
            from hsync_client.lan_websocket import mark_inbound_origin

            mark_inbound_origin(full_path, origin_cid)

        try:
            if is_dir:
                log.info(f"Deleting directory from client request: {full_path}")
                rmtree(full_path, ignore_errors=False)
            else:
                log.info(f"Deleting file from client request: {full_path}")
                os.remove(full_path)
                parent = os.path.dirname(full_path)
                if os.path.basename(full_path) == HSYNCKEEP_FILENAME:
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
        parent = os.path.dirname(deleted_file)
        if not parent or not exists(parent) or not os.path.isdir(parent):
            return
        try:
            entries = os.listdir(parent)
        except OSError:
            return
        has_syncable = any(not is_hidden_file(e) for e in entries)
        if has_syncable:
            return
        sentinel = join(parent, HSYNCKEEP_FILENAME)
        if not exists(sentinel):
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

        if not dir_path or not exists(dir_path) or not os.path.isdir(dir_path):
            return
        if safe_rmdir_if_empty(dir_path):
            log.debug(f'Removed empty directory "{dir_path}"')

    def _handle_health(self) -> None:
        """Handle health check endpoint."""
        self._send_json_response(HTTP_OK, {"status": "ok", "mode": "lan_master"})

    def _handle_time(self) -> None:
        """Handle time endpoint for clock synchronization.

        Returns the master's current timestamp so slaves can calculate
        clock offset using round-trip measurement (NTP-like).
        """
        import time

        self._send_json_response(HTTP_OK, {"timestamp": time.time()})

    def _handle_list_files(self, query: dict) -> None:
        """Handle list files endpoint."""
        filter_path = query.get("path", [None])[0]

        local_bucket = CONFIG.get("LOCAL_BUCKET", "")
        if not local_bucket or not exists(local_bucket):
            self._send_error_response(HTTP_NOT_FOUND, "Bucket not found")
            return

        file_list = []
        for root, dirs, files in walk(local_bucket):
            # Skip hidden directories
            dirs[:] = [d for d in dirs if not is_hidden_dir(d)]

            for file in files:
                # Skip hidden files
                if is_hidden_file(file):
                    continue

                file_obj = File.get(join(root, file))
                if file_obj:
                    file_dict = file_obj.to_dict()
                    # Normalize path to be relative to bucket (like server does)
                    file_normalized = File.from_dict(file_dict, local_bucket).to_dict()

                    # Apply path filter if specified
                    if filter_path:
                        rel_path = file_normalized.get("filepath", "")
                        if not rel_path.startswith(filter_path):
                            continue
                    file_list.append(file_normalized)

        # Sort by mtime ascending (oldest first) to prioritize updating older files
        file_list.sort(key=lambda x: x.get("mtime", 0))

        self._send_json_response(HTTP_OK, file_list)

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

        local_bucket = CONFIG.get("LOCAL_BUCKET", "")
        # Build full path - filename is relative to bucket
        if filename.startswith("/"):
            filepath = join(local_bucket, filename.lstrip("/"))
        else:
            filepath = join(local_bucket, filename)

        if not exists(filepath):
            self._send_error_response(HTTP_NOT_FOUND, "File not found")
            return

        # Security check: ensure file is within bucket
        import os.path

        real_bucket = os.path.realpath(local_bucket)
        real_file = os.path.realpath(filepath)
        if not real_file.startswith(real_bucket):
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
                    # Partial content
                    start, end = range_tuple
                    f.seek(start)
                    content = f.read(end - start)

                    self.send_response(HTTP_PARTIAL_CONTENT)
                    self.send_header("Content-Type", CONTENT_TYPE_OCTET_STREAM)
                    self.send_header("Content-Length", str(len(content)))
                    self.send_header("Content-Range", f"bytes {start}-{end - 1}/{file_size}")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("X-MD5", file_obj.md5sum or "")
                    self.send_header("X-Permissions", file_obj.permissions or "")
                    self.end_headers()
                    self.wfile.write(content)
                else:
                    # Full file - check if compression should be used
                    content = f.read()

                    # Use compression if enabled and beneficial
                    # In LAN mode, use higher threshold (100KB) since local network is fast
                    use_compression = CONFIG.get("COMPRESSION_ENABLED", False) and should_compress(
                        filepath, file_size, is_lan=True
                    )

                    if use_compression:
                        compressed_content = compress_data(content)
                        log.debug(
                            f"Compressed download: {len(content)} -> "
                            f"{len(compressed_content)} bytes"
                        )
                        self.send_response(HTTP_OK)
                        self.send_header("Content-Type", CONTENT_TYPE_OCTET_STREAM)
                        self.send_header("Content-Length", str(len(compressed_content)))
                        self.send_header("Content-Encoding", CONTENT_ENCODING_ZSTD)
                        self.send_header("X-MD5", file_obj.md5sum or "")
                        self.send_header("X-Permissions", file_obj.permissions or "")
                        self.end_headers()
                        self.wfile.write(compressed_content)
                    else:
                        self.send_response(HTTP_OK)
                        self.send_header("Content-Type", CONTENT_TYPE_OCTET_STREAM)
                        self.send_header("Content-Length", str(len(content)))
                        self.send_header("Accept-Ranges", "bytes")
                        self.send_header("X-MD5", file_obj.md5sum or "")
                        self.send_header("X-Permissions", file_obj.permissions or "")
                        self.end_headers()
                        self.wfile.write(content)

        except OSError as e:
            log.error(f"Error reading file {filepath}: {e}")
            self._send_error_response(HTTP_INTERNAL_ERROR, "Error reading file")

    def _handle_upload(self) -> None:
        """Handle file upload endpoint (streaming).

        The request body is parsed incrementally so files of arbitrary size
        can be received without buffering them entirely in RAM. The file part
        is written directly to a temporary ``.part`` file and atomically
        renamed on success.
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
        parser = _StreamingMultipartParser(self.rfile, content_length, boundary)

        # Mutable state shared with the file_open callback
        state: dict[str, object] = {}
        tmp_path: str | None = None
        response_sent = False

        def _file_open(form_fields, field_name, filename):
            path = form_fields.get("path", "")
            is_compressed_local = form_fields.get("compressed", "false").lower() == "true"

            dest_dir = join(local_bucket, path) if path else local_bucket
            os.makedirs(dest_dir, exist_ok=True)
            dest_path_local = join(dest_dir, filename)

            real_bucket = os.path.realpath(local_bucket)
            real_dest = os.path.realpath(dest_path_local)
            if not real_dest.startswith(real_bucket):
                raise PermissionError("Access denied")

            state["dest_path"] = dest_path_local
            tmp_path_local = dest_path_local + ".part"
            state["tmp_path"] = tmp_path_local
            raw = open(tmp_path_local, "wb")  # noqa: SIM115
            state["raw"] = raw
            if is_compressed_local:
                import zstandard as zstd

                writer = zstd.ZstdDecompressor().stream_writer(raw)
                state["f"] = writer
                return writer.write
            state["f"] = raw
            return raw.write

        try:
            form_fields, file_meta = parser.parse(_file_open)

            if file_meta is None or "f" not in state:
                self._send_error_response(HTTP_BAD_REQUEST, "No file uploaded")
                return

            # Close the writer (flushes zstd frame if used)
            writer = state["f"]
            raw = state["raw"]
            try:
                if writer is not raw:
                    writer.close()  # type: ignore[union-attr]
            finally:
                raw.close()  # type: ignore[union-attr]
            state.pop("f", None)
            state.pop("raw", None)

            tmp_path = state.get("tmp_path")  # type: ignore[assignment]
            dest_path = state["dest_path"]  # type: ignore[assignment]

            cid = form_fields.get("cid", "")
            mtime = form_fields.get("mtime", "")
            ctime = form_fields.get("ctime", "")
            permissions = form_fields.get("permissions", "-rw-rw-r--")

            if cid:
                from hsync_client.lan_websocket import mark_inbound_origin

                mark_inbound_origin(dest_path, cid)

            os.replace(tmp_path, dest_path)
            tmp_path = None

            if mtime or ctime:
                try:
                    mtime_val = float(mtime) if mtime else None
                    atime_val = mtime_val or (float(ctime) if ctime else None)
                    if mtime_val:
                        os.utime(dest_path, (atime_val or mtime_val, mtime_val))
                except (ValueError, OSError):
                    pass

            if permissions:
                set_permissions(dest_path, permissions)

            log.info(f"Received file from {cid}: {dest_path}")
            self._send_json_response(
                HTTP_CREATED,
                {"info": "File uploaded successfully", "file": dest_path},
            )
            response_sent = True

        except PermissionError:
            self._send_error_response_safe(HTTP_FORBIDDEN, "Access denied")
            response_sent = True
        except Exception as e:
            log.error(f"Error handling upload: {e}")
            if not response_sent:
                self._send_error_response_safe(HTTP_INTERNAL_ERROR, f"Upload error: {e}")
        finally:
            # Cleanup tmp file on failure
            if tmp_path and exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            # Close any still-open file handle from a failed stream
            f_obj = state.get("f")
            if f_obj is not None:
                try:
                    f_obj.close()  # type: ignore[union-attr]
                except Exception:
                    pass
            raw_obj = state.get("raw")
            if raw_obj is not None:
                try:
                    raw_obj.close()  # type: ignore[union-attr]
                except Exception:
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

        self._server = ThreadingHTTPServer(("0.0.0.0", self._port), LANRequestHandler)
        # Daemonize per-request threads so they don't block process shutdown
        self._server.daemon_threads = True

        # Wrap socket with SSL if HTTPS is enabled
        if self._use_https:
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(self._cert_file, self._key_file)  # type: ignore
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

        self._running = False
        log.info("LAN master server stopped")

    def is_running(self) -> bool:
        """Check if server is running."""
        return self._running
