# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""mDNS/Zeroconf discovery plan for ``hsync p2p`` (LAN, no bootnode).

Owns the Zeroconf service listener/advertiser and the ``MdnsDiscovery``
coordinator that browses and advertises ``_hsync._tcp.local.``, stamps the TXT
flags (``server_ready``, ``connected``), runs the heartbeat and coalesces
presence refreshes. Peer
state and election live in ``p2p_peers``; this module is one of the two
implementations of the ``p2p_discovery`` interface.

Consumers: ``hsync-client`` (p2p handler / CLI when no bootnode is set), tests.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Mapping

from hsync_common.protocol.handshake import PROTOCOL_VERSION_P2P
from loguru import logger as log
from zeroconf import (
    NonUniqueNameException,
    ServiceBrowser,
    ServiceInfo,
    ServiceListener,
    Zeroconf,
)

from hsync_client.config import CONFIG
from hsync_client.constants import (
    DISCOVERY_TIMEOUT_DEFAULT,
    HEARTBEAT_INTERVAL,
    HSYNC_VERSION,
    PEER_TIMEOUT,
)
from hsync_client.net_utils import get_local_ip
from hsync_client.p2p_discovery import DiscoveryPlanState
from hsync_client.p2p_peers import (
    HSYNC_P2P_LISTEN_PORT,
    Peer,
    PeerManager,
    get_free_port,
)
from hsync_client.presence import record_presence, slave_connected_count

HSYNC_SERVICE_TYPE = "_hsync._tcp.local."

# Discovery timeout in seconds.
DISCOVERY_TIMEOUT = DISCOVERY_TIMEOUT_DEFAULT

# Join/leave events can arrive in bursts (e.g. a slave reconnecting): instead
# of re-registering the mDNS TXT record per event, a single refresh runs this
# many seconds after the last change, so peers see a converged ``connected``
# count without mDNS churn.
PRESENCE_REFRESH_COALESCE_S = 2.0

# Android NSD cannot update TXT in place, so its heartbeat is an
# unregister+register cycle that broadcasts a goodbye. Receivers delay
# acting on that goodbye so a fresh announce can cancel the removal.
# A true leave still expires after this window (or via the staleness sweep).
GOODBYE_GRACE_S = 2.0


def _tx_bool(properties: Mapping[bytes, bytes | None], key: bytes) -> bool:
    """Read a TXT boolean flag (present-only-when-true) from mDNS properties.

    Per the p2p-protocol.md rule, boolean TXT flags are advertised only when
    true, so an absent/empty key means false.
    """
    raw = properties.get(key, b"")
    return raw.decode("utf-8").lower() == "true" if raw else False


