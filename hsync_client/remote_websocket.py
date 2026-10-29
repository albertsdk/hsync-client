# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
WebSocket event handling for HSYNC Client.

This module provides WebSocket connection management and event handling
for real-time synchronization with the server.
"""

import asyncio
import sys
from asyncio import (
    AbstractEventLoop,
    CancelledError,
    Task,
    create_task,
    get_running_loop,
    run_coroutine_threadsafe,
    sleep,
    to_thread,
    wait_for,
)
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from functools import partial
from json import JSONDecodeError, dumps, loads
from random import uniform
from ssl import CERT_NONE, Purpose, create_default_context
from typing import Any, cast

from hsync_common.models.event import Event, EventName
from hsync_common.models.file import File
from hsync_common.protocol.handshake import (
    PROTOCOL_VERSION_WS,
    build_ws_auth_request,
    decode_capabilities,
)
from hsync_common.text import safe_json_for_log
from loguru import logger as log
from websockets import ConnectionClosed, connect

from hsync_client.capabilities import adopt_remote_capabilities, local_capabilities
from hsync_client.config import CONFIG, get_api_key
from hsync_client.constants import (
    HSYNC_VERSION,
    RECONNECT_DELAY_MAX,
    WS_MAX_MESSAGE_BYTES,
    ActionLabel,
)
from hsync_client.event_dispatch import EventDispatchContext, apply_inbound_event
from hsync_client.event_queue import EventQueue
from hsync_client.events import UiEventName, emit_event, set_link_status
from hsync_client.exceptions import WatcherStartError
from hsync_client.file_event_handler import BaseFileEventHandler
from hsync_client.net_watch import (
    NetworkChangeMonitor,
    NetworkSignature,
    get_network_monitor,
)
from hsync_client.pools import log_future_error
from hsync_client.presence import record_presence
from hsync_client.sync import log_initial_sync_start, periodic_resync, sync
from hsync_client.transfer import upload_file
from hsync_client.upload_scheduler import PerPathUploadScheduler
from hsync_client.watcher import WatcherManager

# Global resync task reference for cancellation on reconnect
_resync_task: Task[None] | None = None

# Maximum concurrent realtime uploads to the central server. Watchdog
# dispatches every filesystem event on a single thread: before the pool, the
# full upload ran inline on that thread, serializing realtime propagation
# behind the slowest in-flight transfer (including the stability-wait sleep).
# A bounded pool parallelizes uploads while long-lived workers keep their
# per-thread keep-alive HTTP sessions warm.
REALTIME_UPLOAD_MAX_WORKERS = 8

_remote_upload_executor: ThreadPoolExecutor | None = None


def _get_remote_upload_executor() -> ThreadPoolExecutor:
    """Return the process-wide realtime-upload pool (lazy singleton).

    The lazy-singleton machinery is shared by every hsync pool:
    ``hsync_client.pools`` (the reference stays a module attribute so the
    test-suite isolation fixture can reset it).
    """
    from hsync_client.pools import get_singleton_executor

    return get_singleton_executor(
        sys.modules[__name__],
        "_remote_upload_executor",
        max_workers=REALTIME_UPLOAD_MAX_WORKERS,
        thread_name_prefix="hsync-remote-op",
    )


# =============================================================================
# WebSocket Connection Management
# =============================================================================


def _verify_server_wss_fingerprint(websocket: Any, expected_fingerprint: str) -> bool:
    """Pin the server's WSS certificate to *expected_fingerprint* on the live socket.

    The pinned WSS context disables chain validation (trust is the pin), so this
    check is what establishes trust for the event stream. On a mismatch it logs,
    emits a ``fingerprint_changed`` event (scope ``server``) so a GUI can prompt,
    and returns ``False`` — the caller then refuses the connection.
    """
    from hsync_common.ssl_utils import fingerprint_from_der

    from hsync_client.ssl_utils import verify_peer_cert_fingerprint

    transport = getattr(websocket, "transport", None)
    ssl_object = transport.get_extra_info("ssl_object") if transport is not None else None
    if verify_peer_cert_fingerprint(ssl_object, expected_fingerprint):
        return True
    observed = ""
    if ssl_object is not None:
        der = ssl_object.getpeercert(binary_form=True)
        if der:
            observed = fingerprint_from_der(der)
    log.error("Server WSS certificate fingerprint mismatch; refusing connection")
    emit_event(
        UiEventName.fingerprint_changed,
        scope="server",
        fingerprint=observed,
        message="Server WSS certificate fingerprint changed",
    )
    return False


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

    # Configure SSL context for secure WebSocket connections. With a pinned
    # server fingerprint (ADR-0036) the chain is not validated here: trust is the
    # pin, verified on the live connection below.
    ssl_context = None
    expected_fingerprint = CONFIG.get("SERVER_CERT_FINGERPRINT")
    if str(api_base).startswith("wss"):
        if expected_fingerprint:
            from hsync_client.ssl_utils import create_fingerprint_ssl_context

            ssl_context = create_fingerprint_ssl_context(str(expected_fingerprint))
        else:
            ssl_context = create_default_context(Purpose.SERVER_AUTH)
            if CONFIG["NO_VERIFY_SSL"]:
                # Disable SSL verification (insecure mode)
                ssl_context.check_hostname = False
                ssl_context.verify_mode = CERT_NONE
            elif CONFIG.get("CA_CERT"):
                # Load custom CA certificate for self-signed server certs
                ssl_context.load_verify_locations(cafile=CONFIG["CA_CERT"])

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
        # Bind the certificate actually presented during the handshake to the
        # pinned fingerprint, refusing on mismatch (chain validation is disabled
        # on the pinned context by design).
        if expected_fingerprint and not _verify_server_wss_fingerprint(
            websocket, str(expected_fingerprint)
        ):
            return

        log.info(f"WebSocket connected to {uri}")
        set_link_status("connected")

        # Authenticate via first message (more secure than URL token). The frame
        # follows the shared handshake contract (hsync_common.protocol.handshake);
        # the advertised capabilities are the shared runtime-derived set
        # (ADR-0024), identical to the one the P2P handshake announces.
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
        # them as the transfer gates (shared helper, same semantics as the P2P
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

        loop = get_running_loop()
        event_queue = EventQueue(websocket)
        queue_task = create_task(event_queue.process())

        watcher = WatcherManager(CONFIG["LOCAL_BUCKET"])
        event_handler = RemoteFileEventHandler(websocket, loop, event_queue, watcher)
        watcher.start(event_handler)

        # Readiness threshold: the filesystem watcher is live and the
        # WebSocket is authenticated — the node now reacts to events.
        log.info(
            f"{ActionLabel.READY} Client ready for sync — watching "
            f"'{CONFIG['LOCAL_BUCKET']}', server events at {api_base}"
        )

        try:
            # Main event loop - process incoming WebSocket messages
            await _process_remote_events(websocket, bucket)
        except ConnectionClosed as e:
            # WebSocket connection was closed. ``ConnectionClosed.reason`` is
            # deprecated since websockets 13.1; read the received close frame.
            reason = e.rcvd.reason if e.rcvd is not None else ""
            if reason:
                log.warning(f"Connection closed: {reason}")
            else:
                log.warning("Connection closed.")
            set_link_status("waiting")
        except Exception as e:
            # Unexpected error during event processing. The event channel is gone
            # either way, so the status must stop claiming a live link: the
            # reconnect loop retries and re-reports "connected" once the sync
            # succeeds again.
            log.error(f"Exception: {e}")
            emit_event(UiEventName.error, message=f"Exception: {e}")
            set_link_status("waiting")
        finally:
            event_queue.stop()
            queue_task.cancel()
            try:
                await queue_task
            except CancelledError:
                pass  # Expected after cancellation
            except Exception:
                pass  # Task was cancelled
            watcher.stop()


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

        # Presence frame (broadcast on every join/leave): refresh the peers
        # event. Malformed values are dropped as unknown presence.
        if message.get("type") == "presence":
            try:
                connected = int(message.get("connected", -1))
                total = int(message.get("total", -1))
            except (TypeError, ValueError):
                log.debug("Malformed presence frame dropped")
                continue
            if connected < 0 or total < 0:
                log.debug("Malformed presence frame dropped")
                continue
            record_presence(connected, total)
            log.debug(f"Presence update: {connected}/{total} connected")
            continue

        # Validate event type. Symlink events ARE propagated by the server
        # (handle_symlink_event materialises the link and returns the Event,
        # and ConnectionManager.send_message broadcasts it to every peer
        # except the sender): applying them here gives real-time symlink
        # replication between clients in remote mode, matching P2P mode.
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
        # must not kill the connection loop (the P2P client loop isolates
        # per-message failures the same way).
        try:
            await apply_inbound_event(message, REMOTE_DISPATCH_CTX)
        except Exception as e:
            log.error(f"Failed to apply inbound event {message.get('name')!r}: {e}")


# =============================================================================
# Resync and Reconnection
# =============================================================================


async def resync() -> None:
    """Periodic resynchronization task for remote continuous mode."""
    await periodic_resync(p2p_mode=False)


async def _refresh_network_tuning() -> None:
    """Re-measure RTT and re-derive the autotune-managed network keys.

    Only keys listed in ``CONFIG["AUTOTUNE_MANAGED_KEYS"]`` are rewritten, so
    an explicit CLI/env override (``--http-timeout``, ``HSYNC_RECONNECT_DELAY`` …)
    survives the refresh — same guard as the P2P post-discovery refresh
    (``p2p_handler._refresh_p2p_network_tuning``). A failed/unmeasurable probe
    keeps the current values instead of resetting them to the SAFE profile.
    When the passive bandwidth EMA is still empty (startup active probe
    failed), it also re-seeds it from a fresh active probe so the per-cycle
    concurrency refresh has a bandwidth class before the first transfer.
    """
    if not CONFIG.get("AUTOTUNE_ENABLED", False):
        return
    managed = set(CONFIG.get("AUTOTUNE_MANAGED_KEYS", []))
    if not managed:
        return
    try:
        from urllib.parse import urlparse

        import hsync_client.autotune as autotune_mod
        from hsync_client.autotune import (
            classify_network,
            derive_managed_rtt_updates,
            measure_bandwidth_active,
            measure_rtt,
            seed_bw_ema,
        )

        api_url = CONFIG["API"]["buckets"]
        parsed = urlparse(api_url)
        rtt_host = parsed.hostname or "localhost"
        rtt_port = parsed.port or (443 if parsed.scheme == "https" else 80)

        rtt_ms = measure_rtt(rtt_host, rtt_port)
        if rtt_ms is None:
            log.debug("autotune (reconnect): RTT not measurable; keeping current tuning")
            return

        updates = derive_managed_rtt_updates(rtt_ms, managed)
        net_class = classify_network(rtt_ms)
        for key, value in updates.items():
            cast(dict[str, Any], CONFIG)[key] = value
        if updates:
            log.debug(
                f"autotune (reconnect): rtt={rtt_ms:.1f}ms "
                f"net={net_class.value} -> {safe_json_for_log(updates)}"
            )

        # Re-seed bandwidth when the startup active probe failed (server not
        # yet reachable) and no passive sample exists yet: the seed feeds the
        # per-cycle concurrency refresh, the only consumer of the bandwidth
        # class. Offloaded to a worker thread (blocking HTTP, up to ~10 s) and
        # skipped unless concurrency is autotune-managed.
        if "MAX_CONCURRENT_TRANSFERS" in managed and autotune_mod._bw_ema is None:
            server_url = f"{parsed.scheme}://{parsed.netloc}"
            active_mbps = await to_thread(measure_bandwidth_active, server_url)
            if active_mbps:
                seed_bw_ema(active_mbps)
                log.debug(f"autotune (reconnect): bandwidth seed={active_mbps:.1f} Mbps")
    except Exception as e:
        log.debug(f"autotune: reconnect RTT probe failed ({e}); keeping current values")


def _reconnect_delay(base: float, attempts: int) -> float:
    """Full-jitter exponential backoff for reconnect attempts.

    Returns a uniformly random delay in ``[0, min(base * 2**attempts,
    RECONNECT_DELAY_MAX)]``. Repeating full jitter keeps the expected retry
    window short while spreading simultaneous daemons on a shared server, and
    the hard cap bounds the worst case. ``attempts`` counts consecutive
    failures since the last successful connection (0 returns a small random
    sniff so an immediately-available server is not missed).
    """
    cap = min(base * (2**attempts), float(RECONNECT_DELAY_MAX))
    return uniform(0, max(cap, base))


class _ReconnectWaker:
    """Turn a network-change notification into an interruptible backoff sleep.

    The reconnect loop sleeps up to ``RECONNECT_DELAY_MAX`` between attempts, but
    a link that comes back is usually usable long before that window expires: the
    monitor's notification ends the sleep at once. Subscribing in the constructor
    and unsubscribing in :meth:`close` keeps the callback from outliving the loop
    that owns the event.
    """

    def __init__(self, loop: AbstractEventLoop) -> None:
        """Subscribe to the shared network monitor for *loop*."""
        self._loop = loop
        self._event = asyncio.Event()
        self._monitor: NetworkChangeMonitor = get_network_monitor()
        self._monitor.subscribe(self._on_change)

    def _on_change(self, signature: NetworkSignature) -> None:
        """Wake the loop when the link is back (runs on the monitor thread)."""
        if not signature.online:
            return
        try:
            self._loop.call_soon_threadsafe(self._event.set)
        except RuntimeError:
            # The loop is already shutting down: nothing left to wake.
            return

    async def wait(self, delay: float) -> bool:
        """Wait up to *delay* seconds; ``True`` when a network change woke it."""
        # Clear first: a change that arrived while the link was healthy must not
        # cut the next backoff short for no reason.
        self._event.clear()
        try:
            await wait_for(self._event.wait(), timeout=delay)
        except TimeoutError:
            return False
        return True

    def close(self) -> None:
        """Unsubscribe from the monitor (idempotent)."""
        self._monitor.unsubscribe(self._on_change)


async def _reconnect_backoff(
    attempts: int, *, log_msg: str, waker: _ReconnectWaker | None = None
) -> int:
    """Compute, log and wait the next reconnect delay; return the new attempts.

    ``log_msg`` must contain a ``{delay:.1f}`` placeholder for the chosen delay.
    With a *waker*, a network change ends the wait immediately: the link may be
    usable again long before the backoff window expires.
    """
    delay = _reconnect_delay(CONFIG["RECONNECT_DELAY"], attempts)
    attempts += 1
    log.info(log_msg.format(delay=delay))
    if waker is None:
        await sleep(delay)
    elif await waker.wait(delay):
        log.info("Network link is back; retrying the server connection now")
    return attempts


async def remote_connect() -> None:
    """
    Manage WebSocket connection with automatic reconnection.

    Performs initial sync, optionally starts periodic resync,
    then enters continuous event monitoring. Automatically
    reconnects on connection loss after configured delay.
    Cancels previous resync task before creating a new one.
    """
    global _resync_task

    # The backoff below is bounded by RECONNECT_DELAY_MAX, but a link that comes
    # back must be retried at once: the waker ends the sleep on a network change.
    waker = _ReconnectWaker(get_running_loop())

    _attempts = 0
    try:
        while True:
            try:
                # Re-measure RTT before each sync — not only on reconnect — so the
                # first sync after a cold start (startup probes failed because the
                # server was not yet reachable) re-derives timeout/retry/reconnect
                # from a live measurement. No-op when autotune is disabled or the
                # RTT is not measurable.
                await _refresh_network_tuning()

                # Perform initial sync before entering event loop
                log_initial_sync_start()
                await sync()

                # A completed sync proves the server link works: drop the outage's
                # accumulated backoff so the next failure retries promptly instead
                # of waiting out a delay earned by a failure that is already gone.
                _attempts = 0

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

            except WatcherStartError:
                # The local watcher could not start (inotify kernel limits). The
                # parsable [WATCHER ERROR] line and the remediation hint were already
                # logged at the watcher start site; the exponential backoff below is
                # the dedicated retry policy — a kernel limit cannot clear by
                # reconnecting at the normal cadence.
                _attempts = await _reconnect_backoff(
                    _attempts,
                    log_msg="Watcher start failed; reconnecting in {delay:.1f}s…",
                    waker=waker,
                )

            except ConnectionClosed:
                # WebSocket disconnected - wait before reconnecting
                _attempts = await _reconnect_backoff(
                    _attempts,
                    log_msg="WebSocket disconnected; reconnecting in {delay:.1f}s…",
                    waker=waker,
                )

            except Exception as e:
                # Unexpected error - log and retry (transient failures never end
                # the daemon; permanent errors such as auth are already excluded)
                log.error(f"An unexpected error occurred: {e}.")
                _attempts = await _reconnect_backoff(
                    _attempts, log_msg="Reconnecting in {delay:.1f}s…", waker=waker
                )
    finally:
        waker.close()


# =============================================================================
# File System Event Handler
# =============================================================================


class RemoteFileEventHandler(BaseFileEventHandler):
    """Watchdog handler for Remote sync: broadcasts events and uploads files
    to the central server via HTTP.
    """

    def __init__(
        self, websocket, loop, event_queue: EventQueue | None = None, watcher=None
    ) -> None:
        """
        Args:
            websocket: Active WebSocket connection to the server.
            loop: Running asyncio event loop for thread-safe operations.
            event_queue: Optional event queue for reliable message delivery.
            watcher: Optional WatcherManager for dynamic subdirectory watching.
        """
        super().__init__(watcher=watcher)
        self.websocket = websocket
        self.loop = loop
        self.event_queue = event_queue
        # One upload attempt per path: watcher bursts (modify + close + chmod)
        # are coalesced to the latest file state, so a slow delta scan can never
        # race a concurrent full upload for the same file (base_file_changed).
        self._upload_scheduler = PerPathUploadScheduler(
            lambda: _get_remote_upload_executor(),
            # Resolve upload_file at call time (module global), so tests and
            # future instrumentation can patch it.
            lambda file, *, permissions_only, from_closed: upload_file(
                file, permissions_only=permissions_only, from_closed=from_closed
            ),
        )

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
        """Upload the file via HTTP to the central server.

        Scheduled on the bounded realtime pool instead of running inline:
        this handler runs on the single watchdog dispatch thread, and a
        synchronous upload (including the stability-wait sleep) would stall
        the processing of every subsequent filesystem event.
        """
        future = self._upload_scheduler.submit(file, from_closed=from_closed)
        if future is not None:
            future.add_done_callback(partial(log_future_error, label="Realtime upload failed"))

    def _on_permissions_changed(self, file: File, src_path: str) -> None:
        """Propagate a permission-only change (chmod) to the central server.

        The upload carries the same content hash, so the server's same-SHA
        branch applies the new permissions and notifies the other connected
        clients — no bytes are re-stored server-side. ``permissions_only``
        bypasses the upload-status "complete" early-return so the mode change
        actually reaches the server.
        """
        future = self._upload_scheduler.submit(file, permissions_only=True)
        if future is not None:
            future.add_done_callback(partial(log_future_error, label="Realtime upload failed"))

    def _on_metadata_changed(self, file: File, src_path: str) -> None:
        """Propagate a plain ``touch`` to the central server (metadata-only).

        The upload carries the same content hash, so the server's same-SHA
        branch applies the new mtime (and any permission change) without
        re-storing bytes. ``permissions_only`` bypasses the upload-status
        "complete" early-return so the mtime actually reaches the server. The
        touch is self-recorded first so the uploaded causal clock is honest
        (self-record-before-advertise invariant, §3.4.7).
        """
        try:
            if file.sha256sum:
                from hsync_client.sync_state import flush, note_local_edit

                note_local_edit(src_path, file.sha256sum)
                flush()
        except Exception as e:  # pragma: no cover - defensive
            log.debug(f"Could not self-record metadata edit for {src_path}: {e}")
        future = self._upload_scheduler.submit(file, permissions_only=True)
        if future is not None:
            future.add_done_callback(partial(log_future_error, label="Realtime upload failed"))
