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
from contextlib import suppress
from json import JSONDecodeError, dumps, loads
from ssl import CERT_NONE, CERT_REQUIRED, Purpose, create_default_context

from hsync_common.models.event import Event, EventName
from hsync_common.models.file import File
from hsync_common.protocol.handshake import (
    PROTOCOL_VERSION_WS,
    build_ws_auth_request,
    decode_capabilities,
)
from hsync_common.text import safe_json_for_log
from loguru import logger as log
from watchdog.observers import Observer
from websockets import ConnectionClosed, connect

from hsync_client.capabilities import adopt_remote_capabilities, local_capabilities
from hsync_client.config import CONFIG, get_api_key
from hsync_client.constants import HSYNC_VERSION, WS_MAX_MESSAGE_BYTES
from hsync_client.event_dispatch import EventDispatchContext, apply_inbound_event
from hsync_client.event_queue import EventQueue
from hsync_client.events import UiEventName, emit_event
from hsync_client.file_event_handler import BaseFileEventHandler
from hsync_client.sync import sync
from hsync_client.transfer import upload_file
from hsync_client.utils import warn_if_inotify_watch_limit_low

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

    # Connect to WebSocket endpoint with heartbeat. ``max_size`` is set
    # explicitly (events carry file metadata, never bulk content) instead of
    # relying on the library default.
    async with connect(
        uri,
        ssl=ssl_context,
        ping_interval=30,
        ping_timeout=10,
        close_timeout=5,
        max_size=WS_MAX_MESSAGE_BYTES,
    ) as websocket:
        log.info(f"WebSocket connected to {uri}")
        emit_event(UiEventName.status, status="connected")

        # Authenticate via first message (more secure than URL token). The frame
        # follows the shared handshake contract (hsync_common.protocol.handshake);
        # the advertised capabilities are the shared runtime-derived set
        # (ADR-0024), identical to the one the LAN handshake announces.
        auth_message = build_ws_auth_request(
            token=CONFIG.get("API_KEY") or get_api_key(),
            app_version=HSYNC_VERSION,
            capabilities=local_capabilities(),
        )
        await websocket.send(dumps(auth_message))

        # Wait for authentication confirmation
        auth_response = await websocket.recv()
        auth_result = loads(auth_response)
        if auth_result.get("type") != "auth" or auth_result.get("status") != "ok":
            log.error(f"WebSocket authentication failed: {auth_result}")
            emit_event(UiEventName.auth_error, message="WebSocket authentication failed")
            if auth_result.get("error") == "protocol_incompatible":
                log.error(
                    f"Server protocol {auth_result.get('protocol_version')} is "
                    f"incompatible with client protocol {PROTOCOL_VERSION_WS}"
                )
            return

        # Enforce the wire-protocol version: a mismatch is a hard failure
        # (capabilities are feature flags within a version, never a bridge).
        server_protocol = auth_result.get("protocol_version", "")
        if server_protocol != PROTOCOL_VERSION_WS:
            log.error(
                f"Server protocol {server_protocol or 'missing'} is incompatible "
                f"with client protocol {PROTOCOL_VERSION_WS}"
            )
            emit_event(UiEventName.auth_error, message="WebSocket protocol incompatible")
            return

        # Parse the server's advertised capabilities for this session and adopt
        # them as the transfer gates (shared helper, same semantics as the LAN
        # handshake — ADR-0024).
        server_caps = decode_capabilities(auth_result.get("capabilities"))
        adopt_remote_capabilities(server_caps)
        compression_status = "enabled" if CONFIG["SERVER_COMPRESSION_ENABLED"] else "disabled"
        log.info(
            f"WebSocket authenticated successfully "
            f"(server: {auth_result.get('app_version') or 'unknown'}, "
            f"compression: {compression_status})"
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
        observer = Observer()
        event_handler = RemoteFileEventHandler(websocket, loop, event_queue, observer)
        warn_if_inotify_watch_limit_low(CONFIG["LOCAL_BUCKET"])
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
            emit_event(UiEventName.status, status="waiting")
        except Exception as e:
            # Unexpected error during event processing
            log.error(f"Exception: {e}")
            emit_event(UiEventName.error, message=f"Exception: {e}")
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
        try:
            message = loads(data)
        except JSONDecodeError as e:
            log.error(f"Malformed JSON frame dropped (connection kept alive): {e}")
            continue

        log.debug(f"Received message for CID {CONFIG['CID']}: {safe_json_for_log(message)}")

        # Handle server heartbeat ping — respond with pong to keep connection alive
        if message.get("type") == "ping":
            await websocket.send(dumps({"type": "pong"}))
            log.debug("Received ping, sent pong")
            continue

        # Validate event type. Symlink events ARE propagated by the server
        # (handle_symlink_event materialises the link and returns the Event,
        # and ConnectionManager.send_message broadcasts it to every peer
        # except the sender): applying them here gives real-time symlink
        # replication between clients in remote mode, matching LAN mode.
        if message.get("name") not in {
            EventName.added.value,
            EventName.modified.value,
            EventName.renamed.value,
            EventName.deleted.value,
            EventName.symlink.value,
        }:
            log.error(f"Invalid event: {message.get('name')}")
            continue

        # Skip events from different buckets (remote-only filter)
        if message.get("bucket") != bucket:
            log.debug("Different bucket, dropped.")
            continue

        # Echo suppression, upload-only filter, and the per-event apply
        # logic are delegated to the unified dispatcher. One malformed event
        # must not kill the connection loop (the LAN client loop isolates
        # per-message failures the same way).
        try:
            await apply_inbound_event(message, REMOTE_DISPATCH_CTX)
        except Exception as e:
            log.error(f"Failed to apply inbound event {message.get('name')!r}: {e}")


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
            # Skip this cycle if a sync is already in progress (e.g. triggered
            # by a reconnect or a watcher-induced resync). This prevents two
            # concurrent sync() cycles from racing on the same merge-base /
            # tombstone stores. The flag is advisory and non-blocking: a
            # missed cycle just means the next interval picks up the changes.
            from hsync_client.sync import is_sync_cycle_active

            if is_sync_cycle_active():
                log.debug("Force sync skipped: a sync cycle is already in progress")
                continue
            log.info(f"Force sync ({CONFIG['RESYNC']} sec.)")
            await sync()
    except Exception as e:
        # Task was cancelled or error occurred
        log.debug(f"Resync task stopped: {e}")


async def _refresh_network_tuning() -> None:
    """Re-measure RTT on WS reconnect and update network-derived CONFIG keys."""
    if not CONFIG.get("AUTOTUNE_ENABLED", False):
        return
    try:
        from urllib.parse import urlparse

        from hsync_client.autotune import (
            classify_network,
            derive_retry,
            derive_timeout,
            measure_rtt,
        )
        from hsync_client.constants import (
            HTTP_TIMEOUT_MAX,
            HTTP_TIMEOUT_MIN,
            RECONNECT_DELAY_MAX,
            RECONNECT_DELAY_MIN,
        )

        api_url = CONFIG["API"]["buckets"]
        parsed = urlparse(api_url)
        rtt_host = parsed.hostname or "localhost"
        rtt_port = parsed.port or (443 if parsed.scheme == "https" else 80)

        rtt_ms = measure_rtt(rtt_host, rtt_port)
        net_class = classify_network(rtt_ms)

        timeout = derive_timeout(rtt_ms, net_class, HTTP_TIMEOUT_MIN, HTTP_TIMEOUT_MAX)
        min_wait, max_wait, reconnect = derive_retry(
            rtt_ms,
            net_class,
            RECONNECT_DELAY_MIN,
            RECONNECT_DELAY_MAX,
        )

        CONFIG["HTTP_TIMEOUT"] = timeout
        CONFIG["TRANSFER_RETRY_MIN_WAIT"] = float(min_wait)
        CONFIG["TRANSFER_RETRY_MAX_WAIT"] = float(max_wait)
        CONFIG["RECONNECT_DELAY"] = reconnect
        log.debug(
            f"autotune (reconnect): rtt={rtt_ms or '?'}ms "
            f"net={net_class.value} -> timeout={timeout}s "
            f"retry=({min_wait},{max_wait})s reconnect={reconnect}s"
        )
    except Exception as e:
        log.debug(f"autotune: reconnect RTT probe failed ({e}); keeping current values")


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

            # Cancel previous resync task if it exists. CancelledError is a
            # BaseException, so awaiting the task we just cancelled re-raises
            # it and must be suppressed explicitly.
            if _resync_task is not None and not _resync_task.done():
                _resync_task.cancel()
                with suppress(CancelledError):
                    await _resync_task

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
            # Re-measure RTT to catch network changes (e.g. laptop moved
            # from Ethernet to Wi-Fi) before the next sync cycle starts.
            await _refresh_network_tuning()
            log.info(f"Reconnecting in {CONFIG['RECONNECT_DELAY']} sec.…")


# =============================================================================
# File System Event Handler
# =============================================================================


class RemoteFileEventHandler(BaseFileEventHandler):
    """Watchdog handler for Remote sync: broadcasts events and uploads files
    to the central server via HTTP.
    """

    def __init__(
        self, websocket, loop, event_queue: EventQueue | None = None, observer=None
    ) -> None:
        """
        Args:
            websocket: Active WebSocket connection to the server.
            loop: Running asyncio event loop for thread-safe operations.
            event_queue: Optional event queue for reliable message delivery.
            observer: Optional watchdog Observer for dynamic subdirectory watching.
        """
        super().__init__(observer=observer)
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

    def _on_file_ready(
        self,
        file: File,
        src_path: str,
        from_closed: bool = False,
        *,
        trigger: str | None = None,
    ) -> None:
        """Upload the file via HTTP to the central server."""
        upload_file(file, from_closed=from_closed)