class HsyncServiceListener(ServiceListener):
    """
    Listens for hsync service announcements via mDNS.

    Discovers other hsync clients on the local network and
    registers them with the PeerManager.
    """

    def __init__(
        self,
        peer_manager: PeerManager,
        goodbye_grace_s: float = GOODBYE_GRACE_S,
    ) -> None:
        """
        Initialize the service listener.

        Args:
            peer_manager: PeerManager to register discovered peers.
            goodbye_grace_s: Delay before honouring a ``remove_service`` so an
                Android heartbeat re-register can cancel the removal. ``0``
                removes immediately (tests).
        """
        self._peer_manager = peer_manager
        self._goodbye_grace_s = goodbye_grace_s
        self._pending_goodbyes: dict[str, threading.Timer] = {}
        self._pending_lock = threading.Lock()

    def _cancel_pending_goodbye(self, cid: str) -> None:
        """Drop a delayed removal for *cid* (a fresh announce arrived)."""
        with self._pending_lock:
            timer = self._pending_goodbyes.pop(cid, None)
        if timer is not None:
            timer.cancel()

    def _commit_goodbye(self, cid: str) -> None:
        """Honour a goodbye after the grace window, unless WSS is still live."""
        with self._pending_lock:
            self._pending_goodbyes.pop(cid, None)
        if self._peer_manager.is_wss_connected(cid):
            log.debug(f"Ignoring expired goodbye for WSS-connected peer: {cid}")
            return
        try:
            self._peer_manager.remove_peer(cid)
        except Exception as e:
            log.debug(f"Error removing peer after goodbye grace: {e}")

    def _schedule_goodbye(self, cid: str) -> None:
        """Delay *cid* removal so a heartbeat re-announce can cancel it."""
        if self._goodbye_grace_s <= 0:
            self._commit_goodbye(cid)
            return
        with self._pending_lock:
            old = self._pending_goodbyes.pop(cid, None)
            if old is not None:
                old.cancel()
            timer = threading.Timer(self._goodbye_grace_s, self._commit_goodbye, args=(cid,))
            timer.daemon = True
            self._pending_goodbyes[cid] = timer
            timer.start()

    def _register_peer_from_info(self, info: ServiceInfo) -> None:
        """Extract peer information from ServiceInfo and register it."""
        try:
            # Get properties from TXT record
            properties = info.properties
            cid = (properties.get(b"cid") or b"").decode("utf-8")
            bucket = (properties.get(b"bucket") or b"").decode("utf-8")
            # Version is optional - default to "0.0.0" for old clients
            version_bytes = properties.get(b"version", b"")
            version = version_bytes.decode("utf-8") if version_bytes else "0.0.0"
            # Wire-protocol version: a mismatch is a hard failure (no legacy peers)
            protocol_bytes = properties.get(b"protocol", b"")
            protocol = protocol_bytes.decode("utf-8") if protocol_bytes else ""
            # Group is optional - empty string means no group
            group_bytes = properties.get(b"group", b"")
            group = group_bytes.decode("utf-8") if group_bytes else None
            # HTTPS info is optional
            use_https = _tx_bool(properties, b"https")
            cert_fp_bytes = properties.get(b"cert_fp", b"")
            cert_fingerprint = cert_fp_bytes.decode("utf-8") if cert_fp_bytes else None
            compression_enabled = _tx_bool(properties, b"compression")
            delta_enabled = _tx_bool(properties, b"delta")
            peer_download_enabled = _tx_bool(properties, b"peer_download")
            # Server ready flag (indicates HTTP server is listening)
            server_ready = _tx_bool(properties, b"server_ready")
            # Prefer master flag (for master election)
            prefer_master = _tx_bool(properties, b"prefer_master")
            # Never-master flag (peer refuses to ever be master)
            never_master = _tx_bool(properties, b"never_master")
            # Startup time for master election (earlier startup wins)
            startup_time_bytes = properties.get(b"startup_time", b"")
            startup_time = float(startup_time_bytes.decode("utf-8")) if startup_time_bytes else 0.0
            # Sync direction for master election (bidirectional preferred)
            sync_direction_bytes = properties.get(b"sync_direction", b"")
            sync_direction = (
                sync_direction_bytes.decode("utf-8") if sync_direction_bytes else "bidirectional"
            )
            # Connected-clients count advertised by a serving master.
            # Diagnostic only: used for presence display, never by the
            # election. A malformed/absent value degrades to 0 (unknown).
            connected_bytes = properties.get(b"connected", b"")
            try:
                connected = int(connected_bytes.decode("utf-8")) if connected_bytes else 0
            except ValueError:
                connected = 0
            connected = max(connected, 0)

            if not cid or not bucket:
                log.debug(f"Incomplete service info: cid={cid}, bucket={bucket}")
                return

            # Enforce the wire-protocol version: peers on a different protocol
            # are ignored outright (capabilities never bridge protocol versions).
            # The usual cause is an older client still advertising the retired
            # ``lan/1``; name the peer, its version and source address, and say
            # what to do, so the operator can find and update that device.
            if protocol != PROTOCOL_VERSION_P2P:
                mismatch_addresses = info.parsed_addresses()
                location = (
                    f"{mismatch_addresses[0]}:{info.port}"
                    if mismatch_addresses and info.port
                    else (mismatch_addresses[0] if mismatch_addresses else "unknown address")
                )
                log.warning(
                    f"Ignoring peer {cid} (hsync {version}, at {location}) with "
                    f"incompatible protocol {protocol or 'missing'} "
                    f"(local: {PROTOCOL_VERSION_P2P}); update and restart the hsync "
                    "client on that device"
                )
                return

            # Get address and port
            addresses = info.parsed_addresses()
            if not addresses:
                log.debug(f"No addresses for service: {info.name}")
                return

            address = addresses[0]  # Use first available address
            port = info.port
            if port is None:
                log.debug(f"No port for service: {info.name}")
                return

            peer = Peer(
                cid=cid,
                bucket=bucket,
                address=address,
                port=port,
                version=version,
                group=group,
                use_https=use_https,
                cert_fingerprint=cert_fingerprint,
                compression_enabled=compression_enabled,
                delta_enabled=delta_enabled,
                peer_download_enabled=peer_download_enabled,
                capabilities_known=True,
                server_ready=server_ready,
                connected=connected,
                prefer_master=prefer_master,
                never_master=never_master,
                startup_time=startup_time,
                sync_direction=sync_direction,
            )
            self._cancel_pending_goodbye(cid)
            self._peer_manager.add_peer(peer)

        except Exception as e:
            log.error(f"Error registering peer from service info: {e}")

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        """Called when a new service is discovered."""
        info = zc.get_service_info(type_, name)
        if info:
            self._register_peer_from_info(info)

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        """Called when a service is updated."""
        info = zc.get_service_info(type_, name)
        if info:
            self._register_peer_from_info(info)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        """Called when a service is removed."""
        try:
            parts = name.split(".")
            if parts:
                service_name = parts[0]
                if "-" in service_name:
                    cid = service_name.rsplit("-", 1)[-1]
                    # Goodbye grace: Android NSD heartbeats re-announce as
                    # unregister+register, so every interval broadcasts a
                    # goodbye even for live slave<->slave peers (no WSS). A
                    # live WSS link is still an immediate ignore; otherwise
                    # delay the removal so a following add/update can cancel.
                    if self._peer_manager.is_wss_connected(cid):
                        log.debug(f"Ignoring goodbye for WSS-connected peer: {cid}")
                        return
                    self._schedule_goodbye(cid)
        except Exception as e:
            log.debug(f"Error parsing service name on removal: {e}")


