# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Combined mDNS + rdv rendezvous plan.

With a rendezvous configured a node must be discoverable both on the local network
(mDNS) and across networks (rendezvous). ``HybridRendezvous`` runs both source plans
over a **single shared** :class:`~hsync_client.p2p_peers.PeerManager` and exposes
the one :class:`~hsync_client.p2p_rendezvous.RendezvousPlan` surface to the sync
core: peer state and the master election are shared, presence is reported once,
and each source keeps its own transport lifecycle.

A node next to us on the local network is therefore found without a round trip
through the rendezvous, while a node on another network is still found through it;
a peer seen by both sources updates one entry (the candidate lists merge, and the
mDNS TXT capabilities are not cleared by the rendezvous relay that does not carry
them). Each source keeps its own transport lifecycle, so one that stops (an
untrusted rendezvous) degrades rendezvous to the other instead of ending the run.

Consumers: ``hsync-client`` (``build_rendezvous_plan`` when a rendezvous is set).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from loguru import logger as log

from hsync_client.p2p_peers import PeerManager
from hsync_client.p2p_rendezvous import RendezvousPlanState, RendezvousSource
from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

__all__ = ["HybridRendezvous"]


class HybridRendezvous(RendezvousPlanState):
    """Run the mDNS plan and the rendezvous-server source(s) over one PeerManager."""

    def __init__(self, *, mdns: MdnsRendezvous, rendezvous: RendezvousSource) -> None:
        self._mdns = mdns
        self._rendezvous = rendezvous
        # The shared trivial state (port, peer manager, connected-client
        # provider) lives on the mDNS child, which is always present; the
        # rendezvous child adopts the same manager at start.
        self._port = mdns.port
        self._peer_manager: PeerManager | None = None
        self._connected_clients_provider: Callable[[], int] | None = None
        self._rendezvous_announced = False

    # -- RendezvousPlan surface -------------------------------------------------

    def start(self, register_service: bool = True) -> PeerManager:
        """Start every source over one shared :class:`PeerManager`."""
        manager = self._mdns.create_peer_manager()
        self._peer_manager = manager
        self._mdns.start(register_service=register_service, peer_manager=manager)
        self._rendezvous.start(register_service=register_service, peer_manager=manager)
        log.info("P2P rendezvous started (mDNS + discovery servers)")
        return manager

    def stop(self) -> None:
        """Stop every source (the rendezvous sources first, mDNS last)."""
        self._rendezvous.stop()
        self._mdns.stop()

    def wait_for_rendezvous(self, timeout: float) -> None:
        """Wait on the shared peer manager.

        Peers can arrive from either source, so the wait is the mDNS one (wake on
        a rendezvous change, or the window elapsing); presence is emitted exactly
        once, by the mDNS child, and the rendezvous result is logged once. A
        rendezvous source that stopped (untrusted node) never fails the wait: it
        simply delivers no more peers while the local network keeps feeding the
        shared state.
        """
        if self._peer_manager is None:
            return
        if not self._rendezvous_announced:
            log.info(f"Waiting {timeout}s for peer rendezvous...")
            self._rendezvous_announced = True
        else:
            log.debug(f"Waiting {timeout}s for peer rendezvous...")
        self._peer_manager.wait_for_change(timeout)
        self._mdns.emit_rendezvous_status()
        self._rendezvous.log_peer_count()

    def update_endpoint_ready(self, ready: bool) -> None:
        """Publish (or retract) this node's data-plane endpoint on every source."""
        self._mdns.update_endpoint_ready(ready)
        self._rendezvous.update_endpoint_ready(ready)

    def endpoint_published(self) -> bool:
        """True once every source that can still carry traffic published it.

        The mDNS plan has no separate publication step (always ``True``); a
        rendezvous source gates readiness until its endpoint register, so the
        combined plan is ready only when the node is reachable from both. A
        disabled rendezvous source (untrusted node) carries no traffic at all, so
        requiring its publication would only delay readiness for a peer that
        cannot be reached through it.
        """
        if self._rendezvous.source_disabled():
            return self._mdns.endpoint_published()
        return self._mdns.endpoint_published() and self._rendezvous.endpoint_published()

    def update_server_ready(self) -> None:
        """Publish that the local master server is ready on every source."""
        self._mdns.update_server_ready()
        self._rendezvous.update_server_ready()

    def update_server_not_ready(self) -> None:
        """Publish that the local master server is no longer ready on every source."""
        self._mdns.update_server_not_ready()
        self._rendezvous.update_server_not_ready()

    def set_connected_clients(self, count: int) -> None:
        """Publish the connected-client count on every source."""
        self._mdns.set_connected_clients(count)
        self._rendezvous.set_connected_clients(count)

    def relay_dialer(self, target_cid: str) -> Any:
        """Delegate the data relay to the rendezvous source (mDNS has none)."""
        return self._rendezvous.relay_dialer(target_cid)

    def relay_acceptor(self, local_port: int) -> Any:
        """Delegate the data relay listener to the rendezvous source."""
        return self._rendezvous.relay_acceptor(local_port)

    def set_master_eligibility(self, eligible: bool) -> None:
        """Withdraw or restore this node's master candidacy on every source.

        The combined plan advertises nothing itself: each source keeps its own
        election flags and both share one PeerManager, so delegating keeps the
        local election and the announcements in step.
        """
        self._mdns.set_master_eligibility(eligible)
        self._rendezvous.set_master_eligibility(eligible)

    def set_connected_clients_provider(self, provider: Callable[[], int] | None) -> None:
        """Install the connected-client provider on every source."""
        super().set_connected_clients_provider(provider)
        self._mdns.set_connected_clients_provider(provider)
        self._rendezvous.set_connected_clients_provider(provider)
