# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
WebSocket Server for LAN Master Mode.

This module provides real-time event synchronization between LAN peers.
The master runs a WebSocket server that broadcasts file system events
to all connected slaves.

Features:
- WebSocket server for real-time event broadcasting
- File system watching using watchdog
- Event handling for file/directory create, modify, delete, rename
- SSL/TLS support for secure connections
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import sys
import threading
from copy import deepcopy
from os.path import exists, islink
from time import time
from typing import Optional, Set
from watchdog.events import FileSystemEventHandler, DirCreatedEvent, DirDeletedEvent
from watchdog.observers import Observer

from hsync_client.config import CONFIG
from hsync_client.models.dir import Dir, is_hidden_dir
from hsync_client.models.event import Event, EventName
from hsync_client.models.file import File, is_hidden_file
from hsync_client.models.symlink import Symlink
from hsync_client.utils import ensure_str_path, is_same_paths


# Check if on_closed event is supported (Linux with inotify)
SUPPORTS_ON_CLOSED = sys.platform == "linux"
MODIFIED_DEBOUNCE_INTERVAL = 1.0

LOG = logging.getLogger("hsync.lan_websocket")


# =============================================================================
# WebSocket Server
# =============================================================================


class LANWebSocketServer:
    """
    WebSocket server for LAN master mode.
    
    Runs in a background thread and broadcasts file system events
    to all connected slave clients.
    """
    
    def __init__(
        self,
        port: int,
        use_https: bool = False,
        cert_file: Optional[str] = None,
        key_file: Optional[str] = None,
    ) -> None:
        """
        Initialize the WebSocket server.
        
        Args:
            port: Port to listen on (different from HTTP port).
            use_https: Whether to use WSS instead of WS.
            cert_file: Path to SSL certificate file.
            key_file: Path to SSL private key file.
        """
        self._port = port
        self._use_https = use_https
        self._cert_file = cert_file
        self._key_file = key_file
        self._clients: Set[asyncio.Future] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[asyncio.Future] = None
        self._running = False
        self._observer: Optional[Observer] = None
        self._event_handler: Optional[LANFileEventHandler] = None
    
    @property
    def port(self) -> int:
        """Get the WebSocket server port."""
        return self._port
    
    async def _handle_client(self, websocket) -> None:
        """Handle a single WebSocket client connection."""
        client_addr = websocket.remote_address
        LOG.info(f"WebSocket client connected: {client_addr}")
        
        # Check for authentication if LAN_KEY is set
        lan_key = CONFIG.get("LAN_KEY")
        if lan_key:
            try:
                # Wait for auth message
                auth_msg = await asyncio.wait_for(websocket.recv(), timeout=10.0)
                auth_data = json.loads(auth_msg)
                if auth_data.get("type") != "auth" or auth_data.get("key") != lan_key:
                    LOG.warning(f"WebSocket auth failed from {client_addr}")
                    await websocket.send(json.dumps({"type": "auth", "status": "failed"}))
                    await websocket.close()
                    return
                await websocket.send(json.dumps({"type": "auth", "status": "ok"}))
                LOG.info(f"WebSocket client authenticated: {client_addr}")
            except asyncio.TimeoutError:
                LOG.warning(f"WebSocket auth timeout from {client_addr}")
                await websocket.close()
                return
            except Exception as e:
                LOG.warning(f"WebSocket auth error from {client_addr}: {e}")
                await websocket.close()
                return
        
        # Add client to set
        self._clients.add(websocket)
        
        try:
            # Keep connection alive and handle incoming messages
            async for message in websocket:
                # Currently we don't expect messages from slaves
                # but we keep the connection alive
                LOG.debug(f"Received from {client_addr}: {message}")
        except Exception as e:
            LOG.debug(f"WebSocket client {client_addr} disconnected: {e}")
        finally:
            self._clients.discard(websocket)
            LOG.info(f"WebSocket client disconnected: {client_addr}")
    
    async def broadcast(self, event_dict: dict) -> None:
        """Broadcast an event to all connected clients."""
        if not self._clients:
            return

        message = json.dumps(event_dict)
        LOG.debug(f"Broadcasting to {len(self._clients)} clients: {event_dict.get('name')}")

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
            ssl_context.load_cert_chain(self._cert_file, self._key_file)  # type: ignore

        async with websockets.serve(
            self._handle_client,
            "0.0.0.0",
            self._port,
            ssl=ssl_context,
            ping_interval=30,
            ping_timeout=10,
        ) as server:
            self._server = server
            protocol = "WSS" if self._use_https else "WS"
            LOG.info(f"LAN WebSocket server started on port {self._port} ({protocol})")

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
            LOG.error(f"WebSocket server error: {e}")
        finally:
            self._loop.close()

    def start(self, watch_path: Optional[str] = None) -> None:
        """
        Start the WebSocket server and file watcher.

        Args:
            watch_path: Path to watch for file changes. Defaults to LOCAL_BUCKET.
        """
        if self._running:
            LOG.warning("LAN WebSocket server already running")
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

        self._event_handler = LANFileEventHandler(self)
        self._observer = Observer()
        self._observer.schedule(self._event_handler, watch_path, recursive=True)
        self._observer.start()
        LOG.info(f"File watcher started for: {watch_path}")

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
                    try:
                        await client.close()
                    except Exception:
                        pass
            asyncio.run_coroutine_threadsafe(close_all(), self._loop)

        # Wait for thread to finish
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

        LOG.info("LAN WebSocket server stopped")

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
    broadcasts events to all connected slave clients.
    """

    def __init__(self, ws_server: LANWebSocketServer) -> None:
        """
        Initialize the file event handler.

        Args:
            ws_server: WebSocket server for broadcasting events.
        """
        self._ws_server = ws_server
        self._last_modified: dict[str, float] = {}

    def _send_event(self, event: Event) -> None:
        """Send an event to all connected clients."""
        event_dict = event.to_dict()
        self._ws_server.broadcast_sync(event_dict)

    def on_any_event(self, event) -> None:
        """Log debug info for all events."""
        src_path = ensure_str_path(event.src_path)
        if event.is_directory and is_hidden_dir(src_path):
            return
        if not event.is_directory and is_hidden_file(src_path):
            return
        LOG.debug(f"[LAN EVENT ] {event.event_type}: {src_path}")

    def on_created(self, event) -> None:
        """Handle file/directory creation events."""
        src_path = ensure_str_path(event.src_path)

        if event.is_directory and not is_hidden_dir(src_path):
            LOG.info(f'[LAN CREATE] "{src_path}"')
            dir_obj = Dir.get(src_path)
            if dir_obj:
                message = Event(
                    CONFIG["CID"],
                    CONFIG["LOCAL_BUCKET"],
                    CONFIG["REMOTE_BUCKET"],
                    EventName.added,
                    dir_obj,
                )
                self._send_event(message)
        elif not is_hidden_file(src_path) and islink(src_path):
            LOG.info(f'[LAN SYMLINK] "{src_path}"')
            symlink = Symlink.get(src_path)
            if symlink:
                message = Event(
                    CONFIG["CID"],
                    CONFIG["LOCAL_BUCKET"],
                    CONFIG["REMOTE_BUCKET"],
                    EventName.symlink,
                    symlink,
                )
                self._send_event(message)

    def on_modified(self, event) -> None:
        """Handle file/directory modification events."""
        src_path = ensure_str_path(event.src_path)

        if (
            event.is_directory
            and not is_hidden_dir(src_path)
            and not is_same_paths(CONFIG["LOCAL_BUCKET"], src_path)
        ):
            LOG.info(f'[LAN MODIFY] "{src_path}"')
            dir_obj = Dir.get(src_path)
            if dir_obj:
                message = Event(
                    CONFIG["CID"],
                    CONFIG["LOCAL_BUCKET"],
                    CONFIG["REMOTE_BUCKET"],
                    EventName.modified,
                    dir_obj,
                )
                self._send_event(message)

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

        LOG.info(f'[LAN FILE  ] "{src_path}"')
        file_obj = File.get(src_path)
        if file_obj:
            message = Event(
                CONFIG["CID"],
                CONFIG["LOCAL_BUCKET"],
                CONFIG["REMOTE_BUCKET"],
                EventName.modified,
                file_obj,
            )
            self._send_event(message)

    def on_moved(self, event) -> None:
        """Handle file/directory move/rename events."""
        src_path = ensure_str_path(event.src_path)
        dest_path = ensure_str_path(getattr(event, 'dest_path', None))

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

            LOG.info(f'[LAN RENAME] "{src_path}" -> "{dest_path}"')
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

            LOG.info(f'[LAN RENAME] "{src_path}" -> "{dest_path}"')
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
            self._send_event(message)

    def on_deleted(self, event) -> None:
        """Handle file/directory deletion events."""
        src_path = ensure_str_path(event.src_path)

        if event.is_directory and is_hidden_dir(src_path):
            return
        if not event.is_directory and is_hidden_file(src_path):
            return

        # Skip deletion propagation if ignore_delete is enabled
        if CONFIG.get("IGNORE_DELETE", False):
            LOG.debug(f'[LAN IGNORE] "{src_path}" (ignore-delete mode)')
            return

        LOG.info(f'[LAN DELETE] "{src_path}"')

        # Create appropriate object type for the deleted item
        obj = Dir(src_path) if event.is_directory else File(src_path)

        message = Event(
            CONFIG["CID"],
            CONFIG["LOCAL_BUCKET"],
            CONFIG["REMOTE_BUCKET"],
            EventName.deleted,
            obj,
        )
        self._send_event(message)


# =============================================================================
# WebSocket Client for LAN Slave
# =============================================================================


async def lan_websocket_client(
    host: str,
    port: int,
    use_https: bool = False,
    cert_fingerprint: Optional[str] = None,
) -> None:
    """
    Connect to the LAN master's WebSocket server and handle events.

    This function runs until the connection is closed or an error occurs.

    Args:
        host: Master's IP address or hostname.
        port: Master's WebSocket port.
        use_https: Whether to use WSS instead of WS.
        cert_fingerprint: Expected certificate fingerprint for verification.
    """
    import websockets
    from hsync_client.ssl_utils import get_server_cert_fingerprint

    protocol = "wss" if use_https else "ws"
    uri = f"{protocol}://{host}:{port}"

    LOG.info(f"Connecting to master WebSocket: {uri}")

    # Set up SSL context for WSS
    ssl_context = None
    if use_https:
        # Verify certificate fingerprint before connecting
        if cert_fingerprint:
            actual_fingerprint = get_server_cert_fingerprint(host, port)
            if actual_fingerprint and actual_fingerprint != cert_fingerprint:
                LOG.error(f"Certificate fingerprint mismatch! Expected: {cert_fingerprint[:32]}...")
                return

        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    try:
        async with websockets.connect(
            uri,
            ssl=ssl_context,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=5,
        ) as websocket:
            LOG.info("WebSocket connected to master")

            # Authenticate if LAN_KEY is set
            lan_key = CONFIG.get("LAN_KEY")
            if lan_key:
                auth_msg = json.dumps({"type": "auth", "key": lan_key})
                await websocket.send(auth_msg)

                response = await asyncio.wait_for(websocket.recv(), timeout=10.0)
                auth_result = json.loads(response)
                if auth_result.get("status") != "ok":
                    LOG.error("WebSocket authentication failed")
                    return
                LOG.info("WebSocket authenticated with master")

            LOG.info("Listening for events from master...")

            # Process incoming events
            async for message in websocket:
                try:
                    event_data = json.loads(message)
                    await _handle_lan_event(event_data)
                except json.JSONDecodeError as e:
                    LOG.warning(f"Invalid JSON from master: {e}")
                except Exception as e:
                    LOG.error(f"Error handling event: {e}")

    except asyncio.CancelledError:
        LOG.info("WebSocket connection cancelled")
        raise
    except Exception as e:
        LOG.error(f"WebSocket connection error: {e}")


async def _handle_lan_event(event_data: dict) -> None:
    """
    Handle an event received from the LAN master.

    Args:
        event_data: Event dictionary from the master.
    """
    from hsync_client.sync import update_file, update_dir
    from hsync_client.models.dir import Dir
    from hsync_client.models.file import File
    from shutil import rmtree
    from os import remove, rename, makedirs
    from os.path import exists, dirname

    event_name = event_data.get("name")
    src = event_data.get("src")
    dst = event_data.get("dst")

    LOG.debug(f"Received event: {event_name}")

    if not src:
        LOG.warning("Event missing 'src' field")
        return

    # Handle Added / Modified events
    if event_name in {"added", "modified"}:
        if "path" in src:
            # Directory event
            update_dir(src)
        elif "filepath" in src:
            # File event - download the file
            update_file(src)

    # Handle Renamed events
    elif event_name == "renamed":
        if not dst:
            LOG.warning("Rename event missing 'dst' field")
            return

        if "path" in src:
            # Directory rename
            src_dir = Dir.from_dict(src, CONFIG["LOCAL_BUCKET"])
            dst_dir = Dir.from_dict(dst, CONFIG["LOCAL_BUCKET"])
            if src_dir.path and dst_dir.path and exists(src_dir.path):
                LOG.info(f'[LAN RENAME] "{src_dir.path}" -> "{dst_dir.path}"')
                # Create parent directory if needed
                parent = dirname(dst_dir.path)
                if parent and not exists(parent):
                    makedirs(parent, exist_ok=True)
                rename(src_dir.path, dst_dir.path)
        elif "filepath" in src:
            # File rename
            src_file = File.from_dict(src, CONFIG["LOCAL_BUCKET"], False)
            dst_file = File.from_dict(dst, CONFIG["LOCAL_BUCKET"], False)
            if src_file.filepath and dst_file.filepath and exists(src_file.filepath):
                LOG.info(f'[LAN RENAME] "{src_file.filepath}" -> "{dst_file.filepath}"')
                # Create parent directory if needed
                parent = dirname(dst_file.filepath)
                if parent and not exists(parent):
                    makedirs(parent, exist_ok=True)
                rename(src_file.filepath, dst_file.filepath)

    # Handle Deleted events
    elif event_name == "deleted":
        # Check ignore_delete mode
        if CONFIG.get("IGNORE_DELETE", False):
            LOG.debug(f"[LAN IGNORE] Deletion ignored (ignore-delete mode)")
            return

        if "path" in src:
            # Directory deletion
            dir_obj = Dir.from_dict(src, CONFIG["LOCAL_BUCKET"])
            if dir_obj.path and exists(dir_obj.path):
                LOG.info(f'[LAN DELETE] "{dir_obj.path}"')
                rmtree(dir_obj.path, ignore_errors=False)
        elif "filepath" in src:
            # File deletion
            file_obj = File.from_dict(src, CONFIG["LOCAL_BUCKET"], False)
            if file_obj.filepath and exists(file_obj.filepath):
                LOG.info(f'[LAN DELETE] "{file_obj.filepath}"')
                remove(file_obj.filepath)

    # Handle Symlink events
    elif event_name == "symlink":
        if "filepath" in src:
            # Symlink creation - download the file
            update_file(src)

