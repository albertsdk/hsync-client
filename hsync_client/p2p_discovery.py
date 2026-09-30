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

from hsync_common.protocol.tls_trust import TrustPolicy

from hsync_client.constants import (
    BOOTNODE_DEFAULT_PORT,
    BOOTNODE_REFRESH_INTERVAL,
    HEARTBEAT_INTERVAL,
    PEER_TIMEOUT,
)
from hsync_client.p2p_peers import PeerManager
from hsync_client.trust_prompt import build_trust_prompt

if TYPE_CHECKING:
    from hsync_client.config import HsyncConfig


class DiscoveryFatalError(RuntimeError):
    """Discovery stopped permanently; retrying cannot help.

    Raised by a plan's ``wait_for_discovery`` when discovery has stopped for
    good — today the bootnode plan after a certificate pin mismatch
    (:class:`~hsync_client.boot_client.BootnodeFingerprintMismatch`), where the
    lease loop stops and no peer can ever be discovered. The sync core aborts
    with a fatal exit code instead of waiting forever; the mDNS plan never
    raises it.
    """


__all__ = ["DiscoveryFatalError", "DiscoveryPlan", "DiscoveryPlanState", "build_discovery_plan"]


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

        Raises :class:`DiscoveryFatalError` when discovery has permanently
        stopped (e.g. a bootnode certificate pin mismatch); the sync core then
        aborts fatally instead of waiting forever for peers that can never be
        discovered.
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

    def set_connected_clients(self, count: int) -> None:
        """Publish the number of currently connected peer clients."""
        ...

    def set_connected_clients_provider(self, provider: Callable[[], int] | None) -> None:
        """Install a callback that yields the live connected-client count."""
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


def build_discovery_plan(
    config: HsyncConfig,
    *,
    cert_fingerprint: str | None,
    port: int | None = None,
) -> DiscoveryPlan:
    """Select the discovery plan from ``CONFIG`` — the single decision point.

    No bootnode configured → the mDNS plan (historical behavior); a bootnode
    host set (CLI positional or ``HSYNC_BOOTNODE``) → the bootnode plan. The
    sync core (``p2p_handler.run_p2p_mode``) never branches on the plan: it
    receives the adapter here and only uses the :class:`DiscoveryPlan` surface.

    ``port`` overrides ``CONFIG["P2P_LISTEN_PORT"]`` with the already-bound
    serving port, so discovery advertises the real port instead of probing for
    one (no probe/release race).
    """
    from hsync_client.p2p_discovery_bootnode import BootnodeDiscovery
    from hsync_client.p2p_discovery_mdns import MdnsDiscovery

    resolved_port = config["P2P_LISTEN_PORT"] if port is None else port
    shared: dict[str, object] = {
        "cid": config["CID"],
        "bucket": config["REMOTE_BUCKET"],
        "port": resolved_port,
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
    # The bootnode plan has its own lease cadence (spec ``boot-1.md``: refresh
    # every 30 s) sized against the node's query/register rate limits; it must
    # not reuse the mDNS heartbeat, which is far faster.
    bootnode_shared = {key: value for key, value in shared.items() if key != "heartbeat_interval"}
    return BootnodeDiscovery(
        **bootnode_shared,  # type: ignore[arg-type]
        refresh_interval=config.get("BOOTNODE_REFRESH_INTERVAL") or BOOTNODE_REFRESH_INTERVAL,
        bootnode_host=bootnode,
        bootnode_port=config.get("BOOTNODE_PORT") or BOOTNODE_DEFAULT_PORT,
        p2p_key=config.get("P2P_KEY"),
        fingerprints=config.get("BOOTNODE_FINGERPRINT", ()),
        policy=config.get("BOOTNODE_TRUST", TrustPolicy.ask),
        prompt=build_trust_prompt(config),
        upnp_enabled=bool(config.get("UPNP_ENABLED", True)),
        capacity=config.get("P2P_PEER_READ_CAP", 0),
        peer_download_enabled=bool(config.get("P2P_PEER_DOWNLOAD", False)),
    )
