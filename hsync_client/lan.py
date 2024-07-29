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
from collections.abc import Callable
from dataclasses import dataclass, field

# CLOCK_SKEW_THRESHOLD is re-exported here for backwards compatibility; its
# canonical home is hsync_common.clock.
from hsync_common.clock import CLOCK_SKEW_THRESHOLD  # noqa: F401
from loguru import logger as log
from zeroconf import ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf

from hsync_client.config import CONFIG
from hsync_client.constants import HSYNC_VERSION

# =============================================================================
# Constants
# =============================================================================

# mDNS service type for hsync
HSYNC_SERVICE_TYPE = "_hsync._tcp.local."

# Default port for the embedded HTTP server (when acting as master)
# 0 means "use a random available port"
HSYNC_LAN_PORT = 0

# Discovery timeout in seconds
DISCOVERY_TIMEOUT = 10

# Heartbeat interval in seconds
HEARTBEAT_INTERVAL = 5

# Peer timeout (no heartbeat) in seconds
PEER_TIMEOUT = 15


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


def check_version_compatibility(local_version: str, peer_version: str) -> tuple[bool, str]:
    """
    Check if peer version is compatible with local version using Semver.

    Compatibility rules:
    - Different major version → incompatible (False, "major_mismatch")
    - Different minor version → warning but allowed (True, "minor_mismatch")
    - Different patch version → compatible (True, "compatible")
    - Same version → compatible (True, "compatible")

    Args:
        local_version: Local HSYNC version (e.g., "1.0.0").
        peer_version: Peer's HSYNC version.

    Returns:
        Tuple of (is_compatible, reason).
    """
    try:
        local_parts = local_version.split(".")
        peer_parts = peer_version.split(".")

        # Ensure we have at least major.minor.patch
        while len(local_parts) < 3:
            local_parts.append("0")
        while len(peer_parts) < 3:
            peer_parts.append("0")

        # Compare major version
        if local_parts[0] != peer_parts[0]:
            return (False, "major_mismatch")

        # Compare minor version
        if local_parts[1] != peer_parts[1]:
            return (True, "minor_mismatch")

        return (True, "compatible")
    except Exception:
        # If we can't parse versions, assume compatible with warning
        return (True, "parse_error")


