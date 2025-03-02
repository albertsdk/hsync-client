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
import hmac
import json
import ssl
import threading
from os import listdir
from pathlib import Path
from time import time
from typing import Any

from hsync_common.models.dir import is_hidden_dir
from hsync_common.models.event import Event, EventName
from hsync_common.models.file import (
    File,
    invalidate_sha256_cache,
    is_hidden_file,
    is_partial_file,
)
from hsync_common.text import escape_loguru_markup
from loguru import logger as log
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from hsync_client.config import CONFIG
from hsync_client.constants import HSYNCKEEP_FILENAME, WS_MAX_MESSAGE_BYTES, ActionLabel
from hsync_client.event_dispatch import EventDispatchContext, apply_inbound_event
from hsync_client.file_event_handler import (
    MODIFIED_DEBOUNCE_INTERVAL,
    SUPPORTS_ON_CLOSED,
    BaseFileEventHandler,
)
from hsync_client.utils import ensure_str_path, warn_if_inotify_watch_limit_low

# Conservative minimum effective LAN bandwidth used to derive the dynamic
# ``wait_pending`` timeout from the total bytes still in flight (10 MB/s).
MIN_BANDWIDTH_BPS = 10 * 1024 * 1024
# Hard cap on the dynamic ``wait_pending`` timeout to avoid waiting forever
# if a transfer stalls (30 minutes).
MAX_WAIT_PENDING_SECONDS = 30 * 60


# =============================================================================
# Inbound File Origin Tracking (echo suppression for LAN master)
# =============================================================================

# Map of absolute filepath -> (originator_cid, expiration_timestamp).
# The master populates this map every time it writes / deletes / renames a
# path on behalf of a peer (via HTTP POST/DELETE/PATCH).  When the watchdog
# fires the resulting filesystem event, ``LANMasterFileEventHandler._send_event``
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
    ) -> None:
        """
        Initialize the WebSocket server.

        Args:
            port: Port to listen on (different from HTTP port).
            use_https: Whether to use WSS instead of WS.
            cert_file: Path to SSL certificate file.
            key_file: Path to SSL private key file. The persistent master key is
                stored unencrypted and protected by filesystem permissions (see
                ``load_or_create_master_cert``).
        """
        self._port = port
        self._use_https = use_https
        self._cert_file = cert_file
        self._key_file = key_file
        self._clients: set[Any] = set()  # WebSocket connections
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: Any | None = None
        self._running = False
        self._observer: BaseObserver | None = None
        self._event_handler: LANMasterFileEventHandler | None = None

        if use_https and (not cert_file or not key_file):
            raise ValueError("cert_file and key_file are required when use_https=True")

    @property
    def port(self) -> int:
        """Get the WebSocket server port."""
        return self._port

    async def _handle_client(self, websocket) -> None:
        """Handle a single WebSocket client connection."""
        client_addr = websocket.remote_address
        client_cid = "unknown"
        log.info(f"WebSocket client connected: {client_addr}")

        # Send greeting: tell the client whether authentication is required
        lan_key = CONFIG.get("LAN_KEY")
        if lan_key:
            await websocket.send(json.dumps({"type": "auth_required"}))
            try:
                # Wait for auth message
                auth_msg = await asyncio.wait_for(websocket.recv(), timeout=10.0)
                auth_data = json.loads(auth_msg)
                token = auth_data.get("token")
                client_cid = auth_data.get("cid", "unknown")
                if auth_data.get("type") != "auth" or not (
                    isinstance(token, str) and hmac.compare_digest(token, str(lan_key))
                ):
                    log.warning(f"WebSocket auth failed from {client_addr} (CID: {client_cid})")
                    await websocket.send(json.dumps({"type": "auth", "status": "failed"}))
                    await websocket.close()
                    return
                await websocket.send(json.dumps({"type": "auth", "status": "ok"}))
                log.info(f"WebSocket client authenticated: {client_addr} (CID: {client_cid})")
            except TimeoutError:
                log.warning(f"WebSocket auth timeout from {client_addr} (CID: {client_cid})")
                await websocket.send(json.dumps({"type": "auth", "status": "failed"}))
                await websocket.close()
                return
            except Exception as e:
                log.warning(f"WebSocket auth error from {client_addr} (CID: {client_cid}): {e}")
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
                log.debug(
                    f"Received from {client_addr} (CID: {client_cid}): {escape_loguru_markup(message)}"
                )
        except Exception as e:
            log.debug(f"WebSocket client {client_addr} (CID: {client_cid}) disconnected: {e}")
        finally:
            self._clients.discard(websocket)
            log.info(f"WebSocket client disconnected: {client_addr} (CID: {client_cid})")

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
        if not Path(watch_path).exists():
            log.warning(f"Watch path '{watch_path}' does not exist, creating it")
            Path(watch_path).mkdir(parents=True, exist_ok=True)

        self._event_handler = LANMasterFileEventHandler(self)
        warn_if_inotify_watch_limit_low(watch_path)
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


