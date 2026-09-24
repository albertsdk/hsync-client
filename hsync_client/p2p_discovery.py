# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Minimal discovery-plan interface for ``hsync p2p``.

``hsync p2p`` can discover peers over two orthogonal plans:

* the **mDNS plan** (``p2p_discovery_mdns.MdnsDiscovery``) — Zeroconf browse +
  advertise on the local network, the historical behavior;
* the **bootnode plan** (``p2p_discovery_bootnode.BootnodeDiscovery``) —
  registration/query against a ``boot/1`` bootstrap node for cross-network
  discovery.

Both feed the same discovery-agnostic peer state in ``p2p_peers`` and must
expose the same surface to the sync core (``p2p_handler.run_p2p_mode``). The
core receives a plan instance already built — it never branches on which plan
is active, so the two implementations are interchangeable by construction.

Consumers: ``hsync-client`` (p2p handler, the two discovery plans, tests).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from hsync_client.constants import BOOTNODE_DEFAULT_PORT, HEARTBEAT_INTERVAL, PEER_TIMEOUT
from hsync_client.p2p_peers import PeerManager

if TYPE_CHECKING:
    from hsync_client.config import HsyncConfig

__all__ = ["DiscoveryPlan", "build_discovery_plan"]


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
        """Block until the discovery plan turns up peers (or ``timeout``)."""
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

    def set_connected_clients(self, count: int) -> None:
        """Publish the number of currently connected peer clients."""
        ...

    def set_connected_clients_provider(self, provider: Callable[[], int] | None) -> None:
        """Install a callback that yields the live connected-client count."""
        ...


def build_discovery_plan(config: HsyncConfig, *, cert_fingerprint: str | None) -> DiscoveryPlan:
    """Select the discovery plan from ``CONFIG`` — the single decision point.

    No bootnode configured → the mDNS plan (historical behavior); a bootnode
    host set (CLI positional or ``HSYNC_BOOTNODE``) → the bootnode plan. The
    sync core (``p2p_handler.run_p2p_mode``) never branches on the plan: it
    receives the adapter here and only uses the :class:`DiscoveryPlan` surface.
    """
    from hsync_client.p2p_discovery_bootnode import BootnodeDiscovery
    from hsync_client.p2p_discovery_mdns import MdnsDiscovery

    shared: dict[str, object] = {
        "cid": config["CID"],
        "bucket": config["REMOTE_BUCKET"],
        "port": config["P2P_LISTEN_PORT"],
        "group": config["P2P_GROUP"],
        "use_https": True,
        "cert_fingerprint": cert_fingerprint,
        "force_master": config["P2P_FORCE_MASTER"],
        "never_master": config["P2P_NEVER_MASTER"],
        "sync_direction": config["SYNC_DIRECTION"],
        "peer_timeout": config.get("PEER_TIMEOUT") or PEER_TIMEOUT,
        "heartbeat_interval": config.get("HEARTBEAT_INTERVAL") or HEARTBEAT_INTERVAL,
    }
    bootnode = config.get("BOOTNODE")
    if not bootnode:
        return MdnsDiscovery(**shared)  # type: ignore[arg-type]
    return BootnodeDiscovery(
        **shared,  # type: ignore[arg-type]
        bootnode_host=bootnode,
        bootnode_port=config.get("BOOTNODE_PORT") or BOOTNODE_DEFAULT_PORT,
        p2p_key=config.get("P2P_KEY"),
        fingerprints=config.get("BOOTNODE_FINGERPRINT", ()),
        tofu=bool(config.get("BOOTNODE_TOFU", False)),
        insecure=bool(config.get("NO_VERIFY_SSL", False)),
        upnp_enabled=bool(config.get("UPNP_ENABLED", True)),
        capacity=config.get("P2P_PEER_READ_CAP", 0),
        peer_download_enabled=bool(config.get("P2P_PEER_DOWNLOAD", False)),
    )
