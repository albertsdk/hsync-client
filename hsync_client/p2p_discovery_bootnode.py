# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Bootnode discovery plan for ``hsync p2p`` (cross-network, ``boot/1``).

The second implementation of the :class:`~hsync_client.p2p_discovery.DiscoveryPlan`
interface: instead of browsing mDNS, it keeps a lease on a ``boot/1`` bootstrap
node (register → refresh, TTL 60 s), queries it for a random peer sample and
feeds the shared :class:`~hsync_client.p2p_peers.PeerManager`. Master election,
pairing and the data-plane are unchanged — this module only supplies peers.

This source is optional: when the node cannot be reached the lease loop retries
(``BootnodeError``), and when it is not trusted the source is disabled for the
rest of the run instead of failing the whole run, so the remaining sources keep
the group reachable.

Lease semantics (spec ``boot-1.md``): the first ``register`` has no endpoint
(the public address is not known yet); the ``ack`` carries the observed
endpoint (STUN-echo). Once the handler says the data-plane endpoint is ready,
the adapter maps the TCP port with UPnP (best-effort) and sends a **second
``register``** that publishes ``endpoints[]`` and the advisory ``capacity``
hint. ``refresh`` renews the TTL and the ``connected`` count only.

Consumers: ``hsync-client`` (p2p subcommand), tests.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from hsync_common.endpoint import format_authority
from hsync_common.protocol.bootnode import BootPeer, derive_bucket_token
from hsync_common.protocol.tls_trust import TrustPolicy
from loguru import logger as log

from hsync_client.boot_client import (
    BootnodeClient,
    BootnodeError,
    BootnodeFingerprintMismatch,
    BootnodeNotTrusted,
    BootnodeRefused,
    TrustPrompt,
    build_leave_frame,
    build_query_frame,
    build_refresh_frame,
    build_register_frame,
)
from hsync_client.boot_relay import (
    FailoverRelayDialer,
    GroupRelayAcceptor,
    RelayAcceptor,
    RelayDialer,
)
from hsync_client.constants import (
    BOOTNODE_DEFAULT_PORT,
    BOOTNODE_REFRESH_INTERVAL,
    BOOTNODE_REFRESH_INTERVAL_MIN,
    HSYNC_VERSION,
    PEER_TIMEOUT,
)
from hsync_client.events import emit_warning
from hsync_client.nat_traversal import CandidateEndpoint, candidate_endpoints
from hsync_client.net_utils import get_local_ip
from hsync_client.net_watch import (
    NetworkChangeMonitor,
    NetworkSignature,
    get_network_monitor,
)
from hsync_client.p2p_discovery import DiscoveryPlanState
from hsync_client.p2p_peers import (
    HSYNC_P2P_LISTEN_PORT,
    Peer,
    PeerManager,
    dial_candidates,
    get_free_port,
)
from hsync_client.stun import ObservedAddress, observed_address
from hsync_client.upnp import UpnpMapper

__all__ = ["BootnodeDiscovery", "MultiBootnodeDiscovery", "SharedUpnpMapping"]

# How often the lease loop wakes to register/refresh/query. The bootnode
# rate-limits ``query`` to 5/min per (IP, bucket) and ``register``/``refresh`` to
# 10/min (``boot-1.md``), so the client uses the spec's 30 s refresh window
# instead of the mDNS heartbeat cadence.
_DEFAULT_REFRESH_S = BOOTNODE_REFRESH_INTERVAL

# Test-only override of the endpoint list a master publishes. Test-only: no CLI
# flag, no config field, no packaging/docs surface — it exists so a black-box
# test can make a real master advertise undialable candidates on a single host
# and force the relayed path (``tests/test_bb_p2p_bootnode_relay.py``).
_TEST_ADVERTISED_ENDPOINTS_ENV = "HSYNC_P2P_TEST_ADVERTISED_ENDPOINTS"


