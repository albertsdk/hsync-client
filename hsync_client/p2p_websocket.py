# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
WebSocket Server and Client for P2P Mode.

This module provides real-time bidirectional event synchronization between P2P peers.
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
import sys
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
from functools import partial
from pathlib import Path
from time import time
from typing import Any, cast

from aiohttp import web
from hsync_common.endpoint import build_url, format_authority
from hsync_common.models.dir import is_hidden_dir
from hsync_common.models.event import Event, EventName, is_valid_cid
from hsync_common.models.file import (
    File,
    invalidate_sha256_cache,
    is_hidden_file,
    is_partial_file,
)
from hsync_common.models.tombstone import relpath_in_bucket
from hsync_common.protocol.handshake import (
    PROTOCOL_VERSION_P2P,
    Capability,
    build_p2p_auth,
    build_p2p_auth_failed,
    build_p2p_auth_ok,
    build_p2p_auth_required,
    build_p2p_hello,
    decode_capabilities,
)
from hsync_common.protocol.p2p import p2p_ws_events_path
from hsync_common.protocol.peer_download import (
    P2P_WIRE_INT_MAX,
    P2PInventoryAckFrame,
    build_p2p_inventory_ack,
    is_valid_serve_slots,
)
from hsync_common.protocol.rest import (
    REST_DIRS_SUFFIX,
    REST_FILES_PERMISSIONS_SUFFIX,
    REST_FILES_SUFFIX,
    rest_bucket_path,
)
from hsync_common.text import escape_loguru_markup
from loguru import logger as log
from watchdog.events import FileSystemEventHandler

from hsync_client.capabilities import adopt_remote_capabilities, local_capabilities
from hsync_client.config import CONFIG
from hsync_client.constants import (
    HSYNCKEEP_FILENAME,
    P2P_PEER_READ_CAP_DEFAULT,
    STABILITY_DELAY_ON_CLOSED,
    WS_MAX_MESSAGE_BYTES,
    ActionLabel,
)
from hsync_client.event_dispatch import (
    EventDispatchContext,
    apply_inbound_event,
    drain_file_downloads,
)
from hsync_client.events import UiEventName, emit_event
from hsync_client.file_event_handler import (
    MODIFIED_DEBOUNCE_INTERVAL,
    SUPPORTS_ON_CLOSED,
    BaseFileEventHandler,
    classify_stat_change,
)
from hsync_client.p2p_inventory import (
    NOOP_INVENTORY_OBSERVER,
    InventoryObserver,
    InventorySessionLimitError,
    P2PInventoryPublisher,
    P2PInventoryStore,
)
from hsync_client.p2p_sources import P2PSourceResolver
from hsync_client.pools import log_future_error
from hsync_client.utils import (
    cid_suffix,
    ensure_str_path,
    request_headers,
)
from hsync_client.watcher import WatcherManager

# Conservative minimum effective P2P bandwidth used to derive the dynamic
# ``wait_pending`` timeout from the total bytes still in flight (10 MB/s).
MIN_BANDWIDTH_BPS = 10 * 1024 * 1024
# Hard cap on the dynamic ``wait_pending`` timeout to avoid waiting forever
# if a transfer stalls (30 minutes).
MAX_WAIT_PENDING_SECONDS = 30 * 60

# Short window an ``on_created`` upload is deferred so that ``on_closed``
# (``IN_CLOSE_WRITE``) — the reliable "fd closed, content complete" trigger —
# can cancel it and take precedence (it also enables the fast stability
# re-check). Files that arrive already complete with no writer fd (atomic
# rename from outside the watch tree, hard-links, ``mv`` from a sibling
# directory) never receive ``on_closed``, so after this window the deferred
# upload fires as a fallback via the conservative stability poll.
CREATE_UPLOAD_DEFER_S = STABILITY_DELAY_ON_CLOSED


def _inventory_rejection(payload: Mapping[str, object], reason: str) -> P2PInventoryAckFrame:
    """Build a safe rejection ACK when no writable inventory session exists."""
    raw_gen = payload.get("gen")
    raw_seq = payload.get("seq")
    gen = raw_gen if isinstance(raw_gen, int) and not isinstance(raw_gen, bool) else 0
    seq = raw_seq if isinstance(raw_seq, int) and not isinstance(raw_seq, bool) else 0
    return build_p2p_inventory_ack(
        gen=max(0, min(gen, P2P_WIRE_INT_MAX)),
        seq=max(0, min(seq, P2P_WIRE_INT_MAX)),
        status="rejected",
        reason=reason,
    )


# Maximum concurrent realtime operations (uploads to the master, inbound
# event downloads, deletes, renames) executed on behalf of the watcher.
# A bounded pool keeps worker threads (and their per-thread keep-alive HTTP
# sessions) alive, so a burst of events reuses one TCP+TLS connection per
# worker instead of opening one per file, and the parallelism against the
# master stays predictable.
REALTIME_MAX_WORKERS = 8

_realtime_executor: ThreadPoolExecutor | None = None

# One-shot guard for the slave-side ``cid_conflict`` UI event: the slave keeps
# retrying the rejected connection, but the operator must see a single
# notification rather than one per reconnect attempt.
_duplicate_identity_event_sent = False


def _get_realtime_executor() -> ThreadPoolExecutor:
    """Return the process-wide realtime-operation pool (lazy singleton).

    One pool is shared by every ``P2PSlaveFileEventHandler`` instance: only
    one slave handler is active at a time, and keeping the pool process-wide
    means the workers — and their warm keep-alive sessions to the master —
    survive handler recreation (e.g. after a master change). The
    lazy-singleton machinery is shared by every hsync pool:
    ``hsync_client.pools`` (the reference stays a module attribute so the
    test-suite isolation fixture can reset it).
    """
    from hsync_client.pools import get_singleton_executor

    return get_singleton_executor(
        sys.modules[__name__],
        "_realtime_executor",
        max_workers=REALTIME_MAX_WORKERS,
        thread_name_prefix="hsync-lan-op",
    )


# =============================================================================
# Inbound File Origin Tracking (echo suppression for P2P master)
# =============================================================================

# Map of absolute filepath -> (originator_cid, expiration_timestamp, authenticated).
# The master populates this map every time it writes / deletes / renames a
# path on behalf of a peer (via HTTP POST/DELETE/PATCH).  When the watchdog
# fires the resulting filesystem event:
# - ``peek_inbound_origin`` returns the CID regardless of *authenticated*
#   (enables accurate ``REMOTE`` logging even without ``P2P_KEY``).
# - ``consume_inbound_origin`` returns the CID **only** if *authenticated*
#   is True, so a CID that arrived without the shared secret cannot be
#   substituted into the WebSocket broadcast (echo suppression security).
_inbound_origin: dict[str, tuple[str, float, bool]] = {}
_inbound_origin_lock = threading.Lock()
INBOUND_ORIGIN_TTL = 10.0


def mark_inbound_origin(
    filepath: str,
    cid: str,
    ttl: float = INBOUND_ORIGIN_TTL,
    authenticated: bool = True,
) -> None:
    """Record that ``filepath`` was just written/changed on behalf of ``cid``.

    ``authenticated`` controls whether the CID can be used for echo
    suppression (only on authenticated requests).  The CID is always
    stored so that logging can show the correct origin even in the
    absence of a shared secret.
    """
    if not filepath or not cid:
        return
    now = time()
    with _inbound_origin_lock:
        # Opportunistic GC of expired entries
        expired = [k for k, (_, exp, _) in _inbound_origin.items() if exp <= now]
        for k in expired:
            _inbound_origin.pop(k, None)
        _inbound_origin[filepath] = (cid, now + ttl, authenticated)


def consume_inbound_origin(filepath: str) -> str | None:
    """Return and remove the originator CID for ``filepath`` if still fresh.

    The CID is returned **only** when the entry was marked as authenticated
    (i.e. the request carried a valid ``P2P_KEY``).  Unauthenticated CIDs
    are consumed without returning so they cannot be injected into the
    WebSocket broadcast — the master's own CID is used instead.
    """
    if not filepath:
        return None
    with _inbound_origin_lock:
        entry = _inbound_origin.pop(filepath, None)
    if entry is None:
        return None
    cid, expiry, authenticated = entry
    if expiry < time():
        return None
    if not authenticated:
        return None
    return cid


def peek_inbound_origin(filepath: str) -> str | None:
    """Return the originator CID for ``filepath`` without consuming it.

    Unlike :func:`consume_inbound_origin`, this does not remove the entry
    and does **not** require authentication: the CID is returned regardless
    of whether the request was authenticated.  Used by the master's watchdog
    log lines and the ``on_modified`` directory dedup so the ``REMOTE``
    prefix and CID suffix are accurate even without ``P2P_KEY``.
    """
    if not filepath:
        return None
    with _inbound_origin_lock:
        entry = _inbound_origin.get(filepath)
    if entry is None:
        return None
    cid, expiry, _ = entry
    if expiry < time():
        return None
    return cid


# =============================================================================
# Slave CID Registry (shared duplicate-identity guard: WSS + HTTP)
# =============================================================================


class SlaveCidRegistry:
    """Thread-safe registry of the slave CIDs currently admitted by this master.

    Shared by the WSS realtime channel and the HTTP transfer endpoints: the
    WSS server reserves/releases ``(cid, source IP)`` bindings as slaves
    connect and disconnect, while the HTTP write endpoints refuse requests
    whose ``cid`` equals the master's own or is bound to a *different* source
    address. A duplicated device identity — two live hosts presenting the same
    CID — would corrupt the causal vector-clock and tombstone authorship, so
    every admission and write point consults this registry.

    Same-address reconnects are admitted with **replacement** semantics (the
    superseded connection is evicted, mirroring the central server's
    ``_register_client`` behaviour): a slave that reconnects after a network
    blip must not be locked out while its old half-open socket lingers.
    """

    def __init__(self, master_cid: str | None = None) -> None:
        self._master_cid = master_cid
        # cid -> (normalized source IP, websocket that reserved it)
        self._entries: dict[str, tuple[str, Any]] = {}
        self._lock = threading.Lock()
        # Dedup'd (cid, source IP) rejection signatures: mDNS heartbeats and
        # slave retries re-deliver the same conflict, the error is logged once.
        self._reported: set[str] = set()

    @staticmethod
    def normalize_ip(ip: str | None) -> str:
        """Normalize an address for comparison (IPv4-mapped IPv6 → dotted quad)."""
        if not ip:
            return ""
        if ip.startswith("::ffff:") and "." in ip:
            return ip.removeprefix("::ffff:")
        return ip

    def try_reserve(self, cid: str, peer_ip: str | None, websocket: Any) -> tuple[bool, Any | None]:
        """Atomically reserve *cid* for *websocket* arriving from *peer_ip*.

        Returns ``(allowed, stale_websocket)``. On success *stale_websocket* is
        the superseded connection of a same-address reconnect (the caller must
        close it) or ``None``. A claim from a different address for an
        already-bound CID is a duplicated device identity and is refused.
        """
        if self._master_cid and cid == self._master_cid:
            return False, None
        normalized = self.normalize_ip(peer_ip)
        with self._lock:
            bound = self._entries.get(cid)
            if bound is not None and bound[0] != normalized:
                return False, None
            stale = bound[1] if bound is not None else None
            self._entries[cid] = (normalized, websocket)
            return True, stale

    def release(self, cid: str, websocket: Any) -> bool:
        """Drop the *cid* binding, but only if it still belongs to *websocket*.

        Idempotent and safe to call from any teardown path: after a
        same-address replacement the superseded connection's release is a
        no-op, so it can never evict the newer binding.

        Returns:
            ``True`` when this *websocket* was still the current owner and the
            binding was removed, ``False`` otherwise (it was already replaced
            by a same-address reconnect). The caller uses this to decide
            whether to emit a liveness "disconnected" notification, so a
            superseded connection never clears a still-live CID's mark.
        """
        with self._lock:
            bound = self._entries.get(cid)
            if bound is not None and bound[1] is websocket:
                del self._entries[cid]
                return True
            return False

    def request_conflict(self, cid: str, client_ip: str | None) -> bool:
        """Return True when an HTTP write from *client_ip* claiming *cid* is a
        duplicated device identity (the master's own CID, or a CID bound to a
        different source address)."""
        if not cid:
            return False
        if self._master_cid and cid == self._master_cid:
            return True
        normalized = self.normalize_ip(client_ip)
        with self._lock:
            bound = self._entries.get(cid)
            return bound is not None and bound[0] != normalized

    def should_report(self, cid: str, peer_ip: str | None) -> bool:
        """Return True once per ``(cid, source IP)`` duplicate-identity report."""
        signature = f"{cid}@{self.normalize_ip(peer_ip)}"
        with self._lock:
            if signature in self._reported:
                return False
            self._reported.add(signature)
            return True

    def clear(self) -> None:
        """Drop every binding (called when the WSS server stops)."""
        with self._lock:
            self._entries.clear()

    def connected_cids(self) -> set[str]:
        """Snapshot of the currently-bound CIDs (for teardown notifications)."""
        with self._lock:
            return set(self._entries)


