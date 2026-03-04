# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
LAN Peer-to-Peer Synchronization Module.

This module provides peer discovery via mDNS (Bonjour/Avahi),
master election, and coordination for LAN-based file synchronization
without a central server.

The approach uses a leader election model where:
1. Peers discover each other via mDNS service advertising
2. The peer with the lowest CID (lexicographically) becomes master
3. The master acts as the sync coordinator (like a server)
4. Other peers sync with the master as clients
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from hsync_common.protocol.handshake import PROTOCOL_VERSION_LAN
from hsync_common.protocol.lan import (
    SYNC_DIRECTION_BIDIRECTIONAL,
    ElectionCandidate,
    election_sort_key,
)
from loguru import logger as log
from zeroconf import NonUniqueNameException, ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf

from hsync_client.config import CONFIG
from hsync_client.constants import (
    DISCOVERY_TIMEOUT_DEFAULT,
    HSYNC_VERSION,
    PEER_TIMEOUT,
    ActionLabel,
)
from hsync_client.events import UiEventName, emit_event
from hsync_client.presence import record_presence

# =============================================================================
# Constants
# =============================================================================

# mDNS service type for hsync
HSYNC_SERVICE_TYPE = "_hsync._tcp.local."

# Default port for the embedded HTTP server (when acting as master)
# 0 means "use a random available port"
HSYNC_LAN_PORT = 0

# Discovery timeout in seconds
DISCOVERY_TIMEOUT = DISCOVERY_TIMEOUT_DEFAULT

# Join/leave events can arrive in bursts (e.g. a slave reconnecting): instead
# of re-registering the mDNS TXT record per event, a single refresh runs this
# many seconds after the last change, so peers see a converged ``connected``
# count without mDNS churn.
PRESENCE_REFRESH_COALESCE_S = 2.0


# =============================================================================
# Helper Functions
# =============================================================================


