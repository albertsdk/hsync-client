# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
WebSocket event handling for HSYNC Client.

This module provides WebSocket connection management and event handling
for real-time synchronization with the server.
"""

from asyncio import (
    CancelledError,
    Task,
    create_task,
    get_running_loop,
    run_coroutine_threadsafe,
    sleep,
)
from json import dumps, loads
from ssl import CERT_NONE, CERT_REQUIRED, Purpose, create_default_context

from hsync_common.text import safe_json_for_log
from loguru import logger as log
from watchdog.observers import Observer
from websockets import ConnectionClosed, connect

from hsync_client.config import CONFIG, get_api_key
from hsync_client.constants import HSYNC_VERSION
from hsync_client.event_dispatch import EventDispatchContext, apply_inbound_event
from hsync_client.event_queue import EventQueue
from hsync_client.file_event_handler import BaseFileEventHandler
from hsync_client.lan import check_version_compatibility
from hsync_client.models.event import Event, EventName
from hsync_client.models.file import File
from hsync_client.sync import sync
from hsync_client.transfer import upload_file

# Global resync task reference for cancellation on reconnect
_resync_task: Task[None] | None = None


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
    log.info("Continuous sync starting…")

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
        log.info(f"WebSocket connected to {uri}")

        # Authenticate via first message (more secure than URL token)
        # Include client version for compatibility checking
        auth_message = {
            "type": "auth",
            "token": CONFIG.get("API_KEY") or get_api_key(),
            "version": HSYNC_VERSION,
        }
        await websocket.send(dumps(auth_message))

        # Wait for authentication confirmation
        auth_response = await websocket.recv()
        auth_result = loads(auth_response)
        if auth_result.get("type") != "auth" or auth_result.get("status") != "ok":
            log.error(f"WebSocket authentication failed: {auth_result}")
            # Check if it's a version incompatibility error
            if auth_result.get("error") == "version_incompatible":
                log.error(
                    f"Server version {auth_result.get('server_version')} is "
                    f"incompatible with client version {HSYNC_VERSION}"
                )
            return

        # Check server version compatibility
        server_version = auth_result.get("version", "")
        if server_version:
            is_compatible, reason = check_version_compatibility(HSYNC_VERSION, server_version)
            if not is_compatible:
                log.error(
                    f"Server version {server_version} is incompatible with "
                    f"client version {HSYNC_VERSION} (reason: {reason})"
                )
                return
            if reason == "minor_mismatch":
                log.warning(
                    f"Server version {server_version} differs from client "
                    f"version {HSYNC_VERSION}. Sync may have issues."
                )

        # Store server compression capability for file transfer decisions
        server_compression = auth_result.get("compression_enabled", False)
        CONFIG["SERVER_COMPRESSION_ENABLED"] = server_compression
        compression_status = "enabled" if server_compression else "disabled"
        log.info(
            f"WebSocket authenticated successfully "
            f"(server: {server_version or 'unknown'}, compression: {compression_status})"
        )

        log.debug(f"Sync reconnection delay: {CONFIG['RECONNECT_DELAY']} sec.")
        log.debug(f"Force resync: {CONFIG['RESYNC']} sec.")

        log.info(f"Client ID (CID): {CONFIG['CID']}")

        # Set up event queue for reliable message delivery
        loop = get_running_loop()
        event_queue = EventQueue(websocket)

        # Start event queue processor as background task
        queue_task = create_task(event_queue.process())

        # Set up local file system watcher with event queue
        event_handler = RemoteFileEventHandler(websocket, loop, event_queue)
        observer = Observer()
        observer.schedule(event_handler, CONFIG["LOCAL_BUCKET"], recursive=True)
        observer.start()

        try:
            # Main event loop - process incoming WebSocket messages
            await _process_remote_events(websocket, bucket)
        except ConnectionClosed as e:
            # WebSocket connection was closed
            if e.reason:
                log.warning(f"Connection closed: {e.reason}")
            else:
                log.warning("Connection closed.")
        except Exception as e:
            # Unexpected error during event processing
            log.error(f"Exception: {e}")
        finally:
            # Stop event queue processor
            event_queue.stop()
            queue_task.cancel()
            try:
                await queue_task
            except CancelledError:
                pass  # Expected after cancellation
            except Exception:
                pass  # Task was cancelled
            # Clean up file system observer
            observer.stop()
            observer.join()


# Inbound dispatch context for Remote sync: no slave-handler echo
# suppression is needed (the watcher uses CID-based filtering), and a
# missing rename source is recovered by fetching the destination from
# the server.
REMOTE_DISPATCH_CTX = EventDispatchContext(
    log_prefix="",
    create_symlink_locally=True,
    recover_on_missing_rename_src=True,
)


async def _process_remote_events(websocket, bucket: str) -> None:
    """
    Process incoming WebSocket events in the main loop.

    Args:
        websocket: Active WebSocket connection.
        bucket: Remote bucket name for filtering events.
    """
    while True:
        data = await websocket.recv()
        message = loads(data)

        log.debug(f"Received message for CID {CONFIG['CID']}: {safe_json_for_log(message)}")

        # Handle server heartbeat ping — respond with pong to keep connection alive
        if message.get("type") == "ping":
            await websocket.send(dumps({"type": "pong"}))
            log.debug("Received ping, sent pong")
            continue

        # Validate event type (symlink events are not propagated by the server)
        if message.get("name") not in {
            EventName.added.value,
            EventName.modified.value,
            EventName.renamed.value,
            EventName.deleted.value,
        }:
            log.error(f"Invalid event: {message.get('name')}")
            continue

        # Skip events from different buckets (remote-only filter)
        if message.get("bucket") != bucket:
            log.debug("Different bucket, dropped.")
            continue

        # Echo suppression, upload-only filter, and the per-event apply
        # logic are delegated to the unified dispatcher.
        await apply_inbound_event(message, REMOTE_DISPATCH_CTX)


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
            log.info(f"Force sync ({CONFIG['RESYNC']} sec.)")
            await sync()
    except Exception as e:
        # Task was cancelled or error occurred
        log.debug(f"Resync task stopped: {e}")


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
            log.error(f"An unexpected error occurred: {e}.")
            await sleep(CONFIG["RECONNECT_DELAY"])

        finally:
            log.info(f"Reconnecting in {CONFIG['RECONNECT_DELAY']} sec.…")


# =============================================================================
# File System Event Handler
# =============================================================================


class RemoteFileEventHandler(BaseFileEventHandler):
    """Watchdog handler for Remote sync: broadcasts events and uploads files
    to the central server via HTTP.
    """

    def __init__(self, websocket, loop, event_queue: EventQueue | None = None) -> None:
        """
        Args:
            websocket: Active WebSocket connection to the server.
            loop: Running asyncio event loop for thread-safe operations.
            event_queue: Optional event queue for reliable message delivery.
        """
        super().__init__()
        self.websocket = websocket
        self.loop = loop
        self.event_queue = event_queue

    def _broadcast_event(
        self,
        message: Event,
        src_abs_path: str | None = None,
        dst_abs_path: str | None = None,
    ) -> None:
        """Send an event to the server, with optional event-queue retry."""
        if CONFIG.get("SYNC_DIRECTION") == "download-only":
            log.debug("Download-only mode: local event not broadcast.")
            return

        log.debug(f"Send message: {message}")
        message_dict = message.to_dict()

        if self.event_queue:
            run_coroutine_threadsafe(self.event_queue.enqueue(message_dict), self.loop)
        else:
            run_coroutine_threadsafe(self.websocket.send(dumps(message_dict)), self.loop)

    def _on_file_ready(self, file: File, src_path: str) -> None:
        """Upload the file via HTTP to the central server."""
        upload_file(file)
