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
from http.server import HTTPServer, BaseHTTPRequestHandler
from os import walk
from os.path import join, exists
from typing import Optional
from urllib.parse import urlparse, parse_qs

from loguru import logger as LOG

from hsync_client.compression import (
    compress_data,
    decompress_data,
    should_compress,
    CONTENT_ENCODING_ZSTD,
)
from hsync_client.config import CONFIG
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


# =============================================================================
# Request Handler
# =============================================================================


class LANRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for LAN master server."""

    # Reference to the server instance for accessing config
    server: "LANServer"  # type: ignore[assignment]

    def log_message(self, format: str, *args) -> None:
        """Override to use our logger instead of stderr."""
        LOG.debug(f"{self.address_string()} - {format % args}")

    def _send_json_response(self, status: int, data: dict | list) -> None:
        """Send a JSON response."""
        self.send_response(status)
        self.send_header("Content-Type", CONTENT_TYPE_JSON)
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _send_error_response(self, status: int, message: str) -> None:
        """Send an error response."""
        self._send_json_response(status, {"error": message})

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
        """Handle file/directory rename from slave."""
        from os import rename, makedirs
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

            LOG.info(f"Renaming from slave request: {src_path} -> {dst_path}")
            rename(src_path, dst_path)

            self._send_json_response(HTTP_OK, {"info": "Renamed successfully"})

        except Exception as e:
            LOG.error(f"Error renaming {src_path} -> {dst_path}: {e}")
            self._send_error_response(HTTP_INTERNAL_ERROR, f"Rename error: {e}")

    def _handle_delete(self, query: dict) -> None:
        """Handle file/directory deletion from slave."""
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

        try:
            if is_dir:
                LOG.info(f"Deleting directory from slave request: {full_path}")
                rmtree(full_path, ignore_errors=False)
            else:
                LOG.info(f"Deleting file from slave request: {full_path}")
                os.remove(full_path)

            self._send_json_response(HTTP_OK, {"info": "Deleted successfully"})

        except Exception as e:
            LOG.error(f"Error deleting {full_path}: {e}")
            self._send_error_response(HTTP_INTERNAL_ERROR, f"Delete error: {e}")

    def _handle_health(self) -> None:
        """Handle health check endpoint."""
        self._send_json_response(HTTP_OK, {"status": "ok", "mode": "lan_master"})

    def _handle_list_files(self, query: dict) -> None:
        """Handle list files endpoint."""
        sync_order = query.get("sync_order", ["mtime"])[0]
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

        # Sort by configured order
        file_list.sort(key=lambda x: x.get(sync_order, 0))

        self._send_json_response(HTTP_OK, file_list)

    def _parse_range_header(self, range_header: str, file_size: int) -> Optional[tuple[int, int]]:
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
            range_tuple = self._parse_range_header(range_header, file_size) if range_header else None

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
                    use_compression = (
                        CONFIG.get("COMPRESSION_ENABLED", False)
                        and should_compress(filepath, file_size, is_lan=True)
                    )

                    if use_compression:
                        compressed_content = compress_data(content)
                        LOG.debug(
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

        except IOError as e:
            LOG.error(f"Error reading file {filepath}: {e}")
            self._send_error_response(HTTP_INTERNAL_ERROR, "Error reading file")

    def _handle_upload(self) -> None:
        """Handle file upload endpoint."""
        content_type = self.headers.get("Content-Type", "")

        if "multipart/form-data" not in content_type:
            self._send_error_response(HTTP_BAD_REQUEST, "Expected multipart/form-data")
            return

        # Parse multipart form data using our custom parser
        try:
            import os
            from hsync_client.utils import set_permissions

            # Read request body
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length == 0:
                self._send_error_response(400, "No content")
                return

            body = self.rfile.read(content_length)

            # Parse multipart data
            form_data, file_data = _parse_multipart(body, content_type)

            # Extract fields
            cid = form_data.get("cid", "")
            path = form_data.get("path", "")
            mtime = form_data.get("mtime", "")
            ctime = form_data.get("ctime", "")
            permissions = form_data.get("permissions", "-rw-rw-r--")
            is_compressed = form_data.get("compressed", "false").lower() == "true"

            # Get uploaded file
            if "file" not in file_data:
                self._send_error_response(400, "No file uploaded")
                return

            filename, file_content = file_data["file"]

            # Decompress if compressed
            if is_compressed:
                LOG.debug(f"Decompressing uploaded file: {len(file_content)} bytes")
                file_content = decompress_data(file_content)

            # Build destination path
            local_bucket = CONFIG.get("LOCAL_BUCKET", "")
            if path:
                dest_dir = join(local_bucket, path)
            else:
                dest_dir = local_bucket

            # Create directory if needed
            os.makedirs(dest_dir, exist_ok=True)

            dest_path = join(dest_dir, filename)

            # Security check
            real_bucket = os.path.realpath(local_bucket)
            real_dest = os.path.realpath(dest_path)
            if not real_dest.startswith(real_bucket):
                self._send_error_response(HTTP_FORBIDDEN, "Access denied")
                return

            # Write file
            with open(dest_path, "wb") as f:
                f.write(file_content)

            # Set timestamps if provided
            if mtime or ctime:
                try:
                    mtime_val = float(mtime) if mtime else None
                    atime_val = mtime_val or float(ctime) if ctime else None
                    if mtime_val:
                        os.utime(dest_path, (atime_val or mtime_val, mtime_val))
                except (ValueError, OSError):
                    pass

            # Set permissions if provided
            if permissions:
                set_permissions(dest_path, permissions)

            LOG.info(f"Received file from {cid}: {dest_path}")
            self._send_json_response(HTTP_CREATED, {
                "info": "File uploaded successfully",
                "file": dest_path,
            })

        except Exception as e:
            LOG.error(f"Error handling upload: {e}")
            self._send_error_response(HTTP_INTERNAL_ERROR, f"Upload error: {e}")


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
        cert_file: Optional[str] = None,
        key_file: Optional[str] = None,
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
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
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
            LOG.warning("LAN server already running")
            return

        self._server = HTTPServer(("0.0.0.0", self._port), LANRequestHandler)

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
        LOG.info(f"LAN master server started on port {self._port} ({protocol})")

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
        LOG.info("LAN master server stopped")

    def is_running(self) -> bool:
        """Check if server is running."""
        return self._running