def get_free_port() -> int:
    """
    Find a free port on the local machine.

    Uses the OS to allocate a random available port by binding to port 0.

    Returns:
        An available port number.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.listen(1)
        port = s.getsockname()[1]
    return port


# =============================================================================
# Data Classes
# =============================================================================


def _tx_bool(properties: Mapping[bytes, bytes | None], key: bytes) -> bool:
    """Read a TXT boolean flag (present-only-when-true) from mDNS properties.

    Per the lan-protocol.md rule, boolean TXT flags are advertised only when
    true, so an absent/empty key means false.
    """
    raw = properties.get(key, b"")
    return raw.decode("utf-8").lower() == "true" if raw else False


@dataclass
class Peer:
    """Represents a discovered peer on the LAN."""

    cid: str
    bucket: str
    address: str
    port: int
    version: str = "0.0.0"
    group: str | None = None
    use_https: bool = False
    cert_fingerprint: str | None = None
    compression_enabled: bool = False
    delta_enabled: bool = False  # rsync-style delta transfer capability
    server_ready: bool = False  # True when HTTP server is listening
    connected: int = 0  # slaves currently connected to the master's WebSocket
    last_seen: float = field(default_factory=time.time)
    force_master: bool = False  # True if peer wants to force master role
    never_master: bool = False  # True if peer refuses to ever be master
    startup_time: float = 0.0  # Unix timestamp when peer started
    sync_direction: str = "bidirectional"  # bidirectional | upload-only | download-only

    def is_alive(self) -> bool:
        """Check if peer is still considered alive based on last_seen time."""
        return (time.time() - self.last_seen) < PEER_TIMEOUT

    def update_last_seen(self) -> None:
        """Update the last seen timestamp."""
        self.last_seen = time.time()

    def get_server_url(self) -> str:
        """Get the HTTP/HTTPS server URL for this peer."""
        protocol = "https" if self.use_https else "http"
        return f"{protocol}://{self.address}:{self.port}"


# =============================================================================
# Peer Manager
# =============================================================================


class PeerManager:
    """
    Manages discovered peers and master election.

    Thread-safe peer tracking with automatic master election.

    Election priority (highest to lowest):
    1. force_master flag (if set, that peer wins)
    2. Bidirectional sync_direction preference (bidirectional wins)
    3. Earliest startup_time (first started wins)
    4. Lowest CID lexicographically (tiebreaker)
    """

    def __init__(
        self,
        local_cid: str,
        bucket: str,
        group: str | None = None,
        force_master: bool = False,
        never_master: bool = False,
        startup_time: float = 0.0,
        sync_direction: str = "bidirectional",
        local_cert_fingerprint: str | None = None,
        local_address: str | None = None,
    ) -> None:
        """
        Initialize the peer manager.

        Args:
            local_cid: This client's CID.
            bucket: The bucket name for filtering peers.
            group: Optional group name for filtering peers.
            force_master: If True, this client will always try to be master.
            never_master: If True, this client is excluded from master election
                and always remains a client/slave.
            startup_time: Unix timestamp when this client started.
            sync_direction: This client's sync direction
                ("bidirectional", "upload-only", "download-only").
            local_cert_fingerprint: This node's own certificate fingerprint
                (SHA-256), used to tell a duplicated device identity (same CID,
                different cert) apart from a legitimate self-discovery.
            local_address: The IP address this node advertises over mDNS. A
                peer that presents our own CID from a different address is a
                different host cloning our device identity.
        """
        self._local_cid = local_cid
        self._bucket = bucket
        self._group = group
        self._force_master = force_master
        self._never_master = never_master
        self._startup_time = startup_time if startup_time > 0 else time.time()
        self._sync_direction = sync_direction
        self._local_cert_fingerprint = local_cert_fingerprint
        self._local_address = local_address
        self._peers: dict[str, Peer] = {}
        self._lock = threading.Lock()
        # Dedup guard for the duplicate-identity warning: mDNS heartbeats
        # re-deliver the same foreign record every few seconds, so the error is
        # reported once per distinct (address, fingerprint) signature.
        self._duplicate_identity_seen: set[str] = set()
        self._master_cid: str | None = None
        self._local_server_ready = False
        self._on_master_change: Callable[[Peer | None], None] | None = None
        # Generation + Condition: waiters observe a monotonic counter so a
        # change that arrives between wait-return and the next wait is not
        # lost (unlike Event.wait/clear). Heartbeat-only last_seen bumps do
        # not increment the generation.
        self._change_cond = threading.Condition(self._lock)
        self._change_generation = 0
        self._seen_generation = 0

    def set_local_server_ready(self, ready: bool) -> None:
        """Mark the local node as actively serving (incumbent master).

        A node that is already serving its bucket wins master election over
        newcomers (even ``force_master`` ones), preventing takeover and
        split-brain for the same bucket.
        """
        with self._lock:
            if self._local_server_ready == ready:
                return
            self._local_server_ready = ready
            self._elect_master()
            self._signal_change()

    @property
    def local_cid(self) -> str:
        """Get this client's CID."""
        return self._local_cid

    @property
    def bucket(self) -> str:
        """Get the bucket name."""
        return self._bucket

    @property
    def group(self) -> str | None:
        """Get the group name."""
        return self._group

    @property
    def force_master(self) -> bool:
        """Get the force_master flag."""
        return self._force_master

    @property
    def never_master(self) -> bool:
        """Get the never_master flag."""
        return self._never_master

    @property
    def startup_time(self) -> float:
        """Get the startup timestamp."""
        return self._startup_time

    def _is_group_compatible(self, peer_group: str | None) -> bool:
        """
        Check if a peer's group is compatible with this client's group.

        The group is a strict partition key: a peer is compatible only when
        its group is exactly equal to this client's group. "No group"
        (``None``) is itself a distinct partition, so a peer without a group
        matches only other peers without a group.

        Args:
            peer_group: The peer's group (or None if not set).

        Returns:
            True if the peer is compatible, False otherwise.
        """
        return self._group == peer_group

    def add_peer(self, peer: Peer) -> None:
        """Add or update a discovered peer."""
        if peer.bucket != self._bucket:
            log.debug(f"Ignoring peer {peer.cid} - different bucket: {peer.bucket}")
            return

        if not self._is_group_compatible(peer.group):
            log.debug(
                f"Ignoring peer {peer.cid} - different group: {peer.group} (local: {self._group})"
            )
            return

        if peer.cid == self._local_cid:
            # A peer presenting our own CID is either our own mDNS record
            # looped back (legitimate self-discovery) or a different host that
            # cloned this installation's device identity. Distinguish the two
            # and fail loudly on the latter: two live nodes sharing a CID would
            # corrupt the causal vector clocks and tombstone attribution.
            if self._is_foreign_duplicate(peer):
                self._report_duplicate_identity(peer)
            else:
                log.debug("Ignoring self from peer discovery")
            return

        with self._lock:
            existing = self._peers.get(peer.cid)
            if existing is not None:
                # Only an election-relevant change can alter the master choice;
                # a pure mDNS heartbeat refresh (last_seen bump) must not pay
                # for a full candidate scan + sort on every ~5s update. An
                # expired peer is still excluded on the next election-triggering
                # event (remove_service or a relevant field change) — identical
                # master outcome, far less work.
                election_changed = (
                    existing.server_ready != peer.server_ready
                    or existing.force_master != peer.force_master
                    or existing.never_master != peer.never_master
                    or existing.startup_time != peer.startup_time
                    or existing.sync_direction != peer.sync_direction
                )
                discovery_changed = election_changed or (
                    existing.address != peer.address
                    or existing.port != peer.port
                    or existing.use_https != peer.use_https
                    or existing.cert_fingerprint != peer.cert_fingerprint
                    or existing.connected != peer.connected
                    or existing.compression_enabled != peer.compression_enabled
                    or existing.delta_enabled != peer.delta_enabled
                )
                # Update all properties from the new peer object
                existing.address = peer.address
                existing.port = peer.port
                existing.version = peer.version
                existing.group = peer.group
                existing.use_https = peer.use_https
                existing.cert_fingerprint = peer.cert_fingerprint
                existing.compression_enabled = peer.compression_enabled
                existing.delta_enabled = peer.delta_enabled
                if not existing.server_ready and peer.server_ready:
                    log.info(f"Peer {peer.cid} server is now ready")
                existing.server_ready = peer.server_ready
                existing.connected = peer.connected
                existing.force_master = peer.force_master
                existing.never_master = peer.never_master
                existing.startup_time = peer.startup_time
                existing.sync_direction = peer.sync_direction
                existing.update_last_seen()
                log.debug(f"Updated peer: {peer.cid}")
            else:
                self._peers[peer.cid] = peer
                log.info(f"Discovered new peer: {peer.cid} at {peer.address}:{peer.port}")
                election_changed = True
                discovery_changed = True

            # Re-run the election on a first discovery or a relevant field
            # change. The expired-peer check also covers the original
            # behaviour where a stale peer (whose heartbeat stop arriving)
            # was excluded from the candidate list at the next update.
            stale_peer = any(not p.is_alive() for p in self._peers.values())
            if election_changed or stale_peer:
                self._elect_master()
            if discovery_changed or stale_peer:
                self._signal_change()

    def _is_foreign_duplicate(self, peer: Peer) -> bool:
        """Return True when *peer* presents our CID but is a different host.

        A peer advertising our own CID is only "self" when it comes from the
        address this node itself announces (our own mDNS record looped back).
        Any other address is a different host running with a cloned device
        identity (a copied ``HSYNC_DEVICE_DIR``). When no address hint is
        available, a differing certificate fingerprint for the same CID is
        still conclusive: our own record always carries our own fingerprint.
        """
        different_address = (
            self._local_address is not None
            and bool(peer.address)
            and peer.address != self._local_address
        )
        different_fingerprint = (
            self._local_cert_fingerprint is not None
            and peer.cert_fingerprint is not None
            and peer.cert_fingerprint != self._local_cert_fingerprint
        )
        return different_address or different_fingerprint

    def _report_duplicate_identity(self, peer: Peer) -> None:
        """Log (once per signature) a duplicated device identity and ignore it."""
        signature = f"{peer.address or '?'}:{peer.cert_fingerprint or '?'}"
        with self._lock:
            if signature in self._duplicate_identity_seen:
                return
            self._duplicate_identity_seen.add(signature)
        log.error(
            f"{ActionLabel.SECURITY} Duplicate device identity detected: a peer at "
            f"{peer.address}:{peer.port} presents this client's CID "
            f"({self._local_cid}). Refusing to sync with it. Each machine must "
            f"run with its own device identity: do not copy HSYNC_DEVICE_DIR "
            f"between hosts, and give each instance a separate HSYNC_DEVICE_DIR."
        )

    def remove_peer(self, cid: str) -> None:
        """Remove a peer by CID."""
        with self._lock:
            if cid in self._peers:
                del self._peers[cid]
                log.info(f"Peer disconnected: {cid}")
                self._elect_master()
                self._signal_change()

    def get_alive_peers(self) -> list[Peer]:
        """Get all peers that are still alive."""
        with self._lock:
            return [p for p in self._peers.values() if p.is_alive()]

    def _signal_change(self) -> None:
        """Bump the discovery generation and wake waiters. Caller holds ``_lock``."""
        self._change_generation += 1
        self._change_cond.notify_all()

    def wait_for_change(self, timeout: float) -> None:
        """Block until a discovery change is signalled or *timeout* elapses.

        The discovery loop waits on this instead of a fixed sleep: a peer
        discovered (or a master flipping to ``server_ready``) while waiting
        wakes the loop immediately, so startup detection and master readiness
        react in mDNS time (~1 s) rather than after the full polling window.
        A pending generation from before this call is consumed immediately
        (no lost wakeup). Heartbeat-only ``last_seen`` refreshes do not signal.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            while self._change_generation == self._seen_generation:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._change_cond.wait(remaining)
            self._seen_generation = self._change_generation

    def _elect_master(self) -> None:
        """
        Elect the master peer.

        Election priority (highest to lowest):
        1. server_ready flag (an already-serving incumbent master wins;
           prevents takeover and split-brain for the same bucket)
        2. force_master flag (if set, that peer wins)
        3. Bidirectional sync_direction preference (bidirectional wins)
        4. Earliest startup_time (first started wins)
        5. Lowest CID lexicographically (tiebreaker)

        This includes the local client in the election, unless it (or a peer)
        is marked ``never_master`` — such nodes are excluded from candidacy and
        always remain clients/slaves. If every node is ``never_master`` the
        candidate list is empty and no master is elected.
        """
        # Build the candidate list (local node + every alive peer) and delegate
        # the ordering to the shared, cross-language algorithm in hsync_common so
        # the Python client and Android never diverge. ``never_master`` nodes are
        # carried through and filtered by the shared sort key.
        candidates: list[ElectionCandidate] = [
            ElectionCandidate(
                cid=self._local_cid,
                force_master=self._force_master,
                startup_time=self._startup_time,
                sync_direction=self._sync_direction,
                server_ready=self._local_server_ready,
                never_master=self._never_master,
            )
        ]
        for peer in self._peers.values():
            if peer.is_alive():
                candidates.append(
                    ElectionCandidate(
                        cid=peer.cid,
                        force_master=peer.force_master,
                        startup_time=peer.startup_time,
                        sync_direction=peer.sync_direction,
                        server_ready=peer.server_ready,
                        never_master=peer.never_master,
                    )
                )

        eligible = sorted((c for c in candidates if not c.never_master), key=election_sort_key)
        new_master_cid = eligible[0].cid if eligible else None

        # Detect force_master conflicts
        force_master_candidates = [c for c in eligible if c.force_master]
        if len(force_master_candidates) > 1:
            winner_cid = force_master_candidates[0].cid
            for loser in force_master_candidates[1:]:
                loser_cid = loser.cid
                loser_peer = self._peers.get(loser_cid)
                winner_peer = self._peers.get(winner_cid)
                if loser_cid == self._local_cid:
                    # We lost: another force_master peer won
                    winner_addr = winner_peer.address if winner_peer else "unknown"
                    log.warning(
                        f"{ActionLabel.MASTER_CONFLICT} force_master conflict: "
                        f"another master {winner_cid} at {winner_addr} "
                        f"has priority (started earlier). "
                        f"This client will operate as client."
                    )
                    emit_event(
                        UiEventName.master_conflict,
                        conflict_type="conflict_lost",
                        peer_cid=winner_cid,
                        peer_address=winner_addr,
                    )
                elif winner_cid == self._local_cid:
                    # We won: a remote force_master peer lost
                    loser_addr = loser_peer.address if loser_peer else "unknown"
                    log.warning(
                        f"{ActionLabel.MASTER_CONFLICT} force_master conflict: "
                        f"peer {loser_cid} at {loser_addr} also has "
                        f"force_master enabled. This client wins "
                        f"(started earlier)."
                    )
                    emit_event(
                        UiEventName.master_conflict,
                        conflict_type="conflict_won",
                        peer_cid=loser_cid,
                        peer_address=loser_addr,
                    )

        # Detect force_master takeover: remote force_master peer overrides
        # local non-force_master master
        if (
            new_master_cid is not None
            and new_master_cid != self._local_cid
            and not self._force_master
            and self._master_cid == self._local_cid
        ):
            new_peer = self._peers.get(new_master_cid)
            if new_peer and new_peer.force_master:
                log.warning(
                    f"{ActionLabel.MASTER_TAKEOVER} peer {new_master_cid} at "
                    f"{new_peer.address} has force_master enabled "
                    f"and is taking over as master. "
                    f"This client will switch to client."
                )
                emit_event(
                    UiEventName.master_conflict,
                    conflict_type="takeover",
                    peer_cid=new_master_cid,
                    peer_address=new_peer.address,
                )

        if new_master_cid != self._master_cid:
            old_master = self._master_cid
            self._master_cid = new_master_cid

            if new_master_cid == self._local_cid:
                if self._force_master:
                    reason = "force_master"
                elif self._sync_direction == SYNC_DIRECTION_BIDIRECTIONAL:
                    reason = "bidirectional preference"
                else:
                    reason = "earliest startup"
                log.info(
                    f"This client elected as MASTER (CID: {self._local_cid}, reason: {reason})"
                )
                if self._sync_direction != SYNC_DIRECTION_BIDIRECTIONAL:
                    log.warning(
                        f"This client is master with "
                        f"sync_direction={self._sync_direction}. "
                        f"This may limit synchronization capabilities "
                        f"for other peers."
                    )
            else:
                master_peer = self._peers.get(new_master_cid) if new_master_cid else None
                if master_peer:
                    if master_peer.force_master:
                        reason = "force_master"
                    elif master_peer.sync_direction == SYNC_DIRECTION_BIDIRECTIONAL:
                        reason = "bidirectional preference"
                    else:
                        reason = "earliest startup"
                    if master_peer.sync_direction != SYNC_DIRECTION_BIDIRECTIONAL:
                        log.warning(
                            f"Master {new_master_cid} has "
                            f"sync_direction={master_peer.sync_direction}. "
                            f"This may limit synchronization capabilities."
                        )
                else:
                    reason = "unknown"
                log.info(
                    f"Master changed: {old_master} -> {new_master_cid} "
                    f"(at {master_peer.address if master_peer else 'unknown'}, "
                    f"reason: {reason})"
                )

            # Notify callback
            emit_event(UiEventName.role, is_master=(new_master_cid == self._local_cid))
            if self._on_master_change:
                if new_master_cid == self._local_cid or new_master_cid is None:
                    master_peer = None
                else:
                    master_peer = self._peers.get(new_master_cid)
                self._on_master_change(master_peer)

        # NOTE: the ``peers`` event is emitted from the discovery loop
        # (``LANSync.wait_for_discovery``), the single owner of the
        # display-ready connected/total pair for both roles.

    def is_master(self) -> bool:
        """Check if this client is the current master."""
        with self._lock:
            return self._master_cid == self._local_cid

    def get_master(self) -> Peer | None:
        """Get the current master peer (None if local is master)."""
        with self._lock:
            if self._master_cid == self._local_cid or self._master_cid is None:
                return None
            return self._peers.get(self._master_cid)

    def get_master_cid(self) -> str | None:
        """Get the current master's CID."""
        with self._lock:
            return self._master_cid


