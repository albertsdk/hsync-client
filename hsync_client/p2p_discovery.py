# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Minimal discovery-plan interface for ``hsync p2p``.

``hsync p2p`` discovers peers over two orthogonal sources:

* the **mDNS plan** (``p2p_discovery_mdns.MdnsDiscovery``) — Zeroconf browse +
  advertise on the local network, always active;
* the **bootnode plan** (``p2p_discovery_bootnode.BootnodeDiscovery``) —
  registration/query against a ``boot/1`` bootstrap node for cross-network
  discovery, added when a bootnode is configured.

With no bootnode the mDNS plan runs alone. With a bootnode configured both run
together (``p2p_discovery_hybrid.HybridDiscovery``) over **one shared**
:class:`~hsync_client.p2p_peers.PeerManager`, so a node is reachable both on the
local network and across networks and the two sources never double-count peers
or split the election.

Every source feeds the same discovery-agnostic peer state in ``p2p_peers`` and
exposes the same surface to the sync core (``p2p_handler.run_p2p_mode``). The
core receives a plan instance already built — it never branches on which plan is
active, so the implementations are interchangeable by construction.

Consumers: ``hsync-client`` (p2p handler, the two discovery plans, tests).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from hsync_common.protocol.tls_trust import TrustPolicy
from loguru import logger as log

from hsync_client.constants import (
    BOOTNODE_DEFAULT_PORT,
    BOOTNODE_REFRESH_INTERVAL,
    HEARTBEAT_INTERVAL,
    PEER_TIMEOUT,
)
from hsync_client.net_utils import get_local_ip
from hsync_client.p2p_peers import PeerManager
from hsync_client.trust_prompt import build_trust_prompt

if TYPE_CHECKING:
    from hsync_client.config import HsyncConfig


__all__ = [
    "BootnodeSource",
    "DiscoveryPlan",
    "DiscoveryPlanState",
    "build_discovery_plan",
]


@runtime_checkable
class DiscoveryPlan(Protocol):
    """Discovery-agnostic surface consumed by the p2p sync core."""

    @property
    def port(self) -> int:
        """TCP port this node serves on when it becomes master (0 = auto)."""
        ...

    @property
    def peer_manager(self) -> PeerManager | None:
        """Shared peer state, or ``None`` before :meth:`start`."""
        ...

    def start(self, register_service: bool = True) -> PeerManager:
        """Start discovery and return the shared :class:`PeerManager`."""
        ...

    def stop(self) -> None:
        """Stop discovery and release all resources."""
        ...

    def wait_for_discovery(self, timeout: float) -> None:
        """Block until the discovery plan turns up peers (or ``timeout``).

        Never raises for a failing source: a source that cannot deliver peers
        (an unreachable or untrusted bootnode) is disabled while the remaining
        sources keep feeding the shared peer state, so the wait always ends on
        its own timeout or a discovery change.
        """
        ...

    def update_endpoint_ready(self, ready: bool) -> None:
        """Publish whether this node's data-plane endpoint is reachable."""
        ...

    def endpoint_published(self) -> bool:
        """True once the plan has advertised this node's data-plane endpoint.

        The mDNS plan has no separate publication step (readiness is unchanged)
        and returns ``True``; the bootnode plan returns ``True`` only after the
        second ``register`` that publishes ``endpoints[]``.
        """
        ...

    def update_server_ready(self) -> None:
        """Publish that the local master server is ready to accept peers."""
        ...

    def update_server_not_ready(self) -> None:
        """Publish that the local master server is no longer ready."""
        ...

    def set_master_eligibility(self, eligible: bool) -> None:
        """Advertise or withdraw this node's master candidacy at runtime.

        A node parked in continuous dry-run cannot serve, so it withdraws its
        candidacy (``never_master`` on the wire, no ``prefer_master``) and peers
        stop electing it. ``eligible=True`` restores the configured flags.
        """
        ...

    def refresh_election_advertisement(self) -> None:
        """Re-announce the election flags on every discovery source."""
        ...

    def set_connected_clients(self, count: int) -> None:
        """Publish the number of currently connected peer clients."""
        ...

    def set_connected_clients_provider(self, provider: Callable[[], int] | None) -> None:
        """Install a callback that yields the live connected-client count."""
        ...

    def relay_dialer(self, target_cid: str) -> Any:
        """A data-relay tunnel factory towards *target_cid*, or ``None``.

        Discovery plans that cannot relay (mDNS) return ``None``; a discovery
        source returns a ready-to-open
        :class:`hsync_client.boot_relay.RelayDialer`. The caller owns the
        returned object and must close it.
        """
        ...

    def relay_acceptor(self, local_port: int) -> Any:
        """A data-relay listener for this node, or ``None`` when unsupported.

        Only a discovery source can ask the node to relay inbound sessions; the
        caller starts and stops the returned acceptor with the serving role.
        """
        ...


