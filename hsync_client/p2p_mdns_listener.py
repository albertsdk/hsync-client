# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""mDNS/Zeroconf service listener for the LAN rendezvous plan.

Discovers ``_hsync._tcp.local.`` peers and registers them with the
``PeerManager``. The advertising/browsing coordinator lives in
``hsync_client.p2p_rendezvous_mdns``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping

from hsync_common.protocol.handshake import PROTOCOL_VERSION_P2P
from loguru import logger as log
from zeroconf import ServiceInfo, ServiceListener, Zeroconf

from hsync_client.net_utils import get_local_ips
from hsync_client.p2p_peers import (
    HEARTBEAT_SOURCE,
    Peer,
    PeerManager,
    dial_candidates,
)

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
        local_ips: Callable[[], list[str]] | None = None,
    ) -> None:
        """
        Initialize the service listener.

        Args:
            peer_manager: PeerManager to register discovered peers.
            goodbye_grace_s: Delay before honouring a ``remove_service`` so an
                Android heartbeat re-register can cancel the removal. ``0``
                removes immediately (tests).
            local_ips: Supplies this node's own addresses, used to promote a
                discovered peer's same-subnet address to its primary endpoint.
                Defaults to :func:`get_local_ips`.
        """
        self._peer_manager = peer_manager
        self._goodbye_grace_s = goodbye_grace_s
        self._local_ips = local_ips or get_local_ips
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
            # Inbound flag: the peer confirms an externally dialable endpoint
            # (it can serve without the relay). Part of the election.
            inbound = _tx_bool(properties, b"inbound")
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

            port = info.port
            if port is None:
                log.debug(f"No port for service: {info.name}")
                return

            # Every advertised address is a dial candidate; a same-subnet one is
            # promoted to the primary address (the ordering policy lives once, in
            # dial_candidates) so a multi-homed peer is dialed on the interface
            # it shares with this node. With more than one address the full list
            # is kept so ``_select_master_endpoint`` can probe the reachable one
            # (``same_subnet`` is only a /24 heuristic, so on a wider LAN the
            # promoted address may not be the right one); a single-address peer
            # keeps the empty list and the no-probe fast path, unchanged.
            candidates = tuple((address_ip, port) for address_ip in addresses)
            address, primary_port = dial_candidates(
                candidates, addresses[0], port, self._local_ips()
            )[0]
            endpoints = candidates if len(candidates) > 1 else ()

            peer = Peer(
                cid=cid,
                bucket=bucket,
                address=address,
                port=primary_port,
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
                inbound=inbound,
                startup_time=startup_time,
                sync_direction=sync_direction,
                endpoints=endpoints,
            )
            self._cancel_pending_goodbye(cid)
            self._peer_manager.add_peer(peer, source=HEARTBEAT_SOURCE)

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