# =============================================================================
# WebSocket Server
# =============================================================================


class P2PWebSocketServer:
    """
    WebSocket server for P2P master mode.

    Broadcasts file system events to connected clients through the shared
    listener's event loop (see :meth:`set_loop`).
    """

    def __init__(
        self,
        port: int,
        master_cid: str | None = None,
        cid_registry: SlaveCidRegistry | None = None,
        inventory_store: P2PInventoryStore | None = None,
    ) -> None:
        """
        Initialize the WebSocket server.

        Args:
            port: The master's single HTTPS/WSS port (this control plane shares
                the :class:`P2PServer` listener; it opens no socket of its own).
            master_cid: This master's own CID. When set, a slave that declares
                this CID (a duplicated device identity) is refused instead of
                being admitted as a peer.
            cid_registry: Shared :class:`SlaveCidRegistry` (also consulted by
                the HTTP write endpoints). A private one is created when
                omitted, so standalone/test usage keeps working.
        """
        self._port = port
        self._cid_registry = (
            cid_registry if cid_registry is not None else SlaveCidRegistry(master_cid)
        )
        self._inventory_store = inventory_store
        self._clients: set[Any] = set()  # WebSocket connections
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        # Role gate for event pushes: a yielded master stops publishing the
        # instant its role is retired, before its clients are closed, so no
        # event can slip out during the yield window (ADR-0027). The epoch is
        # bumped on every gate close so a broadcast or handshake already in
        # flight observes the retirement.
        self._publishing = False
        self._publish_epoch = 0
        self._watcher: WatcherManager | None = None
        self._event_handler: P2PMasterFileEventHandler | None = None
        self._clients_lock = threading.Lock()
        self._on_clients_changed: Callable[[int], None] | None = None
        self._on_peer_connection: Callable[[str, bool], None] | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Adopt the unified listener's event loop.

        The control plane runs as a route on the shared aiohttp listener; it
        publishes events through the listener's loop (``broadcast_sync`` from the
        watcher thread).
        """
        self._loop = loop

    def set_port(self, port: int) -> None:
        """Record the unified listener's actual bound port (``port=0`` resolved)."""
        self._port = port

    def set_inventory_store(self, store: P2PInventoryStore | None) -> None:
        """Install the master role's inventory store (or clear it on yield).

        The control plane is bound once for the process lifetime; a role change
        installs the store used to acknowledge slave inventory frames.
        """
        self._inventory_store = store

    @property
    def port(self) -> int:
        """The unified listener's port (HTTP and WebSocket share it)."""
        return self._port

    @property
    def running(self) -> bool:
        """Whether the master realtime channel (watcher) is active."""
        return self._running

    @property
    def connected_clients(self) -> int:
        """Number of WebSocket clients currently connected."""
        with self._clients_lock:
            return len(self._clients)

    def set_clients_changed_callback(self, callback: Callable[[int], None] | None) -> None:
        """Install (or clear) the callback invoked with the new client count.

        The callback fires from the asyncio loop thread on every connect and
        disconnect; it must be non-blocking (the master re-registers its mDNS
        record with the updated ``connected`` count).
        """
        self._on_clients_changed = callback

    def set_peer_connection_callback(self, callback: Callable[[str, bool], None] | None) -> None:
        """Install (or clear) the callback invoked as ``(cid, connected)``.

        Fires from the asyncio loop thread on every slave connect/disconnect.
        ``connected`` is ``True`` after a slave CID is admitted and ``False``
        after it disconnects. The P2P handler uses it to feed the
        :class:`~hsync_client.p2p_peers.PeerManager`'s WebSocket-authoritative
        liveness signal, so an mDNS-quiescent but WSS-connected slave is never
        falsely expired.
        """
        self._on_peer_connection = callback

    def _notify_peer_connection(self, cid: str, connected: bool) -> None:
        """Invoke the per-CID connect/disconnect callback, if installed."""
        callback = self._on_peer_connection
        if callback is None:
            return
        callback(cid, connected)

    def _notify_clients_changed(self) -> None:
        """Invoke the count-change callback with the current client count."""
        callback = self._on_clients_changed
        if callback is None:
            return
        try:
            callback(self.connected_clients)
        except Exception:
            log.exception("Connected-clients callback failed")

    @staticmethod
    def _peer_ip(client_addr) -> str:
        """Extract the peer IP from a websocket remote address."""
        return client_addr[0] if isinstance(client_addr, tuple) else str(client_addr)

    @staticmethod
    def _resolve_serve_slots(
        value: object, capabilities: set[Capability]
    ) -> tuple[int | None, str | None]:
        """Validate a slave's capacity advertisement against its capability claim.

        Returns ``(serve_slots, rejection_reason)``. A node that serves peer
        downloads advertises a bounded capacity **and** the ``peer_download``
        capability (it is a source candidate); a node that does not serve
        advertises nothing and gets no publisher session at all.
        """
        if value is None:
            return None, None
        if not is_valid_serve_slots(value):
            return None, "invalid_serve_slots"
        if Capability.peer_download not in capabilities:
            return None, "serve_slots_without_peer_download"
        # The guard above is the proof: ``is_valid_serve_slots`` accepts only a
        # bounded int (bool included in its rejection set). ``cast`` states that
        # for the type checker, which does not propagate the TypeGuard from the
        # imported hsync-common module.
        return cast(int, value), None

    async def _admit_slave(
        self,
        client_cid: str,
        client_addr,
        websocket,
        *,
        authenticated: bool,
        serve_slots: int | None,
    ) -> tuple[bool, str | None, str | None]:
        """Reserve the slave's CID and admit the connection, or reject it.

        A duplicated device identity — the master's own CID, or a CID already
        bound to a *different* source address — is refused (with an
        ``auth_failed`` frame on the authenticated path) and the connection is
        closed. A same-address reconnect replaces the superseded binding and
        the stale connection is closed, mirroring the central server's
        ``_register_client`` eviction.

        ``serve_slots`` is the capacity the slave advertised: a publisher session
        exists exactly when it is present, so a source candidate can never be
        built from a session with no live capacity advertisement (ADR-0030).

        Returns the admission result, inventory owner token, and any reason
        inventory publication is unavailable. On success the CID is already
        reserved before a superseded same-address socket is closed.
        """
        peer_ip = self._peer_ip(client_addr)
        allowed, stale_ws = self._cid_registry.try_reserve(client_cid, peer_ip, websocket)
        if not allowed:
            if self._cid_registry.should_report(client_cid, peer_ip):
                self._log_duplicate_identity(client_addr, client_cid)
            if authenticated:
                with contextlib.suppress(Exception):
                    await websocket.send_str(
                        json.dumps(build_p2p_auth_failed(reason="cid_conflict"))
                    )
            await websocket.close()
            return False, None, None

        inventory_token: str | None = None
        inventory_rejection: str | None = None
        if serve_slots is not None and self._inventory_store is not None:
            try:
                # Install the replacement owner before closing a superseded
                # same-address WSS. Its later teardown is then an old-owner no-op.
                # Off the event loop: the store is SQLite behind a lock shared
                # with the HTTP worker pool.
                inventory_token = await asyncio.to_thread(
                    self._inventory_store.begin_session,
                    client_cid,
                    serve_slots=serve_slots,
                    remote_address=peer_ip,
                )
            except InventorySessionLimitError:
                inventory_rejection = "quota"
            except Exception:
                inventory_rejection = "storage"
                log.exception(f"Could not start P2P inventory session for {client_cid}")
        else:
            inventory_rejection = "unsupported"
        if stale_ws is not None and stale_ws is not websocket:
            log.info(
                f"Slave {client_cid} reconnected from {peer_ip}: closing superseded connection"
            )
            with contextlib.suppress(Exception):
                await stale_ws.close(code=4000, message=b"Replaced by a newer connection")
        return True, inventory_token, inventory_rejection

    def _log_duplicate_identity(self, client_addr, client_cid: str) -> None:
        """Log a duplicated-device-identity rejection (shared by both paths)."""
        log.error(
            f"{ActionLabel.SECURITY} Duplicate device identity: slave at "
            f"{client_addr} presents CID {client_cid} which is already in use "
            f"(this master or another connected slave). Refusing the connection."
        )
        emit_event(
            UiEventName.duplicate_identity,
            peer_cid=client_cid,
            peer_address=self._peer_ip(client_addr),
            message=(
                "Duplicate device identity: a peer presents a CID already in use. "
                "Each machine must use its own HSYNC_DEVICE_DIR."
            ),
        )

    async def _release_failed_admission(
        self, client_cid: str, websocket: object, inventory_token: str | None
    ) -> None:
        """Release ownership when handshake setup fails after CID reservation."""
        if client_cid != "unknown":
            self._cid_registry.release(client_cid, websocket)
        if inventory_token is not None and self._inventory_store is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    self._inventory_store.end_session, client_cid, inventory_token
                )

    async def _recv_text(self, websocket: web.WebSocketResponse, timeout: float) -> str:
        """Await one text frame, or raise on timeout/close/non-text."""
        msg = await asyncio.wait_for(websocket.receive(), timeout=timeout)
        if msg.type is not web.WSMsgType.TEXT:
            raise ConnectionError(f"expected a text frame, got {msg.type!r}")
        return msg.data

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        """aiohttp route handler for one master WebSocket connection.

        Greeting/auth/hello handshake and the inventory loop, served on the
        single HTTPS/WSS listener alongside the REST data plane.
        """
        websocket = web.WebSocketResponse(heartbeat=30.0, max_msg_size=WS_MAX_MESSAGE_BYTES)
        await websocket.prepare(request)
        # Capture the publish epoch: a yield landing during the (awaited)
        # handshake retires the role, so the socket must not be registered.
        publish_epoch = self._publish_epoch
        client_addr = request.remote or ""
        client_cid = "unknown"
        inventory_token: str | None = None
        inventory_rejection: str | None = "unsupported"
        slave_caps: set[Capability] = set()
        log.info(f"WebSocket client connected (pre-auth): {client_addr}")

        # Send greeting: tell the client whether authentication is required.
        # Both paths carry the P2P protocol version and this master's
        # capabilities (ADR-0024): the authenticated path's reply is the
        # capability source of truth; the hello path is at least consistent.
        master_caps = local_capabilities()
        p2p_key = CONFIG.get("P2P_KEY")
        if p2p_key:
            await websocket.send_str(json.dumps(build_p2p_auth_required(capabilities=master_caps)))
            try:
                # Wait for auth message
                auth_msg = await self._recv_text(websocket, 10.0)
                auth_data = json.loads(auth_msg)
                token = auth_data.get("token")
                declared_cid = auth_data.get("cid")
                if not isinstance(declared_cid, str) or not is_valid_cid(declared_cid):
                    log.warning(
                        f"WebSocket auth rejected – invalid CID from {client_addr}: "
                        f"{declared_cid!r}"
                    )
                    await websocket.send_str(
                        json.dumps(build_p2p_auth_failed(reason="invalid_cid"))
                    )
                    await websocket.close()
                    return websocket
                client_cid = declared_cid
                # Protocol is a hard gate inside the auth contract: a slave
                # speaking a different P2P protocol must never authenticate.
                if auth_data.get("protocol_version") != PROTOCOL_VERSION_P2P:
                    log.warning(
                        f"WebSocket auth rejected – incompatible P2P protocol from "
                        f"{client_addr} (CID: {client_cid}): "
                        f"{auth_data.get('protocol_version')!r}"
                    )
                    await websocket.send_str(
                        json.dumps(build_p2p_auth_failed(reason="protocol_incompatible"))
                    )
                    await websocket.close()
                    return websocket
                if auth_data.get("type") != "auth" or not (
                    isinstance(token, str) and hmac.compare_digest(token, str(p2p_key))
                ):
                    log.warning(f"WebSocket auth failed from {client_addr} (CID: {client_cid})")
                    await websocket.send_str(
                        json.dumps(build_p2p_auth_failed(reason="invalid_token"))
                    )
                    await websocket.close()
                    return websocket
                # Reserve the slave's CID before admitting the connection: a
                # duplicated device identity (the master's own CID, or a CID
                # bound to a different host) must never reach the causal model.
                slave_caps = decode_capabilities(auth_data.get("capabilities"))
                serve_slots, slot_rejection = self._resolve_serve_slots(
                    auth_data.get("serve_slots"), slave_caps
                )
                if slot_rejection is not None:
                    log.warning(
                        f"WebSocket auth rejected – invalid capacity advertisement from "
                        f"{client_addr} (CID: {client_cid}): {slot_rejection}"
                    )
                    await websocket.send_str(
                        json.dumps(build_p2p_auth_failed(reason=slot_rejection))
                    )
                    await websocket.close()
                    return websocket
                admitted, inventory_token, inventory_rejection = await self._admit_slave(
                    client_cid,
                    client_addr,
                    websocket,
                    authenticated=True,
                    serve_slots=serve_slots,
                )
                if not admitted:
                    return websocket
                log.debug(
                    f"WebSocket slave {client_cid} capabilities: "
                    f"{sorted(c.value for c in slave_caps) or 'none'}"
                )
                await websocket.send_str(json.dumps(build_p2p_auth_ok(capabilities=master_caps)))
                log.info(f"WebSocket client authenticated: {client_addr} (CID: {client_cid})")
            except TimeoutError:
                await self._release_failed_admission(client_cid, websocket, inventory_token)
                log.warning(f"WebSocket auth timeout from {client_addr} (CID: {client_cid})")
                await websocket.send_str(json.dumps(build_p2p_auth_failed(reason="timeout")))
                await websocket.close()
                return websocket
            except Exception as e:
                # The CID may already be reserved at this point (a failure after
                # a successful admission, e.g. the auth-ok send): release it so
                # a retrying same-address slave is not locked out. Idempotent.
                await self._release_failed_admission(client_cid, websocket, inventory_token)
                log.warning(f"WebSocket auth error from {client_addr} (CID: {client_cid}): {e}")
                await websocket.close()
                return websocket
        else:
            # No shared secret: request the client's CID via a lightweight
            # hello handshake so the operator can distinguish peers in logs.
            # The CID is NOT authenticated (no P2P_KEY), so every log line
            # carries an explicit "(unauthenticated)" qualifier.
            await websocket.send_str(json.dumps(build_p2p_hello(capabilities=master_caps)))
            try:
                hello_msg = await self._recv_text(websocket, 5.0)
                hello_data = json.loads(hello_msg)
                declared_cid = hello_data.get("cid")
                if (
                    hello_data.get("type") == "hello"
                    and hello_data.get("protocol_version") == PROTOCOL_VERSION_P2P
                    and isinstance(declared_cid, str)
                    and is_valid_cid(declared_cid)
                ):
                    client_cid = declared_cid
                    # Reserve the slave's CID before admitting the connection
                    # (no failed frame exists on the unauthenticated hello
                    # path; the slave observes the close).
                    slave_caps = decode_capabilities(hello_data.get("capabilities"))
                    serve_slots, slot_rejection = self._resolve_serve_slots(
                        hello_data.get("serve_slots"), slave_caps
                    )
                    if slot_rejection is not None:
                        log.warning(f"WebSocket hello invalid from {client_addr}: {slot_rejection}")
                        await websocket.close()
                        return websocket
                    admitted, inventory_token, inventory_rejection = await self._admit_slave(
                        client_cid,
                        client_addr,
                        websocket,
                        authenticated=False,
                        serve_slots=serve_slots,
                    )
                    if not admitted:
                        return websocket
                    log.info(
                        f"WebSocket client connected (no auth): {client_addr} "
                        f"(CID: {client_cid}, unauthenticated, capabilities: "
                        f"{sorted(c.value for c in slave_caps) or 'none'})"
                    )
                else:
                    log.warning(
                        f"WebSocket hello invalid from {client_addr}: "
                        f"type={hello_data.get('type')!r} cid={declared_cid!r} "
                        f"protocol={hello_data.get('protocol_version')!r}"
                    )
                    await websocket.close()
                    return websocket
            except TimeoutError:
                await self._release_failed_admission(client_cid, websocket, inventory_token)
                log.warning(f"WebSocket hello timeout from {client_addr}")
                await websocket.close()
                return websocket
            except Exception as e:
                await self._release_failed_admission(client_cid, websocket, inventory_token)
                log.warning(f"WebSocket hello error from {client_addr}: {e}")
                await websocket.close()
                return websocket

        # A yield that raced this handshake must not admit the client: the epoch
        # moves on every role retirement (the gate close), and the socket is only
        # registered after the awaited handshake.
        if publish_epoch != self._publish_epoch:
            await self._release_failed_admission(client_cid, websocket, inventory_token)
            await websocket.close()
            return websocket

        # Add client to set (its CID is already reserved by _admit_slave)
        with self._clients_lock:
            self._clients.add(websocket)
        self._notify_clients_changed()
        if client_cid != "unknown":
            self._notify_peer_connection(client_cid, True)

        try:
            # Keep connection alive and handle incoming messages
            async for msg in websocket:
                if msg.type is web.WSMsgType.TEXT:
                    message: Any = msg.data
                elif msg.type is web.WSMsgType.BINARY:
                    message = msg.data
                else:
                    continue
                try:
                    payload = json.loads(message)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    log.warning(f"Invalid JSON from slave {client_cid}")
                    continue
                if isinstance(payload, Mapping) and payload.get("type") == "inventory":
                    if inventory_token is not None and self._inventory_store is not None:
                        # The inventory store is SQLite behind a re-entrant lock
                        # also taken by the HTTP worker pool; running it inline
                        # would stall this shared loop (and every HTTP worker
                        # bridged through it). Keep per-connection ordering by
                        # awaiting the result before the ACK is sent.
                        ack = await asyncio.to_thread(
                            self._inventory_store.apply_frame,
                            client_cid,
                            inventory_token,
                            payload,
                        )
                    else:
                        ack = _inventory_rejection(payload, inventory_rejection or "unsupported")
                    await websocket.send_str(json.dumps(ack, separators=(",", ":")))
                    continue
                log.debug(
                    f"Received from {client_addr} (CID: {client_cid}): "
                    f"{escape_loguru_markup(str(message))}"
                )
        except Exception as e:
            log.debug(f"WebSocket client {client_addr} (CID: {client_cid}) disconnected: {e}")
        finally:
            with self._clients_lock:
                self._clients.discard(websocket)
            if inventory_token is not None and self._inventory_store is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(
                        self._inventory_store.end_session, client_cid, inventory_token
                    )
            if client_cid != "unknown":
                # Only the current owner of the binding may clear the liveness
                # mark: a superseded connection (same-address reconnect) must
                # not emit "disconnected" for a CID the replacement still holds.
                if self._cid_registry.release(client_cid, websocket):
                    self._notify_peer_connection(client_cid, False)
            self._notify_clients_changed()
            log.info(f"WebSocket client disconnected: {client_addr} (CID: {client_cid})")
        return websocket

    async def broadcast(self, event_dict: dict) -> None:
        """Broadcast an event to all connected clients."""
        epoch = self._publish_epoch
        if not self._publishing or not self._clients:
            return

        message = json.dumps(event_dict)
        origin_cid = event_dict.get("cid")
        origin_info = f" (origin: {origin_cid})" if origin_cid else ""
        log.debug(
            f"Broadcasting to {len(self._clients)} clients: {event_dict.get('name')}{origin_info}"
        )

        # Send to all clients, removing disconnected ones. The epoch is
        # re-checked before every send, so a retirement that lands mid-broadcast
        # stops the remaining pushes instead of delivering to a retired role.
        disconnected = set()
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            if epoch != self._publish_epoch:
                log.debug("Serving role retired mid-broadcast; stopping event push")
                return
            try:
                await client.send_str(message)
            except Exception:
                disconnected.add(client)

        if disconnected:
            with self._clients_lock:
                self._clients.difference_update(disconnected)

    def broadcast_sync(self, event_dict: dict) -> None:
        """Broadcast an event synchronously (from file watcher thread)."""
        if self._loop and self._running and self._publishing:
            asyncio.run_coroutine_threadsafe(self.broadcast(event_dict), self._loop)

    def set_publishing(self, enabled: bool) -> None:
        """Gate event pushes on the current role.

        Called by the listener when the serving role is installed or retired: a
        yielded master must stop pushing events *before* its clients are closed,
        so nothing is delivered during the yield window (ADR-0027). Closing the
        gate bumps the epoch, which an in-flight broadcast or handshake checks.
        """
        if not enabled:
            self._publish_epoch += 1
        self._publishing = enabled

    def start(self, watch_path: str | None = None) -> None:
        """Start the master file watcher (the listener is the unified server).

        The WebSocket control plane opens no socket or thread of its own: the
        :class:`P2PServer` listener routes the canonical WebSocket path here,
        and this method only starts the filesystem watcher that broadcasts
        through the shared loop. ``set_loop`` must have been called first.
        """
        if self._running:
            log.warning("P2P WebSocket server already running")
            return
        if self._loop is None:
            raise RuntimeError("P2PWebSocketServer.set_loop() must be called before start()")

        self._running = True
        self._publishing = True

        if watch_path is None:
            watch_path = CONFIG["LOCAL_BUCKET"]

        # Ensure watch_path exists: watchdog raises FileNotFoundError otherwise.
        # This can happen when the bucket has been emptied or removed externally.
        if not Path(watch_path).exists():
            log.warning(f"Watch path '{watch_path}' does not exist, creating it")
            Path(watch_path).mkdir(parents=True, exist_ok=True)

        watcher = WatcherManager(watch_path)
        self._event_handler = P2PMasterFileEventHandler(self, watcher)
        watcher.start(self._event_handler)
        self._watcher = watcher
        log.info(f"File watcher started for: {watch_path}")

    def close_clients(self) -> Future[None] | None:
        """Close every live control-plane client (role yield).

        Scheduled on the shared listener loop; idempotent and safe when the
        control plane never served. The client set is validated/iterated on the
        loop (where it is mutated), never read cross-thread here. Returns the
        completion future so a caller can wait for the clients to be gone
        (``None`` when the listener loop is not attached).
        """
        if self._loop is None:
            return None
        return asyncio.run_coroutine_threadsafe(self._close_all_clients(), self._loop)

    async def _close_all_clients(self) -> None:
        """Close all control-plane clients on the listener loop."""
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            with contextlib.suppress(Exception):
                await client.close()

    def stop(self) -> None:
        """Stop the master file watcher and drop every connected client."""
        if not self._running:
            return

        self._running = False
        self._publishing = False

        if self._watcher:
            self._watcher.stop()
            self._watcher = None

        # Close all client connections on the shared listener loop
        self.close_clients()

        # Every admitted slave is gone: emit a disconnect for each so the
        # PeerManager clears its WebSocket liveness marks (the handlers' own
        # ``release`` is a no-op after ``clear()`` below, so it can no longer
        # drive the notification), then drop the duplicate-identity bindings so
        # a later re-admission (role regained) starts from a clean registry.
        for cid in self._cid_registry.connected_cids():
            self._notify_peer_connection(cid, False)
        self._cid_registry.clear()

        log.info("P2P WebSocket server stopped")