def _test_advertised_endpoints() -> list[CandidateEndpoint] | None:
    """Return ``HSYNC_P2P_TEST_ADVERTISED_ENDPOINTS`` as wire candidates.

    The value is comma-separated ``host:port`` pairs (TCP, IPv4). When set it
    replaces the UPnP/STUN-derived candidates in the register frame, so a master
    can be made to publish exactly what a CGNAT deployment can — guesses that are
    useless to a remote peer — while its real listener keeps serving loopback for
    the relay bridge. ``None`` (the production case) keeps the derived list.
    """
    raw = os.environ.get(_TEST_ADVERTISED_ENDPOINTS_ENV)
    if not raw:
        return None
    endpoints: list[CandidateEndpoint] = []
    for part in raw.split(","):
        host, sep, port_text = part.strip().rpartition(":")
        if not sep or not host:
            raise ValueError(
                f"{_TEST_ADVERTISED_ENDPOINTS_ENV} must be comma-separated host:port pairs"
            )
        try:
            port = int(port_text)
        except ValueError:
            raise ValueError(
                f"{_TEST_ADVERTISED_ENDPOINTS_ENV} must be comma-separated host:port pairs"
            ) from None
        if not 1 <= port <= 65535:
            raise ValueError(f"{_TEST_ADVERTISED_ENDPOINTS_ENV} port out of range: {port}")
        endpoints.append(CandidateEndpoint("tcp", host, port))
    return endpoints or None


class SharedUpnpMapping:
    """One UPnP mapping shared by every discovery source of this process.

    N sources publish the same data-plane port, so mapping per source would
    create N mappings and up to N different external ports for one service. The
    first publisher creates the mapping and every source reads the same value; a
    network change invalidates it for all of them.
    """

    def __init__(self, mapper_factory: Callable[[], UpnpMapper] = UpnpMapper) -> None:
        self._factory = mapper_factory
        self._lock = threading.Lock()
        self._mapper: UpnpMapper | None = None
        self._external_port: int | None = None

    def external_port(self, local_port: int) -> int | None:
        """Map *local_port* once; ``None`` when the router exposes no mapping."""
        with self._lock:
            if self._external_port is None:
                if self._mapper is None:
                    self._mapper = self._factory()
                self._external_port = self._mapper.map_port(local_port)
            return self._external_port

    def unmap(self) -> None:
        """Drop the mapping so the next publish re-creates it (best-effort)."""
        with self._lock:
            mapper, self._mapper, self._external_port = self._mapper, None, None
        if mapper is not None:
            try:
                mapper.unmap()
            except Exception as exc:  # noqa: BLE001 - best-effort teardown
                log.debug(f"UPnP unmap failed: {exc}")


