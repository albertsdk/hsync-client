# HSYNC Client - HTTP-based file synchronization client
# Copyright (C) 2025 Alberto Basso <alberto.sdk [at] gmail [dot] com>
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

import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from zeroconf import ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf


# =============================================================================
# Constants
# =============================================================================

# mDNS service type for hsync
HSYNC_SERVICE_TYPE = "_hsync._tcp.local."

# Default port for the embedded HTTP server (when acting as master)
# 0 means "use a random available port"
HSYNC_LAN_PORT = 0

# Discovery timeout in seconds
DISCOVERY_TIMEOUT = 5

# Heartbeat interval in seconds
HEARTBEAT_INTERVAL = 5

# Peer timeout (no heartbeat) in seconds
PEER_TIMEOUT = 15


# =============================================================================
# Logger
# =============================================================================

LOG = logging.getLogger("hsync.lan")


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


@dataclass
class Peer:
    """Represents a discovered peer on the LAN."""

    cid: str
    bucket: str
    address: str
    port: int
    group: Optional[str] = None
    use_https: bool = False
    cert_fingerprint: Optional[str] = None
    last_seen: float = field(default_factory=time.time)

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

    Thread-safe peer tracking with automatic master election
    based on lexicographic CID comparison.
    """

    def __init__(
        self, local_cid: str, bucket: str, group: Optional[str] = None
    ) -> None:
        """
        Initialize the peer manager.

        Args:
            local_cid: This client's CID.
            bucket: The bucket name for filtering peers.
            group: Optional group name for filtering peers.
        """
        self._local_cid = local_cid
        self._bucket = bucket
        self._group = group
        self._peers: dict[str, Peer] = {}
        self._lock = threading.Lock()
        self._master_cid: Optional[str] = None
        self._on_master_change: Optional[Callable[[Optional[Peer]], None]] = None

    @property
    def local_cid(self) -> str:
        """Get this client's CID."""
        return self._local_cid

    @property
    def bucket(self) -> str:
        """Get the bucket name."""
        return self._bucket

    @property
    def group(self) -> Optional[str]:
        """Get the group name."""
        return self._group

    def set_master_change_callback(
        self, callback: Callable[[Optional[Peer]], None]
    ) -> None:
        """Set callback to be called when master changes."""
        self._on_master_change = callback

    def _is_group_compatible(self, peer_group: Optional[str]) -> bool:
        """
        Check if a peer's group is compatible with this client's group.

        Filtering logic (Option B - backward compatible):
        - If BOTH have a group defined -> groups must match
        - If at least one has no group -> compatible (no filtering)

        Args:
            peer_group: The peer's group (or None if not set).

        Returns:
            True if the peer is compatible, False otherwise.
        """
        # If at least one has no group, they are compatible
        if self._group is None or peer_group is None:
            return True
        # Both have groups, they must match
        return self._group == peer_group

    def add_peer(self, peer: Peer) -> None:
        """Add or update a discovered peer."""
        if peer.bucket != self._bucket:
            LOG.debug(f"Ignoring peer {peer.cid} - different bucket: {peer.bucket}")
            return

        if not self._is_group_compatible(peer.group):
            LOG.debug(
                f"Ignoring peer {peer.cid} - different group: "
                f"{peer.group} (local: {self._group})"
            )
            return

        if peer.cid == self._local_cid:
            LOG.debug("Ignoring self from peer discovery")
            return

        with self._lock:
            if peer.cid in self._peers:
                self._peers[peer.cid].update_last_seen()
                LOG.debug(f"Updated peer: {peer.cid}")
            else:
                self._peers[peer.cid] = peer
                LOG.info(f"Discovered new peer: {peer.cid} at {peer.address}:{peer.port}")

            self._elect_master()

    def remove_peer(self, cid: str) -> None:
        """Remove a peer by CID."""
        with self._lock:
            if cid in self._peers:
                del self._peers[cid]
                LOG.info(f"Peer disconnected: {cid}")
                self._elect_master()

    def get_peer(self, cid: str) -> Optional[Peer]:
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

        The peer with the lowest CID (lexicographically) becomes master.
        This includes the local client in the election.
        """
        # Collect all CIDs (local + remote peers)
        all_cids = [self._local_cid] + [
            p.cid for p in self._peers.values() if p.is_alive()
        ]

        # Sort lexicographically and pick the lowest
        all_cids.sort()
        new_master_cid = all_cids[0] if all_cids else None

        if new_master_cid != self._master_cid:
            old_master = self._master_cid
            self._master_cid = new_master_cid

            if new_master_cid == self._local_cid:
                LOG.info(f"This client elected as MASTER (CID: {self._local_cid})")
            else:
                master_peer = self._peers.get(new_master_cid) if new_master_cid else None
                LOG.info(
                    f"Master changed: {old_master} -> {new_master_cid} "
                    f"(at {master_peer.address if master_peer else 'unknown'})"
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

    def get_master(self) -> Optional[Peer]:
        """Get the current master peer (None if local is master)."""
        with self._lock:
            if self._master_cid == self._local_cid or self._master_cid is None:
                return None
            return self._peers.get(self._master_cid)

    def get_master_cid(self) -> Optional[str]:
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

    def remove_service(self, _zc: Zeroconf, _type: str, name: str) -> None:
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
            LOG.debug(f"Error parsing service name on removal: {e}")

    def _register_peer_from_info(self, info: ServiceInfo) -> None:
        """Extract peer information from ServiceInfo and register it."""
        try:
            # Get properties from TXT record
            properties = info.properties
            cid = properties.get(b"cid", b"").decode("utf-8")
            bucket = properties.get(b"bucket", b"").decode("utf-8")
            # Group is optional - empty string means no group
            group_bytes = properties.get(b"group", b"")
            group = group_bytes.decode("utf-8") if group_bytes else None
            # HTTPS info is optional
            https_bytes = properties.get(b"https", b"")
            use_https = https_bytes.decode("utf-8").lower() == "true" if https_bytes else False
            cert_fp_bytes = properties.get(b"cert_fp", b"")
            cert_fingerprint = cert_fp_bytes.decode("utf-8") if cert_fp_bytes else None

            if not cid or not bucket:
                LOG.debug(f"Incomplete service info: cid={cid}, bucket={bucket}")
                return

            # Get address and port
            addresses = info.parsed_addresses()
            if not addresses:
                LOG.debug(f"No addresses for service: {info.name}")
                return

            address = addresses[0]  # Use first available address
            port = info.port

            peer = Peer(
                cid=cid,
                bucket=bucket,
                address=address,
                port=port,
                group=group,
                use_https=use_https,
                cert_fingerprint=cert_fingerprint,
            )
            self._peer_manager.add_peer(peer)

        except Exception as e:
            LOG.error(f"Error registering peer from service info: {e}")


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
        group: Optional[str] = None,
        use_https: bool = False,
        cert_fingerprint: Optional[str] = None,
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
        """
        self._cid = cid
        self._bucket = bucket
        self._group = group
        self._use_https = use_https
        self._cert_fingerprint = cert_fingerprint
        # If port is 0, get a random free port
        self._port = port if port != 0 else get_free_port()

        self._zeroconf: Optional[Zeroconf] = None
        self._browser: Optional[ServiceBrowser] = None
        self._service_info: Optional[ServiceInfo] = None
        self._peer_manager: Optional[PeerManager] = None
        self._running = False

    @property
    def port(self) -> int:
        """Get the port being used (useful when random port was requested)."""
        return self._port

    @property
    def group(self) -> Optional[str]:
        """Get the group name."""
        return self._group

    @property
    def use_https(self) -> bool:
        """Check if HTTPS is enabled."""
        return self._use_https

    @property
    def cert_fingerprint(self) -> Optional[str]:
        """Get the SSL certificate fingerprint."""
        return self._cert_fingerprint

    @property
    def peer_manager(self) -> Optional[PeerManager]:
        """Get the peer manager."""
        return self._peer_manager

    def start(self) -> PeerManager:
        """
        Start LAN discovery and service registration.

        Returns:
            PeerManager for monitoring discovered peers and master election.
        """
        group_info = f", group='{self._group}'" if self._group else ""
        LOG.info(
            f"Starting LAN mode for bucket '{self._bucket}' "
            f"(CID: {self._cid}{group_info})"
        )

        # Initialize Zeroconf
        self._zeroconf = Zeroconf()

        # Initialize peer manager
        self._peer_manager = PeerManager(self._cid, self._bucket, self._group)

        # Register our service
        self._register_service()

        # Start listening for other services
        listener = HsyncServiceListener(self._peer_manager, self._zeroconf)
        self._browser = ServiceBrowser(
            self._zeroconf, HSYNC_SERVICE_TYPE, listener
        )

        self._running = True
        LOG.info("LAN discovery started")

        return self._peer_manager

    def stop(self) -> None:
        """Stop LAN discovery and unregister service."""
        if not self._running:
            return

        LOG.info("Stopping LAN mode")

        # Unregister our service
        if self._zeroconf and self._service_info:
            self._zeroconf.unregister_service(self._service_info)
            LOG.debug("Service unregistered")

        # Close zeroconf
        if self._zeroconf:
            self._zeroconf.close()

        self._running = False
        LOG.info("LAN mode stopped")

    def _register_service(self) -> None:
        """Register this client as an mDNS service."""
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
            "version": "0.1.0",
        }
        # Add group if specified
        if self._group:
            properties["group"] = self._group
        # Add HTTPS info if enabled
        if self._use_https:
            properties["https"] = "true"
            if self._cert_fingerprint:
                properties["cert_fp"] = self._cert_fingerprint

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
        LOG.info(f"Registered mDNS service: {service_name} at {local_ip}:{self._port}")

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
            LOG.warning(f"Could not determine local IP: {e}, using 127.0.0.1")
            return "127.0.0.1"

    def wait_for_discovery(self, timeout: float = DISCOVERY_TIMEOUT) -> None:
        """
        Wait for peer discovery to complete.

        Args:
            timeout: Maximum time to wait for discovery in seconds.
        """
        LOG.info(f"Waiting {timeout}s for peer discovery...")
        time.sleep(timeout)

        if self._peer_manager:
            peers = self._peer_manager.get_alive_peers()
            if peers:
                LOG.info(f"Discovered {len(peers)} peer(s)")
                for peer in peers:
                    LOG.debug(f"  - {peer.cid} at {peer.address}:{peer.port}")
            else:
                LOG.info("No other peers discovered - this client is alone")

    def is_running(self) -> bool:
        """Check if LAN sync is running."""
        return self._running