@runtime_checkable
class BootnodeSource(Protocol):
    """The ``boot/1`` surface a combined plan drives, for one node or several.

    One implementation is a single :class:`~hsync_client.p2p_discovery_bootnode.BootnodeDiscovery`;
    :class:`~hsync_client.p2p_discovery_bootnode.MultiBootnodeDiscovery` is the
    fan-out over N of them. ``HybridDiscovery`` depends only on this surface, so
    mDNS plus one node and mDNS plus N nodes are the same composition.
    """

    def start(
        self, register_service: bool = True, peer_manager: PeerManager | None = None
    ) -> PeerManager:
        """Start the lease loop(s) and return the shared :class:`PeerManager`."""
        ...

    def stop(self) -> None:
        """Stop the lease loop(s) and release the transport resources."""
        ...

    def log_peer_count(self) -> None:
        """Log the alive-peer count once per discovery window."""
        ...

    def update_endpoint_ready(self, ready: bool) -> None:
        """Publish (or retract) this node's data-plane endpoint."""
        ...

    def endpoint_published(self) -> bool:
        """True once the data-plane endpoint reached every source that can carry it."""
        ...

    def source_disabled(self) -> bool:
        """True when the source stopped for good (an untrusted node)."""
        ...

    def update_server_ready(self) -> None:
        """Publish that the local master server is ready to accept peers."""
        ...

    def update_server_not_ready(self) -> None:
        """Publish that the local master server is no longer ready."""
        ...

    def set_connected_clients(self, count: int) -> None:
        """Publish the number of currently connected peer clients."""
        ...

    def set_connected_clients_provider(self, provider: Callable[[], int] | None) -> None:
        """Install a callback that yields the live connected-client count."""
        ...

    def set_master_eligibility(self, eligible: bool) -> None:
        """Advertise or withdraw this node's master candidacy."""
        ...

    def refresh_election_advertisement(self) -> None:
        """Re-announce the election flags."""
        ...

    def relay_dialer(self, target_cid: str) -> Any:
        """A data-relay tunnel factory towards *target_cid*, or ``None``."""
        ...

    def relay_acceptor(self, local_port: int) -> Any:
        """A data-relay listener feeding the local master, or ``None``."""
        ...