# =============================================================================
# File Event Handler for P2P Master
# =============================================================================


def _record_deletion_marker(src_path: str) -> None:
    """Record a causal deletion marker and drop the path's merge base.

    Shared chokepoint for deletions, used by the master's ``_on_deleted`` and
    by the slave's watcher. The marker must survive locally even when the
    transport is unavailable: the P2P master can be offline while a slave
    deletes, and a slave can be unreachable while the master records. The
    marker inherits the stored version clock and the recorded content hash
    (``base_hash``), and is ticked by this peer BEFORE the merge-base entry is
    cleared, so both delete-vs-recreate and delete-vs-delete resolve causally
    (ADR-0003). The content hash lets a remote copy of the deleted file be
    recognised and removed instead of being re-downloaded (resurrection guard).
    Best-effort: a failure must never break event propagation.
    """
    try:
        from hsync_client.sync_state import clear_base, flush, get_base_hash, get_vclock
        from hsync_client.tombstones import record_deletion

        # Causal provenance (ADR-0003): the marker inherits the path's recorded
        # clock and content hash and is ticked by this peer BEFORE the
        # merge-base entry is cleared below.
        record_deletion(
            src_path,
            sha256sum=get_base_hash(src_path),
            origin_cid=str(CONFIG.get("CID") or "") or None,
            prev_vclock=get_vclock(src_path),
        )
        clear_base(src_path)
        flush()
    except Exception as e:  # pragma: no cover - defensive
        log.debug(f"Could not record tombstone for {src_path}: {e}")


