# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
WebSocket Server and Client for LAN Mode.

This module provides real-time bidirectional event synchronization between LAN peers.
The master runs a WebSocket server that broadcasts file system events to all
connected slaves. Slaves also monitor their local file systems and upload changes
to the master for propagation to other peers.

Features:
- WebSocket server for real-time event broadcasting (master)
- WebSocket client for receiving events (client)
- Bidirectional sync: slaves can upload changes to master
- File system watching using watchdog (both master and client)
- Event handling for file/directory create, modify, delete, rename
- SSL/TLS support for secure connections
- Echo loop prevention for files received from master
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import ssl
import sys
import threading
from copy import deepcopy
from os import listdir
from os.path import exists, getsize, islink
from os.path import join as path_join
from time import time
from typing import Any

from loguru import logger as log
from watchdog.events import DirCreatedEvent, DirDeletedEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from hsync_client.config import CONFIG
from hsync_client.constants import HSYNCKEEP_FILENAME
from hsync_client.models.dir import Dir, is_hidden_dir
from hsync_client.models.event import Event, EventName
from hsync_client.models.file import File, invalidate_sha256_cache, is_hidden_file
from hsync_client.models.symlink import Symlink
from hsync_client.utils import ensure_str_path, is_same_paths

# Check if on_closed event is supported (Linux with inotify)
SUPPORTS_ON_CLOSED = sys.platform == "linux"
MODIFIED_DEBOUNCE_INTERVAL = 1.0

# Conservative minimum effective LAN bandwidth used to derive the dynamic
# ``wait_pending`` timeout from the total bytes still in flight (10 MB/s).
MIN_BANDWIDTH_BPS = 10 * 1024 * 1024
# Hard cap on the dynamic ``wait_pending`` timeout to avoid waiting forever
# if a transfer stalls (30 minutes).
MAX_WAIT_PENDING_SECONDS = 30 * 60


def _escape_braces(s: str) -> str:
    """Escape curly braces in string for loguru format."""
    return s.replace("{", "{{").replace("}", "}}")


# =============================================================================
# Inbound File Origin Tracking (echo suppression for LAN master)
# =============================================================================

# Map of absolute filepath -> (originator_cid, expiration_timestamp).
# The master populates this map every time it writes / deletes / renames a
# path on behalf of a peer (via HTTP POST/DELETE/PATCH).  When the watchdog
# fires the resulting filesystem event, ``LANFileEventHandler._send_event``
# substitutes the originator's CID into the broadcast so that the original
# sender (and only that sender) can recognise and discard its own echo.
_inbound_origin: dict[str, tuple[str, float]] = {}
_inbound_origin_lock = threading.Lock()
INBOUND_ORIGIN_TTL = 10.0


def mark_inbound_origin(filepath: str, cid: str, ttl: float = INBOUND_ORIGIN_TTL) -> None:
    """Record that ``filepath`` was just written/changed on behalf of ``cid``."""
    if not filepath or not cid:
        return
    now = time()
    with _inbound_origin_lock:
        # Opportunistic GC of expired entries
        expired = [k for k, (_, exp) in _inbound_origin.items() if exp <= now]
        for k in expired:
            _inbound_origin.pop(k, None)
        _inbound_origin[filepath] = (cid, now + ttl)


def consume_inbound_origin(filepath: str) -> str | None:
    """Return and remove the originator CID for ``filepath`` if still fresh."""
    if not filepath:
        return None
    with _inbound_origin_lock:
        entry = _inbound_origin.pop(filepath, None)
    if entry is None:
        return None
    cid, expiry = entry
    if expiry < time():
        return None
    return cid


def _clear_inbound_origin() -> None:
    """Test helper: drop every tracked inbound origin entry."""
    with _inbound_origin_lock:
        _inbound_origin.clear()


# =============================================================================
# WebSocket Server
# =============================================================================


