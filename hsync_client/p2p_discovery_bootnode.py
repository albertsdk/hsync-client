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
    BootnodeFingerprintMismatch,
    BootnodeRefused,
    build_leave_frame,
    build_query_frame,
    build_refresh_frame,
    build_register_frame,
)
from hsync_client.constants import (
    BOOTNODE_DEFAULT_PORT,
    BOOTNODE_REFRESH_INTERVAL,
    BOOTNODE_REFRESH_INTERVAL_MIN,
    HSYNC_VERSION,
    PEER_TIMEOUT,
)
from hsync_client.nat_traversal import candidate_endpoints
from hsync_client.net_utils import get_local_ip
from hsync_client.p2p_discovery import DiscoveryFatalError, DiscoveryPlanState
from hsync_client.p2p_peers import HSYNC_P2P_LISTEN_PORT, Peer, PeerManager, get_free_port
from hsync_client.stun import ObservedAddress, observed_address
from hsync_client.upnp import UpnpMapper

__all__ = ["BootnodeDiscovery"]

# How often the lease loop wakes to register/refresh/query. The bootnode
# rate-limits ``query`` to 5/min per (IP, bucket) and ``register``/``refresh`` to
# 10/min (``boot-1.md``), so the client uses the spec's 30 s refresh window
# instead of the mDNS heartbeat cadence.
_DEFAULT_REFRESH_S = BOOTNODE_REFRESH_INTERVAL


class BootnodeDiscovery(DiscoveryPlanState):
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
        refresh_interval: float = _DEFAULT_REFRESH_S,
        p2p_key: str | None = None,
        fingerprints: tuple[str, ...] = (),
        tofu: bool = False,
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
            fingerprints=fingerprints,
            tofu=tofu,
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
        # Fatal failure that stopped the lease loop (today: a certificate pin
        # mismatch); ``wait_for_discovery`` re-raises it so the sync core aborts.
        self._fatal_error: BaseException | None = None

    # -- DiscoveryPlan surface -------------------------------------------------

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
        # Never contact a node we declared untrusted: a pin mismatch stops the
        # lease loop before any register, so there is no lease to release.
        if self._fatal_error is None:
            self._send_leave()
        if self._upnp is not None:
            self._upnp.unmap()
            self._upnp = None

    def wait_for_discovery(self, timeout: float) -> None:
        """Block until at least one live peer is known, or ``timeout`` elapses.

        The network cadence (register/refresh/query) belongs to the lease loop,
        which runs at :data:`BOOTNODE_REFRESH_INTERVAL` (spec: 30 s) to stay
        inside the bootnode's per-bucket rate limits. This wait therefore only
        parks on the shared peer-change event; it must never poll ``query``
        itself, or it would exhaust the 5/min per-(IP, bucket) query budget and
        the node would answer ``rate_limited`` for the rest of the window
        (regression: a 1 s poll loop turned discovery into a refusal storm).
        """
        if self._fatal_error is not None:
            # Even before the peer manager exists: a trust failure is terminal.
            raise DiscoveryFatalError(str(self._fatal_error)) from self._fatal_error
        if self._peer_manager is None:
            return
        if not self._discovery_announced:
            log.info(f"Waiting {timeout}s for peer discovery...")
            self._discovery_announced = True
        else:
            log.debug(f"Waiting {timeout}s for peer discovery...")

        # Event-driven wait on the shared peer-change condition (no network), in
        # short slices so a fatal trust failure recorded by the lease loop is
        # observed within ~1 s instead of after the whole window.
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self._fatal_error is not None:
                # The lease loop stopped on a trust failure: no peer can ever be
                # discovered. Surface it so the sync core exits fatally instead
                # of waiting for peers that will never arrive.
                raise DiscoveryFatalError(str(self._fatal_error)) from self._fatal_error
            if self._peer_manager.get_alive_peers():
                break
            remaining = deadline - time.monotonic()
            if timeout > 0 and remaining <= 0:
                break
            self._peer_manager.wait_for_change(1.0 if timeout <= 0 else min(remaining, 1.0))

        peers = self._peer_manager.get_alive_peers()
        if len(peers) != self._last_peer_count:
            self._last_peer_count = len(peers)
            if peers:
                log.info(f"Bootnode discovery: found {len(peers)} peer(s)")

    def update_endpoint_ready(self, ready: bool) -> None:
        """Publish (or retract) this node's data-plane endpoint."""
        self._endpoint_ready = ready
        self._wake.set()

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

    # -- Lease loop ------------------------------------------------------------

    def _lease_loop(self) -> None:
        first = True
        while self._running and not self._stop.is_set():
            backoff = 0.0
            action = "query"
            try:
                if first:
                    action = "register"
                    self._register(publish_endpoint=False)
                    first = False
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
            except BootnodeFingerprintMismatch as exc:
                # Fatal: the bootnode failed authentication. Never retry an
                # unpinned/untrusted node (the token would leak to an attacker).
                # Record it so the sync core aborts instead of waiting forever.
                self._fatal_error = exc
                log.error(
                    f"Bootnode {self._bootnode_host}:{self._bootnode_port} trust failed: {exc}"
                )
                return
            except BootnodeRefused as exc:
                # A uniform refusal (rate limit / token) is a valid boot/1 answer:
                # keep the lease state and retry at the normal cadence instead of
                # resetting to a full register (which would deepen the refusal).
                # Honour the node's ``retry_after`` so a rate-limited client waits
                # at least as long as the node asked, never less.
                self._log_refusal(exc)
                backoff = float(exc.retry_after or 0)
            except BootnodeError as exc:
                log.warning(
                    f"Bootnode {self._bootnode_host}:{self._bootnode_port} unreachable: {exc}"
                )
                first = True  # re-register once the node is reachable again
            except Exception as exc:
                log.warning(f"Bootnode lease loop error: {exc}")
            else:
                # One concise line per successful lease cycle instead of the
                # per-frame QUIC/TLS chatter aioquic logs at INFO (now DEBUG).
                peers = self._peer_manager.get_alive_peers() if self._peer_manager else []
                log.info(
                    f"Bootnode {self._bootnode_host}:{self._bootnode_port}: "
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

    def _log_refusal(self, exc: BootnodeRefused) -> None:
        """Report a uniform boot/1 refusal once per error, then at DEBUG."""
        message = (
            f"Bootnode {self._bootnode_host}:{self._bootnode_port} refused the lease "
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
        generation = self._advert_generation
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
                server_ready=self._server_ready,
                force_master=self._force_master,
                never_master=self._never_master,
                startup_time=self._startup_time,
                sync_direction=self._sync_direction,
            )
        )
        if ack is not None:
            self._observed = observed_address(ack)
            if publish_endpoint:
                self._published_endpoints = True
            self._sent_advert_generation = generation
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
                f"Bootnode {self._bootnode_host}:{self._bootnode_port} query refused: "
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
        including the election advertisement (``server_ready``/``force_master``/
        ``never_master``/``startup_time``/``sync_direction``) — is relayed
        verbatim by the node, so a bootnode-discovered peer enters the shared
        election with the same inputs as a mDNS-discovered one. They are
        unauthenticated claims: the data plane pins the peer certificate before
        any file is exchanged.
        """
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
            server_ready=wire["server_ready"],
            force_master=wire["force_master"],
            never_master=wire["never_master"],
            startup_time=wire["startup_time"],
            sync_direction=wire["sync_direction"],
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