def _attach_effective_version(entity, abs_path: str) -> None:
    """Attach the effective causal version to a File entity (in place).

    Only File entities carry a ``version`` field; Dir/Symlink are skipped. The
    version is resolved via ``sync_state.local_version_for_compare`` (pure: no
    store mutation) so the broadcast advertises an honest clock (ADR-0022).
    """
    from hsync_common.models.file import File as _File

    if not isinstance(entity, _File):
        return
    try:
        from hsync_client.sync_state import local_version_for_compare

        eff = local_version_for_compare(abs_path, entity.sha256sum)
        if eff is not None:
            entity.version = {
                "vclock": eff["vclock"],
                "version_id": eff.get("version_id", ""),
                "origin_cid": eff.get("origin_cid"),
                "ts": eff.get("ts", 0),
                "corrected_mtime": eff.get("corrected_mtime", 0),
            }
    except Exception as e:  # pragma: no cover - bookkeeping must never break broadcast
        log.debug(f"Could not attach version to broadcast for {abs_path}: {e}")


class P2PMasterFileEventHandler(BaseFileEventHandler):
    """Watchdog handler for P2P master: broadcasts file system events to all
    connected slaves via WebSocket. File contents are pulled by slaves, so
    ``_on_file_ready`` emits a ``modified`` event instead of uploading bytes.
    """

    def __init__(self, ws_server: P2PWebSocketServer, watcher=None) -> None:
        """
        Args:
            ws_server: WebSocket server for broadcasting events.
            watcher: Optional WatcherManager for dynamic subdirectory watching.
        """
        super().__init__(watcher=watcher)
        self._ws_server = ws_server

    def _origin_cid(self, src_path: str | None = None, dst_path: str | None = None) -> str | None:
        """Return the originator CID for a filesystem event, if known.

        Peeks the inbound-origin map (without consuming) so the log line can
        attribute the event to the peer that triggered it via the HTTP API.
        The CID is *not* consumed here: the subsequent ``_broadcast_event``
        still needs it for echo suppression.
        """
        if src_path:
            cid = peek_inbound_origin(src_path)
            if cid:
                return cid
        if dst_path:
            return peek_inbound_origin(dst_path)
        return None

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
        recently written/deleted/renamed on behalf of a peer via the P2P
        HTTP API, the originator's CID is substituted into the broadcast
        so the sender can recognise and discard its own echo.
        """
        if CONFIG.get("SYNC_DIRECTION") == "download-only":
            log.debug("Download-only mode: master event not broadcast.")
            return
        # Attach the effective causal version to File entities so slaves can
        # run the causal matrix on realtime events (ADR-0022). The src/dst
        # paths are still absolute here (Event.to_dict normalizes them after).
        if src_abs_path:
            _attach_effective_version(event.src, src_abs_path)
        if event.dst and dst_abs_path:
            _attach_effective_version(event.dst, dst_abs_path)
        event_dict = event.to_dict()
        origin_cid = None
        origin_source = "none"
        if src_abs_path:
            origin_cid = consume_inbound_origin(src_abs_path)
            if origin_cid:
                origin_source = "src"
        if origin_cid is None and dst_abs_path:
            origin_cid = consume_inbound_origin(dst_abs_path)
            if origin_cid:
                origin_source = "dst"
        if origin_cid:
            event_dict["cid"] = origin_cid
        # Diagnostic: a "master-fallback" cid means the originator will NOT
        # recognise its own echo (echo suppression misses). Tracked to confirm
        # whether the missed mark is a child of an rmtree'd directory or an
        # already-deleted early-return path.
        log.debug(
            f"[ECHO-SUPPR] name={event_dict.get('name')} "
            f"path={src_abs_path or dst_abs_path} "
            f"cid={'substituted:' + origin_cid if origin_cid else 'master-fallback'} "
            f"via={origin_source}"
        )
        self._ws_server.broadcast_sync(event_dict)

    def _on_permissions_changed(self, file: File, src_path: str) -> None:
        """Broadcast a ``modified`` event so slaves converge the new mode.

        The payload carries the current content hash (unchanged) and the new
        permissions: a slave whose copy matches by hash applies only the
        permissions locally (see ``update_file``), without re-downloading
        bytes.
        """
        try:
            if peek_inbound_origin(src_path) is None and file.sha256sum:
                from hsync_client.sync_state import flush, note_local_edit

                note_local_edit(src_path, file.sha256sum)
                flush()
        except Exception as e:  # pragma: no cover - defensive
            log.debug(f"Could not self-record local edit for {src_path}: {e}")
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

    def _on_metadata_changed(self, file: File, src_path: str) -> None:
        """Broadcast a ``modified`` event so slaves converge the new mtime.

        Content and mode are unchanged (a plain ``touch``); the slave applies
        the advertised corrected mtime locally without re-downloading bytes
        (see ``update_file``).
        """
        try:
            if peek_inbound_origin(src_path) is None and file.sha256sum:
                from hsync_client.sync_state import flush, note_local_edit

                note_local_edit(src_path, file.sha256sum)
                flush()
        except Exception as e:  # pragma: no cover - defensive
            log.debug(f"Could not self-record metadata edit for {src_path}: {e}")
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

    def _on_file_ready(
        self,
        file: File,
        src_path: str,
        from_closed: bool = False,
        *,
        trigger: str | None = None,
    ) -> None:
        """Broadcast a ``modified`` event so slaves can pull the new bytes.

        The master only propagates metadata (slaves pull the bytes), so
        ``from_closed`` carries no upload semantics here and is accepted purely
        to honour the :class:`BaseFileEventHandler` hook contract. ``trigger``
        overrides the log trigger label (e.g. ``"safe-save"``).
        """
        # Distinguish a genuinely new file (first time this watcher sees it)
        # from an update to an already-known path.  The broadcast is always
        # ``EventName.modified`` (slaves must pull the bytes either way);
        # only the log label changes so the operator sees the right verb.
        label = "CREATE" if src_path not in self._last_broadcast_stat else "MODIFY"
        actual_trigger = trigger or ("closed" if from_closed else "modified")
        log.info(
            f'{self._label(label, src_path)} "{src_path}"'
            f" ({actual_trigger}){self._cid_suffix(src_path)}"
        )
        # A (re-)created/modified file invalidates any prior deletion marker so
        # peers do not delete the freshly written file on their next full sync.
        # Best-effort: tombstone bookkeeping must never break event propagation.
        try:
            from hsync_client.tombstones import clear_deletion

            clear_deletion(src_path)
        except Exception as e:  # pragma: no cover - defensive
            log.debug(f"Could not clear tombstone for {src_path}: {e}")
        # Anti-aliasing invariant (§3.4.7): persist the self-record for a
        # *local* edit before broadcasting. Inbound edits (written on behalf
        # of a peer via the HTTP API) are skipped — their clock was already
        # registered with the merged history at upload acceptance (ADR-0022 §5), and a
        # tick here would mis-attribute the edit to the master.
        try:
            if peek_inbound_origin(src_path) is None and file.sha256sum:
                from hsync_client.sync_state import flush, note_local_edit

                note_local_edit(src_path, file.sha256sum)
                flush()
        except Exception as e:  # pragma: no cover - defensive
            log.debug(f"Could not self-record local edit for {src_path}: {e}")
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

        Fires both for the master's own local removals and for slave-initiated
        deletes applied via the HTTP API (which trigger the same filesystem
        event). Directories carry no content hash so only files are recorded.
        """
        if is_directory:
            return
        _record_deletion_marker(src_path)


# =============================================================================
# WebSocket Client for P2P Client
# =============================================================================