class LANWebSocketServer:
    """
    WebSocket server for LAN master mode.

    Runs in a background thread and broadcasts file system events
    to all connected client clients.
    """

    def __init__(
        self,
        port: int,
        use_https: bool = False,
        cert_file: str | None = None,
        key_file: str | None = None,
        key_password: bytes | None = None,
    ) -> None:
        """
        Initialize the WebSocket server.

        Args:
            port: Port to listen on (different from HTTP port).
            use_https: Whether to use WSS instead of WS.
            cert_file: Path to SSL certificate file.
            key_file: Path to SSL private key file.
            key_password: Password used to decrypt ``key_file`` (required if
                ``use_https=True``; the on-disk key is always encrypted).
        """
        self._port = port
        self._use_https = use_https
        self._cert_file = cert_file
        self._key_file = key_file
        self._key_password = key_password
        self._clients: set[Any] = set()  # WebSocket connections
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: Any | None = None
        self._running = False
        self._observer: BaseObserver | None = None
        self._event_handler: LANFileEventHandler | None = None

        if use_https and not key_password:
            raise ValueError("key_password is required when use_https=True")

    @property
    def port(self) -> int:
        """Get the WebSocket server port."""
        return self._port

    async def _handle_client(self, websocket) -> None:
        """Handle a single WebSocket client connection."""
        client_addr = websocket.remote_address
        log.info(f"WebSocket client connected: {client_addr}")

        # Send greeting: tell the client whether authentication is required
        lan_key = CONFIG.get("LAN_KEY")
        if lan_key:
            await websocket.send(json.dumps({"type": "auth_required"}))
            try:
                # Wait for auth message
                auth_msg = await asyncio.wait_for(websocket.recv(), timeout=10.0)
                auth_data = json.loads(auth_msg)
                if auth_data.get("type") != "auth" or auth_data.get("token") != lan_key:
                    log.warning(f"WebSocket auth failed from {client_addr}")
                    await websocket.send(json.dumps({"type": "auth", "status": "failed"}))
                    await websocket.close()
                    return
                await websocket.send(json.dumps({"type": "auth", "status": "ok"}))
                log.info(f"WebSocket client authenticated: {client_addr}")
            except TimeoutError:
                log.warning(f"WebSocket auth timeout from {client_addr}")
                await websocket.send(json.dumps({"type": "auth", "status": "failed"}))
                await websocket.close()
                return
            except Exception as e:
                log.warning(f"WebSocket auth error from {client_addr}: {e}")
                await websocket.close()
                return
        else:
            await websocket.send(json.dumps({"type": "connected"}))

        # Add client to set
        self._clients.add(websocket)

        try:
            # Keep connection alive and handle incoming messages
            async for message in websocket:
                # Currently we don't expect messages from slaves
                # but we keep the connection alive
                log.debug(f"Received from {client_addr}: {_escape_braces(message)}")
        except Exception as e:
            log.debug(f"WebSocket client {client_addr} disconnected: {e}")
        finally:
            self._clients.discard(websocket)
            log.info(f"WebSocket client disconnected: {client_addr}")

    async def broadcast(self, event_dict: dict) -> None:
        """Broadcast an event to all connected clients."""
        if not self._clients:
            return

        message = json.dumps(event_dict)
        log.debug(f"Broadcasting to {len(self._clients)} clients: {event_dict.get('name')}")

        # Send to all clients, removing disconnected ones
        disconnected = set()
        for client in self._clients.copy():
            try:
                await client.send(message)
            except Exception:
                disconnected.add(client)

        self._clients -= disconnected

    def broadcast_sync(self, event_dict: dict) -> None:
        """Broadcast an event synchronously (from file watcher thread)."""
        if self._loop and self._running:
            asyncio.run_coroutine_threadsafe(self.broadcast(event_dict), self._loop)

    async def _run_server(self) -> None:
        """Run the WebSocket server."""
        import websockets

        ssl_context = None
        if self._use_https:
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(
                self._cert_file,  # type: ignore[arg-type]
                self._key_file,  # type: ignore[arg-type]
                password=self._key_password,
            )

        async with websockets.serve(
            self._handle_client,
            "0.0.0.0",
            self._port,
            ssl=ssl_context,
            ping_interval=60,
            ping_timeout=30,
        ) as server:
            self._server = server
            protocol = "WSS" if self._use_https else "WS"
            log.info(f"LAN WebSocket server started on port {self._port} ({protocol})")

            # Wait until stopped
            while self._running:
                await asyncio.sleep(0.1)

    def _run_loop(self) -> None:
        """Run the asyncio event loop in the background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            self._loop.run_until_complete(self._run_server())
        except Exception as e:
            log.error(f"WebSocket server error: {e}")
        finally:
            self._loop.close()

    def start(self, watch_path: str | None = None) -> None:
        """
        Start the WebSocket server and file watcher.

        Args:
            watch_path: Path to watch for file changes. Defaults to LOCAL_BUCKET.
        """
        if self._running:
            log.warning("LAN WebSocket server already running")
            return

        self._running = True

        # Start WebSocket server in background thread
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        # Wait for loop to be ready
        for _ in range(50):  # 5 seconds max
            if self._loop is not None:
                break
            threading.Event().wait(0.1)

        # Start file watcher
        if watch_path is None:
            watch_path = CONFIG["LOCAL_BUCKET"]

        # Ensure watch_path exists: watchdog raises FileNotFoundError otherwise.
        # This can happen when the bucket has been emptied or removed externally.
        from os import makedirs

        if not exists(watch_path):
            log.warning(f"Watch path '{watch_path}' does not exist, creating it")
            makedirs(watch_path, exist_ok=True)

        self._event_handler = LANFileEventHandler(self)
        observer = Observer()
        observer.schedule(self._event_handler, watch_path, recursive=True)
        observer.start()
        self._observer = observer
        log.info(f"File watcher started for: {watch_path}")

    def stop(self) -> None:
        """Stop the WebSocket server and file watcher."""
        if not self._running:
            return

        self._running = False

        # Stop file watcher
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=2.0)
            self._observer = None

        # Close all client connections
        if self._loop and self._clients:

            async def close_all():
                for client in self._clients.copy():
                    with contextlib.suppress(Exception):
                        await client.close()

            asyncio.run_coroutine_threadsafe(close_all(), self._loop)

        # Wait for thread to finish
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

        log.info("LAN WebSocket server stopped")

    def is_running(self) -> bool:
        """Check if server is running."""
        return self._running


# =============================================================================
# File Event Handler for LAN Master
# =============================================================================


class LANFileEventHandler(FileSystemEventHandler):
    """
    File system event handler for LAN master mode.

    Monitors the local bucket directory for file system changes and
    broadcasts events to all connected client clients.
    """

    def __init__(self, ws_server: LANWebSocketServer) -> None:
        """
        Initialize the file event handler.

        Args:
            ws_server: WebSocket server for broadcasting events.
        """
        self._ws_server = ws_server
        self._last_modified: dict[str, float] = {}

    def _send_event(
        self,
        event: Event,
        src_abs_path: str | None = None,
        dst_abs_path: str | None = None,
    ) -> None:
        """Send an event to all connected clients.

        Skipped when SYNC_DIRECTION is 'download-only' (master should not
        broadcast local changes to slaves).

        If ``src_abs_path`` (or ``dst_abs_path``) matches a path that was
        recently written/deleted/renamed on behalf of a peer via the LAN
        HTTP API, the originator's CID is substituted into the broadcast
        so the sender can recognise and discard its own echo.
        """
        if CONFIG.get("SYNC_DIRECTION") == "download-only":
            log.debug("Download-only mode: master event not broadcast.")
            return
        event_dict = event.to_dict()
        origin_cid = None
        if src_abs_path:
            origin_cid = consume_inbound_origin(src_abs_path)
        if origin_cid is None and dst_abs_path:
            origin_cid = consume_inbound_origin(dst_abs_path)
        if origin_cid:
            event_dict["cid"] = origin_cid
        self._ws_server.broadcast_sync(event_dict)

    def on_any_event(self, event) -> None:
        """Log debug info for all events."""
        src_path = ensure_str_path(event.src_path)
        if event.is_directory and is_hidden_dir(src_path):
            return
        if not event.is_directory and is_hidden_file(src_path):
            return
        log.debug(f"[LAN EVENT ] {event.event_type}: {src_path}")

    def on_created(self, event) -> None:
        """Handle file/directory creation events."""
        src_path = ensure_str_path(event.src_path)

        if event.is_directory and not is_hidden_dir(src_path):
            log.info(f'[LAN CREATE] "{src_path}"')
            dir_obj = Dir.get(src_path)
            if dir_obj:
                message = Event(
                    CONFIG["CID"],
                    CONFIG["LOCAL_BUCKET"],
                    CONFIG["REMOTE_BUCKET"],
                    EventName.added,
                    dir_obj,
                )
                self._send_event(message, src_abs_path=src_path)
        elif not is_hidden_file(src_path) and islink(src_path):
            log.info(f'[LAN SYMLINK] "{src_path}"')
            symlink = Symlink.get(src_path)
            if symlink:
                message = Event(
                    CONFIG["CID"],
                    CONFIG["LOCAL_BUCKET"],
                    CONFIG["REMOTE_BUCKET"],
                    EventName.symlink,
                    symlink,
                )
                self._send_event(message, src_abs_path=src_path)

    def on_modified(self, event) -> None:
        """Handle file/directory modification events."""
        src_path = ensure_str_path(event.src_path)

        if (
            event.is_directory
            and not is_hidden_dir(src_path)
            and not is_same_paths(CONFIG["LOCAL_BUCKET"], src_path)
        ):
            log.info(f'[LAN MODIFY] "{src_path}"')
            dir_obj = Dir.get(src_path)
            if dir_obj:
                message = Event(
                    CONFIG["CID"],
                    CONFIG["LOCAL_BUCKET"],
                    CONFIG["REMOTE_BUCKET"],
                    EventName.modified,
                    dir_obj,
                )
                self._send_event(message, src_abs_path=src_path)

        # For files, use debouncing on systems without on_closed
        if not SUPPORTS_ON_CLOSED and not event.is_directory and not is_hidden_file(src_path):
            current_time = time()
            last_time = self._last_modified.get(src_path, 0)
            if current_time - last_time >= MODIFIED_DEBOUNCE_INTERVAL:
                self._last_modified[src_path] = current_time
                self._handle_file_modified(src_path)

    def on_closed(self, event) -> None:
        """Handle file close events (file write completed)."""
        src_path = ensure_str_path(event.src_path)

        if not event.is_directory and not is_hidden_file(src_path):
            self._handle_file_modified(src_path)

    def _handle_file_modified(self, src_path: str) -> None:
        """Handle a file modification (create or update)."""
        if not exists(src_path):
            return

        log.info(f'[LAN FILE  ] "{src_path}"')
        invalidate_sha256_cache(src_path)
        file_obj = File.get(src_path)
        if file_obj:
            message = Event(
                CONFIG["CID"],
                CONFIG["LOCAL_BUCKET"],
                CONFIG["REMOTE_BUCKET"],
                EventName.modified,
                file_obj,
            )
            self._send_event(message, src_abs_path=src_path)

    def on_moved(self, event) -> None:
        """Handle file/directory move/rename events."""
        src_path = ensure_str_path(event.src_path)
        dest_path = ensure_str_path(getattr(event, "dest_path", None))

        if not dest_path:
            return

        message = None

        if event.is_directory:
            # Handle hidden directory scenarios
            if is_hidden_dir(src_path) and is_hidden_dir(dest_path):
                return
            if is_hidden_dir(src_path) and not is_hidden_dir(dest_path):
                # Hidden to visible - treat as creation
                created_event = DirCreatedEvent(dest_path)
                self.on_created(created_event)
                return
            if is_hidden_dir(dest_path):
                # Moving to hidden - treat as deletion
                deleted_event = DirDeletedEvent(src_path)
                self.on_deleted(deleted_event)
                return

            log.info(f'[LAN RENAME] "{src_path}" -> "{dest_path}"')
            dst = Dir.get(dest_path)
            if dst:
                src = deepcopy(dst)
                src.path = src_path
                message = Event(
                    CONFIG["CID"],
                    CONFIG["LOCAL_BUCKET"],
                    CONFIG["REMOTE_BUCKET"],
                    EventName.renamed,
                    src,
                    dst,
                )
        else:
            # File move/rename
            if is_hidden_file(src_path):
                return

            log.info(f'[LAN RENAME] "{src_path}" -> "{dest_path}"')
            invalidate_sha256_cache(src_path)
            invalidate_sha256_cache(dest_path)
            dst = File.get(dest_path)
            if dst:
                src = deepcopy(dst)
                src.filepath = src_path
                message = Event(
                    CONFIG["CID"],
                    CONFIG["LOCAL_BUCKET"],
                    CONFIG["REMOTE_BUCKET"],
                    EventName.renamed,
                    src,
                    dst,
                )

        if message:
            self._send_event(message, src_abs_path=src_path, dst_abs_path=dest_path)

    def on_deleted(self, event) -> None:
        """Handle file/directory deletion events."""
        src_path = ensure_str_path(event.src_path)

        if event.is_directory and is_hidden_dir(src_path):
            return
        if not event.is_directory and is_hidden_file(src_path):
            return

        # Skip deletion propagation if ignore_delete is enabled
        if CONFIG.get("IGNORE_DELETE", False):
            log.debug(f'[LAN IGNORE] "{src_path}" (ignore-delete mode)')
            return

        log.info(f'[LAN DELETE] "{src_path}"')

        # Create appropriate object type for the deleted item
        obj = Dir(src_path) if event.is_directory else File(src_path)

        message = Event(
            CONFIG["CID"],
            CONFIG["LOCAL_BUCKET"],
            CONFIG["REMOTE_BUCKET"],
            EventName.deleted,
            obj,
        )
        self._send_event(message, src_abs_path=src_path)


# =============================================================================
# WebSocket Client for LAN Client
# =============================================================================


async def lan_websocket_client(
    host: str,
    port: int,
    use_https: bool = False,
) -> None:
    """
    Connect to the LAN master's WebSocket server and handle events.

    This function runs until the connection is closed or an error occurs.
    Also starts a local file watcher to upload changes to the master
    (bidirectional sync).

    Note: Certificate fingerprint verification is not performed here because
    it was already verified during the initial HTTP sync. The WebSocket
    connection uses the same certificate.

    Args:
        host: Master's IP address or hostname.
        port: Master's WebSocket port (HTTP port is port - 1).
        use_https: Whether to use WSS instead of WS.
    """
    global _slave_event_handler
    import websockets

    protocol = "wss" if use_https else "ws"
    uri = f"{protocol}://{host}:{port}"

    log.info(f"Connecting to master WebSocket: {uri}")

    # Start local file watcher for bidirectional sync
    # HTTP port is WebSocket port - 1
    http_port = port - 1
    _slave_event_handler = SlaveFileEventHandler(host, http_port, use_https)
    observer = Observer()
    watch_path = CONFIG["LOCAL_BUCKET"]

    # Ensure watch_path exists: watchdog raises FileNotFoundError otherwise.
    # This can happen when the bucket has been emptied or removed externally.
    from os import makedirs

    if not exists(watch_path):
        log.warning(f"Watch path '{watch_path}' does not exist, creating it")
        makedirs(watch_path, exist_ok=True)

    observer.schedule(_slave_event_handler, watch_path, recursive=True)
    observer.start()
    log.info(f"Client file watcher started for bidirectional sync: {watch_path}")

    # Set up SSL context for WSS
    # Note: Certificate fingerprint was already verified during initial HTTP sync.
    # We trust the same certificate for WebSocket connection.
    ssl_context = None
    if use_https:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    try:
        async with websockets.connect(
            uri,
            ssl=ssl_context,
            ping_interval=60,
            ping_timeout=30,
            close_timeout=5,
        ) as websocket:
            log.info("WebSocket connected to master")

            # Read server greeting to determine if auth is required
            greeting = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            greeting_data = json.loads(greeting)

            if greeting_data.get("type") == "auth_required":
                lan_key = CONFIG.get("LAN_KEY")
                if not lan_key:
                    log.error("WebSocket authentication failed – server requires LAN key")
                    return
                auth_msg = json.dumps({"type": "auth", "token": lan_key})
                await websocket.send(auth_msg)

                response = await asyncio.wait_for(websocket.recv(), timeout=10.0)
                auth_result = json.loads(response)
                if auth_result.get("status") != "ok":
                    log.error("WebSocket authentication failed")
                    return
                log.info("WebSocket authenticated with master")
            else:
                log.info("No WebSocket authentication required")

            log.info("Listening for events from master...")

            # Process incoming events
            async for message in websocket:
                try:
                    event_data = json.loads(message)
                    await _handle_lan_event(event_data, _slave_event_handler)
                except json.JSONDecodeError as e:
                    log.warning(f"Invalid JSON from master: {e}")
                except Exception as e:
                    log.error(f"Error handling event: {e}")

    except asyncio.CancelledError:
        log.info("WebSocket connection cancelled")
        raise
    except Exception as e:
        log.error(f"WebSocket connection error: {e}")
    finally:
        # Wait for pending client→master operations to complete before
        # returning to the discovery loop (which triggers a full sync).
        # This prevents race conditions where a rename/upload/delete is
        # still in-flight while the full sync runs and reverts it.
        if _slave_event_handler is not None:
            _slave_event_handler.wait_pending(timeout=30.0)
        # Stop the file watcher
        observer.stop()
        observer.join(timeout=2.0)
        _slave_event_handler = None
        log.info("Client file watcher stopped")


async def _handle_lan_event(
    event_data: dict,
    slave_handler: SlaveFileEventHandler | None = None,
) -> None:
    """
    Handle an event received from the LAN master.

    Args:
        event_data: Event dictionary from the master.
        slave_handler: Optional client event handler for marking files as receiving.
    """
    from os import makedirs, remove, rename
    from os.path import basename, dirname, exists
    from shutil import rmtree

    from hsync_client.models.dir import Dir
    from hsync_client.models.file import File
    from hsync_client.sync import update_dir, update_file

    event_name = event_data.get("name")
    src = event_data.get("src")
    dst = event_data.get("dst")

    log.debug(f"Received event: {event_name}")

    # Drop events that originated from this very client (echo suppression).
    # The master substitutes the originator's CID into the broadcast for
    # operations that it performed on the originator's behalf via the
    # LAN HTTP API.  Without this filter the originator would re-download
    # the file it just uploaded.
    own_cid = CONFIG.get("CID")
    event_cid = event_data.get("cid")
    if own_cid and event_cid and event_cid == own_cid:
        log.debug(f"[LAN ECHO  ] Ignoring own event: {event_name}")
        return

    # Skip events from master when in upload-only mode (no downloads)
    if CONFIG.get("SYNC_DIRECTION") == "upload-only":
        log.debug("Upload-only mode: LAN event from master ignored.")
        return

    if not src:
        log.warning("Event missing 'src' field")
        return

    # Helper to mark/unmark files as receiving to prevent echo
    def mark_receiving(filepath: str) -> None:
        if slave_handler:
            slave_handler.mark_receiving(filepath)

    def unmark_receiving(filepath: str) -> None:
        if slave_handler:
            slave_handler.unmark_receiving(filepath)

    # Handle Added / Modified events
    # All blocking I/O (download, rename, rmtree) is dispatched to a worker
    # thread via ``asyncio.to_thread`` so the WebSocket event loop stays
    # responsive to keep-alive pings while large transfers are in flight.
    if event_name in {"added", "modified"}:
        if "path" in src:
            # Directory event
            dir_obj = Dir.from_dict(src, CONFIG["LOCAL_BUCKET"])
            if dir_obj.path:
                mark_receiving(dir_obj.path)
                try:
                    await asyncio.to_thread(update_dir, src)
                finally:
                    unmark_receiving(dir_obj.path)
        elif "filepath" in src:
            # File event - download the file
            file_obj = File.from_dict(src, CONFIG["LOCAL_BUCKET"], False)
            if file_obj.filepath:
                mark_receiving(file_obj.filepath)
                try:
                    await asyncio.to_thread(update_file, src)
                finally:
                    unmark_receiving(file_obj.filepath)

    # Handle Renamed events
    elif event_name == "renamed":
        if not dst:
            log.warning("Rename event missing 'dst' field")
            return

        if "path" in src:
            # Directory rename
            src_dir = Dir.from_dict(src, CONFIG["LOCAL_BUCKET"])
            dst_dir = Dir.from_dict(dst, CONFIG["LOCAL_BUCKET"])
            if src_dir.path and dst_dir.path and exists(src_dir.path):
                mark_receiving(src_dir.path)
                mark_receiving(dst_dir.path)
                try:
                    log.info(f'[LAN RENAME] "{src_dir.path}" -> "{dst_dir.path}"')
                    # Create parent directory if needed
                    parent = dirname(dst_dir.path)
                    if parent and not exists(parent):
                        makedirs(parent, exist_ok=True)
                    await asyncio.to_thread(rename, src_dir.path, dst_dir.path)
                finally:
                    unmark_receiving(src_dir.path)
                    unmark_receiving(dst_dir.path)
        elif "filepath" in src:
            # File rename
            src_file = File.from_dict(src, CONFIG["LOCAL_BUCKET"], False)
            dst_file = File.from_dict(dst, CONFIG["LOCAL_BUCKET"], False)
            if src_file.filepath and dst_file.filepath and exists(src_file.filepath):
                mark_receiving(src_file.filepath)
                mark_receiving(dst_file.filepath)
                try:
                    log.info(f'[LAN RENAME] "{src_file.filepath}" -> "{dst_file.filepath}"')
                    # Create parent directory if needed
                    parent = dirname(dst_file.filepath)
                    if parent and not exists(parent):
                        makedirs(parent, exist_ok=True)
                    await asyncio.to_thread(rename, src_file.filepath, dst_file.filepath)
                finally:
                    unmark_receiving(src_file.filepath)
                    unmark_receiving(dst_file.filepath)

    # Handle Deleted events
    elif event_name == "deleted":
        # Check ignore_delete mode
        if CONFIG.get("IGNORE_DELETE", False):
            log.debug("[LAN IGNORE] Deletion ignored (ignore-delete mode)")
            return

        if "path" in src:
            # Directory deletion
            dir_obj = Dir.from_dict(src, CONFIG["LOCAL_BUCKET"])
            if dir_obj.path and exists(dir_obj.path):
                mark_receiving(dir_obj.path)
                try:
                    log.info(f'[LAN DELETE] "{dir_obj.path}"')
                    await asyncio.to_thread(rmtree, dir_obj.path, False)
                finally:
                    unmark_receiving(dir_obj.path)
        elif "filepath" in src:
            # File deletion
            file_obj = File.from_dict(src, CONFIG["LOCAL_BUCKET"], False)
            if file_obj.filepath and exists(file_obj.filepath):
                mark_receiving(file_obj.filepath)
                try:
                    log.info(f'[LAN DELETE] "{file_obj.filepath}"')
                    await asyncio.to_thread(remove, file_obj.filepath)
                    parent = dirname(file_obj.filepath)
                    if basename(file_obj.filepath) == HSYNCKEEP_FILENAME:
                        # Sentinel removed — clean up the now-empty directory.
                        # Never remove the LOCAL_BUCKET root itself.
                        from hsync_client.utils import safe_rmdir_if_empty

                        if safe_rmdir_if_empty(parent):
                            log.debug(f'Removed empty directory "{parent}"')
                finally:
                    unmark_receiving(file_obj.filepath)

    # Handle Symlink events
    elif event_name == "symlink" and "filepath" in src:
        # Symlink creation - download the file
        file_obj = File.from_dict(src, CONFIG["LOCAL_BUCKET"], False)
        if file_obj.filepath:
            mark_receiving(file_obj.filepath)
            try:
                await asyncio.to_thread(update_file, src)
            finally:
                unmark_receiving(file_obj.filepath)


# =============================================================================
# Client File Event Handler (for bidirectional sync)
# =============================================================================


class SlaveFileEventHandler(FileSystemEventHandler):
    """
    File system event handler for LAN client mode.

    Monitors the local bucket directory for file system changes and
    uploads changes to the master server for propagation to other slaves.
    """

    def __init__(
        self,
        master_host: str,
        master_port: int,
        use_https: bool = False,
    ) -> None:
        """
        Initialize the client file event handler.

        Args:
            master_host: Master's IP address or hostname.
            master_port: Master's HTTP port (not WebSocket port).
            use_https: Whether to use HTTPS for uploads.
        """
        self._master_host = master_host
        self._master_port = master_port
        self._use_https = use_https
        self._last_modified: dict[str, float] = {}
        # Track files we're currently receiving from master to avoid echo
        self._receiving_files: set[str] = set()
        self._receiving_lock = threading.Lock()
        # Track pending operations (rename, upload, delete) for graceful
        # shutdown.  Each entry is ``(thread, bytes_estimate)`` so that
        # ``wait_pending`` can derive a timeout proportional to the volume
        # still in flight (large uploads can legitimately take minutes).
        self._pending_ops: list[tuple[threading.Thread, int]] = []
        self._pending_ops_lock = threading.Lock()

    def mark_receiving(self, filepath: str) -> None:
        """Mark a file as being received from master (to avoid upload echo)."""
        with self._receiving_lock:
            self._receiving_files.add(filepath)

    def unmark_receiving(self, filepath: str) -> None:
        """Unmark a file after receiving is complete."""
        with self._receiving_lock:
            self._receiving_files.discard(filepath)

    def is_receiving(self, filepath: str) -> bool:
        """Check if a file is currently being received from master."""
        with self._receiving_lock:
            return filepath in self._receiving_files

    def _start_operation(
        self,
        target,
        args: tuple = (),
        name: str | None = None,
        bytes_estimate: int = 0,
    ) -> threading.Thread:
        """Start a background operation and track it for graceful shutdown.

        Args:
            target: Callable to run in the thread.
            args: Arguments to pass to target.
            name: Optional thread name for debugging.
            bytes_estimate: Expected payload size in bytes (used by
                ``wait_pending`` to derive a proportional timeout).
                Defaults to 0 for fast metadata operations (delete/rename).

        Returns:
            The started thread.
        """
        thread = threading.Thread(target=target, args=args, daemon=True, name=name)
        with self._pending_ops_lock:
            # Clean up completed threads
            self._pending_ops = [(t, b) for t, b in self._pending_ops if t.is_alive()]
            self._pending_ops.append((thread, max(bytes_estimate, 0)))
        thread.start()
        return thread

    def wait_pending(self, timeout: float = 30.0) -> int:
        """Wait for all pending operations to complete.

        Called before reconnect sync to avoid race conditions between
        pending client→master operations and the full sync.  The effective
        timeout is scaled by the total bytes still in flight so that
        multi-gigabyte uploads are not cut short.

        Args:
            timeout: Base timeout in seconds; the actual deadline is
                ``max(timeout, total_bytes / MIN_BANDWIDTH_BPS + timeout)``
                capped at ``MAX_WAIT_PENDING_SECONDS``.

        Returns:
            Number of operations that timed out (0 = all completed).
        """
        with self._pending_ops_lock:
            active = [(t, b) for t, b in self._pending_ops if t.is_alive()]

        if not active:
            return 0

        total_bytes = sum(b for _, b in active)
        if total_bytes > 0:
            bandwidth_seconds = total_bytes / MIN_BANDWIDTH_BPS
            effective_timeout = min(
                max(timeout, bandwidth_seconds + timeout),
                MAX_WAIT_PENDING_SECONDS,
            )
            log.info(
                f"Waiting up to {effective_timeout:.0f}s for {len(active)} "
                f"pending operation(s) (~{total_bytes / (1024 * 1024):.1f} MB)..."
            )
        else:
            effective_timeout = timeout
            log.info(f"Waiting for {len(active)} pending operation(s) to complete...")

        timed_out = 0
        deadline = time() + effective_timeout
        for thread, _ in active:
            remaining = max(deadline - time(), 0)
            if remaining <= 0:
                timed_out += 1
                continue
            thread.join(timeout=remaining)
            if thread.is_alive():
                timed_out += 1

        if timed_out:
            log.warning(f"{timed_out} pending operation(s) timed out")
        else:
            log.info("All pending operations completed")

        return timed_out

    def _is_temp_file(self, filepath: str) -> bool:
        """Check if a file is a temporary file that should be ignored."""
        # .part files are used during downloads
        return filepath.endswith(".part")

    def _upload_to_master(self, file_obj: File) -> None:
        """Upload a file to the master server."""
        from hsync_client.transfer import upload_file

        try:
            log.info(f'[CLIENT UP  ] "{file_obj.filepath}"')
            upload_file(file_obj, check_size=True)
        except Exception as e:
            log.error(f"Failed to upload to master: {e}")

    def _delete_on_master(self, filepath: str, is_dir: bool = False) -> None:
        """Notify master about a file/directory deletion."""
        import requests

        try:
            protocol = "https" if self._use_https else "http"
            # Use the files endpoint with DELETE method
            prefix = CONFIG["LOCAL_BUCKET"].rstrip("/")
            rel_path = filepath.removeprefix(prefix).lstrip("/")

            url = (
                f"{protocol}://{self._master_host}:{self._master_port}"
                f"/api/v1/buckets/{CONFIG['REMOTE_BUCKET']}/files"
            )

            params = {"path": rel_path, "is_dir": str(is_dir).lower()}
            # Include our CID so the master can attribute the resulting
            # filesystem event back to us and suppress the echo broadcast.
            own_cid = CONFIG.get("CID")
            if own_cid:
                params["cid"] = own_cid
            headers: dict[str, str] = {}
            req_headers = CONFIG.get("REQUEST_HEADERS")
            if req_headers and isinstance(req_headers, dict):
                for k, v in req_headers.items():
                    if isinstance(k, str) and isinstance(v, str):
                        headers[k] = v

            log.info(f'[CLIENT DEL ] "{filepath}"')
            response = requests.delete(
                url,
                params=params,
                headers=headers,
                verify=False,  # Self-signed cert
                timeout=30,
            )

            if response.status_code not in (200, 204, 404):
                log.warning(f"Delete request failed: {response.status_code}")

        except Exception as e:
            log.error(f"Failed to delete on master: {e}")

    def _rename_on_master(self, src_path: str, dst_path: str) -> None:
        """Notify master about a file/directory rename."""
        import requests

        try:
            protocol = "https" if self._use_https else "http"
            prefix = CONFIG["LOCAL_BUCKET"].rstrip("/")
            src_rel = src_path.removeprefix(prefix).lstrip("/")
            dst_rel = dst_path.removeprefix(prefix).lstrip("/")

            url = (
                f"{protocol}://{self._master_host}:{self._master_port}"
                f"/api/v1/buckets/{CONFIG['REMOTE_BUCKET']}/files"
            )

            params = {"src": src_rel, "dst": dst_rel}
            # Include our CID so the master can attribute the resulting
            # filesystem event back to us and suppress the echo broadcast.
            own_cid = CONFIG.get("CID")
            if own_cid:
                params["cid"] = own_cid
            headers: dict[str, str] = {}
            req_headers = CONFIG.get("REQUEST_HEADERS")
            if req_headers and isinstance(req_headers, dict):
                for k, v in req_headers.items():
                    if isinstance(k, str) and isinstance(v, str):
                        headers[k] = v

            log.info(f'[CLIENT REN ] "{src_path}" -> "{dst_path}"')
            response = requests.patch(
                url,
                params=params,
                headers=headers,
                verify=False,  # Self-signed cert
                timeout=30,
            )

            if response.status_code not in (200, 204):
                log.warning(f"Rename request failed: {response.status_code}")

        except Exception as e:
            log.error(f"Failed to rename on master: {e}")

    def on_created(self, event) -> None:
        """Handle file/directory creation events."""
        src_path = ensure_str_path(event.src_path)

        # Skip temp files (.part files used during downloads)
        if self._is_temp_file(src_path):
            return

        # Skip if we're receiving this file from master
        if self.is_receiving(src_path):
            return

        if event.is_directory and not is_hidden_dir(src_path):
            # Create a .hsynckeep sentinel so the empty directory is
            # synced to the master.  The watchdog will pick up the new
            # file and trigger an upload via on_closed / on_modified.
            log.debug(f'[CLIENT DIR ] "{src_path}"')
            self._ensure_sentinel(src_path)
        elif not event.is_directory and not is_hidden_file(src_path):
            # Check if it's a symlink
            if islink(src_path):
                # Symlink - upload immediately
                self._handle_file_upload(src_path)
            else:
                # Files placed via an atomic rename from outside the watched
                # tree (e.g. file managers using `IN_MOVED_TO` without a
                # corresponding `IN_MOVED_FROM`, hard-links, `mv` from a
                # sibling directory, etc.) arrive already complete and will
                # not generate a subsequent on_closed / on_modified event.
                # Trigger the upload immediately when the file is non-empty
                # at creation time; otherwise wait for on_closed / on_modified.
                try:
                    size = getsize(src_path)
                except OSError:
                    size = 0
                if size > 0:
                    log.debug(f'[CLIENT NEW ] "{src_path}" ({size} bytes, complete on create)')
                    self._handle_file_upload(src_path)

    def on_modified(self, event) -> None:
        """Handle file/directory modification events."""
        src_path = ensure_str_path(event.src_path)

        # Skip temp files (.part files used during downloads)
        if self._is_temp_file(src_path):
            return

        # Skip if we're receiving this file from master
        if self.is_receiving(src_path):
            return

        if event.is_directory:
            return

        if is_hidden_file(src_path):
            return

        # Debounce rapid modifications
        now = time()
        last_time = self._last_modified.get(src_path, 0)
        if now - last_time < MODIFIED_DEBOUNCE_INTERVAL:
            return
        self._last_modified[src_path] = now

        # On non-Linux, use on_modified as fallback for file completion
        if not SUPPORTS_ON_CLOSED:
            self._handle_file_upload(src_path)

    def on_closed(self, event) -> None:
        """Handle file close events (Linux only)."""
        if not SUPPORTS_ON_CLOSED:
            return

        src_path = ensure_str_path(event.src_path)

        # Skip temp files (.part files used during downloads)
        if self._is_temp_file(src_path):
            return

        # Skip if we're receiving this file from master
        if self.is_receiving(src_path):
            return

        if event.is_directory or is_hidden_file(src_path):
            return

        self._handle_file_upload(src_path)

    def _handle_file_upload(self, src_path: str) -> None:
        """Handle a file modification - upload to master."""
        # Skip uploads in download-only mode
        if CONFIG.get("SYNC_DIRECTION") == "download-only":
            return
        if not exists(src_path):
            return

        invalidate_sha256_cache(src_path)
        file_obj = File.get(src_path)
        if file_obj:
            # Bytes estimate lets ``wait_pending`` scale its timeout when
            # large files are still being uploaded at WS disconnect time.
            try:
                size_hint = getsize(src_path)
            except OSError:
                size_hint = 0
            # Run upload in a separate thread to not block the watcher
            self._start_operation(
                target=self._upload_to_master,
                args=(file_obj,),
                name=f"upload-{src_path}",
                bytes_estimate=size_hint,
            )

    def _ensure_sentinel(self, dir_path: str) -> None:
        """Create a .hsynckeep sentinel in *dir_path* if it is empty.

        The sentinel guarantees that the directory is picked up by the
        sync engine and propagated to the master.  The watchdog will
        detect the new file and trigger an upload automatically.
        """
        try:
            entries = listdir(dir_path)
        except OSError:
            return
        # Consider the directory empty if it has no visible (syncable) files
        has_syncable = any(not is_hidden_file(e) for e in entries)
        if has_syncable:
            return
        sentinel = path_join(dir_path, HSYNCKEEP_FILENAME)
        if not exists(sentinel):
            try:
                open(sentinel, "w").close()  # noqa: SIM115
                log.debug(f'[CLIENT KEEP] Created sentinel "{sentinel}"')
            except OSError as exc:
                log.warning(f'Cannot create sentinel "{sentinel}": {exc}')

    def on_deleted(self, event) -> None:
        """Handle file/directory deletion events."""
        src_path = ensure_str_path(event.src_path)

        # Skip temp files (.part files used during downloads)
        if self._is_temp_file(src_path):
            return

        # Skip in download-only mode (don't propagate local deletions)
        if CONFIG.get("SYNC_DIRECTION") == "download-only":
            return

        # Skip hidden files/dirs
        if event.is_directory and is_hidden_dir(src_path):
            return
        if not event.is_directory and is_hidden_file(src_path):
            return

        # Skip if in ignore-delete mode
        if CONFIG.get("IGNORE_DELETE", False):
            log.debug("[CLIENT IGN ] Deletion ignored (ignore-delete mode)")
            return

        # Notify master about deletion
        self._start_operation(
            target=self._delete_on_master,
            args=(src_path, event.is_directory),
            name=f"delete-{src_path}",
        )

    def on_moved(self, event) -> None:
        """Handle file/directory move/rename events."""
        src_path = ensure_str_path(event.src_path)
        dest_path = ensure_str_path(getattr(event, "dest_path", None))

        if not dest_path:
            return

        # Skip in download-only mode (don't propagate local renames)
        if CONFIG.get("SYNC_DIRECTION") == "download-only":
            return

        # Skip temp files (.part files used during downloads)
        if self._is_temp_file(src_path) or self._is_temp_file(dest_path):
            return

        # Skip if we're receiving these files from master
        if self.is_receiving(src_path) or self.is_receiving(dest_path):
            return

        # Handle hidden file/dir scenarios
        if event.is_directory:
            src_hidden = is_hidden_dir(src_path)
            dest_hidden = is_hidden_dir(dest_path)
        else:
            src_hidden = is_hidden_file(src_path)
            dest_hidden = is_hidden_file(dest_path)

        # Both hidden - ignore
        if src_hidden and dest_hidden:
            return

        # Hidden to visible - treat as creation
        if src_hidden and not dest_hidden:
            if not event.is_directory and exists(dest_path):
                invalidate_sha256_cache(dest_path)
                file_obj = File.get(dest_path)
                if file_obj:
                    try:
                        size_hint = getsize(dest_path)
                    except OSError:
                        size_hint = 0
                    self._start_operation(
                        target=self._upload_to_master,
                        args=(file_obj,),
                        name=f"upload-{dest_path}",
                        bytes_estimate=size_hint,
                    )
            return

        # Visible to hidden - treat as deletion
        if not src_hidden and dest_hidden:
            if not CONFIG.get("IGNORE_DELETE", False):
                self._start_operation(
                    target=self._delete_on_master,
                    args=(src_path, event.is_directory),
                    name=f"delete-{src_path}",
                )
            return

        # Normal rename - use PATCH endpoint
        self._start_operation(
            target=self._rename_on_master,
            args=(src_path, dest_path),
            name=f"rename-{src_path}",
        )


# Global client event handler for marking files as receiving
_slave_event_handler: SlaveFileEventHandler | None = None


def get_slave_event_handler() -> SlaveFileEventHandler | None:
    """Get the global client event handler."""
    return _slave_event_handler