class BootnodeDiscovery(DiscoveryPlanState):
    """``DiscoveryPlan`` backed by a ``boot/1`` bootstrap node."""

    def __init__(
        self,
        cid: str,
        bucket: str,
        *,
        bootnode_host: str,
        bootnode_port: int = BOOTNODE_DEFAULT_PORT,
        bootnode_ports: tuple[int, ...] = (),
        port: int = HSYNC_P2P_LISTEN_PORT,
        group: str | None = None,
        use_https: bool = True,
        cert_fingerprint: str | None = None,
        prefer_master: bool = False,
        never_master: bool = False,
        startup_time: float = 0.0,
        sync_direction: str = "bidirectional",
        peer_timeout: float = PEER_TIMEOUT,
        refresh_interval: float = _DEFAULT_REFRESH_S,
        p2p_key: str | None = None,
        fingerprints: tuple[str, ...] = (),
        policy: TrustPolicy = TrustPolicy.ask,
        prompt: TrustPrompt | None = None,
        upnp_enabled: bool = False,
        shared_upnp: SharedUpnpMapping | None = None,
        capacity: int = 0,
        peer_download_enabled: bool = False,
        client_factory: Callable[..., BootnodeClient] = BootnodeClient,
        upnp_factory: Callable[[], UpnpMapper] = UpnpMapper,
        local_ip: str | None = None,
    ) -> None:
        self._bootnode_host = bootnode_host
        self._bootnode_port = bootnode_port
        self._init_common_fields(
            cid=cid,
            bucket=bucket,
            group=group,
            use_https=use_https,
            cert_fingerprint=cert_fingerprint,
            prefer_master=prefer_master,
            never_master=never_master,
            startup_time=startup_time,
            sync_direction=sync_direction,
            peer_timeout=peer_timeout,
        )
        self._refresh_interval = max(float(BOOTNODE_REFRESH_INTERVAL_MIN), refresh_interval)
        # Earliest monotonic time the next ``query`` may run: keeps the client
        # inside the bootnode's 5/min per-(IP, bucket) query budget and honours a
        # ``rate_limited`` ``retry_after`` instead of hammering the node.
        self._next_query_at = 0.0
        self._upnp_enabled = upnp_enabled
        self._capacity = capacity if peer_download_enabled else 0
        self._local_ip = local_ip
        self._port = port if port != 0 else get_free_port()

        self._client: Any = client_factory(
            bootnode_host,
            bootnode_port,
            ports=bootnode_ports,
            fingerprints=fingerprints,
            policy=policy,
            prompt=prompt,
        )
        self._upnp_factory = upnp_factory
        # One mapping per process, not per source: the multi-source plan passes a
        # shared holder, a single-source plan owns the one it creates here.
        self._shared_upnp = shared_upnp
        if self._shared_upnp is None and upnp_enabled:
            self._shared_upnp = SharedUpnpMapping(upnp_factory)
        self._owns_upnp_mapping = shared_upnp is None
        self._token = derive_bucket_token(p2p_key.encode("utf-8"), bucket) if p2p_key else None

        self._peer_manager: PeerManager | None = None
        self._running = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

        self._observed: ObservedAddress | None = None
        self._endpoint_ready = False
        self._published_endpoints = False
        # Address carried by the last endpoint register. Only a full register
        # publishes endpoints (a refresh does not), so this is what detects that
        # the candidates relayed by the node went stale after a network change.
        self._published_local_ip: str | None = None
        self._external_port: int | None = None
        self._network_monitor: NetworkChangeMonitor | None = None

        self._server_ready = False
        self._connected_clients = 0
        self._connected_clients_provider: Callable[[], int] | None = None

        # Advertisement publication: ``register`` is the only frame carrying the
        # election advertisement (and the endpoint/capacity), so a role change
        # bumps the generation and the lease loop re-registers until the node
        # acknowledged the new value. A generation (not a boolean) cannot lose a
        # flip that lands while a re-register is in flight.
        self._advert_generation = 0
        self._sent_advert_generation = 0
        # Uniform refusals (rate limit / token) are legitimate boot/1 answers:
        # log the first occurrence of each error at WARNING, repeats at DEBUG,
        # so a persistent misconfiguration stays visible without spamming.
        self._refusals_logged: set[str] = set()

        self._discovery_announced = False
        # Alive-peer count at the previous discovery window, so the "found N
        # peer(s)" line is emitted only when the count changes.
        self._last_peer_count = 0
        # This source stopped for good (the node is not trusted). The lease loop
        # has exited, so it delivers no more peers for the rest of the run while
        # the combined plan keeps serving its remaining sources.
        self._source_disabled = False
        # The node failed authentication: never contact it again, not even for
        # the departure frame.
        self._untrusted = False

    # -- DiscoveryPlan surface -------------------------------------------------

    def start(
        self, register_service: bool = True, peer_manager: PeerManager | None = None
    ) -> PeerManager:
        """Start the lease loop and return the shared :class:`PeerManager`.

        ``peer_manager`` lets a combined plan share one manager across both
        discovery sources (mDNS + bootnode); it is built from this plan's
        configuration when omitted.
        """
        manager = peer_manager or self.create_peer_manager()
        self._peer_manager = manager
        # Resolve the local role before the first wait: a lone --prefer-master
        # node must become master and serve immediately so a joining slave can
        # connect (the election otherwise runs only on peer events).
        manager.elect()
        self._running = True
        self._thread = threading.Thread(
            target=self._lease_loop, name="p2p-bootnode-lease", daemon=True
        )
        self._thread.start()
        # Wake the lease loop on a network change so a changed address is
        # republished at once instead of after the next 30 s lease tick.
        self._network_monitor = get_network_monitor()
        self._network_monitor.subscribe(self._on_network_change)
        return manager

    def stop(self) -> None:
        """Stop the lease loop, announce departure and tear the UPnP mapping down."""
        if not self._running and self._thread is None:
            return
        self._running = False
        self._stop.set()
        self._wake.set()
        if self._network_monitor is not None:
            self._network_monitor.unsubscribe(self._on_network_change)
            self._network_monitor = None
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            self._thread = None
        # Never contact a node we declared untrusted: a trust refusal stops the
        # lease loop before any register, so there is no lease to release.
        if not self._untrusted:
            self._send_leave()
        if self._owns_upnp_mapping and self._shared_upnp is not None:
            self._shared_upnp.unmap()

    def wait_for_discovery(self, timeout: float) -> None:
        """Block until at least one live peer is known, or ``timeout`` elapses.

        The network cadence (register/refresh/query) belongs to the lease loop,
        which runs at :data:`BOOTNODE_REFRESH_INTERVAL` (spec: 30 s) to stay
        inside the bootnode's per-bucket rate limits. This wait therefore only
        parks on the shared peer-change event; it never polls ``query`` itself,
        or it would exhaust the 5/min per-(IP, bucket) query budget and the node
        would answer ``rate_limited`` for the rest of the window.
        """
        if self._peer_manager is None:
            return
        if not self._discovery_announced:
            log.info(f"Waiting {timeout}s for peer discovery...")
            self._discovery_announced = True
        else:
            log.debug(f"Waiting {timeout}s for peer discovery...")

        # Event-driven wait on the shared peer-change condition (no network),
        # in short slices: peers from the other sources keep arriving while
        # this one is parked.
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self._peer_manager.get_alive_peers():
                break
            remaining = deadline - time.monotonic()
            if timeout > 0 and remaining <= 0:
                break
            self._peer_manager.wait_for_change(1.0 if timeout <= 0 else min(remaining, 1.0))

        self.log_peer_count()

    def source_disabled(self) -> bool:
        """True when this source delivers no more peers for the rest of the run.

        Set when the node is not trusted (policy ``ask`` without a terminal, a
        changed pin, ``strict`` without one). The combined plan reads it so it
        stops requiring this source's endpoint publication; the sync core never
        sees it: a disabled source is a degraded discovery state, never a fatal
        run, and the remaining sources keep the group reachable.
        """
        return self._source_disabled

    def log_peer_count(self) -> None:
        """Log the bootnode discovery result when the alive-peer count changes."""
        peers = self._peer_manager.get_alive_peers() if self._peer_manager else []
        if len(peers) != self._last_peer_count:
            self._last_peer_count = len(peers)
            if peers:
                log.info(f"Bootnode discovery: found {len(peers)} peer(s)")

    def update_endpoint_ready(self, ready: bool) -> None:
        """Publish (or retract) this node's data-plane endpoint."""
        self._endpoint_ready = ready
        self._wake.set()

    def relay_dialer(self, target_cid: str) -> RelayDialer | None:
        """Tunnel factory towards *target_cid* through the node's data relay.

        The relay is the last resort of ``_select_master_endpoint``: it is only
        asked for after every advertised candidate failed, which is exactly the
        case of two peers behind carrier-grade NAT. The caller owns the returned
        dialer (``open()`` then ``close()``).
        """
        if not self._running:
            return None
        return RelayDialer(
            self._client,
            cid=self._cid,
            bucket=self._bucket,
            target_cid=target_cid,
            group=self._group,
            bucket_token=self._token,
        )

    def relay_acceptor(self, local_port: int) -> RelayAcceptor | None:
        """Listener that accepts relayed sessions and feeds the local master."""
        if not self._running:
            return None
        return RelayAcceptor(
            self._client,
            cid=self._cid,
            bucket=self._bucket,
            local_port=local_port,
            group=self._group,
            bucket_token=self._token,
        )

    def endpoint_published(self) -> bool:
        """True once the bootnode has been sent our endpoint (second register)."""
        return self._published_endpoints

    def update_server_ready(self) -> None:
        """The local master server is ready to accept peers.

        Publishes the incumbency twice: in the bootnode advertisement (so relayed
        peers rank this node as the serving master, exactly like the mDNS
        ``server_ready`` TXT flag) and in the shared :class:`PeerManager` (an
        incumbent must also win its *own* election, otherwise the next relayed
        seeder would demote it and the role would flap).
        """
        self._server_ready = True
        self._advert_generation += 1
        self._wake.set()
        self._notify_local_server_ready(True)

    def update_server_not_ready(self) -> None:
        """The local master server is no longer ready (mastership yielded).

        Clears both the relayed advertisement and the local incumbent flag: a
        yielded node left marked ``server_ready`` would be a *zombie incumbent*
        — still ranked top of every peer's election while no longer serving
        (ADR-0027).
        """
        self._server_ready = False
        self._advert_generation += 1
        self._wake.set()
        self._notify_local_server_ready(False)

    def _notify_local_server_ready(self, ready: bool) -> None:
        """Mirror the local incumbency into the shared peer state (once started)."""
        manager = self._peer_manager
        if manager is not None:
            manager.set_local_server_ready(ready)

    def set_connected_clients(self, count: int) -> None:
        """Publish the number of connected peer clients."""
        self._connected_clients = count
        self._wake.set()

    def refresh_election_advertisement(self) -> None:
        """Re-publish the election flags (``prefer_master``/``never_master``).

        The lease loop rebuilds the register frame from the live flags, so a
        generation bump is enough to re-announce them.
        """
        self._advert_generation += 1
        self._wake.set()

    def _on_network_change(self, signature: NetworkSignature) -> None:
        """Wake the lease loop so a changed local address is republished.

        The loop owns every publication flag: it compares the freshly sampled
        address with the one carried by the last endpoint register and forces a
        full register when they differ. Waking it keeps the republish prompt
        instead of waiting up to one lease interval. Called on the
        network-monitor thread.
        """
        if not signature.online:
            return
        self._wake.set()

    def _republish_stale_endpoint(self) -> bool:
        """Invalidate the published endpoints when the local address changed.

        ``refresh`` carries no endpoints (only a full ``register`` does), so
        while the lease survives a network change the node keeps relaying the
        pre-outage candidates: the address a DHCP reassignment replaced, plus a
        UPnP mapping that belongs to the previous network. Returns ``True`` when
        the caller must publish the endpoints again.
        """
        if not (self._endpoint_ready or self._server_ready or self._published_endpoints):
            return False
        if self._published_local_ip is None:
            return False
        current = self._local_ip or get_local_ip()
        if current == self._published_local_ip:
            return False
        log.info(
            f"Local address changed from {self._published_local_ip} to {current}; "
            "republishing the bootnode endpoint"
        )
        self._published_endpoints = False
        if self._shared_upnp is not None:
            # The mapping was created on the previous network (another router or
            # external address): drop it so the next publish maps the live port.
            self._shared_upnp.unmap()
        self._external_port = None
        return True

    # -- Lease loop ------------------------------------------------------------

    def _lease_loop(self) -> None:
        first = True
        while self._running and not self._stop.is_set():
            backoff = 0.0
            action = "query"
            try:
                if first:
                    action = "register"
                    # A re-register after an unreachable node is also the first
                    # chance to republish endpoints that a network change made
                    # stale: publish them in this frame (which re-creates the
                    # UPnP mapping for the new network) instead of one tick later.
                    if self._republish_stale_endpoint():
                        self._publish_endpoint()
                    else:
                        self._register(publish_endpoint=False)
                    first = False
                elif self._republish_stale_endpoint():
                    action = "publish endpoint"
                    self._publish_endpoint()
                elif self._sent_advert_generation != self._advert_generation:
                    # The role changed (promotion or yield): re-register so the
                    # node relays the fresh advertisement immediately instead of
                    # advertising a stale role until the next full register.
                    action = "republish"
                    self._republish_advert()
                elif (self._endpoint_ready or self._server_ready) and not self._published_endpoints:
                    action = "publish endpoint"
                    self._publish_endpoint()
                elif self._server_ready or self._published_endpoints:
                    action = "refresh"
                    self._refresh()
                self._query_peers()
            except BootnodeNotTrusted as exc:
                # Policy ``ask`` could not establish trust (declined, or no
                # terminal). Never re-prompt every cycle, and never contact the
                # node again — but it is an optional cross-network source, so
                # disable it instead of ending the run the local network can
                # still serve.
                self._disable_source(
                    f"Bootnode {format_authority(self._bootnode_host, self._bootnode_port)} "
                    f"not trusted: {exc}",
                )
                return
            except BootnodeFingerprintMismatch as exc:
                # The bootnode failed authentication: never retry an
                # unpinned/untrusted node (the token would leak to an attacker).
                # The source is disabled for the rest of the run while the other
                # discovery sources keep the group reachable; the full SSH-style
                # recovery block is logged here, once.
                self._disable_source(str(exc), guidance=exc.guidance)
                return
            except BootnodeRefused as exc:
                # A uniform refusal (token / rate limit / node capacity) is a valid
                # boot/1 answer: keep the lease state and retry at the normal
                # cadence instead of resetting to a full register (which would
                # deepen a rate limit). Honour the node's ``retry_after`` so a
                # rate-limited or overloaded client waits at least as long as the
                # node asked, never less.
                self._log_refusal(exc)
                backoff = float(exc.retry_after or 0)
            except BootnodeError as exc:
                message = (
                    f"Bootnode {format_authority(self._bootnode_host, self._bootnode_port)} "
                    f"unreachable: {exc}"
                )
                log.warning(message)
                # Structured counterpart: discovery keeps retrying, so this is a
                # warning (the run continues), not an error. Deduplicated, so a
                # flapping node cannot flood the front-end.
                emit_warning(message, source="bootnode", reason="unreachable")
                first = True  # re-register once the node is reachable again
            except Exception as exc:
                log.warning(f"Bootnode lease loop error: {exc}")
            else:
                # One concise line per successful lease cycle instead of the
                # per-frame QUIC/TLS chatter aioquic logs at INFO (now DEBUG).
                peers = self._peer_manager.get_alive_peers() if self._peer_manager else []
                log.info(
                    f"Bootnode {format_authority(self._bootnode_host, self._bootnode_port)}: "
                    f"{action} ok ({len(peers)} peer(s))"
                )
            self._wake.wait(timeout=max(self._refresh_interval, backoff))
            self._wake.clear()

    def _republish_advert(self) -> None:
        """Re-``register`` with the current advertisement (role changed).

        ``register`` is the only frame that carries the advertisement, so a
        promotion/yield republishes through it (with the endpoint/capacity state
        unchanged). The sent marker is updated by :meth:`_register` only after the
        node acknowledged the frame, so a transient failure retries on the next
        wake instead of stranding a stale role.
        """
        if self._endpoint_ready or self._server_ready or self._published_endpoints:
            self._publish_endpoint()
        else:
            self._register(publish_endpoint=False)

    def _disable_source(self, message: str, *, guidance: str | None = None) -> None:
        """Stop this discovery source for the rest of the run.

        A node that is not trusted must never be contacted again (the
        ``bucket_token`` would leak to an attacker), but it cannot remove the
        local network either: the combined plan keeps serving its remaining
        sources, so the failure is reported as a warning and the source simply
        delivers no more peers. ``guidance`` carries the long SSH-style recovery
        block to the log only — the structured warning stays one line.
        """
        self._source_disabled = True
        self._untrusted = True
        log.error(guidance or message)
        emit_warning(
            f"{message} — bootnode discovery stopped, local network discovery continues",
            source="bootnode",
            reason="untrusted",
        )

    def _log_refusal(self, exc: BootnodeRefused) -> None:
        """Report a uniform boot/1 refusal once per error, then at DEBUG."""
        message = (
            f"Bootnode {format_authority(self._bootnode_host, self._bootnode_port)} refused the lease "
            f"frame: {exc.error}"
        )
        if exc.retry_after:
            message += f" (retry after {exc.retry_after}s)"
        if exc.error in self._refusals_logged:
            log.debug(message)
        else:
            self._refusals_logged.add(exc.error)
            log.warning(message)

    def _current_connected(self) -> int:
        if self._connected_clients_provider is not None:
            try:
                return int(self._connected_clients_provider())
            except Exception:
                return self._connected_clients
        return self._connected_clients

    def _register(self, *, publish_endpoint: bool) -> None:
        """Send ``register`` with the current lease + election advertisement.

        The advertisement is read once, before the frame is built, and recorded
        as *sent* only after the node acknowledged it: a role flip landing
        mid-exchange leaves the generation unsent, so the lease loop republishes
        on the next wake instead of advertising a stale role indefinitely.
        """
        cert_fp = self._cert_fingerprint
        if not cert_fp:
            # ``cert_fp`` is the pin the requester compares: the frame carries a
            # full SHA-256 digest or it is not sent at all (an empty/placeholder
            # value would be an invalid frame — and a pin nobody can compare).
            log.warning("Bootnode register skipped: no local certificate fingerprint yet")
            return
        generation = self._advert_generation
        # Sample the address once: it both builds the endpoint candidates and
        # records what this publication carries, so the comparison against a
        # later network change is exact.
        local_ip = self._local_ip or get_local_ip()
        endpoints = self._endpoint_wire(local_ip) if publish_endpoint else []
        ack = self._client.register(
            build_register_frame(
                cid=self._cid,
                bucket=self._bucket,
                cert_fp=cert_fp,
                version=_client_version(),
                group=self._group,
                bucket_token=self._token,
                capacity=self._capacity if publish_endpoint else 0,
                endpoints=endpoints,
                connected=self._current_connected() if self._server_ready else None,
                server_ready=self._server_ready,
                prefer_master=self._prefer_master,
                never_master=self._never_master,
                startup_time=self._startup_time,
                sync_direction=self._sync_direction,
            )
        )
        if ack is not None:
            self._observed = observed_address(ack)
            if publish_endpoint:
                self._published_endpoints = True
                self._published_local_ip = local_ip
            self._sent_advert_generation = generation
            log.debug(
                "Bootnode register ok: observed "
                f"{format_authority(self._observed.host, self._observed.port)}, "
                f"ttl {ack['ttl']}s"
            )

    def _publish_endpoint(self) -> None:
        if self._shared_upnp is not None:
            self._external_port = self._shared_upnp.external_port(self._port)
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
            # A serving master's candidates must not be dropped with the lease:
            # publishing again is what makes it reachable for the next peer.
            self._register(publish_endpoint=self._endpoint_ready or self._server_ready)

    def _query_peers(self) -> None:
        if self._peer_manager is None:
            return
        now = time.monotonic()
        if now < self._next_query_at:
            # Still inside the query budget window (or a node-imposed backoff):
            # skip this tick instead of adding to the 5/min per-(IP, bucket)
            # budget and getting ``rate_limited``.
            return
        response = self._client.query(
            build_query_frame(bucket=self._bucket, group=self._group, bucket_token=self._token)
        )
        if response is None:
            return
        if response.get("error"):
            # Uniform refusal (rate limit / token): the sample is empty by
            # contract — keep the peers already known and honour ``retry_after``
            # (falling back to the normal cadence) so the client backs off
            # instead of deepening the refusal.
            retry_after = response.get("retry_after")
            if isinstance(retry_after, int) and retry_after > 0:
                self._next_query_at = now + float(retry_after)
            else:
                self._next_query_at = now + self._refresh_interval
            log.debug(
                f"Bootnode {format_authority(self._bootnode_host, self._bootnode_port)} query refused: "
                f"{response['error']}"
            )
            return
        self._next_query_at = now + self._refresh_interval
        for wire_peer in response["peers"]:
            peer = self._peer_from_wire(wire_peer)
            if peer is not None and peer.cid != self._cid:
                self._peer_manager.add_peer(peer)

    def _peer_from_wire(self, wire: BootPeer) -> Peer | None:
        """Map a relayed peer to the shared :class:`Peer`; skip unreachable ones.

        ``reachable=false`` entries carry no dialable data-plane endpoint (a
        bootstrap-only register), so they are not peers yet. Every other field —
        including the election advertisement (``server_ready``/``prefer_master``/
        ``never_master``/``startup_time``/``sync_direction``) — is relayed
        verbatim by the node, so a bootnode-discovered peer enters the shared
        election with the same inputs as a mDNS-discovered one. They are
        unauthenticated claims: the data plane pins the peer certificate before
        any file is exchanged.

        The full ``endpoints`` candidate list is kept on the peer and a
        same-subnet candidate is promoted to the primary address, so two peers
        behind the same NAT dial the local path instead of the public endpoint
        (which has no hairpin route on most home routers).
        """
        if not wire.get("reachable", True):
            return None
        candidates = tuple(
            (endpoint["host"], endpoint["port"])
            for endpoint in wire["endpoints"]
            if endpoint["proto"] == "tcp"
        )
        if not candidates:
            return None
        # Promote a same-subnet/loopback candidate to the primary address (the
        # ordering policy lives once, in dial_candidates), so a same-NAT peer
        # dials the local path; the full list is kept for connect-time fallback.
        primary_host, primary_port = dial_candidates(
            candidates, candidates[0][0], candidates[0][1], get_local_ip()
        )[0]
        return Peer(
            cid=wire["cid"],
            bucket=wire["bucket"],
            address=primary_host,
            port=primary_port,
            version=wire.get("version", "0.0.0"),
            group=wire.get("group"),
            use_https=True,
            cert_fingerprint=wire.get("cert_fp"),
            peer_download_enabled=False,
            server_ready=wire["server_ready"],
            connected=int(wire.get("connected") or 0),
            prefer_master=wire["prefer_master"],
            never_master=wire["never_master"],
            startup_time=wire["startup_time"],
            sync_direction=wire["sync_direction"],
            endpoints=candidates,
        )

    def _endpoint_wire(self, local_ip: str) -> list[dict[str, object]]:
        override = _test_advertised_endpoints()
        if override is not None:
            return [candidate.as_wire() for candidate in override]
        return [
            candidate.as_wire()
            for candidate in candidate_endpoints(
                local_ip=local_ip,
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


class MultiBootnodeDiscovery(DiscoveryPlanState):
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

    The children must be built with the same serving ``port``; ``build_discovery_plan``
    resolves it once for exactly that reason.
    """

    def __init__(
        self,
        sources: Sequence[BootnodeDiscovery],
        *,
        shared_upnp: SharedUpnpMapping | None = None,
    ) -> None:
        if not sources:
            raise ValueError("at least one discovery source is required")
        self._sources = list(sources)
        self._shared_upnp = shared_upnp
        self._port = self._sources[0].port
        self._peer_manager: PeerManager | None = None
        self._connected_clients_provider: Callable[[], int] | None = None
        self._discovery_announced = False

    # -- DiscoveryPlan surface -------------------------------------------------

    def create_peer_manager(self) -> PeerManager:
        """Refuse: the composite never owns a manager of its own.

        ``start`` builds exactly one manager (from the first source's config) and
        injects it into every source, so peer state is single. Building another
        one here would split discovery and the election; the error makes that
        misuse loud instead of silent.
        """
        raise RuntimeError(
            "MultiBootnodeDiscovery.start() creates the single shared PeerManager; "
            "the composite cannot build another one"
        )

    def start(self, register_service: bool = True) -> PeerManager:
        """Start every lease loop over one shared :class:`PeerManager`."""
        manager = self._sources[0].create_peer_manager()
        self._peer_manager = manager
        for source in self._sources:
            source.start(register_service=register_service, peer_manager=manager)
        log.info(f"P2P discovery started ({len(self._sources)} discovery server(s))")
        return manager

    def stop(self) -> None:
        """Stop every lease loop and drop the shared UPnP mapping (reverse order)."""
        for source in reversed(self._sources):
            source.stop()
        if self._shared_upnp is not None:
            self._shared_upnp.unmap()

    def wait_for_discovery(self, timeout: float) -> None:
        """Wait once on the shared manager: peers can arrive from any source."""
        if self._peer_manager is None:
            return
        if not self._discovery_announced:
            log.info(f"Waiting {timeout}s for peer discovery...")
            self._discovery_announced = True
        else:
            log.debug(f"Waiting {timeout}s for peer discovery...")
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