async def p2p_websocket_client(
    host: str,
    port: int,
    use_https: bool = False,
    inventory_publisher: P2PInventoryPublisher | None = None,
    source_resolver: P2PSourceResolver | None = None,
) -> None:
    """
    Connect to the P2P master's WebSocket server and handle events.

    This function runs until the connection is closed or an error occurs.
    Also starts a local file watcher to upload changes to the master
    (bidirectional sync).

    In WSS mode the master's certificate is pinned on the live connection: the
    certificate actually presented during the handshake is bound to the expected
    SHA-256 fingerprint (TOFU), and the connection is refused on mismatch.

    Args:
        host: Master's IP address or hostname.
        port: Master's unified HTTPS/WSS port (REST and WebSocket share it).
        use_https: Whether to use WSS instead of WS.
    """
    global _slave_event_handler
    import websockets

    protocol = "wss" if use_https else "ws"
    uri = build_url(
        protocol, host, port, p2p_ws_events_path(CONFIG["REMOTE_BUCKET"], CONFIG["CID"])
    )

    # A node that serves peer downloads announces its capacity on the handshake:
    # the master records it with the publisher session and echoes it to
    # requesters, which use it as their per-seeder admission limit (ADR-0030).
    # No publisher means no seeder bound, so nothing is advertised at all.
    serving_slots = (
        int(CONFIG.get("P2P_PEER_READ_CAP", P2P_PEER_READ_CAP_DEFAULT))
        if inventory_publisher is not None
        else None
    )

    log.info(f"Connecting to master WebSocket: {uri}")

    # The master serves the REST data plane and the WebSocket control plane on
    # the SAME port (unified listener).
    http_port = port
    inventory_observer: InventoryObserver = inventory_publisher or NOOP_INVENTORY_OBSERVER
    if inventory_publisher is None:
        _slave_event_handler = P2PSlaveFileEventHandler(host, http_port, use_https)
    else:
        _slave_event_handler = P2PSlaveFileEventHandler(
            host,
            http_port,
            use_https,
            inventory_observer=inventory_observer,
        )
    watch_path = CONFIG["LOCAL_BUCKET"]

    # Ensure watch_path exists: watchdog raises FileNotFoundError otherwise.
    # This can happen when the bucket has been emptied or removed externally.
    if not Path(watch_path).exists():
        log.warning(f"Watch path '{watch_path}' does not exist, creating it")
        Path(watch_path).mkdir(parents=True, exist_ok=True)

    watcher = WatcherManager(watch_path)
    watcher.start(_slave_event_handler)

    # Periodic full resync as a safety net against silently missed
    # file-system events (e.g. inotify queue overflow). The master is the
    # source of truth in P2P mode, so a full sync pulls back any drift.
    resync_task: asyncio.Task[None] | None = None
    if CONFIG.get("RESYNC"):
        resync_task = asyncio.create_task(
            _p2p_periodic_resync(inventory_publisher, source_resolver)
        )

    # Set up SSL context for WSS. The self-signed master cert is trusted by
    # pinned fingerprint (TOFU), not by chain validation, so we accept any cert
    # at the handshake and verify the fingerprint on the live connection below.
    ssl_context = None
    expected_fingerprint = CONFIG.get("MASTER_CERT_FINGERPRINT")
    if use_https:
        from hsync_client.ssl_utils import create_fingerprint_ssl_context

        ssl_context = create_fingerprint_ssl_context(str(expected_fingerprint or ""))

    # Bound so the teardown path below can drain pending event downloads even
    # when the connection failed before the dispatch context was built.
    ctx: EventDispatchContext | None = None
    # Tracks whether the slave→master link was established, so the teardown can
    # report the drop back to the UI (continuous mode reconnects afterwards).
    connected = False

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
            # fingerprint, refusing on mismatch. Chain validation is disabled on
            # the WSS context by design (the self-signed master certificate is
            # trusted by pin, not by a CA), so the check below is what
            # establishes trust.
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

            # Read server greeting to determine if auth is required.
            # The greeting carries the P2P protocol version and the master's
            # capabilities (ADR-0024): hard-fail on a protocol mismatch and,
            # on the authenticated path, adopt the master's advertised
            # capability set as the session's capability source of truth.
            greeting = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            greeting_data = json.loads(greeting)
            master_caps = decode_capabilities(greeting_data.get("capabilities"))

            if greeting_data.get("type") == "auth_required":
                # Protocol gate first: a greeting we do not speak must never
                # proceed to authentication.
                if greeting_data.get("protocol_version") != PROTOCOL_VERSION_P2P:
                    log.error(
                        "Master P2P protocol mismatch "
                        f"({greeting_data.get('protocol_version')!r} vs "
                        f"{PROTOCOL_VERSION_P2P}); refusing connection"
                    )
                    return
                p2p_key = CONFIG.get("P2P_KEY")
                if not p2p_key:
                    log.error("WebSocket authentication failed – server requires P2P key")
                    return
                auth_msg = json.dumps(
                    build_p2p_auth(
                        token=p2p_key,
                        cid=CONFIG["CID"],
                        capabilities=local_capabilities(),
                        serve_slots=serving_slots,
                    )
                )
                await websocket.send(auth_msg)

                response = await asyncio.wait_for(websocket.recv(), timeout=10.0)
                auth_result = json.loads(response)
                if auth_result.get("status") != "ok":
                    reason = auth_result.get("reason") or "unknown"
                    if reason == "cid_conflict":
                        log.error(
                            f"{ActionLabel.SECURITY} Master refused this client: its device "
                            f"identity (CID {CONFIG['CID']}) is already in use by the master "
                            f"or another connected peer. This machine's device directory "
                            f"was likely copied from another machine; give each instance "
                            f"its own HSYNC_DEVICE_DIR."
                        )
                        global _duplicate_identity_event_sent
                        if not _duplicate_identity_event_sent:
                            _duplicate_identity_event_sent = True
                            emit_event(
                                UiEventName.duplicate_identity,
                                peer_cid=CONFIG["CID"],
                                peer_address=host,
                                message=(
                                    "This machine's device identity is already in use by the "
                                    "master or another peer. Give each machine its own "
                                    "HSYNC_DEVICE_DIR."
                                ),
                            )
                    else:
                        log.error(f"WebSocket authentication failed ({reason})")
                    return
                master_caps = decode_capabilities(auth_result.get("capabilities"))
                adopt_remote_capabilities(master_caps)
                if source_resolver is not None:
                    source_resolver.set_authenticated_master_capability(
                        Capability.peer_download in master_caps
                    )
                log.info("WebSocket authenticated with master")
            elif greeting_data.get("type") == "hello":
                # Lightweight hello handshake: the master has no P2P_KEY
                # but still wants to know our CID for log attribution.
                # The CID is sent unauthenticated (the master logs it
                # with an "(unauthenticated)" qualifier). Capabilities are
                # exchanged for coherence, not trusted (no shared secret).
                await websocket.send(
                    json.dumps(
                        build_p2p_hello(
                            capabilities=local_capabilities(),
                            cid=CONFIG["CID"],
                            serve_slots=serving_slots,
                        )
                    )
                )
                log.info("WebSocket hello sent to master (no auth)")
            else:
                log.error(
                    f"Unexpected master greeting {greeting_data.get('type')!r}; refusing connection"
                )
                return

            if (
                inventory_publisher is not None
                and Capability.peer_download in master_caps
                and Capability.peer_download in local_capabilities()
            ):
                inventory_publisher.start(websocket)

            log.info("Listening for events from master...")

            # Readiness threshold: the local watcher is running and the master
            # handshake is complete — the slave now reacts to events in both
            # directions.
            log.info(
                f"{ActionLabel.READY} Client ready for sync — watching "
                f"'{CONFIG['LOCAL_BUCKET']}', master at {format_authority(host, port)}"
            )

            # The slave is linked to the master and reacting to events: flip the
            # desktop status from "connecting" to "connected".
            emit_event(UiEventName.status, status="connected")
            connected = True

            # One dispatch context per connection: the inbound directory
            # dedup cache must persist across events (a per-event context
            # would reset the cache each time and the duplicate added/
            # modified burst a single mkdir broadcast produces could
            # never be suppressed).
            ctx = _build_p2p_dispatch_ctx(
                _slave_event_handler,
                inventory_observer,
                source_resolver,
                fan_out=True,
            )

            # Process incoming events
            async for message in websocket:
                try:
                    event_data = json.loads(message)
                    if inventory_publisher is not None and inventory_publisher.handle_ack(
                        event_data
                    ):
                        continue
                    if not isinstance(event_data, dict):
                        log.warning("Invalid non-object event from master")
                        continue
                    await _handle_p2p_event(event_data, _slave_event_handler, ctx)
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
        if inventory_publisher is not None:
            await inventory_publisher.stop()
        # Drain pending event-driven downloads before returning to the
        # discovery loop: the reconnect's full sync must not race writes that
        # are still in flight (fan-out path).
        if ctx is not None:
            await drain_file_downloads(ctx)
        # Wait for pending client→master operations to complete before
        # returning to the discovery loop (which triggers a full sync).
        # This prevents race conditions where a rename/upload/delete is
        # still in-flight while the full sync runs and reverts it.
        if _slave_event_handler is not None:
            # Cancel any not-yet-fired on_created-deferred uploads first so
            # they cannot start new uploads during/after the wait; their
            # files will be reconciled by the full sync on reconnect.
            _slave_event_handler.cancel_all_create_timers()
            _slave_event_handler.wait_pending(timeout=30.0)
        watcher.stop()
        _slave_event_handler = None
        log.info("Client file watcher stopped")
        if connected:
            # The link dropped; continuous mode returns to discovery and will
            # emit "connected" again once a master is reached.
            emit_event(UiEventName.status, status="connecting")


async def _p2p_periodic_resync(
    inventory_observer: InventoryObserver | None = None,
    source_resolver: P2PSourceResolver | None = None,
) -> None:
    """Periodic full resync for the P2P slave (safety net for missed events)."""
    from hsync_client.sync import periodic_resync

    await periodic_resync(
        p2p_mode=True,
        inventory_observer=inventory_observer,
        source_resolver=source_resolver,
    )


def _build_p2p_dispatch_ctx(
    slave_handler: P2PSlaveFileEventHandler | None,
    inventory_observer: InventoryObserver = NOOP_INVENTORY_OBSERVER,
    source_resolver: P2PSourceResolver | None = None,
    *,
    fan_out: bool = False,
) -> EventDispatchContext:
    """Build the P2P slave's inbound event dispatch context.

    A P2P-specific :class:`EventDispatchContext` with echo suppression via
    ``slave_handler.mark_receiving``/``unmark_receiving``, no symlink
    materialisation and no rename-source recovery. One context is built
    per WebSocket connection: the ``dir_dedup`` cache (default factory)
    must survive across events, otherwise every event starts with an empty
    cache and the duplicate added/modified burst a single directory
    broadcast produces is re-logged as a fresh change.

    ``fan_out=True`` (the WSS receive loop) additionally arms the bounded
    per-path file-download fan-out; the default keeps the inline
    behavior for one-off callers (``_handle_p2p_event`` without a context).
    """
    return EventDispatchContext(
        mark_receiving=slave_handler.mark_receiving if slave_handler else None,
        unmark_receiving=slave_handler.unmark_receiving if slave_handler else None,
        record_inbound_rename=(slave_handler.record_inbound_rename if slave_handler else None),
        record_inbound_delete=(slave_handler.record_inbound_delete if slave_handler else None),
        recover_on_missing_rename_src=False,
        inventory_observer=inventory_observer,
        source_resolver=source_resolver,
        # Fan out inbound file downloads: the WSS receive loop must never
        # serialize on them (measured ~60 files/s ceiling during bursts,
        # vs 200+ files/s the master serves). Bounded by the same knob as
        # the reconcile transfers; per-path ordering is preserved.
        file_download_semaphore=(
            asyncio.Semaphore(int(CONFIG.get("MAX_CONCURRENT_TRANSFERS", 5))) if fan_out else None
        ),
    )


async def _handle_p2p_event(
    event_data: dict,
    slave_handler: P2PSlaveFileEventHandler | None = None,
    ctx: EventDispatchContext | None = None,
) -> None:
    """Apply an event received from the P2P master to the local file system.

    Delegates to the unified :func:`apply_inbound_event` dispatcher with a
    P2P-specific :class:`EventDispatchContext`.

    Args:
        event_data: Event dictionary from the master.
        slave_handler: Optional client event handler for marking files
            as receiving (suppresses the slave's own watchdog echo).
        ctx: Optional per-connection dispatch context. The WebSocket loop
            passes the connection-scoped context so the inbound directory
            dedup cache persists across events; when omitted (one-off
            callers, tests) a fresh context is built.
    """
    if ctx is None:
        ctx = _build_p2p_dispatch_ctx(slave_handler)
    await apply_inbound_event(event_data, ctx)


