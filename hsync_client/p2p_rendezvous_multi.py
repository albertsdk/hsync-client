# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Fan-out rendezvous over several discovery servers.

``MultiRdvRendezvous`` leases/queries N ``RdvRendezvous`` children (one per
endpoint) over one shared ``PeerManager``; the per-server client lives in
``hsync_client.p2p_rendezvous_rdv``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from loguru import logger as log

from hsync_client.p2p_peers import PeerManager
from hsync_client.p2p_rendezvous import RendezvousPlanState
from hsync_client.p2p_rendezvous_rdv import RdvRendezvous, SharedUpnpMapping
from hsync_client.rdv_relay import FailoverRelayDialer, GroupRelayAcceptor


class MultiRdvRendezvous(RendezvousPlanState):
    """Lease and query several discovery servers over one shared ``PeerManager``.

    Each child keeps its own transport, trust policy and lease state (the
    observed endpoint and the published candidates are per node), while peer
    state and the master election stay single: ``PeerManager.add_peer`` merges
    the samples by CID, so a peer seen through two servers is one candidate and
    one election input.

    Readiness is "at least one source published": a server that is down, refused
    or not trusted must not delay the data plane, because a peer can still reach
    this node through any server that did see it. A source that stops for good
    (an untrusted node) is disabled on its own, never for its siblings.

    The children must be built with the same serving ``port``; ``build_rendezvous_plan``
    resolves it once for exactly that reason.
    """

    def __init__(
        self,
        sources: Sequence[RdvRendezvous],
        *,
        shared_upnp: SharedUpnpMapping | None = None,
    ) -> None:
        if not sources:
            raise ValueError("at least one rendezvous source is required")
        self._sources = list(sources)
        self._shared_upnp = shared_upnp
        self._port = self._sources[0].port
        self._peer_manager: PeerManager | None = None
        self._connected_clients_provider: Callable[[], int] | None = None
        self._rendezvous_announced = False

    # -- RendezvousPlan surface -------------------------------------------------

    def create_peer_manager(self) -> PeerManager:
        """Refuse: the composite never owns a manager of its own.

        ``start`` builds exactly one manager (from the first source's config) and
        injects it into every source, so peer state is single. Building another
        one here would split rendezvous and the election; the error makes that
        misuse loud instead of silent.
        """
        raise RuntimeError(
            "MultiRdvRendezvous.start() creates the single shared PeerManager; "
            "the composite cannot build another one"
        )

    def start(self, register_service: bool = True) -> PeerManager:
        """Start every lease loop over one shared :class:`PeerManager`."""
        manager = self._sources[0].create_peer_manager()
        self._peer_manager = manager
        for source in self._sources:
            source.start(register_service=register_service, peer_manager=manager)
        log.info(f"P2P rendezvous started ({len(self._sources)} discovery server(s))")
        return manager

    def stop(self) -> None:
        """Stop every lease loop and drop the shared UPnP mapping (reverse order)."""
        for source in reversed(self._sources):
            source.stop()
        if self._shared_upnp is not None:
            self._shared_upnp.unmap()

    def wait_for_rendezvous(self, timeout: float) -> None:
        """Wait once on the shared manager: peers can arrive from any source."""
        if self._peer_manager is None:
            return
        if not self._rendezvous_announced:
            log.info(f"Waiting {timeout}s for peer rendezvous...")
            self._rendezvous_announced = True
        else:
            log.debug(f"Waiting {timeout}s for peer rendezvous...")
        self._peer_manager.wait_for_change(timeout)
        self.log_peer_count()

    def update_endpoint_ready(self, ready: bool) -> None:
        """Publish (or retract) this node's data-plane endpoint on every source."""
        for source in self._sources:
            source.update_endpoint_ready(ready)

    def endpoint_published(self) -> bool:
        """True once any source that can still carry traffic published the endpoint."""
        usable = [source for source in self._sources if not source.source_disabled()]
        return any(source.endpoint_published() for source in usable)

    def update_server_ready(self) -> None:
        """Publish that the local master server is ready on every source."""
        for source in self._sources:
            source.update_server_ready()

    def update_server_not_ready(self) -> None:
        """Publish that the local master server is no longer ready on every source."""
        for source in self._sources:
            source.update_server_not_ready()

    def set_connected_clients(self, count: int) -> None:
        """Publish the connected-client count on every source."""
        for source in self._sources:
            source.set_connected_clients(count)

    def request_sample(self) -> None:
        """Pull a sample from every source now."""
        for source in self._sources:
            source.request_sample()

    def set_master_eligibility(self, eligible: bool) -> None:
        """Withdraw or restore this node's master candidacy on every source."""
        for source in self._sources:
            source.set_master_eligibility(eligible)

    def refresh_election_advertisement(self) -> None:
        """Re-announce the election flags on every source."""
        for source in self._sources:
            source.refresh_election_advertisement()

    def set_connected_clients_provider(self, provider: Callable[[], int] | None) -> None:
        """Install the connected-client provider on every source."""
        super().set_connected_clients_provider(provider)
        for source in self._sources:
            source.set_connected_clients_provider(provider)

    def log_peer_count(self) -> None:
        """Log the shared alive-peer count once (each source would repeat it)."""
        if self._sources:
            self._sources[0].log_peer_count()

    def source_disabled(self) -> bool:
        """True only when every source stopped for good."""
        return all(source.source_disabled() for source in self._sources)

    def discovery_settled(self) -> bool:
        """True once any source's first query round has been answered or timed out."""
        return any(source.discovery_settled() for source in self._sources)

    def relay_dialer(self, target_cid: str) -> Any:
        """Pair the relay session through the first server that accepts it."""
        dialers = [
            dialer
            for source in self._sources
            if (dialer := source.relay_dialer(target_cid)) is not None
        ]
        return FailoverRelayDialer(dialers) if dialers else None

    def relay_acceptor(self, local_port: int) -> Any:
        """Register the relay listener on every server."""
        acceptors = [
            acceptor
            for source in self._sources
            if (acceptor := source.relay_acceptor(local_port)) is not None
        ]
        return GroupRelayAcceptor(acceptors) if acceptors else None
