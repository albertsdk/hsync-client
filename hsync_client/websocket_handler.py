# HSYNC Client - HTTP-based file synchronization client
# Copyright (C) 2025 Alberto Basso
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
WebSocket event handling for HSYNC Client.

This module provides WebSocket connection management and event handling
for real-time synchronization with the server.
"""

import logging
import sys

from asyncio import create_task, get_running_loop, sleep, Task
from asyncio import run_coroutine_threadsafe
from copy import deepcopy
from json import dumps, loads
from os import makedirs, remove, rename, symlink as os_symlink, unlink
from os.path import dirname, exists, islink
from shutil import rmtree
from ssl import create_default_context, Purpose, CERT_NONE, CERT_REQUIRED
from time import time
from typing import Optional
from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer
from websockets import connect, ConnectionClosed

# Check if on_closed event is supported (Linux with inotify)
# On macOS (FSEvents) and Windows, on_closed is not triggered
SUPPORTS_ON_CLOSED = sys.platform == "linux"

# Debounce interval for on_modified fallback (seconds)
MODIFIED_DEBOUNCE_INTERVAL = 1.0

from hsync_client.config import CONFIG, HSYNC_API_KEY
from hsync_client.constants import HSYNC_VERSION
from hsync_client.event_queue import EventQueue
from hsync_client.lan import check_version_compatibility
from hsync_client.models.dir import Dir, is_hidden_dir
from hsync_client.models.event import Event, EventName
from hsync_client.models.file import File, is_hidden_file
from hsync_client.models.symlink import Symlink
from hsync_client.sync import sync, update_dir, update_file
from hsync_client.transfer import download_file, upload_file
from hsync_client.utils import ensure_str_path, is_same_paths


# Logger instance for this module
LOG = logging.getLogger(__name__)

# Global resync task reference for cancellation on reconnect
_resync_task: Optional[Task[None]] = None


# =============================================================================
# WebSocket Connection Management
# =============================================================================


async def remote_events() -> None:
    """
    Handle continuous synchronization via WebSocket events.

    Connects to the server's WebSocket endpoint and processes
    incoming events (file additions, modifications, renames,
    deletions). Also sets up local file watching to detect
    changes and broadcast them to other clients.

    This function runs until the WebSocket connection is closed.
    """
    LOG.info("Continuous sync starting…")

    # Build WebSocket URI (no token in URL for security)
    api_base = CONFIG["API"]["events"]
    cid = CONFIG["CID"]
    bucket = CONFIG["REMOTE_BUCKET"]
    uri = f"{api_base}/{bucket}/events/{cid}"

    # Configure SSL context for secure WebSocket connections
    ssl_context = None
    if str(api_base).startswith("wss"):
        ssl_context = create_default_context(Purpose.SERVER_AUTH)
        if CONFIG["NO_VERIFY_SSL"]:
            # Disable SSL verification (insecure mode)
            ssl_context.check_hostname = False
            ssl_context.verify_mode = CERT_NONE
        elif CONFIG.get("CA_CERT"):
            # Load custom CA certificate for self-signed server certs
            ssl_context.load_verify_locations(cafile=CONFIG["CA_CERT"])
            ssl_context.check_hostname = True
            ssl_context.verify_mode = CERT_REQUIRED
        else:
            # Default SSL verification
            ssl_context.check_hostname = True
            ssl_context.verify_mode = CERT_REQUIRED

    # Connect to WebSocket endpoint with heartbeat
    async with connect(
        uri,
        ssl=ssl_context,
        ping_interval=30,
        ping_timeout=10,
        close_timeout=5,
    ) as websocket:
        LOG.info(f"WebSocket connected to {uri}")

        # Authenticate via first message (more secure than URL token)
        # Include client version for compatibility checking
        auth_message = {
            "type": "auth",
            "token": HSYNC_API_KEY,
            "version": HSYNC_VERSION,
        }
        await websocket.send(dumps(auth_message))

        # Wait for authentication confirmation
        auth_response = await websocket.recv()
        auth_result = loads(auth_response)
        if auth_result.get("type") != "auth" or auth_result.get("status") != "ok":
            LOG.error(f"WebSocket authentication failed: {auth_result}")
            # Check if it's a version incompatibility error
            if auth_result.get("error") == "version_incompatible":
                LOG.error(
                    f"Server version {auth_result.get('server_version')} is "
                    f"incompatible with client version {HSYNC_VERSION}"
                )
            return

        # Check server version compatibility
        server_version = auth_result.get("version", "")
        if server_version:
            is_compatible, reason = check_version_compatibility(
                HSYNC_VERSION, server_version
            )
            if not is_compatible:
                LOG.error(
                    f"Server version {server_version} is incompatible with "
                    f"client version {HSYNC_VERSION} (reason: {reason})"
                )
                return
            if reason == "minor_mismatch":
                LOG.warning(
                    f"Server version {server_version} differs from client "
                    f"version {HSYNC_VERSION}. Sync may have issues."
                )

        # Store server compression capability for file transfer decisions
        server_compression = auth_result.get("compression_enabled", False)
        CONFIG["SERVER_COMPRESSION_ENABLED"] = server_compression
        compression_status = "enabled" if server_compression else "disabled"
        LOG.info(
            f"WebSocket authenticated successfully "
            f"(server: {server_version or 'unknown'}, compression: {compression_status})"
        )

        LOG.debug(f"Sync reconnection delay: {CONFIG['RECONNECT_DELAY']} sec.")
        LOG.debug(f"Force resync: {CONFIG['RESYNC']} sec.")

        LOG.info(f"Client ID (CID): {CONFIG['CID']}")

        # Set up event queue for reliable message delivery
        loop = get_running_loop()
        event_queue = EventQueue(websocket)

        # Start event queue processor as background task
        queue_task = create_task(event_queue.process())

        # Set up local file system watcher with event queue
        event_handler = FileEventHandler(websocket, loop, event_queue)
        observer = Observer()
        observer.schedule(event_handler, CONFIG["LOCAL_BUCKET"], recursive=True)
        observer.start()

        try:
            # Main event loop - process incoming WebSocket messages
            await _process_websocket_events(websocket, bucket)
        except ConnectionClosed as e:
            # WebSocket connection was closed
            if e.reason:
                LOG.warning(f"Connection closed: {e.reason}")
            else:
                LOG.warning("Connection closed.")
        except Exception as e:
            # Unexpected error during event processing
            LOG.error(f"Exception: {e}")
        finally:
            # Stop event queue processor
            event_queue.stop()
            queue_task.cancel()
            try:
                await queue_task
            except Exception:
                pass  # Task was cancelled
            # Clean up file system observer
            observer.stop()
            observer.join()


async def _process_websocket_events(websocket, bucket: str) -> None:
    """
    Process incoming WebSocket events in the main loop.

    Args:
        websocket: Active WebSocket connection.
        bucket: Remote bucket name for filtering events.
    """
    while True:
        data = await websocket.recv()
        message = loads(data)

        LOG.debug(f"Received message for CID {CONFIG['CID']}: {message}")

        # Validate event type
        if message.get("name") not in {
            EventName.added.value,
            EventName.modified.value,
            EventName.renamed.value,
            EventName.deleted.value,
        }:
            LOG.error(f"Invalid event: {message.get('name')}")
            continue

        # Skip events from this client (echo prevention)
        if message.get("cid") == CONFIG["CID"]:
            LOG.debug("Same CID, dropped.")
            continue

        # Skip events from different buckets
        if message.get("bucket") != bucket:
            LOG.debug("Different bucket, dropped.")
            continue

        # Extract event data
        src = message.get("src", None)
        dst = message.get("dst", None)
        event_name = EventName(message.get("name"))

        # Handle Added / Modified events
        if event_name in {EventName.added, EventName.modified}:
            if src and "path" in src:
                # Directory event
                update_dir(src)
            elif src and "filepath" in src:
                # File event
                update_file(src)

        # Handle Renamed events
        elif event_name == EventName.renamed:
            _handle_rename_event(src, dst)

        # Handle Deleted events
        elif event_name == EventName.deleted:
            _handle_delete_event(src)

        # Handle Symlink events
        elif event_name == EventName.symlink:
            _handle_symlink_event(src)


def _handle_rename_event(src: Optional[dict], dst: Optional[dict]) -> None:
    """Handle rename events for files and directories."""
    if src and "path" in src and dst:
        # Directory rename
        src_dir = Dir.from_dict(src, CONFIG["LOCAL_BUCKET"])
        dst_dir = Dir.from_dict(dst, CONFIG["LOCAL_BUCKET"])

        if src_dir.path and dst_dir.path and exists(src_dir.path):
            # Source exists - perform rename
            rename(src_dir.path, dst_dir.path)
        else:
            # Source doesn't exist - log warning and create destination
            LOG.warning(f"Cannot rename: {src_dir.path} -> {dst_dir.path}")
            LOG.warning(f"Not found: {src_dir.path}")
            update_dir(dst)

    elif src and "filepath" in src and dst:
        # File rename event
        src_file = File.from_dict(src, CONFIG["LOCAL_BUCKET"], False)
        dst_file = File.from_dict(dst, CONFIG["LOCAL_BUCKET"], False)

        if src_file.filepath and dst_file.filepath and exists(src_file.filepath):
            # Source exists - perform rename
            rename(src_file.filepath, dst_file.filepath)
        else:
            # Source doesn't exist - log warning and download destination
            LOG.warning(
                f'Cannot rename: "{src_file.filepath}" -> "{dst_file.filepath}"'
            )
            LOG.warning(f'Not found: "{src_file.filepath}"')
            download_file(dst_file)


def _handle_delete_event(src: Optional[dict]) -> None:
    """Handle delete events for files and directories.

    If IGNORE_DELETE is enabled, remote deletions are ignored (backup mode).
    """
    # Skip remote deletions if ignore_delete is enabled (backup mode)
    if CONFIG.get("IGNORE_DELETE", False):
        path = src.get("path") or src.get("filepath") if src else None
        LOG.debug(f'[IGNORE DEL] "{path}" (ignore-delete mode)')
        return

    if src and "path" in src:
        # Directory deletion event
        dir = Dir.from_dict(src, CONFIG["LOCAL_BUCKET"])
        if dir.path and exists(dir.path):
            # Directory exists - remove recursively
            rmtree(dir.path, ignore_errors=False)
        else:
            LOG.warning(f'Cannot delete: "{dir.path}"')
            LOG.warning(f'Not found: "{dir.path}"')

    elif src and "filepath" in src:
        # File deletion event
        file = File.from_dict(src, CONFIG["LOCAL_BUCKET"], False)
        if file.filepath and exists(file.filepath):
            # File exists - remove it
            remove(file.filepath)
        else:
            LOG.warning(f'Cannot delete: "{file.filepath}"')
            LOG.warning(f'Not found: "{file.filepath}"')


def _handle_symlink_event(src: Optional[dict]) -> None:
    """Handle symlink creation events."""
    if src and src.get("type") == "symlink":
        # Create symlink locally
        link = Symlink.from_dict(src)
        link.filepath = f"{CONFIG['LOCAL_BUCKET']}/{link.filepath.lstrip('/')}"
        target = link.target

        if link.filepath and target:
            LOG.info(f'[SYMLINK   ] Creating symlink: "{link.filepath}" -> "{target}"')
            # Ensure parent directory exists
            parent_dir = dirname(link.filepath)
            if not exists(parent_dir):
                makedirs(parent_dir, exist_ok=True)

            # Remove existing file/symlink if present
            if islink(link.filepath) or exists(link.filepath):
                unlink(link.filepath)

            # Create the symlink
            os_symlink(target, link.filepath)


# =============================================================================
# Resync and Reconnection
# =============================================================================


async def resync() -> None:
    """
    Periodic resynchronization task.

    Runs as a background task during continuous sync mode,
    performing full sync cycles at configured intervals to
    catch any missed events or resolve inconsistencies.
    """
    try:
        while True:
            await sleep(CONFIG["RESYNC"] or 60)
            LOG.info(f"Force sync ({CONFIG['RESYNC']} sec.)")
            await sync()
    except Exception as e:
        # Task was cancelled or error occurred
        LOG.debug(f"Resync task stopped: {e}")


async def remote_connect() -> None:
    """
    Manage WebSocket connection with automatic reconnection.

    Performs initial sync, optionally starts periodic resync,
    then enters continuous event monitoring. Automatically
    reconnects on connection loss after configured delay.
    Cancels previous resync task before creating a new one.
    """
    global _resync_task

    while True:
        try:
            # Perform initial sync before entering event loop
            await sync()

            # Cancel previous resync task if it exists
            if _resync_task is not None and not _resync_task.done():
                _resync_task.cancel()
                try:
                    await _resync_task
                except Exception:
                    pass  # Task was cancelled, ignore

            # Start background resync task if configured
            if CONFIG["RESYNC"]:
                _resync_task = create_task(resync())

            # Enter continuous event monitoring
            await remote_events()

        except ConnectionClosed:
            # WebSocket disconnected - wait before reconnecting
            await sleep(CONFIG["RECONNECT_DELAY"])

        except Exception as e:
            # Unexpected error - log and retry
            LOG.error(f"An unexpected error occurred: {e}.")
            await sleep(CONFIG["RECONNECT_DELAY"])

        finally:
            LOG.info(f"Reconnecting in {CONFIG['RECONNECT_DELAY']} sec.…")


# =============================================================================
# File System Event Handler
# =============================================================================


class FileEventHandler(FileSystemEventHandler):
    """
    File system event handler for local file watching.

    Monitors the local bucket directory for file system changes and
    broadcasts events to other HSYNC clients via WebSocket. Uses the
    watchdog library's Observer pattern to detect create, modify,
    move, and delete events.

    Attributes:
        websocket: WebSocket connection to the server.
        loop: asyncio event loop for scheduling coroutines.
    """

    def __init__(self, websocket, loop, event_queue: Optional[EventQueue] = None) -> None:
        """
        Initialize the file event handler.

        Args:
            websocket: Active WebSocket connection to the server.
            loop: Running asyncio event loop for thread-safe operations.
            event_queue: Optional event queue for reliable message delivery.
        """
        self.websocket = websocket
        self.loop = loop
        self.event_queue = event_queue
        # Track last modified time for debouncing on systems without on_closed
        self._last_modified: dict[str, float] = {}

    def _send_message(self, message: Event) -> None:
        """
        Send an event message to the server.

        Uses event queue if available for reliable delivery with retry,
        otherwise sends directly via WebSocket.

        Args:
            message: Event to send.
        """
        LOG.debug(f"Send message: {message}")

        message_dict = message.to_dict()

        if self.event_queue:
            # Use event queue for reliable delivery
            run_coroutine_threadsafe(
                self.event_queue.enqueue(message_dict), self.loop
            )
        else:
            # Direct send (legacy)
            run_coroutine_threadsafe(
                self.websocket.send(dumps(message_dict)), self.loop
            )

    def on_any_event(self, event) -> None:
        """
        Log debug information for all events.

        Called for every file system event. In DEBUG mode, logs when
        hidden files or directories are being skipped.

        Args:
            event: File system event from watchdog.
        """
        hidden_prefix = "dir" if event.is_directory else "file"
        src_path = ensure_str_path(event.src_path)
        dest_path = ensure_str_path(getattr(event, 'dest_path', None))
        paths = {"src": src_path, "dest": dest_path}

        for kind, path in paths.items():
            if path and (
                (event.is_directory and is_hidden_dir(path))
                or (not event.is_directory and is_hidden_file(path))
            ):
                LOG.debug(f"Skip hidden {kind} {hidden_prefix}: {path}")

    def on_created(self, event) -> None:
        """
        Handle file/directory creation events.

        For directories: broadcasts 'added' event to server.
        For symlinks: broadcasts 'symlink' event to server.

        Args:
            event: File system creation event from watchdog.
        """
        src_path = ensure_str_path(event.src_path)

        if event.is_directory and not is_hidden_dir(src_path):
            # Directory created - broadcast event
            LOG.info(f'[CREATE    ] "{src_path}"')
            dir = Dir.get(src_path)
            if dir:
                message = Event(
                    CONFIG["CID"],
                    CONFIG["LOCAL_BUCKET"],
                    CONFIG["REMOTE_BUCKET"],
                    EventName.added,
                    dir,
                )
                self._send_message(message)
        elif not is_hidden_file(src_path) and islink(src_path):
            # Symlink created - broadcast symlink event
            LOG.info(f'[SYMLINK   ] "{src_path}"')
            symlink = Symlink.get(src_path)
            if symlink:
                message = Event(
                    CONFIG["CID"],
                    CONFIG["LOCAL_BUCKET"],
                    CONFIG["REMOTE_BUCKET"],
                    EventName.symlink,
                    symlink,
                )
                self._send_message(message)

    def on_modified(self, event) -> None:
        """
        Handle file/directory modification events.

        For directories: broadcasts 'modified' event if permissions changed.
        For files on Linux: handled by on_closed() instead.
        For files on macOS/Windows: handles file modifications with debouncing
        since on_closed is not supported on these platforms.

        Args:
            event: File system modification event from watchdog.
        """
        src_path = ensure_str_path(event.src_path)

        if event.is_directory:
            if (
                not is_hidden_dir(src_path)
                and not is_same_paths(CONFIG["LOCAL_BUCKET"], src_path)
            ):
                # Directory modified (permissions change) - broadcast event
                LOG.info(f'[MOVE      ] "{src_path}"')
                dir = Dir.get(src_path)
                if dir:
                    message = Event(
                        CONFIG["CID"],
                        CONFIG["LOCAL_BUCKET"],
                        CONFIG["REMOTE_BUCKET"],
                        EventName.modified,
                        dir,
                    )
                    self._send_message(message)
        elif not SUPPORTS_ON_CLOSED:
            # Fallback for macOS/Windows where on_closed is not supported
            # Use debouncing to avoid multiple uploads during file writes
            if not is_hidden_file(src_path):
                current_time = time()
                last_time = self._last_modified.get(src_path, 0)

                if current_time - last_time >= MODIFIED_DEBOUNCE_INTERVAL:
                    self._last_modified[src_path] = current_time
                    try:
                        if exists(src_path):
                            file = File.get(src_path)
                            if file:
                                LOG.debug(
                                    f'[MODIFIED  ] "{src_path}" '
                                    f"(fallback for non-Linux system)"
                                )
                                upload_file(file)
                    except Exception as e:
                        LOG.error(
                            f'Error handling modified event for "{src_path}": {e}'
                        )

    def on_closed(self, event) -> None:
        """
        Handle file close events (file write completed).

        Triggered when a file is closed after writing. This is the
        appropriate time to upload the file since writing is complete.

        Args:
            event: File close event from watchdog.
        """
        src_path = ensure_str_path(event.src_path)

        # Only process files, not directories or hidden files
        if not event.is_directory and not is_hidden_file(src_path):
            try:
                if exists(src_path):
                    # File exists - upload it
                    file = File.get(src_path)
                    if file:
                        upload_file(file)
                else:
                    # File was deleted before we could upload
                    LOG.warning(
                        f'File "{src_path}" not found. '
                        f"It might have been altered by an external tool."
                    )
            except Exception as e:
                LOG.error(f'Error handling closed event for "{src_path}": {e}')

    def on_moved(self, event) -> None:
        """
        Handle file/directory move/rename events.

        Broadcasts 'renamed' events when files or directories are moved.
        Handles special cases like moves involving hidden directories.

        Args:
            event: File system move event from watchdog.
        """
        src_path = ensure_str_path(event.src_path)
        dest_path = ensure_str_path(getattr(event, 'dest_path', None))

        if dest_path:
            LOG.debug(f'[MOVE      ] "{src_path}" -> "{dest_path}"')
        else:
            LOG.debug(f'[MOVE      ] "{src_path}"')

        message = None

        if event.is_directory:
            # Handle various hidden directory scenarios
            if is_hidden_dir(src_path) and not dest_path:
                # Hidden source, no destination - ignore
                return
            if is_hidden_dir(src_path) and is_hidden_dir(dest_path):
                # Both hidden - ignore
                return
            if is_hidden_dir(src_path) and not is_hidden_dir(dest_path):
                # Hidden to visible - treat as creation of visible directory
                LOG.debug(f"Switch event to created: {dest_path}")
                created_event = DirCreatedEvent(dest_path)
                self.on_created(created_event)
                return
            if is_hidden_dir(dest_path):
                # Moving to hidden directory - treat as deletion
                LOG.warning(f"Switch event to deleted: {dest_path}")
                deleted_event = DirDeletedEvent(dest_path)
                self.on_deleted(deleted_event)
                return

            # Build event message for directory rename
            dst = Dir.get(dest_path)
            if dst:
                src = deepcopy(dst)
                src.path = src_path

                # Send rename event
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

            # Build event message for file rename
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

        # Send event to server via WebSocket
        if message:
            self._send_message(message)

    def on_deleted(self, event) -> None:
        """
        Handle file/directory deletion events.

        Broadcasts 'deleted' events when files or directories are removed
        from the local filesystem. Ignores hidden files and directories.
        If IGNORE_DELETE is enabled, deletions are not propagated (backup mode).

        Args:
            event: File system deletion event from watchdog.
        """
        src_path = ensure_str_path(event.src_path)

        # Skip hidden directories
        if event.is_directory and is_hidden_dir(src_path):
            return

        # Skip hidden files
        if not event.is_directory and is_hidden_file(src_path):
            return

        # Skip deletion propagation if ignore_delete is enabled (backup mode)
        if CONFIG.get("IGNORE_DELETE", False):
            LOG.debug(f'[IGNORE DEL] "{src_path}" (ignore-delete mode)')
            return

        LOG.info(f'[REMOVE    ] "{src_path}"')

        # Create appropriate object type for the deleted item
        obj = Dir(src_path) if event.is_directory else File(src_path)

        # Build deletion event message
        message = Event(
            CONFIG["CID"],
            CONFIG["LOCAL_BUCKET"],
            CONFIG["REMOTE_BUCKET"],
            EventName.deleted,
            obj,
        )

        # Send event to server via WebSocket
        self._send_message(message)