# =============================================================================
# Client File Event Handler (for bidirectional sync)
# =============================================================================


class _TtlMap[K]:
    """Lock-guarded map with prune-on-write TTL."""

    def __init__(self, ttl: float) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._items: dict[K, float] = {}

    def record(self, key: K) -> None:
        now = time()
        with self._lock:
            self._items[key] = now
            cutoff = now - self._ttl
            expired = [k for k, ts in self._items.items() if ts < cutoff]
            for k in expired:
                del self._items[k]

    def get_live(self, key: K) -> float | None:
        now = time()
        with self._lock:
            ts = self._items.get(key)
            if ts is None:
                return None
            if now - ts > self._ttl:
                del self._items[key]
                return None
            return ts

    def live_items(self) -> list[tuple[K, float]]:
        now = time()
        live: list[tuple[K, float]] = []
        with self._lock:
            for k, ts in list(self._items.items()):
                if now - ts > self._ttl:
                    del self._items[k]
                else:
                    live.append((k, ts))
        return live


def _rename_descendant_match(src: str, dst: str, r_src: str, r_dst: str) -> bool:
    """True when ``(src, dst)`` is the same-suffix descendant of ``(r_src, r_dst)``."""
    if not dst.startswith(r_dst):
        return False
    suffix = dst.removeprefix(r_dst)
    return (not suffix or suffix.startswith("/")) and src == r_src + suffix