# =============================================================================
# Data Classes
# =============================================================================


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
    server_ready: bool = False  # True when HTTP server is listening
    clock_offset: float = 0.0  # Estimated clock offset vs local (seconds)
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
        """
        self._local_cid = local_cid
        self._bucket = bucket
        self._group = group
        self._force_master = force_master
        self._never_master = never_master
        self._startup_time = startup_time if startup_time > 0 else time.time()
        self._sync_direction = sync_direction
        self._peers: dict[str, Peer] = {}
        self._lock = threading.Lock()
        self._master_cid: str | None = None
        self._local_server_ready = False
        self._on_master_change: Callable[[Peer | None], None] | None = None

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

    def set_master_change_callback(self, callback: Callable[[Peer | None], None]) -> None:
        """Set callback to be called when master changes."""
        self._on_master_change = callback

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
            log.debug("Ignoring self from peer discovery")
            return

        with self._lock:
            if peer.cid in self._peers:
                existing = self._peers[peer.cid]
                # Update all properties from the new peer object
                existing.address = peer.address
                existing.port = peer.port
                existing.version = peer.version
                existing.group = peer.group
                existing.use_https = peer.use_https
                existing.cert_fingerprint = peer.cert_fingerprint
                existing.compression_enabled = peer.compression_enabled
                if not existing.server_ready and peer.server_ready:
                    log.info(f"Peer {peer.cid} server is now ready")
                existing.server_ready = peer.server_ready
                existing.clock_offset = peer.clock_offset
                existing.force_master = peer.force_master
                existing.never_master = peer.never_master
                existing.startup_time = peer.startup_time
                existing.sync_direction = peer.sync_direction
                existing.update_last_seen()
                log.debug(f"Updated peer: {peer.cid}")
            else:
                self._peers[peer.cid] = peer
                log.info(f"Discovered new peer: {peer.cid} at {peer.address}:{peer.port}")

            self._elect_master()

    def remove_peer(self, cid: str) -> None:
        """Remove a peer by CID."""
        with self._lock:
            if cid in self._peers:
                del self._peers[cid]
                log.info(f"Peer disconnected: {cid}")
                self._elect_master()

    def get_peer(self, cid: str) -> Peer | None:
        """Get a peer by CID."""
        with self._lock:
            return self._peers.get(cid)

    def get_all_peers(self) -> list[Peer]:
        """Get all known peers."""
        with self._lock:
            return list(self._peers.values())

    def get_alive_peers(self) -> list[Peer]:
        """Get all peers that are still alive."""
        with self._lock:
            return [p for p in self._peers.values() if p.is_alive()]

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
        # Build list of candidates with their election criteria.
        # Each candidate: (cid, force_master, startup_time, sync_direction, server_ready)
        # never_master nodes are excluded entirely so they are never elected.
        candidates: list[tuple[str, bool, float, str, bool]] = []
        if not self._never_master:
            candidates.append(
                (
                    self._local_cid,
                    self._force_master,
                    self._startup_time,
                    self._sync_direction,
                    self._local_server_ready,
                )
            )

        for peer in self._peers.values():
            if peer.is_alive() and not peer.never_master:
                candidates.append(
                    (
                        peer.cid,
                        peer.force_master,
                        peer.startup_time,
                        peer.sync_direction,
                        peer.server_ready,
                    )
                )

        if not candidates:
            new_master_cid = None
        else:
            # Sort by: server_ready (desc), force_master (desc),
            #          bidirectional preference, startup_time (asc), cid (asc)
            # not server_ready: False=0 < True=1, so an incumbent master wins
            # not force_master: False=0 < True=1, so force_master=True first
            # sync_direction != "bidirectional": False=0 < True=1,
            #   so bidirectional peers come first
            candidates.sort(key=lambda c: (not c[4], not c[1], c[3] != "bidirectional", c[2], c[0]))
            new_master_cid = candidates[0][0]

        # Detect force_master conflicts
        force_master_candidates = [c for c in candidates if c[1]]
        if len(force_master_candidates) > 1:
            winner_cid = force_master_candidates[0][0]
            for loser in force_master_candidates[1:]:
                loser_cid = loser[0]
                loser_peer = self._peers.get(loser_cid)
                winner_peer = self._peers.get(winner_cid)
                if loser_cid == self._local_cid:
                    # We lost: another force_master peer won
                    winner_addr = winner_peer.address if winner_peer else "unknown"
                    log.warning(
                        f"[MASTER_CONFLICT] force_master conflict: "
                        f"another master {winner_cid} at {winner_addr} "
                        f"has priority (started earlier). "
                        f"This client will operate as client."
                    )
                elif winner_cid == self._local_cid:
                    # We won: a remote force_master peer lost
                    loser_addr = loser_peer.address if loser_peer else "unknown"
                    log.warning(
                        f"[MASTER_CONFLICT] force_master conflict: "
                        f"peer {loser_cid} at {loser_addr} also has "
                        f"force_master enabled. This client wins "
                        f"(started earlier)."
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
                    f"[MASTER_TAKEOVER] peer {new_master_cid} at "
                    f"{new_peer.address} has force_master enabled "
                    f"and is taking over as master. "
                    f"This client will switch to client."
                )

        if new_master_cid != self._master_cid:
            old_master = self._master_cid
            self._master_cid = new_master_cid

            if new_master_cid == self._local_cid:
                if self._force_master:
                    reason = "force_master"
                elif self._sync_direction == "bidirectional":
                    reason = "bidirectional preference"
                else:
                    reason = "earliest startup"
                log.info(
                    f"This client elected as MASTER (CID: {self._local_cid}, reason: {reason})"
                )
                if self._sync_direction != "bidirectional":
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
                    elif master_peer.sync_direction == "bidirectional":
                        reason = "bidirectional preference"
                    else:
                        reason = "earliest startup"
                    if master_peer.sync_direction != "bidirectional":
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
            if self._on_master_change:
                if new_master_cid == self._local_cid or new_master_cid is None:
                    master_peer = None
                else:
                    master_peer = self._peers.get(new_master_cid)
                self._on_master_change(master_peer)

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

    def __init__(self, peer_manager: PeerManager, zeroconf: Zeroconf) -> None:
        """
        Initialize the service listener.

        Args:
            peer_manager: PeerManager to register discovered peers.
            zeroconf: Zeroconf instance for service queries.
        """
        self._peer_manager = peer_manager
        self._zeroconf = zeroconf

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
        # Extract CID from service name (format: bucket-cid._hsync._tcp.local.)
        try:
            parts = name.split(".")
            if parts:
                service_name = parts[0]
                # Service name format: bucket-cid
                if "-" in service_name:
                    cid = service_name.rsplit("-", 1)[-1]
                    self._peer_manager.remove_peer(cid)
        except Exception as e:
            log.debug(f"Error parsing service name on removal: {e}")

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
            # Group is optional - empty string means no group
            group_bytes = properties.get(b"group", b"")
            group = group_bytes.decode("utf-8") if group_bytes else None
            # HTTPS info is optional
            https_bytes = properties.get(b"https", b"")
            use_https = https_bytes.decode("utf-8").lower() == "true" if https_bytes else False
            cert_fp_bytes = properties.get(b"cert_fp", b"")
            cert_fingerprint = cert_fp_bytes.decode("utf-8") if cert_fp_bytes else None
            # Compression capability is optional
            compression_bytes = properties.get(b"compression", b"")
            compression_enabled = (
                compression_bytes.decode("utf-8").lower() == "true" if compression_bytes else False
            )
            # Server ready flag (indicates HTTP server is listening)
            server_ready_bytes = properties.get(b"server_ready", b"")
            server_ready = (
                server_ready_bytes.decode("utf-8").lower() == "true"
                if server_ready_bytes
                else False
            )
            # Force master flag (for master election)
            force_master_bytes = properties.get(b"force_master", b"")
            force_master = (
                force_master_bytes.decode("utf-8").lower() == "true"
                if force_master_bytes
                else False
            )
            # Never-master flag (peer refuses to ever be master)
            never_master_bytes = properties.get(b"never_master", b"")
            never_master = (
                never_master_bytes.decode("utf-8").lower() == "true"
                if never_master_bytes
                else False
            )
            # Startup time for master election (earlier startup wins)
            startup_time_bytes = properties.get(b"startup_time", b"")
            startup_time = float(startup_time_bytes.decode("utf-8")) if startup_time_bytes else 0.0
            # Sync direction for master election (bidirectional preferred)
            sync_direction_bytes = properties.get(b"sync_direction", b"")
            sync_direction = (
                sync_direction_bytes.decode("utf-8") if sync_direction_bytes else "bidirectional"
            )

            if not cid or not bucket:
                log.debug(f"Incomplete service info: cid={cid}, bucket={bucket}")
                return

            # Check version compatibility
            is_compatible, reason = check_version_compatibility(HSYNC_VERSION, version)
            if not is_compatible:
                log.warning(
                    f"Ignoring peer {cid} with incompatible version {version} "
                    f"(local: {HSYNC_VERSION}, reason: {reason})"
                )
                return

            if reason == "minor_mismatch":
                log.warning(
                    f"Peer {cid} has different minor version {version} "
                    f"(local: {HSYNC_VERSION}). Sync may have issues."
                )
            elif reason == "parse_error":
                log.warning(
                    f"Could not parse version for peer {cid}: {version}. Proceeding with caution."
                )

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
                server_ready=server_ready,
                force_master=force_master,
                never_master=never_master,
                startup_time=startup_time,
                sync_direction=sync_direction,
            )
            self._peer_manager.add_peer(peer)

        except Exception as e:
            log.error(f"Error registering peer from service info: {e}")


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

        self._zeroconf: Zeroconf | None = None
        self._browser: ServiceBrowser | None = None
        self._service_info: ServiceInfo | None = None
        self._peer_manager: PeerManager | None = None
        self._running = False

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
        )

        # Register our service (can be deferred if register_service=False)
        if register_service:
            self._register_service()

        # Start listening for other services
        listener = HsyncServiceListener(self._peer_manager, self._zeroconf)
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
        if not self._zeroconf or not self._service_info:
            log.warning("Cannot update service: not registered")
            return

        # Unregister old service
        self._zeroconf.unregister_service(self._service_info)
        self._service_info = None

        # Re-register with server_ready=true
        self._register_service(server_ready=True)
        # Mark ourselves as incumbent so the election won't hand the master
        # role to a newcomer (prevents takeover/split-brain on this bucket).
        if self._peer_manager:
            self._peer_manager.set_local_server_ready(True)
        log.info("Updated mDNS service: server_ready=true")

    def stop(self) -> None:
        """Stop LAN discovery and unregister service."""
        if not self._running:
            return

        log.info("Stopping LAN mode")

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

        # Get local IP address
        local_ip = self._get_local_ip()

        # Create service name: bucket-cid
        service_name = f"{self._bucket}-{self._cid}.{HSYNC_SERVICE_TYPE}"

        # Create TXT record properties
        properties: dict[str, str] = {
            "cid": self._cid,
            "bucket": self._bucket,
            "version": HSYNC_VERSION,
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

        # Create service info
        self._service_info = ServiceInfo(
            HSYNC_SERVICE_TYPE,
            service_name,
            port=self._port,
            properties=properties,
            addresses=[socket.inet_aton(local_ip)],
        )

        # Register the service
        self._zeroconf.register_service(self._service_info)
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

        Args:
            timeout: Maximum time to wait for discovery in seconds.
        """
        log.info(f"Waiting {timeout}s for peer discovery...")
        time.sleep(timeout)

        if self._peer_manager:
            peers = self._peer_manager.get_alive_peers()
            if peers:
                log.info(f"Discovered {len(peers)} peer(s)")
                for peer in peers:
                    log.debug(f"  - {peer.cid} at {peer.address}:{peer.port}")
            else:
                log.info("No other peers discovered - this client is alone")

    def is_running(self) -> bool:
        """Check if LAN sync is running."""
        return self._running