# =============================================================================
# File Event Handler for LAN Master
# =============================================================================


class LANMasterFileEventHandler(BaseFileEventHandler):
    """Watchdog handler for LAN master: broadcasts file system events to all
    connected slaves via WebSocket. File contents are pulled by slaves, so
    ``_on_file_ready`` emits a ``modified`` event instead of uploading bytes.
    """

    _LOG_PREFIX = "LAN "

    def __init__(self, ws_server: LANWebSocketServer) -> None:
        """
        Args:
            ws_server: WebSocket server for broadcasting events.
        """
        super().__init__()
        self._ws_server = ws_server

    def _broadcast_event(
        self,
        event: Event,
        src_abs_path: str | None = None,
        dst_abs_path: str | None = None,
    ) -> None:
        """Send an event to all connected slaves.

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

    def _on_file_ready(self, file: File, src_path: str, from_closed: bool = False) -> None:
        """Broadcast a ``modified`` event so slaves can pull the new bytes.

        The master only propagates metadata (slaves pull the bytes), so
        ``from_closed`` carries no upload semantics here and is accepted purely
        to honour the :class:`BaseFileEventHandler` hook contract.
        """
        log.info(f'{ActionLabel.LAN_FILE} "{src_path}"')
        # A (re-)created/modified file invalidates any prior deletion marker so
        # peers do not delete the freshly written file on their next full sync.
        # Best-effort: tombstone bookkeeping must never break event propagation.
        try:
            from hsync_client.tombstones import clear_deletion

            clear_deletion(src_path)
        except Exception as e:  # pragma: no cover - defensive
            log.debug(f"Could not clear tombstone for {src_path}: {e}")
        self._broadcast_event(
            Event(
                CONFIG["CID"],
                CONFIG["LOCAL_BUCKET"],
                CONFIG["REMOTE_BUCKET"],
                EventName.modified,
                file,
            ),
            src_abs_path=src_path,
        )

    def _on_deleted(self, src_path: str, is_directory: bool) -> None:
        """Record a deletion marker so offline peers do not resurrect the path.

        This is the master's single chokepoint for deletions: it fires both for
        the master's own local removals and for slave-initiated deletes applied
        via the HTTP API (which trigger the same filesystem event). Directories
        carry no content hash so only files are recorded. Best-effort: a failure
        to persist the marker must never break event propagation.
        """
        if is_directory:
            return
        try:
            from hsync_client.tombstones import record_deletion

            record_deletion(src_path)
        except Exception as e:  # pragma: no cover - defensive
            log.debug(f"Could not record tombstone for {src_path}: {e}")
        # Drop the merge base too: a re-created path must start a fresh lineage.
        try:
            from hsync_client.sync_state import clear_base, flush

            clear_base(src_path)
            flush()
        except Exception as e:  # pragma: no cover - defensive
            log.debug(f"Could not clear sync-state for {src_path}: {e}")


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

    In WSS mode the master's certificate is pinned on the live connection: the
    certificate actually presented during the handshake is bound to the expected
    SHA-256 fingerprint (TOFU), and the connection is refused on mismatch.

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
    _slave_event_handler = LANSlaveFileEventHandler(host, http_port, use_https)
    observer = Observer()
    watch_path = CONFIG["LOCAL_BUCKET"]

    # Ensure watch_path exists: watchdog raises FileNotFoundError otherwise.
    # This can happen when the bucket has been emptied or removed externally.
    if not Path(watch_path).exists():
        log.warning(f"Watch path '{watch_path}' does not exist, creating it")
        Path(watch_path).mkdir(parents=True, exist_ok=True)

    warn_if_inotify_watch_limit_low(watch_path)
    observer.schedule(_slave_event_handler, watch_path, recursive=True)
    observer.start()
    log.info(f"Client file watcher started for bidirectional sync: {watch_path}")

    # Periodic full resync as a safety net against silently missed
    # file-system events (e.g. inotify queue overflow). The master is the
    # source of truth in LAN mode, so a full sync pulls back any drift.
    resync_task: asyncio.Task[None] | None = None
    if CONFIG.get("RESYNC"):
        resync_task = asyncio.create_task(_lan_periodic_resync())

    # Set up SSL context for WSS. The self-signed master cert is trusted by
    # pinned fingerprint (TOFU), not by chain validation, so we accept any cert
    # at the handshake and verify the fingerprint on the live connection below.
    ssl_context = None
    expected_fingerprint = CONFIG.get("MASTER_CERT_FINGERPRINT")
    if use_https:
        from hsync_client.ssl_utils import create_fingerprint_ssl_context

        ssl_context = create_fingerprint_ssl_context(str(expected_fingerprint or ""))

    try:
        async with websockets.connect(
            uri,
            ssl=ssl_context,
            ping_interval=60,
            ping_timeout=30,
            close_timeout=5,
            max_size=WS_MAX_MESSAGE_BYTES,
        ) as websocket:
            # Pin the master's identity on this live WSS connection: bind the
            # certificate actually presented during the handshake to the pinned
            # fingerprint, refusing on mismatch. This closes the gap where the
            # WebSocket previously trusted any certificate (CERT_NONE).
            if use_https and expected_fingerprint:
                from hsync_client.ssl_utils import verify_peer_cert_fingerprint

                transport = getattr(websocket, "transport", None)
                ssl_object = (
                    transport.get_extra_info("ssl_object") if transport is not None else None
                )
                if not verify_peer_cert_fingerprint(ssl_object, str(expected_fingerprint)):
                    log.error("Master WSS certificate fingerprint mismatch; refusing connection")
                    return

            log.info("WebSocket connected to master")

            # Read server greeting to determine if auth is required
            greeting = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            greeting_data = json.loads(greeting)

            if greeting_data.get("type") == "auth_required":
                lan_key = CONFIG.get("LAN_KEY")
                if not lan_key:
                    log.error("WebSocket authentication failed – server requires LAN key")
                    return
                auth_msg = json.dumps({"type": "auth", "token": lan_key, "cid": CONFIG["CID"]})
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
        # Stop the periodic resync task before tearing down the watcher.
        # CancelledError is a BaseException, so it must be suppressed
        # explicitly: awaiting the task we just cancelled re-raises it.
        if resync_task is not None:
            resync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await resync_task
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


async def _lan_periodic_resync() -> None:
    """Periodic full resync for the LAN slave (safety net for missed events).

    Runs as a background task while connected to the master, performing a
    full sync at the configured ``RESYNC`` interval to recover any
    file-system events dropped by the watcher (e.g. inotify overflow).
    """
    from hsync_client.sync import sync

    try:
        while True:
            await asyncio.sleep(CONFIG["RESYNC"] or 60)
            log.info(f"LAN force resync ({CONFIG['RESYNC']} sec.)")
            await sync(lan_mode=True)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # pragma: no cover - defensive
        log.debug(f"LAN resync task stopped: {e}")


async def _handle_lan_event(
    event_data: dict,
    slave_handler: LANSlaveFileEventHandler | None = None,
) -> None:
    """Apply an event received from the LAN master to the local file system.

    Builds a LAN-specific :class:`EventDispatchContext` (echo suppression
    via ``slave_handler.mark_receiving``/``unmark_receiving``, no symlink
    materialisation, no rename-source recovery) and delegates to the
    unified :func:`apply_inbound_event` dispatcher.

    Args:
        event_data: Event dictionary from the master.
        slave_handler: Optional client event handler for marking files
            as receiving (suppresses the slave's own watchdog echo).
    """
    ctx = EventDispatchContext(
        log_prefix="LAN ",
        mark_receiving=slave_handler.mark_receiving if slave_handler else None,
        unmark_receiving=slave_handler.unmark_receiving if slave_handler else None,
        create_symlink_locally=False,
        recover_on_missing_rename_src=False,
    )
    await apply_inbound_event(event_data, ctx)


# =============================================================================
# Client File Event Handler (for bidirectional sync)
# =============================================================================


class LANSlaveFileEventHandler(FileSystemEventHandler):
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
        return is_partial_file(filepath)

    def _upload_to_master(self, file_obj: File, from_closed: bool = False) -> None:
        """Upload a file to the master server.

        ``from_closed`` is True when the upload was triggered by an ``on_closed``
        (``IN_CLOSE_WRITE``) event, enabling the fast stability re-check that
        cuts the Slave -> Master latency to sub-second.
        """
        from hsync_client.transfer import upload_file

        try:
            log.info(f'{ActionLabel.CLIENT_UP} "{file_obj.filepath}"')
            upload_file(file_obj, check_size=True, from_closed=from_closed)
        except Exception as e:
            log.error(f"Failed to upload to master: {e}")

    def _delete_on_master(self, filepath: str, is_dir: bool = False) -> None:
        """Notify master about a file/directory deletion."""
        from hsync_client.utils import get_ssl_verify, http_delete

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

            log.info(f'{ActionLabel.CLIENT_DEL} "{filepath}"')
            response = http_delete(
                url,
                params=params,
                headers=headers,
                verify=get_ssl_verify(),
                timeout=30,
            )

            if response.status_code not in (200, 204, 404):
                log.warning(f"Delete request failed: {response.status_code}")

        except Exception as e:
            log.error(f"Failed to delete on master: {e}")

    def _rename_on_master(self, src_path: str, dst_path: str) -> None:
        """Notify master about a file/directory rename."""
        from hsync_client.utils import get_ssl_verify, http_patch

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

            log.info(f'{ActionLabel.CLIENT_REN} "{src_path}" -> "{dst_path}"')
            response = http_patch(
                url,
                params=params,
                headers=headers,
                verify=get_ssl_verify(),
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
            log.debug(f'{ActionLabel.CLIENT_DIR} "{src_path}"')
            self._ensure_sentinel(src_path)
        elif not event.is_directory and not is_hidden_file(src_path):
            # Check if it's a symlink
            if Path(src_path).is_symlink():
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
                    size = Path(src_path).stat().st_size
                except OSError:
                    size = 0
                if size > 0:
                    log.debug(
                        f'{ActionLabel.CLIENT_NEW} "{src_path}" ({size} bytes, complete on create)'
                    )
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

        self._handle_file_upload(src_path, from_closed=True)

    def _handle_file_upload(self, src_path: str, from_closed: bool = False) -> None:
        """Handle a file modification - upload to master.

        ``from_closed`` is forwarded to :meth:`_upload_to_master` so an
        ``on_closed`` (``IN_CLOSE_WRITE``) trigger uses the fast re-check.
        """
        # Skip uploads in download-only mode
        if CONFIG.get("SYNC_DIRECTION") == "download-only":
            return
        if not Path(src_path).exists():
            return

        invalidate_sha256_cache(src_path)
        file_obj = File.get(src_path)
        if file_obj:
            # Bytes estimate lets ``wait_pending`` scale its timeout when
            # large files are still being uploaded at WS disconnect time.
            try:
                size_hint = Path(src_path).stat().st_size
            except OSError:
                size_hint = 0
            # Run upload in a separate thread to not block the watcher
            self._start_operation(
                target=self._upload_to_master,
                args=(file_obj, from_closed),
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
        sentinel = str(Path(dir_path) / HSYNCKEEP_FILENAME)
        if not Path(sentinel).exists():
            try:
                open(sentinel, "w").close()  # noqa: SIM115
                log.debug(f'{ActionLabel.CLIENT_KEEP} Created sentinel "{sentinel}"')
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
            log.debug(f"{ActionLabel.CLIENT_IGN} Deletion ignored (ignore-delete mode)")
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
            if not event.is_directory and Path(dest_path).exists():
                invalidate_sha256_cache(dest_path)
                file_obj = File.get(dest_path)
                if file_obj:
                    try:
                        size_hint = Path(dest_path).stat().st_size
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
_slave_event_handler: LANSlaveFileEventHandler | None = None
