# HSYNC Client - HTTP-based file synchronization client
# Copyright (C) 2025 Alberto Basso <alberto.sdk [at] gmail [dot] com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
Embedded HTTP Server for LAN Master Mode.

This module provides a lightweight HTTP server that runs when a client
becomes the master in LAN P2P mode. It exposes the essential hsync API
endpoints so that other peers can sync with the master.

Endpoints:
- GET  /api/v1/health              - Health check
- GET  /api/v1/buckets/{bucket}/files - List files
- GET  /api/v1/buckets/{bucket}/files/download - Download file
- POST /api/v1/buckets/{bucket}/files - Upload file
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from os import walk
from os.path import join, exists, dirname
from typing import Optional
from urllib.parse import urlparse, parse_qs

from hsync_client.config import CONFIG
from hsync_client.models.dir import is_hidden_dir
from hsync_client.models.file import File, is_hidden_file


# =============================================================================
# Constants
# =============================================================================

LOG = logging.getLogger("hsync.lan_server")


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
        self.send_header("Content-Type", "application/json")
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

        self._send_error_response(401, "Unauthorized")
        return False

    def do_GET(self) -> None:
        """Handle GET requests."""
        if not self._check_auth():
            return

        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # Health check
        if path == "/api/v1/health":
            self._handle_health()
            return

        # List files: /api/v1/buckets/{bucket}/files
        if path.endswith("/files") and "/buckets/" in path:
            self._handle_list_files(query)
            return

        # Download file: /api/v1/buckets/{bucket}/files/download
        if path.endswith("/files/download") and "/buckets/" in path:
            self._handle_download(query)
            return

        self._send_error_response(404, "Not found")

    def do_POST(self) -> None:
        """Handle POST requests."""
        if not self._check_auth():
            return

        parsed = urlparse(self.path)
        path = parsed.path

        # Upload file: /api/v1/buckets/{bucket}/files
        if path.endswith("/files") and "/buckets/" in path:
            self._handle_upload()
            return

        self._send_error_response(404, "Not found")

    def _handle_health(self) -> None:
        """Handle health check endpoint."""
        self._send_json_response(200, {"status": "ok", "mode": "lan_master"})

    def _handle_list_files(self, query: dict) -> None:
        """Handle list files endpoint."""
        sync_order = query.get("sync_order", ["mtime"])[0]
        filter_path = query.get("path", [None])[0]

        local_bucket = CONFIG.get("LOCAL_BUCKET", "")
        if not local_bucket or not exists(local_bucket):
            self._send_error_response(404, "Bucket not found")
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
                    # Apply path filter if specified
                    if filter_path:
                        rel_path = file_dict.get("filepath", "")
                        if not rel_path.startswith(filter_path):
                            continue
                    file_list.append(file_dict)

        # Sort by configured order
        file_list.sort(key=lambda x: x.get(sync_order, 0))

        self._send_json_response(200, file_list)

    def _handle_download(self, query: dict) -> None:
        """Handle file download endpoint."""
        filename = query.get("filename", [None])[0]
        if not filename:
            self._send_error_response(400, "Missing filename parameter")
            return

        local_bucket = CONFIG.get("LOCAL_BUCKET", "")
        # Build full path - filename is relative to bucket
        if filename.startswith("/"):
            filepath = join(local_bucket, filename.lstrip("/"))
        else:
            filepath = join(local_bucket, filename)

        if not exists(filepath):
            self._send_error_response(404, "File not found")
            return

        # Security check: ensure file is within bucket
        import os.path
        real_bucket = os.path.realpath(local_bucket)
        real_file = os.path.realpath(filepath)
        if not real_file.startswith(real_bucket):
            self._send_error_response(403, "Access denied")
            return

        try:
            # Get file metadata
            file_obj = File.get(filepath)
            if not file_obj:
                self._send_error_response(404, "File not found")
                return

            # Send file
            with open(filepath, "rb") as f:
                content = f.read()

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("X-MD5", file_obj.md5sum or "")
            self.send_header("X-Permissions", file_obj.permissions or "")
            self.end_headers()
            self.wfile.write(content)

        except IOError as e:
            LOG.error(f"Error reading file {filepath}: {e}")
            self._send_error_response(500, "Error reading file")

    def _handle_upload(self) -> None:
        """Handle file upload endpoint."""
        content_type = self.headers.get("Content-Type", "")

        if "multipart/form-data" not in content_type:
            self._send_error_response(400, "Expected multipart/form-data")
            return

        # Parse multipart form data
        try:
            import cgi
            from typing import IO, Any, cast
            form = cgi.FieldStorage(
                fp=cast(IO[Any], self.rfile),
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": content_type,
                }
            )

            # Extract fields
            cid = form.getvalue("cid", "")
            path = form.getvalue("path", "")
            mtime = form.getvalue("mtime", "")
            ctime = form.getvalue("ctime", "")
            md5sum = form.getvalue("md5sum", "")
            permissions = form.getvalue("permissions", "-rw-rw-r--")

            # Get uploaded file
            file_item = form["file"]
            if not file_item.file:
                self._send_error_response(400, "No file uploaded")
                return

            filename = file_item.filename
            file_content = file_item.file.read()

            # Build destination path
            local_bucket = CONFIG.get("LOCAL_BUCKET", "")
            if path:
                dest_dir = join(local_bucket, path)
            else:
                dest_dir = local_bucket

            # Create directory if needed
            import os
            os.makedirs(dest_dir, exist_ok=True)

            dest_path = join(dest_dir, filename)

            # Security check
            real_bucket = os.path.realpath(local_bucket)
            real_dest = os.path.realpath(dest_path)
            if not real_dest.startswith(real_bucket):
                self._send_error_response(403, "Access denied")
                return

            # Write file
            with open(dest_path, "wb") as f:
                f.write(file_content)

            # Set timestamps if provided
            if mtime or ctime:
                try:
                    mtime_val = float(mtime) if mtime else None
                    # Use mtime for both if ctime not available
                    atime_val = mtime_val or float(ctime) if ctime else None
                    if mtime_val:
                        os.utime(dest_path, (atime_val or mtime_val, mtime_val))
                except (ValueError, OSError):
                    pass

            # Set permissions if provided
            if permissions:
                from hsync_client.utils import set_permissions
                set_permissions(dest_path, permissions)

            LOG.info(f"Received file from {cid}: {dest_path}")
            self._send_json_response(201, {
                "info": "File uploaded successfully",
                "file": dest_path,
            })

        except Exception as e:
            LOG.error(f"Error handling upload: {e}")
            self._send_error_response(500, f"Upload error: {e}")


# =============================================================================
# LAN Server
# =============================================================================


class LANServer:
    """
    Embedded HTTP server for LAN master mode.

    Runs in a background thread and serves the hsync API to peer clients.
    """

    def __init__(self, port: int) -> None:
        """
        Initialize the LAN server.

        Args:
            port: Port to listen on.
        """
        self._port = port
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    @property
    def port(self) -> int:
        """Get the server port."""
        return self._port

    def start(self) -> None:
        """Start the server in a background thread."""
        if self._running:
            LOG.warning("LAN server already running")
            return

        self._server = HTTPServer(("0.0.0.0", self._port), LANRequestHandler)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self._running = True
        LOG.info(f"LAN master server started on port {self._port}")

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