class DiscoveryPlanState:
    """Shared trivial state for the concrete discovery plans.

    ``MdnsDiscovery`` and ``BootnodeDiscovery`` expose the same
    :class:`DiscoveryPlan` surface and keep the same trivial state: the serving
    port, the shared :class:`PeerManager` and the connected-client provider.
    Holding that here keeps the two plans aligned on the interface without
    merging their transport-specific logic (each keeps its own
    ``start``/``stop``/refresh behavior).

    Consumers: the two discovery plans only.
    """

    _port: int
    _peer_manager: PeerManager | None
    _connected_clients_provider: Callable[[], int] | None
    # Shared configuration every plan holds (identical values across plans), so
    # one PeerManager can be built for either plan or for a combined plan.
    _cid: str
    _bucket: str
    _group: str | None
    _prefer_master: bool
    _never_master: bool
    # Configured election flags, kept so a runtime withdrawal (a node parked in
    # continuous dry-run, see set_master_eligibility) can be undone.
    _configured_prefer_master: bool
    _configured_never_master: bool
    _startup_time: float
    _sync_direction: str
    _cert_fingerprint: str | None
    _use_https: bool
    _local_ip: str | None
    _peer_timeout: float

    def _init_common_fields(
        self,
        *,
        cid: str,
        bucket: str,
        group: str | None,
        use_https: bool,
        cert_fingerprint: str | None,
        prefer_master: bool,
        never_master: bool,
        startup_time: float,
        sync_direction: str,
        peer_timeout: float,
    ) -> None:
        """Set the election-advertisement fields shared by every discovery plan.

        Single definition of the values both plans carry identically (also the
        ones :meth:`create_peer_manager` reads), so they cannot drift.
        """
        self._cid = cid
        self._bucket = bucket
        self._group = group
        self._use_https = use_https
        self._cert_fingerprint = cert_fingerprint
        self._configured_prefer_master = prefer_master
        self._configured_never_master = never_master
        self._prefer_master = prefer_master
        self._never_master = never_master
        self._startup_time = startup_time if startup_time > 0 else time.time()
        self._sync_direction = sync_direction
        self._peer_timeout = peer_timeout

    @property
    def port(self) -> int:
        """TCP port this node serves on when it becomes master (0 = auto)."""
        return self._port

    @property
    def peer_manager(self) -> PeerManager | None:
        """Shared peer state, or ``None`` before :meth:`start`."""
        return self._peer_manager

    def set_connected_clients_provider(self, provider: Callable[[], int] | None) -> None:
        """Install a callback that yields the live connected-client count."""
        self._connected_clients_provider = provider

    def relay_dialer(self, target_cid: str) -> Any:
        """No data relay: a LAN-only plan has no public rendezvous point."""
        return None

    def relay_acceptor(self, local_port: int) -> Any:
        """No data relay: a LAN-only plan serves its listen port directly."""
        return None

    def create_peer_manager(self) -> PeerManager:
        """Build the :class:`PeerManager` from this plan's shared configuration.

        The single construction point for both plans and for the combined plan,
        which builds one manager and injects it into both so peer state and the
        election are shared rather than duplicated.
        """
        return PeerManager(
            local_cid=self._cid,
            bucket=self._bucket,
            group=self._group,
            prefer_master=self._prefer_master,
            never_master=self._never_master,
            startup_time=self._startup_time,
            sync_direction=self._sync_direction,
            local_cert_fingerprint=self._cert_fingerprint,
            local_address=self._local_ip or get_local_ip(),
            peer_timeout=self._peer_timeout,
        )

    def set_master_eligibility(self, eligible: bool) -> None:
        """Advertise or withdraw this node's master candidacy at runtime.

        A node parked in continuous dry-run (waiting for the desktop's
        ``PREVIEW``/``SYNC`` commands) cannot serve, so while it waits it
        advertises ``never_master`` and drops ``prefer_master``: peers must not
        elect it and then wait forever for its ``server_ready``. The local
        election drops the node too. ``eligible=True`` restores the configured
        flags and candidacy.
        """
        self._prefer_master = self._configured_prefer_master if eligible else False
        self._never_master = self._configured_never_master if eligible else True
        if self._peer_manager is not None:
            self._peer_manager.set_master_candidacy(eligible)
        self.refresh_election_advertisement()

    def refresh_election_advertisement(self) -> None:
        """Re-announce the election flags on every discovery source."""
        raise NotImplementedError