# =============================================================================
# mDNS Service Listener
# =============================================================================


class HsyncServiceListener(ServiceListener):
    """
    Listens for hsync service announcements via mDNS.

    Discovers other hsync clients on the local network and
    registers them with the PeerManager.
    """

    def __init__(self, peer_manager: PeerManager) -> None:
        """
        Initialize the service listener.

        Args:
            peer_manager: PeerManager to register discovered peers.
        """
        self._peer_manager = peer_manager

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
            # Optional transfer capabilities (advertised only when enabled)
            compression_enabled = _tx_bool(properties, b"compression")
            delta_enabled = _tx_bool(properties, b"delta")
            # Server ready flag (indicates HTTP server is listening)
            server_ready = _tx_bool(properties, b"server_ready")
            # Force master flag (for master election)
            force_master = _tx_bool(properties, b"force_master")
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
            if connected < 0:
                connected = 0

            if not cid or not bucket:
                log.debug(f"Incomplete service info: cid={cid}, bucket={bucket}")
                return

            # Enforce the wire-protocol version: peers on a different protocol
            # are ignored outright (capabilities never bridge protocol versions).
            if protocol != PROTOCOL_VERSION_LAN:
                log.warning(
                    f"Ignoring peer {cid} with incompatible protocol "
                    f"{protocol or 'missing'} (local: {PROTOCOL_VERSION_LAN})"
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
                server_ready=server_ready,
                connected=connected,
                force_master=force_master,
                never_master=never_master,
                startup_time=startup_time,
                sync_direction=sync_direction,
            )
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
                    self._peer_manager.remove_peer(cid)
        except Exception as e:
            log.debug(f"Error parsing service name on removal: {e}")


