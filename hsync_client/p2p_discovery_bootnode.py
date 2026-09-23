# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Bootnode discovery plan for ``hsync p2p`` (cross-network, ``boot/1``).

The second implementation of the :class:`~hsync_client.p2p_discovery.DiscoveryPlan`
interface: instead of browsing mDNS, it keeps a lease on a ``boot/1`` bootstrap
node (register → refresh, TTL 60 s), queries it for a random peer sample and
feeds the shared :class:`~hsync_client.p2p_peers.PeerManager`. Master election,
pairing and the data-plane are unchanged — this module only supplies peers.

Lease semantics (spec ``boot-1.md``): the first ``register`` has no endpoint
(the public address is not known yet); the ``ack`` carries the observed
endpoint (STUN-echo). Once the handler says the data-plane endpoint is ready,
the adapter maps the TCP port with UPnP (best-effort) and sends a **second
``register``** that publishes ``endpoints[]`` and the advisory ``capacity``
hint. ``refresh`` renews the TTL and the ``connected`` count only.

Consumers: ``hsync-client`` (p2p subcommand), tests.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from hsync_common.protocol.bootnode import BootPeer, derive_bucket_token
from loguru import logger as log

from hsync_client.boot_client import (
    BootnodeClient,
    BootnodeError,
    build_leave_frame,
    build_query_frame,
    build_refresh_frame,
    build_register_frame,
)
from hsync_client.constants import (
    BOOTNODE_DEFAULT_PORT,
    HEARTBEAT_INTERVAL,
    HSYNC_VERSION,
    PEER_TIMEOUT,
)
from hsync_client.nat_traversal import candidate_endpoints
from hsync_client.net_utils import get_local_ip
from hsync_client.p2p_peers import HSYNC_P2P_LISTEN_PORT, Peer, PeerManager, get_free_port
from hsync_client.stun import ObservedAddress, observed_address
from hsync_client.upnp import UpnpMapper

__all__ = ["BootnodeDiscovery"]

# How often the lease loop wakes to refresh/query. Reuses the shared heartbeat
# cadence (which the handler already keeps well below PEER_TIMEOUT).
_DEFAULT_REFRESH_S = HEARTBEAT_INTERVAL