def build_discovery_plan(
    config: HsyncConfig,
    *,
    cert_fingerprint: str | None,
    port: int | None = None,
) -> DiscoveryPlan:
    """Select the discovery plan from ``CONFIG`` — the single decision point.

    No bootnode configured → the mDNS plan (historical behavior); a bootnode
    endpoint set (`--discovery-server` or ``HSYNC_DISCOVERY_SERVER_ENDPOINTS``)
    → the discovery plan. The
    sync core (``p2p_handler.run_p2p_mode``) never branches on the plan: it
    receives the adapter here and only uses the :class:`DiscoveryPlan` surface.

    ``port`` overrides ``CONFIG["P2P_LISTEN_PORT"]`` with the already-bound
    serving port, so discovery advertises the real port instead of probing for
    one (no probe/release race).
    """
    from hsync_client.p2p_discovery_bootnode import (
        BootnodeDiscovery,
        MultiBootnodeDiscovery,
        SharedUpnpMapping,
    )
    from hsync_client.p2p_discovery_mdns import MdnsDiscovery

    resolved_port = config["P2P_LISTEN_PORT"] if port is None else port
    endpoints = tuple(config.get("DISCOVERY_SERVER_ENDPOINTS") or ())
    discovery_refresh = config.get("DISCOVERY_SERVER_REFRESH_INTERVAL") or BOOTNODE_REFRESH_INTERVAL
    peer_timeout: float = float(config.get("PEER_TIMEOUT") or PEER_TIMEOUT)
    # A discovery-server peer is only re-seen when its CID is drawn in the
    # node's random sample, and samples arrive at the lease cadence
    # (``DISCOVERY_SERVER_REFRESH_INTERVAL``, spec ``boot-1.md``: 30 s). The
    # shared PeerManager holds ONE liveness window for every source, so with a
    # discovery server that window must span at least two query cycles —
    # mirroring the mDNS rule ``PEER_TIMEOUT >= 2 * HEARTBEAT_INTERVAL`` — or a
    # live cross-network peer is declared stale between two samples and the
    # election churns. An explicit larger ``HSYNC_PEER_TIMEOUT`` is preserved.
    if endpoints:
        required_timeout = 2.0 * float(discovery_refresh)
        if peer_timeout < required_timeout:
            log.warning(
                f"Raising the peer liveness timeout to {required_timeout:.0f}s: "
                f"discovery-server sampling runs every {discovery_refresh}s"
            )
            peer_timeout = required_timeout
    shared: dict[str, object] = {
        "cid": config["CID"],
        "bucket": config["REMOTE_BUCKET"],
        "port": resolved_port,
        "group": config["P2P_GROUP"],
        "use_https": True,
        "cert_fingerprint": cert_fingerprint,
        "prefer_master": config["P2P_PREFER_MASTER"],
        "never_master": config["P2P_NEVER_MASTER"],
        "sync_direction": config["SYNC_DIRECTION"],
        "peer_timeout": peer_timeout,
        "heartbeat_interval": config.get("HEARTBEAT_INTERVAL") or HEARTBEAT_INTERVAL,
    }
    if not endpoints:
        return MdnsDiscovery(**shared)  # type: ignore[arg-type]
    # Every discovery source follows the boot/1 lease cadence (spec: refresh
    # every 30 s), sized against the node's query/register rate limits; it must
    # not reuse the mDNS heartbeat, which is far faster.
    bootnode_shared = {key: value for key, value in shared.items() if key != "heartbeat_interval"}
    map_data_port = bool(config.get("MAP_DATA_PORT", False))
    # One UPnP mapping for the whole process: N sources publish the same
    # data-plane port, so a mapper per source would create N mappings. With a
    # single source the child owns the mapping it creates.
    shared_upnp = SharedUpnpMapping() if map_data_port and len(endpoints) > 1 else None
    sources = [
        BootnodeDiscovery(
            **bootnode_shared,  # type: ignore[arg-type]
            refresh_interval=discovery_refresh,
            bootnode_host=endpoint.host,
            bootnode_port=endpoint.port or BOOTNODE_DEFAULT_PORT,
            bootnode_ports=tuple(config.get("DISCOVERY_SERVER_EXTRA_PORTS") or ()),
            p2p_key=config.get("P2P_KEY"),
            fingerprints=tuple(config.get("DISCOVERY_SERVER_FINGERPRINTS") or ()),
            policy=config.get("DISCOVERY_SERVER_TRUST", TrustPolicy.ask),
            prompt=build_trust_prompt(config),
            upnp_enabled=map_data_port,
            shared_upnp=shared_upnp,
            capacity=config.get("P2P_PEER_READ_CAP", 0),
            peer_download_enabled=bool(config.get("P2P_PEER_DOWNLOAD", False)),
        )
        for endpoint in endpoints
    ]
    bootnode_plan = (
        sources[0]
        if len(sources) == 1
        else MultiBootnodeDiscovery(sources, shared_upnp=shared_upnp)
    )
    # mDNS stays active alongside the discovery servers: a node next to us on the
    # local network is found without a round trip through a server, and the
    # sources share one PeerManager so the election stays single.
    from hsync_client.p2p_discovery_hybrid import HybridDiscovery

    return HybridDiscovery(mdns=MdnsDiscovery(**shared), bootnode=bootnode_plan)  # type: ignore[arg-type]