# =============================================================================
# LAN Sync Coordinator
# =============================================================================


class LANSync:
    """
    Coordinates LAN-based peer-to-peer synchronization.

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
        port: int = HSYNC_LAN_PORT,
        group: str | None = None,
        use_https: bool = False,
        cert_fingerprint: str | None = None,
        force_master: bool = False,
        never_master: bool = False,
        startup_time: float = 0.0,
        sync_direction: str = "bidirectional",
    ) -> None:
        """
        Initialize LAN sync coordinator.

        Args:
            cid: This client's unique identifier.
            bucket: The bucket name to sync.
            port: Port to use when acting as master. Use 0 for random port.
            group: Optional group name for peer filtering.
            use_https: Whether to use HTTPS for communication.
            cert_fingerprint: SHA-256 fingerprint of the SSL certificate (if HTTPS).
            force_master: If True, this client will always try to be master.
            startup_time: Unix timestamp when this client started.
            sync_direction: This client's sync direction
                ("bidirectional", "upload-only", "download-only").
        """
        self._cid = cid
        self._bucket = bucket
        self._group = group
        self._use_https = use_https
        self._cert_fingerprint = cert_fingerprint
        self._force_master = force_master
        self._never_master = never_master
        self._startup_time = startup_time if startup_time > 0 else time.time()
        self._sync_direction = sync_direction
        # If port is 0, get a random free port
        self._port = port if port != 0 else get_free_port()
        # The IP this node advertises over mDNS, computed once so the advertised
        # address and the discovery self-check compare against the same value.
        self._local_ip = self._get_local_ip()

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
        # provider that exposes it (installed by the LAN handler once the
        # WebSocket server is running).
        self._connected_clients: int = 0
        self._connected_clients_provider: Callable[[], int] | None = None

        # Debounced record refresh: the TXT ``connected`` count changes on
        # every slave join/leave, which can happen in bursts. Only the last
        # change per coalescing window re-registers the record.
        self._refresh_lock = threading.Lock()
        self._refresh_timer: threading.Timer | None = None

    @property
    def port(self) -> int:
        """Get the port being used (useful when random port was requested)."""
        return self._port

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

    @property
    def peer_manager(self) -> PeerManager | None:
        """Get the peer manager."""
        return self._peer_manager

    def start(self, register_service: bool = True) -> PeerManager:
        """
        Start LAN discovery and optionally register service.

        Args:
            register_service: If True, register mDNS service immediately.
                If False, call register_service() later (e.g., after starting
                the HTTP server when becoming master).

        Returns:
            PeerManager for monitoring discovered peers and master election.
        """
        group_info = f", group='{self._group}'" if self._group else ""
        log.info(f"Starting LAN mode for bucket '{self._bucket}' (CID: {self._cid}{group_info})")

        # Initialize Zeroconf
        self._zeroconf = Zeroconf()

        # Initialize peer manager
        self._peer_manager = PeerManager(
            self._cid,
            self._bucket,
            self._group,
            force_master=self._force_master,
            never_master=self._never_master,
            startup_time=self._startup_time,
            sync_direction=self._sync_direction,
            local_cert_fingerprint=self._cert_fingerprint,
            local_address=self._local_ip,
        )

        # Register our service (can be deferred if register_service=False)
        if register_service:
            self._register_service()

        # Start listening for other services
        listener = HsyncServiceListener(self._peer_manager)
        self._browser = ServiceBrowser(self._zeroconf, HSYNC_SERVICE_TYPE, listener)

        self._running = True
        log.info("LAN discovery started")

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

    def update_server_ready(self) -> None:
        """
        Update the mDNS service to indicate the server is ready.

        Call this after starting the HTTP server when becoming master.
        This re-registers the service with server_ready=true.
        """
        self._refresh_service(server_ready=True)
        log.info("Updated mDNS service: server_ready=true")

    def _refresh_service(self, server_ready: bool) -> None:
        """Unregister and re-register the mDNS service with fresh TXT data.

        Shared by :meth:`update_server_ready` (server just started) and
        :meth:`set_connected_clients` (presence count changed). Re-registration
        also marks this node as the incumbent master so the election never
        hands the role to a newcomer (prevents takeover/split-brain).
        """
        if not self._zeroconf or not self._service_info:
            log.warning("Cannot update service: not registered")
            return

        # Unregister old service
        self._zeroconf.unregister_service(self._service_info)
        self._service_info = None

        # Re-register with the fresh record.
        self._register_service(server_ready=server_ready)
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
        if count < 0:
            count = 0
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
        self._refresh_service(server_ready=True)

    def set_connected_clients_provider(self, provider: Callable[[], int] | None) -> None:
        """Install (or clear) the callable that returns the live connected count.

        The LAN handler installs the master WebSocket server's counter while
        this node serves as master, and clears it when the role is lost, so
        :meth:`wait_for_discovery` always reads the count from the correct
        source for the current role.
        """
        self._connected_clients_provider = provider

    def stop(self) -> None:
        """Stop LAN discovery and unregister service."""
        if not self._running:
            return

        log.info("Stopping LAN mode")

        # Cancel any pending debounced refresh before touching Zeroconf.
        with self._refresh_lock:
            if self._refresh_timer is not None:
                self._refresh_timer.cancel()
                self._refresh_timer = None

        # Unregister our service
        if self._zeroconf and self._service_info:
            self._zeroconf.unregister_service(self._service_info)
            log.debug("Service unregistered")

        # Close zeroconf
        if self._zeroconf:
            self._zeroconf.close()

        self._running = False
        log.info("LAN mode stopped")

    def _register_service(self, server_ready: bool = False) -> None:
        """Register this client as an mDNS service.

        Args:
            server_ready: If True, indicates the HTTP server is listening.
        """
        if not self._zeroconf:
            return

        # Get local IP address (computed once in __init__ so discovery's
        # self-check compares against the exact address this record advertises)
        local_ip = self._local_ip

        # Create service name: bucket-cid
        service_name = f"{self._bucket}-{self._cid}.{HSYNC_SERVICE_TYPE}"

        # Create TXT record properties
        properties: dict[str, str] = {
            "cid": self._cid,
            "bucket": self._bucket,
            "version": HSYNC_VERSION,
            "protocol": PROTOCOL_VERSION_LAN,
        }
        # Add group if specified
        if self._group:
            properties["group"] = self._group
        # Add HTTPS info if enabled
        if self._use_https:
            properties["https"] = "true"
            if self._cert_fingerprint:
                properties["cert_fp"] = self._cert_fingerprint
        # Add compression capability
        if CONFIG.get("COMPRESSION_ENABLED", False):
            properties["compression"] = "true"
        # Add delta transfer capability
        if CONFIG.get("DELTA_ENABLED", False):
            properties["delta"] = "true"
        # Add server ready flag
        if server_ready:
            properties["server_ready"] = "true"
        # Add force_master flag for master election
        if self._force_master:
            properties["force_master"] = "true"
        # Add never_master flag so other peers never elect this node
        if self._never_master:
            properties["never_master"] = "true"
        # Add startup time for master election (earlier startup wins)
        properties["startup_time"] = str(self._startup_time)
        # Add sync direction for master election (bidirectional preferred)
        properties["sync_direction"] = self._sync_direction
        # Add the connected-clients count once this node serves as master.
        # Diagnostic only: slaves mirror it for presence display, it never
        # participates in the election.
        if server_ready:
            properties["connected"] = str(self._connected_clients)

        # Create service info
        self._service_info = ServiceInfo(
            HSYNC_SERVICE_TYPE,
            service_name,
            port=self._port,
            properties=properties,
            addresses=[socket.inet_aton(local_ip)],
        )

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
        log.info(f"Registered mDNS service: {service_name} at {local_ip}:{self._port}")

    def _get_local_ip(self) -> str:
        """Get the local IP address for LAN communication."""
        try:
            # Create a socket to determine the local IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0)
            try:
                # Connect to a non-routable address to get local interface
                s.connect(("10.254.254.254", 1))
                local_ip = s.getsockname()[0]
            except Exception:
                local_ip = "127.0.0.1"
            finally:
                s.close()
            return local_ip
        except Exception as e:
            log.warning(f"Could not determine local IP: {e}, using 127.0.0.1")
            return "127.0.0.1"

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

        if self._peer_manager:
            peers = self._peer_manager.get_alive_peers()
            total = 1 + len(peers)

            # Connected count: while this node serves as master, the provider
            # installed by the LAN handler gives the live WebSocket client
            # count. As a slave, the master's published count (or the local
            # snapshot) is the shared group value.
            if self._connected_clients_provider is not None:
                self._connected_clients = max(self._connected_clients_provider(), 0)
            elif self._peer_manager.get_master_cid() != self._cid:
                master_peer = self._peer_manager.get_master()
                if master_peer is not None:
                    self._connected_clients = master_peer.connected
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