class BootnodeDiscovery:
    """``DiscoveryPlan`` backed by a ``boot/1`` bootstrap node."""

    def __init__(
        self,
        cid: str,
        bucket: str,
        *,
        bootnode_host: str,
        bootnode_port: int = BOOTNODE_DEFAULT_PORT,
        port: int = HSYNC_P2P_LISTEN_PORT,
        group: str | None = None,
        use_https: bool = True,
        cert_fingerprint: str | None = None,
        force_master: bool = False,
        never_master: bool = False,
        startup_time: float = 0.0,
        sync_direction: str = "bidirectional",
        peer_timeout: float = PEER_TIMEOUT,
        heartbeat_interval: float = _DEFAULT_REFRESH_S,
        p2p_key: str | None = None,
        ca_cert: str | None = None,
        insecure: bool = False,
        upnp_enabled: bool = True,
        capacity: int = 0,
        peer_download_enabled: bool = False,
        client_factory: Callable[..., BootnodeClient] = BootnodeClient,
        upnp_factory: Callable[[], UpnpMapper] = UpnpMapper,
        local_ip: str | None = None,
    ) -> None:
        self._cid = cid
        self._bucket = bucket
        self._group = group
        self._bootnode_host = bootnode_host
        self._bootnode_port = bootnode_port
        self._use_https = use_https
        self._cert_fingerprint = cert_fingerprint
        self._force_master = force_master
        self._never_master = never_master
        self._startup_time = startup_time if startup_time > 0 else time.time()
        self._sync_direction = sync_direction
        self._peer_timeout = peer_timeout
        self._heartbeat_interval = max(1.0, heartbeat_interval)
        self._upnp_enabled = upnp_enabled
        self._capacity = capacity if peer_download_enabled else 0
        self._local_ip = local_ip
        self._port = port if port != 0 else get_free_port()

        self._client: Any = client_factory(
            bootnode_host,
            bootnode_port,
            ca_cert=ca_cert,
            insecure=insecure,
        )
        self._upnp_factory = upnp_factory
        self._token = derive_bucket_token(p2p_key.encode("utf-8"), bucket) if p2p_key else None

        self._peer_manager: PeerManager | None = None
        self._running = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

        self._observed: ObservedAddress | None = None
        self._endpoint_ready = False
        self._published_endpoints = False
        self._external_port: int | None = None
        self._upnp: UpnpMapper | None = None

        self._server_ready = False
        self._connected_clients = 0
        self._connected_provider: Callable[[], int] | None = None

        self._discovery_announced = False

    # -- DiscoveryPlan surface -------------------------------------------------

    @property
    def port(self) -> int:
        """TCP port this node serves on when it becomes master."""
        return self._port

    @property
    def peer_manager(self) -> PeerManager | None:
        """Shared peer state (``None`` before :meth:`start`)."""
        return self._peer_manager

    def start(self, register_service: bool = True) -> PeerManager:
        """Start the lease loop and return the shared :class:`PeerManager`."""
        manager = PeerManager(
            local_cid=self._cid,
            bucket=self._bucket,
            group=self._group,
            force_master=self._force_master,
            never_master=self._never_master,
            startup_time=self._startup_time,
            sync_direction=self._sync_direction,
            local_cert_fingerprint=self._cert_fingerprint,
            local_address=self._local_ip,
            peer_timeout=self._peer_timeout,
        )
        self._peer_manager = manager
        self._running = True
        self._thread = threading.Thread(
            target=self._lease_loop, name="p2p-bootnode-lease", daemon=True
        )
        self._thread.start()
        return manager

    def stop(self) -> None:
        """Stop the lease loop, announce departure and tear the UPnP mapping down."""
        if not self._running and self._thread is None:
            return
        self._running = False
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            self._thread = None
        self._send_leave()
        if self._upnp is not None:
            self._upnp.unmap()
            self._upnp = None

    def wait_for_discovery(self, timeout: float) -> None:
        """Block until at least one live peer is known, or ``timeout`` elapses."""
        if self._peer_manager is None:
            return
        deadline = time.monotonic() + timeout
        while True:
            if self._peer_manager.get_alive_peers():
                if not self._discovery_announced:
                    self._discovery_announced = True
                    log.info(
                        f"Bootnode discovery: found {len(self._peer_manager.get_alive_peers())} peer(s)"
                    )
                return
            if timeout > 0 and time.monotonic() >= deadline:
                return
            try:
                self._query_peers()
            except BootnodeError as exc:
                # A transient bootnode failure must not abort the wait: the
                # background lease loop owns the user-visible warning and keeps
                # retrying, so this path stays quiet.
                log.debug(f"Bootnode query failed while waiting for discovery: {exc}")
            self._wake.wait(timeout=min(self._heartbeat_interval, 1.0))
            self._wake.clear()

    def update_endpoint_ready(self, ready: bool) -> None:
        """Publish (or retract) this node's data-plane endpoint."""
        self._endpoint_ready = ready
        self._wake.set()

    def endpoint_published(self) -> bool:
        """True once the bootnode has been sent our endpoint (second register)."""
        return self._published_endpoints

    def update_server_ready(self) -> None:
        """The local master server is ready to accept peers."""
        self._server_ready = True
        self._wake.set()

    def update_server_not_ready(self) -> None:
        """The local master server is no longer ready."""
        self._server_ready = False
        self._wake.set()

    def set_connected_clients(self, count: int) -> None:
        """Publish the number of connected peer clients."""
        self._connected_clients = count
        self._wake.set()

    def set_connected_clients_provider(self, provider: Callable[[], int] | None) -> None:
        """Install a callback yielding the live connected-client count."""
        self._connected_provider = provider

    # -- Lease loop ------------------------------------------------------------

    def _lease_loop(self) -> None:
        first = True
        while self._running and not self._stop.is_set():
            try:
                if first:
                    self._register(publish_endpoint=False)
                    first = False
                elif (self._endpoint_ready or self._server_ready) and not self._published_endpoints:
                    self._publish_endpoint()
                elif self._server_ready or self._published_endpoints:
                    self._refresh()
                self._query_peers()
            except BootnodeError as exc:
                log.warning(
                    f"Bootnode {self._bootnode_host}:{self._bootnode_port} unreachable: {exc}"
                )
                first = True  # re-register once the node is reachable again
            except Exception as exc:  # noqa: BLE001 - the loop must never die silently
                log.warning(f"Bootnode lease loop error: {exc}")
            self._wake.wait(timeout=self._heartbeat_interval)
            self._wake.clear()

    def _current_connected(self) -> int:
        if self._connected_provider is not None:
            try:
                return int(self._connected_provider())
            except Exception:  # noqa: BLE001 - a provider must never break the lease
                return self._connected_clients
        return self._connected_clients

    def _register(self, *, publish_endpoint: bool) -> None:
        endpoints = self._endpoint_wire() if publish_endpoint else []
        ack = self._client.register(
            build_register_frame(
                cid=self._cid,
                bucket=self._bucket,
                cert_fp=self._cert_fingerprint or "",
                version=_client_version(),
                group=self._group,
                bucket_token=self._token,
                capacity=self._capacity if publish_endpoint else 0,
                endpoints=endpoints,
                connected=self._current_connected() if self._server_ready else None,
            )
        )
        if ack is not None:
            self._observed = observed_address(ack)
            if publish_endpoint:
                self._published_endpoints = True
            log.debug(
                "Bootnode register ok: observed "
                f"{self._observed.host}:{self._observed.port}, "
                f"ttl {ack['ttl']}s"
            )

    def _publish_endpoint(self) -> None:
        if self._upnp_enabled and self._upnp is None:
            self._upnp = self._upnp_factory()
            self._external_port = self._upnp.map_port(self._port)
        self._register(publish_endpoint=True)

    def _refresh(self) -> None:
        ack = self._client.refresh(
            build_refresh_frame(
                cid=self._cid,
                bucket=self._bucket,
                group=self._group,
                bucket_token=self._token,
                connected=self._current_connected() if self._server_ready else None,
            )
        )
        if ack is None:  # the node dropped the lease: fall back to a full register
            self._published_endpoints = False
            self._register(publish_endpoint=self._endpoint_ready)

    def _query_peers(self) -> None:
        if self._peer_manager is None:
            return
        response = self._client.query(
            build_query_frame(bucket=self._bucket, group=self._group, bucket_token=self._token)
        )
        if response is None:
            return
        for wire_peer in response["peers"]:
            peer = self._peer_from_wire(wire_peer)
            if peer is not None and peer.cid != self._cid:
                self._peer_manager.add_peer(peer)

    def _peer_from_wire(self, wire: BootPeer) -> Peer | None:
        """Map a relayed peer to the shared :class:`Peer`; skip unreachable ones."""
        if not wire.get("reachable", True):
            return None
        if wire["proto"] != "tcp":
            return None
        return Peer(
            cid=wire["cid"],
            bucket=wire["bucket"],
            address=wire["ip"],
            port=wire["port"],
            version=wire.get("version", "0.0.0"),
            group=wire.get("group"),
            use_https=True,
            cert_fingerprint=wire.get("cert_fp"),
            peer_download_enabled=False,
            force_master=False,
            never_master=False,
            startup_time=0.0,
            sync_direction="bidirectional",
            # A relayed peer with a published TCP endpoint is a serving master:
            # ``reachable`` is derived from ``bool(endpoints)`` at register time,
            # and the master only publishes endpoints once its servers are up.
            # Without this the slave would wait forever for a TXT ``server_ready``
            # flag that the boot/1 wire does not carry.
            server_ready=True,
        )

    def _endpoint_wire(self) -> list[dict[str, object]]:
        return [
            candidate.as_wire()
            for candidate in candidate_endpoints(
                local_ip=self._local_ip or get_local_ip(),
                listen_port=self._port,
                observed=self._observed,
                external_port=self._external_port,
            )
        ]

    def _send_leave(self) -> None:
        try:
            self._client.leave(
                build_leave_frame(cid=self._cid, bucket=self._bucket, bucket_token=self._token)
            )
        except BootnodeError as exc:
            log.debug(f"Bootnode leave failed (best-effort): {exc}")


def _client_version() -> str:
    return HSYNC_VERSION