class MdnsDiscovery(DiscoveryPlanState):
    """
    Coordinates P2P-based peer-to-peer synchronization.

    Handles:
    - mDNS service registration (advertising this client)
    - mDNS service discovery (finding other clients)
    - Master election coordination
    - Starting/stopping based on role (master vs client)
    """

    def __init__(
        self,
        cid: str,
        bucket: str,
        port: int = HSYNC_P2P_LISTEN_PORT,
        group: str | None = None,
        use_https: bool = False,
        cert_fingerprint: str | None = None,
        prefer_master: bool = False,
        never_master: bool = False,
        startup_time: float = 0.0,
        sync_direction: str = "bidirectional",
        peer_timeout: float = PEER_TIMEOUT,
        heartbeat_interval: float = HEARTBEAT_INTERVAL,
    ) -> None:
        """
        Initialize P2P sync coordinator.

        Args:
            cid: This client's unique identifier.
            bucket: The bucket name to sync.
            port: Port to use when acting as master. Use 0 for random port.
            group: Optional group name for peer filtering.
            use_https: Whether to use HTTPS for communication.
            cert_fingerprint: SHA-256 fingerprint of the SSL certificate (if HTTPS).
            prefer_master: If True, this client will always try to be master.
            startup_time: Unix timestamp when this client started.
            sync_direction: This client's sync direction
                ("bidirectional", "upload-only", "download-only").
            peer_timeout: Staleness threshold (seconds) for peer liveness,
                threaded through to :class:`PeerManager` (honours
                ``HSYNC_PEER_TIMEOUT``).
            heartbeat_interval: Cadence (seconds) of the liveness heartbeat
                re-announce (honours ``HSYNC_HEARTBEAT_INTERVAL``). Must be
                shorter than ``peer_timeout`` so a single missed heartbeat never
                expires a live peer.
        """
        self._init_common_fields(
            cid=cid,
            bucket=bucket,
            group=group,
            use_https=use_https,
            cert_fingerprint=cert_fingerprint,
            prefer_master=prefer_master,
            never_master=never_master,
            startup_time=startup_time,
            sync_direction=sync_direction,
            peer_timeout=peer_timeout,
        )
        self._heartbeat_interval = heartbeat_interval
        # If port is 0, get a random free port
        self._port = port if port != 0 else get_free_port()
        # The IP this node advertises over mDNS, computed once so the advertised
        # address and the discovery self-check compare against the same value.
        self._local_ip = get_local_ip()

        self._zeroconf: Zeroconf | None = None
        self._browser: ServiceBrowser | None = None
        self._service_info: ServiceInfo | None = None
        self._peer_manager: PeerManager | None = None
        self._running = False

        # Discovery logging state: the discovery loop polls continuously, so
        # these guards keep the "waiting"/result messages from being repeated
        # on every iteration once nothing relevant changes.
        self._discovery_announced = False
        self._last_discovery_state: tuple[int, int] | None = None

        # Presence state: the number of WebSocket clients currently connected
        # to this node (only meaningful while it serves as master) and the
        # provider that exposes it (installed by the P2P handler once the
        # WebSocket server is running).
        self._connected_clients: int = 0
        self._connected_clients_provider: Callable[[], int] | None = None

        # Debounced record refresh: the TXT ``connected`` count changes on
        # every slave join/leave, which can happen in bursts. Only the last
        # change per coalescing window re-registers the record.
        self._refresh_lock = threading.Lock()
        self._refresh_timer: threading.Timer | None = None

        # Liveness heartbeat: a monotonic counter re-announced every
        # HEARTBEAT_INTERVAL so peers' staleness sweep sees a *changing* TXT
        # value for otherwise-quiescent nodes (idle slaves never change their
        # record otherwise). The counter is opaque; the value change is the
        # signal. Tracks the serving state so the heartbeat re-announces the
        # record exactly as it currently is.
        self._heartbeat_counter = 0
        self._server_ready = False
        self._endpoint_ready = False
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

        # Serializes every mDNS record publication (initial register, heartbeat
        # re-announce, in-place refresh, teardown) across the three threads that
        # touch the record: the heartbeat thread, the debounced presence-refresh
        # timer and the main thread. Without it, concurrent ``update_service``
        # calls and ``_service_info`` assignments can interleave and leave the
        # published record inconsistent with the local state.
        self._record_lock = threading.Lock()

    @property
    def group(self) -> str | None:
        """Get the group name."""
        return self._group

    @property
    def use_https(self) -> bool:
        """Check if HTTPS is enabled."""
        return self._use_https

    @property
    def cert_fingerprint(self) -> str | None:
        """Get the SSL certificate fingerprint."""
        return self._cert_fingerprint

    def start(
        self, register_service: bool = True, peer_manager: PeerManager | None = None
    ) -> PeerManager:
        """
        Start P2P discovery and optionally register service.

        Args:
            register_service: If True, register mDNS service immediately.
                If False, call register_service() later (e.g., after starting
                the HTTP server when becoming master).
            peer_manager: A pre-built :class:`PeerManager` to share with another
                plan (the combined mDNS + bootnode discovery). Built from this
                plan's configuration when omitted.

        Returns:
            PeerManager for monitoring discovered peers and master election.
        """
        group_info = f", group='{self._group}'" if self._group else ""
        log.info(f"Starting P2P mode for bucket '{self._bucket}' (CID: {self._cid}{group_info})")

        # Initialize Zeroconf
        self._zeroconf = Zeroconf()

        # Initialize (or adopt the shared) peer manager
        self._peer_manager = peer_manager or self.create_peer_manager()
        # Resolve the local role before the first wait: a lone --prefer-master
        # node must become master and serve immediately so a joining slave can
        # connect (the election otherwise runs only on peer events).
        self._peer_manager.elect()

        # Register our service (can be deferred if register_service=False)
        if register_service:
            self._register_service()

        # Start listening for other services
        listener = HsyncServiceListener(self._peer_manager)
        self._browser = ServiceBrowser(self._zeroconf, HSYNC_SERVICE_TYPE, listener)

        self._running = True
        log.info("P2P discovery started")

        # Start the liveness heartbeat: peers must see a changing TXT value at
        # least every HEARTBEAT_INTERVAL or their staleness sweep falsely
        # expires quiescent-but-alive nodes (split-brain on failover).
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="hsync-lan-heartbeat", daemon=True
        )
        self._heartbeat_thread.start()

        return self._peer_manager

    def register_service(self, server_ready: bool = False) -> None:
        """
        Register this client's mDNS service.

        Args:
            server_ready: If True, indicates the HTTP server is listening.
                Call with server_ready=True after starting the HTTP server
                when becoming master.
        """
        if self._service_info is not None:
            log.debug("Service already registered")
            return
        self._register_service(server_ready=server_ready)

    def endpoint_published(self) -> bool:
        """mDNS has no separate endpoint publication: readiness is unchanged."""
        return True

    def update_server_ready(self) -> None:
        """
        Update the mDNS service to indicate the server is ready.

        Call this after starting the HTTP server when becoming master.
        This re-registers the service with server_ready=true.
        """
        self._refresh_service(server_ready=True, endpoint_ready=False)
        log.info("Updated mDNS service: server_ready=true")

    def update_server_not_ready(self) -> None:
        """Revoke the serving incumbency on the wire (leadership lease, ADR-0027).

        Call this when the node **yields** mastership (it stopped serving): the
        mDNS record is re-registered without ``server_ready``/``connected`` and
        the local ``server_ready`` flag is cleared, so neither this node nor any
        peer treats it as an incumbent in the next election. A yielded node that
        left ``server_ready=true`` would be a *zombie incumbent* — still ranked
        top of the election while no longer serving — prolonging a split-brain.
        """
        self._refresh_service(server_ready=False, endpoint_ready=False)
        log.info("Updated mDNS service: server_ready=false")

    def update_endpoint_ready(self, ready: bool) -> None:
        """Advertise or withdraw this slave's reachable read-only seeder endpoint.

        Drives the endpoint-material TXT keys (``https``/``cert_fp``) only: whether
        this node can actually serve a peer download is advertised over the
        authenticated WSS handshake, never on the discovery record (ADR-0030).
        """
        self._refresh_service(server_ready=False, endpoint_ready=ready)
        log.info(f"Updated mDNS service: seeder endpoint={'ready' if ready else 'down'}")

    def refresh_election_advertisement(self) -> None:
        """Re-announce the election flags (``prefer_master``/``never_master``).

        Called when the node withdraws or restores its master candidacy at
        runtime (a node parked in continuous dry-run). The readiness state is
        preserved so the update only changes the election flags.
        """
        self._refresh_service(server_ready=self._server_ready)
        log.info(
            "Updated mDNS service: election flags "
            f"(prefer_master={self._prefer_master}, never_master={self._never_master})"
        )

    def _refresh_service(self, server_ready: bool, endpoint_ready: bool | None = None) -> None:
        """Update the mDNS service's TXT record in place (no goodbye).

        Shared by :meth:`update_server_ready` (server just started),
        :meth:`update_server_not_ready` (mastership yielded) and
        :meth:`set_connected_clients` (presence count changed). The update
        sets the local incumbent flag to ``server_ready``: a serving incumbent
        wins the election over newcomers (prevents takeover/split-brain), and a
        yielded node clears it so it is no longer ranked as an incumbent.

        The record is updated via ``zeroconf.update_service`` rather than an
        unregister+register cycle. An unregister broadcasts a goodbye packet
        that makes every slave observe a spurious ``remove_service`` for the
        master (a "Peer disconnected" + re-election churn) on a value that only
        changed for diagnostics. ``update_service`` re-announces the fresh TXT
        record in place, so peers see a value *change* (an ``update_service``
        callback) and never a removal.
        """
        with self._record_lock:
            if not self._zeroconf or not self._service_info or not self._running:
                log.warning("Cannot update service: not registered")
                return

            if endpoint_ready is None:
                endpoint_ready = self._endpoint_ready
            if server_ready:
                endpoint_ready = False
            new_info = self._build_service_info(
                server_ready=server_ready,
                endpoint_ready=endpoint_ready,
            )
            # ``register_service`` sets the server hostname implicitly; an in-place
            # ``update_service`` requires it explicitly so the registry key matches.
            new_info.set_server_if_missing()
            self._zeroconf.update_service(new_info)
            self._service_info = new_info
            self._server_ready = server_ready
            self._endpoint_ready = endpoint_ready
        # Notify outside the record lock: the peer manager takes its own locks
        # and no code path ever acquires them in the opposite order, but holding
        # two locks would needlessly widen the critical section.
        if self._peer_manager:
            self._peer_manager.set_local_server_ready(server_ready)

    def set_connected_clients(self, count: int) -> None:
        """Record how many WebSocket clients are connected to this master.

        The count feeds two consumers: the ``connected`` value advertised in
        the mDNS TXT record (so slaves can display group presence without a
        server round-trip) and the discovery status log line. A serving master
        re-registers its record so peers observe the new value on their next
        mDNS heartbeat.
        """
        count = max(count, 0)
        if count == self._connected_clients:
            return
        self._connected_clients = count
        # Schedule one record refresh for the burst: rapid join/leave sequences
        # coalesce into a single mDNS re-registration instead of flooding the
        # network with one TXT update per event. The discovery loop always
        # reads the live count immediately, so the delay only affects the
        # record consumers on other peers (who poll mDNS anyway).
        with self._refresh_lock:
            if self._refresh_timer is not None:
                self._refresh_timer.cancel()
            if self._service_info is None:
                self._refresh_timer = None
                return
            timer = threading.Timer(PRESENCE_REFRESH_COALESCE_S, self._refresh_after_coalesce)
            self._refresh_timer = timer
            timer.daemon = True
            timer.start()

    def _refresh_after_coalesce(self) -> None:
        """Run the record refresh once the join/leave burst window elapsed."""
        with self._refresh_lock:
            self._refresh_timer = None
            if self._service_info is None:
                return
        self._refresh_service(server_ready=self._server_ready, endpoint_ready=self._endpoint_ready)

    def _heartbeat(self) -> None:
        """Re-announce the mDNS record with an incremented liveness counter.

        zeroconf only re-delivers a TXT record when its *value* changes, so a
        periodically incrementing counter is the only way to make a quiescent
        peer's ``last_seen`` stay fresh on every other node. Without it, idle
        slaves (whose record never otherwise changes) go stale and are falsely
        demoted during master failover — a split-brain.
        """
        with self._record_lock:
            if not self._zeroconf or not self._service_info or not self._running:
                return
            self._heartbeat_counter += 1
            new_info = self._build_service_info(
                server_ready=self._server_ready,
                endpoint_ready=self._endpoint_ready,
            )
            new_info.set_server_if_missing()
            self._zeroconf.update_service(new_info)
            self._service_info = new_info

    def _heartbeat_loop(self) -> None:
        """Background loop that emits the liveness heartbeat at a fixed cadence."""
        while not self._heartbeat_stop.wait(self._heartbeat_interval):
            try:
                self._heartbeat()
            except Exception:
                log.exception("Heartbeat re-announce failed")

    def stop(self) -> None:
        """Stop P2P discovery and unregister service."""
        if not self._running:
            return

        log.info("Stopping P2P mode")

        # Stop the heartbeat loop first and wait for a possibly in-flight
        # re-announce (bounded by the zeroconf update_service timeout) so it
        # cannot race the unregister/close below.
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2.0)
            self._heartbeat_thread = None

        # Cancel any pending debounced refresh before touching Zeroconf.
        with self._refresh_lock:
            if self._refresh_timer is not None:
                self._refresh_timer.cancel()
                self._refresh_timer = None

        # Unregister our service. The record lock excludes any in-flight
        # publication (heartbeat/refresh) so the unregister never races an
        # ``update_service``; a queued refresh that resumes afterwards sees
        # ``_running = False`` and bails out.
        with self._record_lock:
            self._running = False
            if self._zeroconf and self._service_info:
                self._zeroconf.unregister_service(self._service_info)
                log.debug("Service unregistered")

            # Close zeroconf
            if self._zeroconf:
                self._zeroconf.close()

        log.info("P2P mode stopped")

    def _register_service(self, server_ready: bool = False) -> None:
        """Register this client as an mDNS service.

        Args:
            server_ready: If True, indicates the HTTP server is listening.
        """
        with self._record_lock:
            if not self._zeroconf:
                return

            self._service_info = self._build_service_info(server_ready=server_ready)

            # Register the service
            try:
                self._zeroconf.register_service(self._service_info)
            except NonUniqueNameException:
                # The service name is "{bucket}-{device_id}": a clash means another
                # process presenting THIS device identity already announces THIS
                # bucket on the network — almost always a second instance on this
                # host sharing the same device directory.
                raise RuntimeError(
                    f"mDNS name clash: another hsync instance with the same device "
                    f"identity already announces bucket '{self._bucket}' on this host. "
                    "Instances syncing the same bucket on one host must run as distinct "
                    "devices: set HSYNC_DEVICE_DIR to a separate directory for this "
                    "instance. (Instances syncing different buckets can share one "
                    "device identity.)"
                ) from None
            log.info(
                f"Registered mDNS service: {self._service_info.name} at {self._local_ip}:{self._port}"
            )

    def _build_service_info(
        self,
        server_ready: bool = False,
        endpoint_ready: bool = False,
    ) -> ServiceInfo:
        """Build the :class:`ServiceInfo` for this client's current TXT state.

        Shared by :meth:`_register_service` (initial announce) and
        :meth:`_refresh_service` (in-place update), so both paths advertise the
        exact same property set for a given ``server_ready`` value.
        """
        # Get local IP address (computed once in __init__ so discovery's
        # self-check compares against the exact address this record advertises)
        local_ip = self._local_ip or get_local_ip()

        # Create service name: bucket-cid
        service_name = f"{self._bucket}-{self._cid}.{HSYNC_SERVICE_TYPE}"

        # Create TXT record properties
        properties: dict[str, str] = {
            "cid": self._cid,
            "bucket": self._bucket,
            "version": HSYNC_VERSION,
            "protocol": PROTOCOL_VERSION_P2P,
        }
        # Add group if specified
        if self._group:
            properties["group"] = self._group
        # Endpoint material is advertised only while a listener is actually
        # bound: the master's HTTP server or a slave's read-only seeder. A
        # discovery-only node must never claim a reachable TLS endpoint.
        advertise_endpoint = server_ready or endpoint_ready
        if advertise_endpoint and self._use_https:
            properties["https"] = "true"
            if self._cert_fingerprint:
                properties["cert_fp"] = self._cert_fingerprint
        # Add compression capability
        if CONFIG.get("COMPRESSION_ENABLED", False):
            properties["compression"] = "true"
        # Add delta transfer capability
        if CONFIG.get("DELTA_ENABLED", False):
            properties["delta"] = "true"
        if CONFIG.get("P2P_PEER_DOWNLOAD", True):
            properties["peer_download"] = "true"
        # Add server ready flag
        if server_ready:
            properties["server_ready"] = "true"
        # Add prefer_master flag for master election
        if self._prefer_master:
            properties["prefer_master"] = "true"
        # Add never_master flag so other peers never elect this node
        if self._never_master:
            properties["never_master"] = "true"
        # Add startup time for master election (earlier startup wins)
        properties["startup_time"] = str(self._startup_time)
        # Add sync direction for master election (bidirectional preferred)
        properties["sync_direction"] = self._sync_direction
        # Liveness heartbeat: opaque monotonic counter, re-announced every
        # HEARTBEAT_INTERVAL. Receivers only rely on it *changing* so a
        # quiescent node's record is never falsely expired. Never part of the
        # election.
        properties["heartbeat"] = str(self._heartbeat_counter)
        # Add the connected-clients count once this node serves as master.
        # Diagnostic only: slaves mirror it for presence display, it never
        # participates in the election.
        if server_ready:
            properties["connected"] = str(self._connected_clients)

        return ServiceInfo(
            HSYNC_SERVICE_TYPE,
            service_name,
            port=self._port,
            properties=properties,
            addresses=[socket.inet_aton(local_ip)],
        )

    def wait_for_discovery(self, timeout: float = DISCOVERY_TIMEOUT) -> None:
        """
        Wait for peer discovery to complete.

        The discovery loop calls this on every iteration, so the status
        messages are emitted only when they carry new information: the
        "waiting" notice is announced once at the start of the countdown, and
        the discovery result is logged only when the alive-peer count changes.
        Steady-state polling stays silent (downgraded to ``debug``).

        Args:
            timeout: Time to wait for this discovery polling window in seconds.
        """
        if not self._discovery_announced:
            log.info(f"Waiting {timeout}s for peer discovery...")
            self._discovery_announced = True
        else:
            log.debug(f"Waiting {timeout}s for peer discovery...")
        # Wake on a discovery change (peer added/updated/removed, local
        # server_ready flip) instead of sleeping the full window: a peer that
        # is already advertising is detected in mDNS time, not after ``timeout``.
        if self._peer_manager is not None:
            self._peer_manager.wait_for_change(timeout)
        else:
            time.sleep(timeout)

        self.emit_discovery_status()

    def emit_discovery_status(self) -> None:
        """Emit the presence pair and the peer-count log for the last window.

        Called from :meth:`wait_for_discovery`, and by the combined discovery
        plan after it waits on the shared peer manager, so the presence pair is
        reported exactly once.
        """
        if not self._peer_manager:
            return
        peers = self._peer_manager.get_alive_peers()
        total = 1 + len(peers)

        # Connected count: while this node serves as master, the provider
        # installed by the P2P handler gives the live WebSocket client
        # count. As a slave, the master's published count (or the local
        # snapshot) is the shared group value.
        if self._connected_clients_provider is not None:
            self._connected_clients = max(self._connected_clients_provider(), 0)
        elif self._peer_manager.get_master_cid() != self._cid:
            master_peer = self._peer_manager.get_master()
            if master_peer is not None:
                self._connected_clients = slave_connected_count(self._peer_manager, master_peer)
        connected = 1 + self._connected_clients
        # Guard the display invariant: a stale mDNS count can exceed the
        # currently-known total, so connected is clamped to total.
        connected = min(connected, total)

        changed = (self._connected_clients, total) != self._last_discovery_state
        self._last_discovery_state = (self._connected_clients, total)

        record_presence(connected, total)

        if peers:
            if changed:
                log.info(f"Discovered {len(peers)} peer(s)")
            for peer in peers:
                log.debug(f"  - {peer.cid} at {peer.address}:{peer.port}")
        elif changed:
            if self._connected_clients > 0:
                log.info(
                    f"No other peers discovered - {self._connected_clients} client(s) connected"
                )
            else:
                log.info("No other peers discovered - this client is alone")