class P2PSlaveFileEventHandler(FileSystemEventHandler):
    """
    File system event handler for P2P client mode.

    Monitors the local bucket directory for file system changes and
    uploads changes to the master server for propagation to other slaves.
    """

    def __init__(
        self,
        master_host: str,
        master_port: int,
        use_https: bool = False,
        inventory_observer: InventoryObserver = NOOP_INVENTORY_OBSERVER,
    ) -> None:
        """
        Initialize the client file event handler.

        Args:
            master_host: Master's IP address or hostname.
            master_port: The master's single HTTPS/WSS port (REST and the
                realtime control plane share it).
            use_https: Whether to use HTTPS for uploads.
        """
        self._master_host = master_host
        self._master_port = master_port
        self._use_https = use_https
        self._inventory_observer = inventory_observer
        self._last_modified: dict[str, float] = {}
        # Track the last observed permission mode per file so a pure ``chmod``
        # (IN_ATTRIB, which inotify delivers without a close) is recognised and
        # propagated as a permission-only upload. Pop the entry on delete/rename
        # so a re-created path with a different mode is never suppressed.
        self._last_mode: dict[str, int] = {}
        # Same idea for a content-identical ``touch`` (utime also fires only
        # IN_ATTRIB on Linux): the mtime is tracked separately so a slave-local
        # touch is pushed to the master as a metadata-only PATCH instead of
        # being silently discarded. Popped on delete/rename like ``_last_mode``.
        self._last_mtime: dict[str, int] = {}
        # Same dedup, for directories: a directory chmod fires only IN_ATTRIB
        # (no close, no content hash), so the mode is tracked separately and a
        # change triggers a /dirs call to the master (directory-permission
        # propagation — the master then re-broadcasts it to the other slaves).
        self._last_dir_mode: dict[str, int] = {}
        # Track files we're currently receiving from master to avoid echo
        self._receiving_files: set[str] = set()
        self._receiving_lock = threading.Lock()
        # Track recently applied inbound renames (exact (src,dst) pair) so
        # a watchdog echo of the same rename can be distinguished from a real
        # 404 divergence when the slave re-sends it to the master.  The echo
        # always carries the identical (src,dst) of the inbound rename; a
        # genuine user rename that collides with the source path does not.
        self._inbound_renames: _TtlMap[tuple[str, str]] = _TtlMap(10.0)
        # Track directory renames this slave has just sent to the master (or
        # about to send). When the local watcher renames a directory it emits
        # an `on_moved` for the directory AND one for each child (e.g. the
        # `.hsynckeep` sentinel); but a single PATCH of a directory rename on
        # the master already renames the whole tree, so the child PATCHes are
        # redundant and fail with 404. This map lets `on_moved` skip those
        # redundant child PATCHes (outbound direction, same `/`-suffix guard).
        self._outbound_dir_renames: _TtlMap[tuple[str, str]] = _TtlMap(10.0)
        # Track inbound directory deletes this slave just applied on behalf of
        # the master (recursive `deleted` → `rmtree`). The `rmtree` only
        # marks the tree root for echo suppression, so the slave's own
        # watcher fires `deleted` events for every descendant: those echoes
        # must not be re-sent to the master. A path equal to, or under, a
        # cached directory delete is recognised as a benign echo (same
        # `/`-suffix guard as the rename descendant match).
        self._inbound_deletes: _TtlMap[str] = _TtlMap(10.0)
        # Track pending operations (rename, upload, delete) for graceful
        # shutdown.  Each entry is ``(future, bytes_estimate)`` so that
        # ``wait_pending`` can derive a timeout proportional to the volume
        # still in flight (large uploads can legitimately take minutes).
        self._pending_ops: list[tuple[Future, int]] = []
        self._pending_ops_lock = threading.Lock()
        # Deferred ``on_created`` uploads: a short daemon ``Timer`` per path
        # that fires only if no ``on_closed`` arrives in time. ``on_closed``
        # is the reliable trigger (fd closed → content complete, fast
        # stability re-check) and takes precedence by cancelling the timer;
        # the timer is the fallback for files that arrive already complete
        # with no writer fd to close (atomic rename / hard-link / mv-in).
        self._pending_create_timers: dict[str, threading.Timer] = {}
        self._pending_create_timers_lock = threading.Lock()
        # Delete coalescing: buffered (path, is_directory) pairs pending a
        # flush guarded by _delete_lock; _delete_timer is the pending Timer.
        self._pending_deletes: list[tuple[str, bool]] = []
        self._delete_timer: threading.Timer | None = None
        self._delete_lock = threading.Lock()
        self._DELETE_COALESCE_WINDOW_S: float = 0.2

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

    def record_inbound_rename(self, src: str, dst: str) -> None:
        """Record a rename applied locally on behalf of an inbound master event.

        The exact ``(src, dst)`` pair is cached for a short TTL so a later
        watchdog echo of the *same* rename can be recognised as benign when
        the master responds 404 (source already renamed).  A genuine user
        rename that re-uses one of the paths will have a different pair and
        will not match.
        """
        self._inbound_renames.record((src, dst))

    def is_inbound_rename_echo(self, src: str, dst: str) -> bool:
        """Return True if ``(src, dst)`` matches a recent inbound rename.

        Matches either the exact ``(src, dst)`` pair cached when the rename
        was applied on behalf of an inbound master event, or a *descendant*
        echo of a cached directory rename: if the master renamed
        ``r_src -> r_dst``, the slave's own watcher may also emit echo events
        for children under the old path (e.g. ``r_src/.hsynckeep ->
        r_dst/.hsynckeep``). A descendant is recognised only when the trailing
        suffix (empty, or starting with ``/``) is byte-identical on both
        sides, so a genuine rename that merely shares a path prefix
        (``ABC-keeper -> ABC-123-keeper``) is never suppressed.
        """
        if self._inbound_renames.get_live((src, dst)) is not None:
            return True
        return any(
            _rename_descendant_match(src, dst, r_src, r_dst)
            for (r_src, r_dst), _ts in self._inbound_renames.live_items()
        )

    def record_inbound_delete(self, path: str) -> None:
        """Record a directory delete applied locally on behalf of an inbound event.

        Cached for a short TTL so the watchdog echo of the resulting
        ``rmtree`` (one ``deleted`` event per descendant, fired by our own
        watcher outside the mark/unmark window of the tree root) is
        recognised as benign and not re-sent to the master.
        """
        self._inbound_deletes.record(path)

    def is_inbound_delete_echo(self, path: str) -> bool:
        """Return True if *path* is an echo of a recent inbound directory delete.

        Matches the exact cached root or a descendant: ``rmtree(root)`` makes
        the slave's own watcher report every removed child as a local delete.
        A descendant is recognised only when the trailing suffix is empty or
        starts with ``/``, so a genuine deletion that merely shares a path
        prefix (``ABC -> ABC-1``) is never suppressed.
        """
        for root, _ts in self._inbound_deletes.live_items():
            if path == root:
                return True
            suffix = path.removeprefix(root)
            if suffix != path and (suffix.startswith("/") or not suffix):
                return True
        return False

    def _record_outbound_dir_rename(self, src: str, dst: str) -> None:
        """Record a directory rename this slave is about to send to the master.

        The callback chain is: watchdog fires ``on_moved`` for the directory
        first, then for each child. Recording the directory pair lets the
        subsequent child events be recognised as redundant (the single PATCH
        of the directory already renamed the whole tree on the master).
        """
        self._outbound_dir_renames.record((src, dst))

    def is_outbound_dir_descendant(self, src: str, dst: str) -> bool:
        """Return True if ``(src, dst)`` is a child of a recent outbound dir rename.

        If the slave sent ``r_src -> r_dst`` for a directory, a later child
        event ``r_src/<suffix> -> r_dst/<suffix>`` (e.g. the ``.hsynckeep``
        sentinel) is redundant: the master already renamed the whole tree.
        The trailing suffix must start with ``/`` (or be empty), so a genuine
        rename sharing a path prefix (``ABC-keeper -> ABC-123-keeper``) is
        never suppressed.
        """
        return any(
            _rename_descendant_match(src, dst, r_src, r_dst)
            for (r_src, r_dst), _ts in self._outbound_dir_renames.live_items()
        )

    def _start_operation(
        self,
        target,
        args: tuple = (),
        kwargs: dict | None = None,
        name: str | None = None,
        bytes_estimate: int = 0,
    ) -> Future:
        """Schedule a background operation on the bounded realtime pool.

        The pool (``REALTIME_MAX_WORKERS`` workers) keeps long-lived workers
        that reuse their keep-alive HTTP sessions to the master instead of
        opening a fresh TCP+TLS connection per event, and bursts are queued
        instead of spawning unbounded threads.

        Args:
            target: Callable to run on the pool.
            args: Arguments to pass to target.
            kwargs: Keyword arguments to pass to target.
            name: Accepted for call-site compatibility; pooled workers are
                pre-named via the executor's ``thread_name_prefix`` so this
                value is unused.
            bytes_estimate: Expected payload size in bytes (used by
                ``wait_pending`` to derive a proportional timeout).
                Defaults to 0 for fast metadata operations (delete/rename).

        Returns:
            The Future for the scheduled operation.
        """
        future = _get_realtime_executor().submit(target, *args, **(kwargs or {}))
        future.add_done_callback(partial(log_future_error, label="Realtime operation failed"))
        with self._pending_ops_lock:
            self._pending_ops = [(f, b) for f, b in self._pending_ops if not f.done()]
            self._pending_ops.append((future, max(bytes_estimate, 0)))
        return future

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
            active = [(f, b) for f, b in self._pending_ops if not f.done()]

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

        _, not_done = wait(
            fs=[future for future, _ in active],
            timeout=effective_timeout,
        )
        timed_out = len(not_done)

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
            upload_file(
                file_obj,
                check_size=True,
                from_closed=from_closed,
                inventory_observer=self._inventory_observer,
            )
        except Exception as e:
            log.error(f"Failed to upload to master: {e}")

    def _rel_in_bucket(self, abs_path: str) -> str:
        """Bucket-relative path via the component-aware SSOT helper.

        Delegates to :func:`hsync_common.models.tombstone.relpath_in_bucket`
        so a sibling-prefix directory (e.g. ``/data/bucket2`` vs
        ``/data/bucket``) is never stripped into a bogus relative path
        (``2/x``). A path outside the bucket raises ``ValueError``; the
        caller's broad ``except`` logs and skips the operation.
        """
        rel = relpath_in_bucket(abs_path, str(CONFIG["LOCAL_BUCKET"]))
        if rel is None:
            raise ValueError(f"Path outside local bucket: {abs_path}")
        return rel

    def _master_url(self, suffix: str) -> str:
        protocol = "https" if self._use_https else "http"
        return build_url(
            protocol,
            self._master_host,
            self._master_port,
            rest_bucket_path(CONFIG["REMOTE_BUCKET"], suffix),
        )

    def _master_params(self, **extra: str) -> dict[str, str]:
        params = dict(extra)
        own_cid = CONFIG.get("CID")
        if own_cid:
            params["cid"] = str(own_cid)
        return params

    def _call_master(self, http_fn, suffix: str, params: dict[str, str]):
        from hsync_client.utils import get_ssl_verify

        return http_fn(
            self._master_url(suffix),
            params=params,
            headers=request_headers(),
            verify=get_ssl_verify(),
            timeout=30,
        )

    def _create_dir_on_master(self, dir_path: str, permissions: str | None = None) -> None:
        """Notify master about a directory creation.

        POSTs to the master's ``/dirs`` endpoint so the directory (and any
        missing parents) is created on the master and propagated to other
        slaves. Respects ``SYNC_DIRECTION == "download-only"``.
        When *permissions* is given (a directory chmod propagate), the master
        applies the mode to the already-existing directory (owner-floor
        preserved) and re-broadcasts it.
        """
        from hsync_client.utils import http_post

        if CONFIG.get("SYNC_DIRECTION") == "download-only":
            return

        try:
            params = self._master_params(path=self._rel_in_bucket(dir_path))
            if permissions:
                params["permissions"] = permissions
            log.info(f'{ActionLabel.LOCAL_CREATE} "{dir_path}" (dir)')
            response = self._call_master(http_post, REST_DIRS_SUFFIX, params)
            if response.status_code not in (200, 201, 204):
                log.warning(f"Create dir request failed: {response.status_code}")
        except Exception as e:
            log.error(f"Failed to create dir on master: {e}")

    def _flush_pending_deletes(self) -> None:
        """Flush the coalesced delete buffer at most once per window.

        inotify emits the per-descendant delete events before the directory
        event itself, so a removed tree produces one ``deleted`` per entry.
        Any path covered by a deleted ancestor directory is folded into that
        ancestor's single ``DELETE`` request (``is_dir=true`` → the master
        applies ``rmtree``), so a whole tree removal costs ONE message.

        A path re-created inside the window is dropped (and its causal
        tombstone cleared) instead of being sent: the DELETE would make the
        master remove the fresh file — the source losing data on a stale
        delete echo.
        """
        from hsync_client.file_event_handler import split_stale_deletes
        from hsync_client.tombstones import clear_deletion

        with self._delete_lock:
            pending = self._pending_deletes
            self._pending_deletes = []
            self._delete_timer = None
        if not pending:
            return

        pending, stale = split_stale_deletes(pending)
        for src_path, is_directory in stale:
            # Files only: directories never carry a tombstone marker (mirrors
            # the record side in on_deleted).
            if not is_directory:
                try:
                    clear_deletion(src_path)
                except Exception as e:  # pragma: no cover - defensive
                    log.debug(f"Could not clear tombstone for {src_path}: {e}")
        if not pending:
            return

        deleted_dirs = [p for p, is_dir in pending if is_dir]

        def covered_by_deleted_ancestor(path: str) -> bool:
            prefix = path.rstrip("/") + "/"
            return any(prefix.startswith(d.rstrip("/") + "/") and d != path for d in deleted_dirs)

        for src_path, is_directory in pending:
            if covered_by_deleted_ancestor(src_path):
                log.debug(f'Folded into ancestor delete (slave): "{src_path}"')
                continue
            self._start_operation(
                target=self._delete_on_master,
                args=(src_path, is_directory),
                name=f"delete-{src_path}",
            )

    def _delete_on_master(self, filepath: str, is_dir: bool = False) -> None:
        """Notify master about a file/directory deletion."""
        from hsync_client.utils import http_delete

        try:
            params = self._master_params(
                path=self._rel_in_bucket(filepath), is_dir=str(is_dir).lower()
            )
            log.info(f'{ActionLabel.LOCAL_DELETE} "{filepath}"')
            response = self._call_master(http_delete, REST_FILES_SUFFIX, params)
            if response.status_code not in (200, 204, 404):
                log.warning(f"Delete request failed: {response.status_code}")
        except Exception as e:
            log.error(f"Failed to delete on master: {e}")

    def _rename_on_master(self, src_path: str, dst_path: str) -> None:
        """Notify master about a file/directory rename."""
        from hsync_client.utils import http_patch

        try:
            params = self._master_params(
                src=self._rel_in_bucket(src_path), dst=self._rel_in_bucket(dst_path)
            )
            log.info(f'{ActionLabel.LOCAL_RENAME} "{src_path}" -> "{dst_path}"')
            response = self._call_master(http_patch, REST_FILES_SUFFIX, params)
            if response.status_code not in (200, 204):
                if response.status_code == 404 and (
                    self.is_inbound_rename_echo(src_path, dst_path)
                    or self.is_outbound_dir_descendant(src_path, dst_path)
                ):
                    log.debug(
                        f"Rename source already gone on master (redundant echo): "
                        f'"{src_path}" -> "{dst_path}"'
                    )
                else:
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
            log.debug(f'{ActionLabel.LOCAL_CREATE} "{src_path}" (dir)')
            self._ensure_sentinel(src_path)
            # Notify the master to create the directory (and any missing
            # parents) so it is propagated to other slaves.  The sentinel
            # upload is skipped by _handle_file_upload, so the directory
            # itself must be propagated via the dedicated /dirs endpoint.
            self._start_operation(
                target=self._create_dir_on_master,
                args=(src_path,),
                name=f"create-dir-{src_path}",
            )
        elif not event.is_directory and not is_hidden_file(src_path):
            if Path(src_path).is_symlink():
                # Symlink - skip if its target escapes the bucket (a
                # file-symlink to /etc/passwd must never be dereferenced and
                # exfiltrated). An in-bucket symlink is replicated
                # STRUCTURALLY: the (path, target) pair goes to the master,
                # never the target content. The symlink is created in one
                # atomic syscall, so no stability wait is needed.
                from hsync_common.models.symlink import Symlink
                from hsync_common.path_validation import is_safe_symlink_target

                try:
                    link = Symlink.get(src_path)
                except OSError:
                    link = None
                if link is not None and not is_safe_symlink_target(
                    src_path, link.target, CONFIG["LOCAL_BUCKET"]
                ):
                    log.warning(
                        f"{ActionLabel.SECURITY} Symlink target escapes bucket, "
                        f'not uploading: "{src_path}" -> "{link.target}"'
                    )
                    return
                # Symlink - upload structurally (single atomic op, no wait).
                if link is not None:
                    from hsync_client.transfer import upload_symlink

                    self._start_operation(
                        target=upload_symlink,
                        args=(link,),
                        name=f"symlink-{src_path}",
                    )
            else:
                # Files placed via an atomic rename from outside the watched
                # tree (e.g. file managers using `IN_MOVED_TO` without a
                # corresponding `IN_MOVED_FROM`, hard-links, `mv` from a
                # sibling directory, etc.) arrive already complete and will
                # not generate a subsequent on_closed / on_modified event.
                # Defer the upload for a short window so that, when the file
                # WAS opened for writing, on_closed (the reliable, fd-closed
                # trigger) can cancel this fallback and take precedence. If
                # no on_closed arrives (atomic rename / hard-link), the timer
                # fires and uploads the already-complete file. Empty files
                # wait for on_closed / on_modified to avoid uploading a
                # half-opened file.
                try:
                    size = Path(src_path).stat().st_size
                except OSError:
                    size = 0
                if size > 0:
                    log.debug(
                        f'{ActionLabel.LOCAL_CREATE} "{src_path}"'
                        f" ({size} bytes, deferred to on_closed)"
                    )
                    self._schedule_create_upload(src_path)

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
            if not is_hidden_dir(src_path):
                # Suppress the IN_ATTRIB burst of an inbound recursive delete:
                # inotify fires IN_ATTRIB on a directory whenever a child is
                # removed, so rmtree of a deep tree produces one spurious
                # directory modify per removed child.  A path equal to, or
                # under, a directory already cached as an inbound delete is
                # noise, not a permission change, and must not be propagated
                # to the master.
                if self.is_inbound_delete_echo(src_path):
                    return
                self._propagate_dir_permission_change(src_path)
            return

        if is_hidden_file(src_path):
            return

        if SUPPORTS_ON_CLOSED:
            # On inotify platforms content changes propagate via on_closed; a
            # chmod or a touch fires only IN_ATTRIB (no close). Inspect the mode
            # first (chmod -> permission-only upload), then the mtime (a
            # content-identical touch -> metadata-only PATCH); the master's
            # watcher then broadcasts the change to the other slaves.
            change = classify_stat_change(src_path, self._last_mode, self._last_mtime)
            if change == "mode":
                log.info(f'[PERMISSION] mode change: "{src_path}"')
                self._handle_file_upload(src_path)
                return
            if change == "mtime":
                log.info(f'[TOUCH] mtime change: "{src_path}"')
                self._propagate_metadata_change(src_path)
            return

        # A file-symlink created via a deferred path (e.g. atomic rename of a
        # symlink, or a symlink modified in place) is uploaded by
        # ``_handle_file_upload`` → ``upload_file``, which refuses targets
        # escaping the bucket. No extra guard needed here.
        now = time()
        last_time = self._last_modified.get(src_path, 0)
        if now - last_time < MODIFIED_DEBOUNCE_INTERVAL:
            return
        self._last_modified[src_path] = now

        # On non-Linux, use on_modified as fallback for file completion
        self._handle_file_upload(src_path)

    def _propagate_dir_permission_change(self, src_path: str) -> None:
        """Propagate a directory permission-only change to the master.

        A directory ``chmod`` fires only ``IN_ATTRIB`` (no close, no content);
        the master's watcher would never see it from this slave. On a mode
        change (after the first observation), the directory is pushed through
        the existing ``POST /dirs`` surface with its current permissions: the
        master applies the mode (owner-floor preserved) and its watcher
        re-broadcasts the change to the other slaves.
        """
        try:
            mode = Path(src_path).stat().st_mode & 0o7777
        except OSError:
            return
        prev = self._last_dir_mode.get(src_path)
        self._last_dir_mode[src_path] = mode
        if prev is None:
            return
        if prev == mode:
            return
        log.info(f'[PERMISSION] mode change: "{src_path}" (dir)')
        self._start_operation(
            target=self._create_dir_on_master,
            args=(src_path,),
            kwargs={"permissions": f"{mode:o}"},
            name=f"dir-permissions-{src_path}",
        )

    def _propagate_metadata_change(self, src_path: str) -> None:
        """Push a content-identical local ``touch`` to the master.

        Linux fires only IN_ATTRIB for a utime (no close), so the content path
        never sees it. The slave self-records the edit and sends a metadata-only
        PATCH (corrected mtime + effective vclock) to the master's
        ``/files/permissions`` route; the master applies/records it and
        re-broadcasts to the other slaves. Inbound adoptions run while
        ``update_file`` holds the receiving mark, so they never echo back here.
        """
        if CONFIG.get("SYNC_DIRECTION") == "download-only":
            return
        try:
            rel = self._rel_in_bucket(src_path)
        except ValueError:
            return
        file_obj = File.get(src_path)
        if not file_obj or not file_obj.sha256sum:
            return
        try:
            from hsync_client.sync_state import (
                flush,
                note_local_edit,
                resolve_upload_version_fields,
            )

            note_local_edit(src_path, file_obj.sha256sum)
            flush()
            vclock_json, version_id, corrected_mtime = resolve_upload_version_fields(file_obj)
        except Exception as e:  # pragma: no cover - bookkeeping must never break propagation
            log.debug(f"Could not self-record metadata edit for {src_path}: {e}")
            return
        from hsync_client.utils import http_patch

        params = self._master_params(
            filename=rel,
            permissions=file_obj.permissions or "",
        )
        if corrected_mtime > 0:
            params["mtime"] = str(corrected_mtime)
        if vclock_json and vclock_json != "{}":
            params["vclock"] = vclock_json
        if version_id:
            params["version_id"] = version_id
        self._start_operation(
            target=self._call_master,
            args=(http_patch, REST_FILES_PERMISSIONS_SUFFIX, params),
            name=f"metadata-{src_path}",
        )

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

        # Cancel any on_created-deferred upload for this path: on_closed is
        # the reliable trigger (fd closed → content complete) and takes
        # precedence, using the fast stability re-check. If the deferred
        # timer already fired its upload is in flight; the stability check
        # converges both on the final content (redundant, not data-losing).
        self._cancel_create_upload(src_path)
        self._handle_file_upload(src_path, from_closed=True)

    def _schedule_create_upload(self, src_path: str) -> None:
        """Defer an ``on_created`` upload so a later ``on_closed`` can win.

        ``on_closed`` (``IN_CLOSE_WRITE``) is the reliable trigger — the
        writer fd is closed so the content is guaranteed complete, and it
        uses the fast stability re-check. By deferring for a short window we
        let ``on_closed`` cancel this fallback and take over. If
        ``on_closed`` never arrives (atomic rename from outside the watch
        tree, hard-link, ``mv`` from a sibling directory — no writer fd to
        close), the timer fires and uploads the already-complete file via
        the conservative stability poll.
        """
        with self._pending_create_timers_lock:
            # Replace any prior pending timer for this path (rapid
            # create/create) so only the latest creation uploads.
            prior = self._pending_create_timers.pop(src_path, None)
            if prior is not None:
                prior.cancel()
            timer = threading.Timer(
                CREATE_UPLOAD_DEFER_S,
                self._handle_file_upload,
                args=(src_path,),
            )
            timer.daemon = True
            timer.name = f"create-upload-{src_path}"
            self._pending_create_timers[src_path] = timer
            to_start = timer
        to_start.start()

    def _cancel_create_upload(self, src_path: str) -> None:
        """Cancel a pending ``on_created``-deferred upload for *src_path*.

        Called from ``on_closed`` (the reliable trigger takes precedence) and
        from ``on_deleted`` / ``on_moved(src)`` (the file no longer exists at
        that path). If the timer already fired its upload is in flight and
        cannot be stopped, which is acceptable: the stability check converges
        both the fallback and the close-triggered upload on the final content.
        """
        with self._pending_create_timers_lock:
            timer = self._pending_create_timers.pop(src_path, None)
        if timer is not None:
            timer.cancel()

    def cancel_all_create_timers(self) -> None:
        """Cancel every pending ``on_created``-deferred upload (e.g. shutdown)."""
        with self._pending_create_timers_lock:
            timers = list(self._pending_create_timers.values())
            self._pending_create_timers.clear()
        for timer in timers:
            timer.cancel()

    def _handle_file_upload(self, src_path: str, from_closed: bool = False) -> None:
        """Handle a file modification - upload to master.

        ``from_closed`` is forwarded to :meth:`_upload_to_master` so an
        ``on_closed`` (``IN_CLOSE_WRITE``) trigger uses the fast re-check.
        """
        if not Path(src_path).exists():
            return

        # .hsynckeep is a per-peer local sentinel: the periodic reconcile
        # already excludes it from sync actions in both directions, so the
        # event-driven upload path must too. Each peer maintains its own
        # sentinels (ensure_hsynckeep_files / _ensure_sentinel*), and the
        # directory itself is propagated by the directory event — uploading
        # the sentinel only produces a 0-byte [UPLOAD] line and a broadcast
        # round-trip.
        if Path(src_path).name == HSYNCKEEP_FILENAME:
            log.debug(f'Skip sentinel upload: "{src_path}"')
            return

        invalidate_sha256_cache(src_path)
        file_obj = File.get(src_path)
        if file_obj:
            if CONFIG.get("SYNC_DIRECTION") == "download-only":
                self._inventory_observer.upsert(file_obj)
                return
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
        from hsync_client.sync import ensure_sentinel

        ensure_sentinel(dir_path, label=ActionLabel.LOCAL_KEEP)

    def _reset_path_tracking(self, path: str) -> None:
        """Cancel a deferred create-upload and drop cached mode/mtime for *path*.

        Shared by ``on_deleted``/``on_moved``: a path that disappears or moves
        away must not leave a stale deferred-upload timer or a stale mode/mtime
        baseline behind.
        """
        self._cancel_create_upload(path)
        self._last_mode.pop(path, None)
        self._last_mtime.pop(path, None)
        self._last_dir_mode.pop(path, None)

    def on_deleted(self, event) -> None:
        """Handle file/directory deletion events."""
        src_path = ensure_str_path(event.src_path)

        # The file no longer exists: cancel any on_created-deferred upload so
        # a stale timer does not try to upload a deleted file.
        self._reset_path_tracking(src_path)

        # Skip temp files (.part files used during downloads)
        if self._is_temp_file(src_path):
            return

        # Skip if we're receiving this deletion from master (echo suppression).
        if self.is_receiving(src_path):
            return

        # Skip the echo of an inbound recursive delete: `apply_inbound_event`
        # marks only the tree root (target of `rmtree`), so the slave's own
        # watcher still fires `deleted` per descendant. Sending them back to
        # the master is redundant noise (the master already deleted the whole
        # tree; each request would just 404 as benign).
        if self.is_inbound_delete_echo(src_path):
            log.debug(f'Delete echo of inbound recursive delete skipped: "{src_path}"')
            return

        # Skip hidden files/dirs
        if event.is_directory and is_hidden_dir(src_path):
            return
        if not event.is_directory and is_hidden_file(src_path):
            return

        if event.is_directory:
            self._inventory_observer.remove_prefix(src_path)
        else:
            self._inventory_observer.remove_file(src_path)

        # Skip in download-only mode (don't propagate local deletions)
        if CONFIG.get("SYNC_DIRECTION") == "download-only":
            return

        # Skip if in ignore-delete mode
        if CONFIG.get("IGNORE_DELETE", False):
            log.debug(f"{ActionLabel.LOCAL_IGNORE_DEL} Deletion ignored (ignore-delete mode)")
            return

        # Causal tombstone (ADR-0003): the deletion must survive locally even
        # when the master is unreachable. The DELETE below is best-effort, but
        # the marker guarantees the next reconciliation recognises the deletion
        # instead of resurrecting the file from the master's listing.
        if not event.is_directory:
            _record_deletion_marker(src_path)

        # Buffer the deletion instead of notifying immediately: the sibling
        # descendant events (and the directory event itself) arrive within
        # the coalescing window and fold into a single recursive DELETE.
        # An identical buffered pair is a duplicate (a directory with its own
        # watch fires both IN_DELETE, from the parent's watch, and
        # IN_DELETE_SELF, from its own watch on the same path).
        with self._delete_lock:
            entry = (src_path, event.is_directory)
            if entry in self._pending_deletes:
                log.debug(f'Duplicate delete skipped (slave): "{src_path}"')
            else:
                self._pending_deletes.append(entry)
            if self._delete_timer is None:
                self._delete_timer = threading.Timer(
                    self._DELETE_COALESCE_WINDOW_S, self._flush_pending_deletes
                )
                self._delete_timer.daemon = True
                self._delete_timer.start()

    def on_moved(self, event) -> None:
        """Handle file/directory move/rename events."""
        src_path = ensure_str_path(event.src_path)
        dest_path = ensure_str_path(getattr(event, "dest_path", None))

        if not dest_path:
            return

        # The file moved away from src_path: cancel any on_created-deferred
        # upload for it so a stale timer does not upload a moved file.
        self._reset_path_tracking(src_path)
        # The destination is a fresh path: reset any mode tracking so the
        # first event for it records the current mode instead of comparing
        # against the stale one from the source.
        self._reset_path_tracking(dest_path)

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
            if event.is_directory:
                from hsync_client.sync import get_files_local

                self._inventory_observer.replace_snapshot(get_files_local())
            if not event.is_directory and Path(dest_path).exists():
                invalidate_sha256_cache(dest_path)
                file_obj = File.get(dest_path)
                if file_obj:
                    self._inventory_observer.upsert(file_obj)
                    if CONFIG.get("SYNC_DIRECTION") == "download-only":
                        return
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
            if event.is_directory:
                self._inventory_observer.remove_prefix(src_path)
            else:
                self._inventory_observer.remove_file(src_path)
            if CONFIG.get("SYNC_DIRECTION") == "download-only":
                return
            if not CONFIG.get("IGNORE_DELETE", False):
                # Causal tombstone (ADR-0003): a hide/move-away is a local
                # deletion and must survive locally even when the master is
                # unreachable (same guarantee as ``on_deleted``).
                if not event.is_directory:
                    _record_deletion_marker(src_path)
                self._start_operation(
                    target=self._delete_on_master,
                    args=(src_path, event.is_directory),
                    name=f"delete-{src_path}",
                )
            return

        if event.is_directory:
            self._inventory_observer.rename_prefix(src_path, dest_path)
        else:
            self._inventory_observer.rename_file(src_path, dest_path)

        # Inventory tracks local state even when this direction does not push
        # local renames to the master.
        if CONFIG.get("SYNC_DIRECTION") == "download-only":
            return

        # Normal rename - use PATCH endpoint, unless this rename is redundant:
        # - the echo of an inbound `renamed` event we just applied locally
        #   (the master already performed the rename), or
        # - a child of a directory rename this slave just sent (the single
        #   PATCH of the directory already renamed the whole tree on the
        #   master, so the child PATCH would 404).
        # `is_inbound_rename_echo` / `is_outbound_dir_descendant` cover both
        # the exact pair and descendants (same `/`-suffix guard).
        if self.is_inbound_rename_echo(src_path, dest_path) or self.is_outbound_dir_descendant(
            src_path, dest_path
        ):
            own_cid = CONFIG.get("CID")
            log.debug(
                f'{ActionLabel.LOCAL_RENAME} "{src_path}" -> "{dest_path}"'
                f"{cid_suffix(own_cid)} (redundant rename, skipped)"
            )
            return

        # Record a directory rename before sending its PATCH: the watchdog
        # will also emit an `on_moved` for each child, which a single PATCH on
        # the master already covers (renaming the whole tree). This lets those
        # child events be skipped above as redundant instead of 404-ing.
        if event.is_directory:
            self._record_outbound_dir_rename(src_path, dest_path)

        self._start_operation(
            target=self._rename_on_master,
            args=(src_path, dest_path),
            name=f"rename-{src_path}",
        )


# Global client event handler for marking files as receiving
_slave_event_handler: P2PSlaveFileEventHandler | None = None
