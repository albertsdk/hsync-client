# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Shared P2P peer state, staleness and master-election glue (SSOT ``p2p/1``).

This module holds the rendezvous-agnostic half of the peer mode: the ``Peer``
record, its immutable ``PeerSnapshot`` projection, the ``PeerManager`` that
tracks liveness/staleness and runs the election (ordering from
``hsync_common.protocol.p2p``), and the small socket helper ``get_free_port``.

Both rendezvous plans feed it: the mDNS plan (``p2p_rendezvous_mdns``) and the
rendezvous plan (``p2p_rendezvous_rdv``). The ``p2p_rendezvous`` module defines
the minimal interface they expose to ``p2p_handler``.

Consumers: ``hsync-client`` (p2p handler, the two rendezvous plans, tests).
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from hsync_common.clock import now_corrected
from hsync_common.endpoint import build_url
from hsync_common.protocol.p2p import (
    SYNC_DIRECTION_BIDIRECTIONAL,
    ElectionCandidate,
    election_sort_key,
    rank_election_candidates,
)
from loguru import logger as log

from hsync_client.constants import (
    PEER_TIMEOUT,
    ActionLabel,
)
from hsync_client.events import UiEventName, emit_event
from hsync_client.net_utils import same_subnet

# mDNS service type for hsync (used for the TXT-flag helpers shared with the
# mDNS plan); the rendezvous plan does not use it.
HSYNC_SERVICE_TYPE = "_hsync._tcp.local."

# Default port for the embedded HTTP server (when acting as master).
# 0 means "use a random available port".
HSYNC_P2P_LISTEN_PORT = 0

# Name of the heartbeat source every manager has: the mDNS plan announces a peer
# (and re-announces it every ``HEARTBEAT_INTERVAL``), so a peer it announced is
# stale once ``peer_timeout`` passes without another announce.
HEARTBEAT_SOURCE = "heartbeat"


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


def server_url(address: str, port: int, *, use_https: bool) -> str:
    """Build the base HTTP(S) URL for a peer data-plane endpoint."""
    protocol = "https" if use_https else "http"
    return build_url(protocol, address, port)


def dial_candidates(
    endpoints: Iterable[tuple[str, int]] | None,
    address: str,
    port: int,
    local_ips: Sequence[str] | None = None,
) -> tuple[tuple[str, int], ...]:
    """Order a peer's dial candidates, host/LAN candidates first.

    Same-subnet candidates (and loopback) are tried before the externally
    advertised ones, so two peers behind the same NAT connect over the local
    path — the public endpoint has no hairpin route on most home routers.
    Candidates not known to be local keep their advertised order. Falls back to
    the primary ``(address, port)`` when no candidate list is available (the
    mDNS plan, or a caller that only knows one endpoint).

    ``local_ips`` is every local interface address (a multi-homed node checks
    each, so an interface on the peer's subnet makes that candidate local).
    ``endpoints`` is typed loosely because callers may pass any iterable of
    ``(host, port)`` pairs (including an untyped test double); a missing or
    non-iterable value degrades to the primary pair.
    """
    candidates: tuple[tuple[str, int], ...] = ()
    if isinstance(endpoints, Iterable):
        candidates = tuple(endpoints)
    if not candidates:
        candidates = ((address, port),)
    if not local_ips:
        return candidates
    local = [
        (host, candidate_port)
        for host, candidate_port in candidates
        if host in ("127.0.0.1", "::1")
        or any(same_subnet(local_ip, host) for local_ip in local_ips)
    ]
    remote = [candidate for candidate in candidates if candidate not in local]
    return tuple(local) + tuple(remote)


@dataclass
class LivenessSource:
    """One source of peer-liveness evidence, in its own unit.

    Peers are discovered by independent sources that see them at different
    cadences: the mDNS plan announces every ``HEARTBEAT_INTERVAL``, while a
    rendezvous server answers a *random sample* of the group at the lease
    cadence. Measuring both with one clock forces the timeout to encode a
    discovery cadence it knows nothing about, so each source carries its own:

    * ``seconds`` — a source that is expected to report at a cadence (the
      heartbeat): the stamp is the clock and the window is a duration.
    * ``samples`` — a source that answers samples: the stamp is the
      ``generation`` of the answered sample that drew the peer, and the window
      counts **answered** samples. A query that is refused or never answered
      carries no information about any peer, so it must not consume the window
      (the peer is not absent, it simply was not asked about).
    """

    name: str
    unit: Literal["seconds", "samples"]
    window: float
    # Answered samples so far; only the ``samples`` unit reads it.
    generation: float = 0.0
    # How long the source's own evidence stays valid without a new answer. A
    # sample source is only as good as its own liveness: a node that went away —
    # or was distrusted and disabled — would otherwise hold a dead peer forever.
    # ``0`` means "no lease": the window is already a clock (heartbeat).
    lease: float = 0.0
    # Monotonic time of the last answered sample (or of the registration).
    last_answer: float = 0.0


@dataclass
class Peer:
    """Represents a discovered peer on the P2P."""

    cid: str
    bucket: str
    address: str
    port: int
    version: str = "0.0.0"
    group: str | None = None
    use_https: bool = False
    cert_fingerprint: str | None = None
    compression_enabled: bool = False
    delta_enabled: bool = False  # rsync-style delta transfer capability
    peer_download_enabled: bool = False
    # Whether the three capability flags above were advertised by the source
    # (the mDNS TXT record carries them; the rendezvous relay does not). When two
    # plans feed one PeerManager, an update that does not know the capabilities
    # must not clear the ones another source advertised.
    capabilities_known: bool = False
    server_ready: bool = False  # True when HTTP server is listening
    # True when the peer advertised a confirmed externally dialable endpoint
    # (rendezvous) — it can serve every peer without the relay, so it out-ranks
    # a relay-only peer in the election. mDNS sources never know it (False).
    inbound: bool = False
    connected: int = 0  # slaves currently connected to the master's WebSocket
    # Liveness evidence, per source and **in that source's own unit** (a clock
    # stamp for a heartbeat source, a sample counter for a discovery server).
    # See ``LivenessSource``: a peer is alive while any source holds it, and a
    # source that has no information about a peer never counts against it.
    evidence: dict[str, float] = field(default_factory=dict)
    prefer_master: bool = False  # True if peer wants to prefer master role
    never_master: bool = False  # True if peer refuses to ever be master
    startup_time: float = 0.0  # Unix timestamp when peer started
    sync_direction: str = "bidirectional"  # bidirectional | upload-only | download-only
    # Every dialable (host, port) candidate the peer advertised for its data
    # plane, in advertised order (rdv rendezvous relays the full list; mDNS
    # supplies a single address and leaves this empty). ``address``/``port``
    # remain the primary endpoint for callers that need one URL.
    endpoints: tuple[tuple[str, int], ...] = ()

    def note_evidence(self, source: str, stamp: float) -> None:
        """Record that *source* has just observed this peer, at its own stamp."""
        self.evidence[source] = stamp

    def get_server_url(self) -> str:
        """Get the HTTP/HTTPS server URL for this peer's primary endpoint."""
        return server_url(self.address, self.port, use_https=self.use_https)


@dataclass(frozen=True, slots=True)
class PeerSnapshot:
    """Immutable peer endpoint state safe to consume outside the manager lock."""

    cid: str
    address: str
    port: int
    use_https: bool
    cert_fingerprint: str | None
    compression_enabled: bool
    peer_download_enabled: bool


# Human-readable names for the election sort-key components, in the order of
# ``election_sort_key``. Used to report which component actually decided an
# election instead of restating the winner's highest flag.
_ELECTION_REASON_LABELS = (
    "server_ready",
    "prefer_master",
    "inbound reachability",
    "bidirectional preference",
    "earliest startup",
    "lowest cid",
)


class PeerManager:
    """
    Manages discovered peers and master election.

    Thread-safe peer tracking with automatic master election.

    Election priority (highest to lowest):
    1. prefer_master flag (if set, that peer wins)
    2. Bidirectional sync_direction preference (bidirectional wins)
    3. Earliest startup_time (first started wins)
    4. Lowest CID lexicographically (tiebreaker)
    """

    def __init__(
        self,
        local_cid: str,
        bucket: str,
        group: str | None = None,
        prefer_master: bool = False,
        never_master: bool = False,
        startup_time: float = 0.0,
        sync_direction: str = "bidirectional",
        local_cert_fingerprint: str | None = None,
        local_address: str | None = None,
        peer_timeout: float = PEER_TIMEOUT,
    ) -> None:
        """
        Initialize the peer manager.

        Args:
            local_cid: This client's CID.
            bucket: The bucket name for filtering peers.
            group: Optional group name for filtering peers.
            prefer_master: If True, this client will always try to be master.
            never_master: If True, this client is excluded from master election
                and always remains a client/slave.
            startup_time: Unix timestamp when this client started.
            sync_direction: This client's sync direction
                ("bidirectional", "upload-only", "download-only").
            local_cert_fingerprint: This node's own certificate fingerprint
                (SHA-256), used to tell a duplicated device identity (same CID,
                different cert) apart from a legitimate self-rendezvous.
            local_address: The IP address this node advertises over mDNS. A
                peer that presents our own CID from a different address is a
                different host cloning our device identity.
            peer_timeout: Staleness threshold (seconds) for peer liveness.
                A peer whose heartbeat has not been seen within this window
                is considered dead and excluded from election. Defaults to
                ``PEER_TIMEOUT`` (honours ``HSYNC_PEER_TIMEOUT``).
        """
        self._local_cid = local_cid
        self._bucket = bucket
        self._group = group
        self._prefer_master = prefer_master
        self._never_master = never_master
        # Cross-host election timebase: the SNTP-corrected wall clock, not the
        # raw local clock (see RendezvousPlanState._init_common_fields).
        self._startup_time = startup_time if startup_time > 0 else now_corrected()
        self._sync_direction = sync_direction
        self._local_cert_fingerprint = local_cert_fingerprint
        self._local_address = local_address
        self._peer_timeout = peer_timeout
        self._peers: dict[str, Peer] = {}
        # Peers with a live WebSocket connection to (or from) this node. mDNS
        # staleness must never demote them: the WSS channel is the authoritative
        # liveness signal for the master<->slave links, while mDNS is discovery
        # (and the fallback for slave<->slave failover detection).
        self._connected_cids: set[str] = set()
        # Dedicated lock for ``_connected_cids``: the WSS server feeds it from
        # its asyncio loop thread, so it must never contend with ``_lock``,
        # which other threads hold across the (heavier) election/emit work.
        self._connected_lock = threading.Lock()
        # Peers this node has a live relay tunnel to (the rendezvous data plane).
        # A paired tunnel is proof the peer is registered and reachable, so it
        # holds the peer exactly like the WebSocket does.
        self._relay_cids: set[str] = set()
        # The liveness sources, each in its own unit. The heartbeat source is
        # always present; a rendezvous plan registers one per discovery server
        # when it starts (``register_source``), so its sample cadence never has
        # to be encoded in the heartbeat timeout.
        self._sources: dict[str, LivenessSource] = {
            HEARTBEAT_SOURCE: LivenessSource(HEARTBEAT_SOURCE, "seconds", peer_timeout)
        }
        self._lock = threading.Lock()
        # Dedup guard for the duplicate-identity warning: mDNS heartbeats
        # re-deliver the same foreign record every few seconds, so the error is
        # reported once per distinct (address, fingerprint) signature.
        self._duplicate_identity_seen: set[str] = set()
        self._master_cid: str | None = None
        self._local_server_ready = False
        self._local_inbound = False
        self._master_candidacy = True
        self._on_master_change: Callable[[Peer | None], None] | None = None
        # Generation + Condition: waiters observe a monotonic counter so a
        # change that arrives between wait-return and the next wait is not
        # lost (unlike Event.wait/clear). Heartbeat-only last_seen bumps do
        # not increment the generation.
        self._change_cond = threading.Condition(self._lock)
        self._change_generation = 0
        self._seen_generation = 0

    def set_local_server_ready(self, ready: bool) -> None:
        """Mark the local node as actively serving (incumbent master).

        A node that is already serving its bucket wins master election over
        newcomers (even ``prefer_master`` ones), preventing takeover and
        split-brain for the same bucket.
        """
        with self._lock:
            if self._local_server_ready == ready:
                return
            self._local_server_ready = ready
            self._elect_master()
            self._signal_change()

    def set_local_inbound(self, inbound: bool) -> None:
        """Mark the local node as having a confirmed dialable inbound endpoint.

        A node that can be reached directly serves every peer without the relay,
        so it out-ranks a relay-only peer in the master election (after
        ``server_ready``/``prefer_master``). Set from the rendezvous plan once
        the UPnP mapping confirms an external port; a direct-path node keeps it
        for the run.
        """
        with self._lock:
            if self._local_inbound == inbound:
                return
            self._local_inbound = inbound
            self._elect_master()
            self._signal_change()

    def set_local_address(self, address: str) -> None:
        """Record the address this node currently advertises (network change).

        ``_is_foreign_duplicate`` compares a peer presenting our own CID against
        the address we announce. After a DHCP reassignment the record we send
        (and see looped back) carries the new address, so leaving the old one in
        place would report our own record as a cloned device identity.
        """
        with self._lock:
            self._local_address = address

    def set_master_candidacy(self, eligible: bool) -> None:
        """Include or exclude the local node from the master election.

        A node parked in continuous dry-run (waiting for the desktop's
        ``PREVIEW``/``SYNC`` commands) cannot serve, so it must not be elected:
        a peer that elected it would wait forever for its ``server_ready``. The
        handler drops candidacy while it waits and restores it when the dry run
        ends.
        """
        with self._lock:
            if self._master_candidacy == eligible:
                return
            self._master_candidacy = eligible
            self._elect_master()
            self._signal_change()

    @property
    def master_candidacy(self) -> bool:
        """Whether the local node may be elected master."""
        return self._master_candidacy

    @property
    def local_cid(self) -> str:
        """Get this client's CID."""
        return self._local_cid

    @property
    def bucket(self) -> str:
        """Get the bucket name."""
        return self._bucket

    @property
    def group(self) -> str | None:
        """Get the group name."""
        return self._group

    @property
    def prefer_master(self) -> bool:
        """Get the prefer_master flag."""
        return self._prefer_master

    @property
    def never_master(self) -> bool:
        """Get the never_master flag."""
        return self._never_master

    @property
    def startup_time(self) -> float:
        """Get the startup timestamp."""
        return self._startup_time

    def _is_group_compatible(self, peer_group: str | None) -> bool:
        """
        Check if a peer's group is compatible with this client's group.

        The group is a strict partition key: a peer is compatible only when
        its group is exactly equal to this client's group. "No group"
        (``None``) is itself a distinct partition, so a peer without a group
        matches only other peers without a group.

        Args:
            peer_group: The peer's group (or None if not set).

        Returns:
            True if the peer is compatible, False otherwise.
        """
        return self._group == peer_group

    def add_peer(self, peer: Peer, source: str = HEARTBEAT_SOURCE) -> None:
        """Add or update a peer *source* has just observed.

        The stamp the source writes is its own (a clock reading for the
        heartbeat, the current sample generation for a discovery server), so a
        peer discovered by one source is never judged by another source's clock.
        """
        if peer.bucket != self._bucket:
            log.debug(f"Ignoring peer {peer.cid} - different bucket: {peer.bucket}")
            return

        if not self._is_group_compatible(peer.group):
            log.debug(
                f"Ignoring peer {peer.cid} - different group: {peer.group} (local: {self._group})"
            )
            return

        if peer.cid == self._local_cid:
            # A peer presenting our own CID is either our own mDNS record
            # looped back (legitimate self-rendezvous) or a different host that
            # cloned this installation's device identity. Distinguish the two
            # and fail loudly on the latter: two live nodes sharing a CID would
            # corrupt the causal vector clocks and tombstone attribution.
            if self._is_foreign_duplicate(peer):
                self._report_duplicate_identity(peer)
            else:
                log.debug("Ignoring self from peer rendezvous")
            return

        with self._lock:
            existing = self._peers.get(peer.cid)
            if existing is not None:
                # Only an election-relevant change can alter the master choice;
                # a pure mDNS heartbeat refresh (last_seen bump) must not pay
                # for a full candidate scan + sort on every ~5s update. An
                # expired peer is still excluded on the next election-triggering
                # event (remove_service or a relevant field change) — identical
                # master outcome, far less work.
                election_changed = (
                    existing.server_ready != peer.server_ready
                    or existing.prefer_master != peer.prefer_master
                    or existing.never_master != peer.never_master
                    or existing.startup_time != peer.startup_time
                    or existing.sync_direction != peer.sync_direction
                    # Inbound is OR-merged across sources (mDNS never knows it),
                    # so it only changes the election when it turns true.
                    or (peer.inbound and not existing.inbound)
                )
                # The rendezvous relay carries no capability flags, so a
                # rendezvous-sourced update must not clear capabilities learned
                # from the mDNS TXT record: when one PeerManager is fed by both
                # plans, only an update that knows the capabilities (or the
                # first one) may set them.
                take_capabilities = peer.capabilities_known or not existing.capabilities_known
                # Keep the richer candidate list: mDNS supplies one address, the
                # rendezvous relays the full list. An empty list must not clear a
                # known one.
                take_endpoints = bool(peer.endpoints)
                rendezvous_changed = election_changed or (
                    existing.address != peer.address
                    or existing.port != peer.port
                    or existing.use_https != peer.use_https
                    or existing.cert_fingerprint != peer.cert_fingerprint
                    or existing.connected != peer.connected
                    or (
                        take_capabilities
                        and (
                            existing.compression_enabled != peer.compression_enabled
                            or existing.delta_enabled != peer.delta_enabled
                            or existing.peer_download_enabled != peer.peer_download_enabled
                        )
                    )
                    or (take_endpoints and existing.endpoints != peer.endpoints)
                )
                # Update all properties from the new peer object
                existing.address = peer.address
                existing.port = peer.port
                existing.version = peer.version
                existing.group = peer.group
                existing.use_https = peer.use_https
                existing.cert_fingerprint = peer.cert_fingerprint
                if take_capabilities:
                    existing.compression_enabled = peer.compression_enabled
                    existing.delta_enabled = peer.delta_enabled
                    existing.peer_download_enabled = peer.peer_download_enabled
                    existing.capabilities_known = peer.capabilities_known
                if take_endpoints:
                    existing.endpoints = peer.endpoints
                if not existing.server_ready and peer.server_ready:
                    log.info(f"Peer {peer.cid} server is now ready")
                existing.server_ready = peer.server_ready
                existing.connected = peer.connected
                existing.prefer_master = peer.prefer_master
                existing.never_master = peer.never_master
                existing.inbound = existing.inbound or peer.inbound
                existing.startup_time = peer.startup_time
                existing.sync_direction = peer.sync_direction
                existing.note_evidence(source, self._stamp(source))
                log.debug(f"Updated peer: {peer.cid}")
            else:
                peer.note_evidence(source, self._stamp(source))
                self._peers[peer.cid] = peer
                log.info(f"Discovered new peer: {peer.cid} at {peer.address}:{peer.port}")
                election_changed = True
                rendezvous_changed = True

            # Re-run the election on a first rendezvous or a relevant field
            # change. The expired-peer check also covers the original
            # behaviour where a stale peer (whose heartbeat stop arriving)
            # was excluded from the candidate list at the next update.
            stale_peer = any(not self._is_peer_alive(p) for p in self._peers.values())
            if election_changed or stale_peer:
                self._elect_master()
            if rendezvous_changed or stale_peer:
                self._signal_change()

    def _is_foreign_duplicate(self, peer: Peer) -> bool:
        """Return True when *peer* presents our CID but is a different host.

        A peer advertising our own CID is only "self" when it comes from the
        address this node itself announces (our own mDNS record looped back).
        Any other address is a different host running with a cloned device
        identity (a copied ``HSYNC_DEVICE_DIR``). When no address hint is
        available, a differing certificate fingerprint for the same CID is
        still conclusive: our own record always carries our own fingerprint.
        """
        different_address = (
            self._local_address is not None
            and bool(peer.address)
            and peer.address != self._local_address
        )
        different_fingerprint = (
            self._local_cert_fingerprint is not None
            and peer.cert_fingerprint is not None
            and peer.cert_fingerprint != self._local_cert_fingerprint
        )
        return different_address or different_fingerprint

    def _report_duplicate_identity(self, peer: Peer) -> None:
        """Log (once per signature) a duplicated device identity and ignore it."""
        signature = f"{peer.address or '?'}:{peer.cert_fingerprint or '?'}"
        with self._lock:
            if signature in self._duplicate_identity_seen:
                return
            self._duplicate_identity_seen.add(signature)
        log.error(
            f"{ActionLabel.SECURITY} Duplicate device identity detected: a peer at "
            f"{peer.address}:{peer.port} presents this client's CID "
            f"({self._local_cid}). Refusing to sync with it. Each machine must "
            f"run with its own device identity: do not copy HSYNC_DEVICE_DIR "
            f"between hosts, and give each instance a separate HSYNC_DEVICE_DIR."
        )
        emit_event(
            UiEventName.duplicate_identity,
            peer_cid=self._local_cid,
            peer_address=peer.address or "",
            message=(
                "Duplicate device identity detected: a peer presents this client's "
                "CID. Each machine must use its own HSYNC_DEVICE_DIR."
            ),
        )

    def remove_peer(self, cid: str) -> None:
        """Remove a peer by CID."""
        with self._lock:
            if cid in self._peers:
                del self._peers[cid]
                log.info(f"Peer disconnected: {cid}")
                self._elect_master()
                self._signal_change()

    def reconcile_stale_peers(self) -> bool:
        """Expire peers whose heartbeats stopped and re-elect if needed.

        Rendezvous normally re-elects on peer events (``add_peer`` /
        ``remove_peer`` / ``set_local_server_ready``). Those events are driven
        by mDNS announcements, so a peer that crashes *hard* (no goodbye packet,
        no further announces) produces no event on its own — with only two
        peers, the survivor would otherwise wait forever for a master whose
        record is still present but stale. The rendezvous loop calls this on
        every wake (and the slave path calls it on WebSocket loss) so that a
        master whose heartbeat stopped is demoted within ``peer_timeout`` and
        the deterministic election promotes the agreed successor.

        Returns:
            ``True`` when at least one peer was expired (and the election was
            re-run), ``False`` otherwise.
        """
        with self._lock:
            now = time.monotonic()
            stale = [cid for cid, p in self._peers.items() if not self._is_peer_alive(p)]
            if not stale:
                return False
            for cid in stale:
                peer = self._peers[cid]
                del self._peers[cid]
                log.info(f"Peer timed out: {cid}")
                # Liveness forensics: an expiry means every source that had seen
                # the peer stopped holding it — never that a source merely had no
                # information. Losing the elected master is what turns it into an
                # election churn, so that case is called out.
                detail = self._liveness_detail(peer, now)
                if cid == self._master_cid:
                    log.warning(f"Peer liveness lost for the elected master: {cid} {detail}")
                else:
                    log.info(f"Peer liveness lost: {cid} {detail}")
            self._elect_master()
            self._signal_change()
            return True

    def elect(self) -> None:
        """Re-run the master election for the current candidate set.

        Rendezvous calls this once when it starts. The election otherwise runs
        only on a peer add/remove, a local readiness change or a stale sweep, so
        a node with no peers yet never resolves its role: a lone
        ``--prefer-master`` node (the documented single-device master) would never
        serve and a joining slave would wait for a master that never starts its
        server. Evaluating the local candidate up front lets a lone eligible node
        claim mastership immediately, exactly like the peer-driven elections.
        """
        with self._lock:
            self._elect_master()
            self._signal_change()

    def mark_peer_connected(self, cid: str) -> None:
        """Mark *cid* as live via an out-of-band channel (WebSocket).

        A peer with a live WebSocket connection to (or from) this node is
        alive even while its mDNS record is quiescent: zeroconf only delivers
        an update callback when the TXT record *changes*, so a healthy but
        idle peer otherwise goes stale and gets falsely demoted. The WSS link
        is the authoritative liveness signal for the master<->slave channels.
        """
        with self._connected_lock:
            self._connected_cids.add(cid)

    def mark_peer_disconnected(self, cid: str) -> None:
        """Clear the out-of-band liveness mark for *cid* (WebSocket closed)."""
        with self._connected_lock:
            self._connected_cids.discard(cid)

    def is_wss_connected(self, cid: str) -> bool:
        """Return whether *cid* is currently live via the WebSocket channel."""
        with self._connected_lock:
            return cid in self._connected_cids

    def mark_relay_tunnel(self, cid: str, up: bool) -> None:
        """Mark *cid* as reachable through a relay tunnel, or clear the mark.

        A paired relay tunnel is proof the peer is registered with its discovery
        server and serving: the node pairs a session only for a target that has a
        listener, so an open tunnel holds the peer exactly like the WebSocket.
        """
        with self._connected_lock:
            if up:
                self._relay_cids.add(cid)
            else:
                self._relay_cids.discard(cid)

    def is_relay_tunnel(self, cid: str) -> bool:
        """Return whether *cid* is currently reachable through a relay tunnel."""
        with self._connected_lock:
            return cid in self._relay_cids

    def register_source(
        self,
        name: str,
        *,
        unit: Literal["seconds", "samples"],
        window: float,
        lease: float = 0.0,
    ) -> None:
        """Declare a liveness source with its own unit, window and lease.

        A discovery server registers itself when it starts, so its window is
        expressed in its own unit — answered samples for a rendezvous, seconds
        for a heartbeat — instead of borrowing the heartbeat's clock. ``lease``
        bounds how long the source's own evidence stays valid without a new
        answer; a source that is registered again (a plan restarted on the same
        server) keeps its sample counter, so the peers it already stamped stay
        comparable.
        """
        with self._lock:
            existing = self._sources.get(name)
            self._sources[name] = LivenessSource(
                name,
                unit,
                window,
                generation=existing.generation if existing else 0.0,
                lease=lease,
                last_answer=time.monotonic(),
            )

    def forget_source(self, name: str) -> None:
        """Drop a liveness source and the evidence it contributed.

        A source disabled for the rest of the run (an untrusted node) must not
        keep holding the peers it introduced: their stamp goes with it and the
        sweep plus the election re-run without them, instead of leaving a peer
        alive on evidence that will never be refreshed again.
        """
        with self._lock:
            self._sources.pop(name, None)
            for peer in self._peers.values():
                peer.evidence.pop(name, None)
        self.reconcile_stale_peers()

    def advance_source(self, name: str) -> None:
        """Move a sample source forward: one **answered** sample was received.

        Only answered samples move it, so a refused or unanswered query leaves
        the window where it was: the peers such a sample did not mention are not
        thereby absent, they simply were not asked about. An answer also renews
        the source's own lease.
        """
        with self._lock:
            source = self._sources.get(name)
            if source is not None and source.unit == "samples":
                source.generation += 1.0
                source.last_answer = time.monotonic()

    def _stamp(self, source: str) -> float:
        """The stamp *source* writes for a peer it has just observed.

        Callers hold ``_lock`` (see :meth:`add_peer`). An unknown source is
        treated as a heartbeat at the configured timeout, so a caller cannot
        stamp evidence with a unit no predicate reads.
        """
        entry = self._sources.get(source)
        if entry is None:
            entry = LivenessSource(source, "seconds", self._peer_timeout)
            self._sources[source] = entry
        return entry.generation if entry.unit == "samples" else time.monotonic()

    def _is_peer_alive(self, peer: Peer) -> bool:
        """Return whether *peer* is alive by any source of evidence.

        A peer is alive while at least one source holds it: a heartbeat inside
        its window, a sample that drew it within the last ``window`` **answered**
        samples of that discovery server while the server itself is still
        answering (its ``lease``), or a live data-plane channel (WebSocket, relay
        tunnel). Only evidence can expire a peer — a source with no information
        about it never counts against it — but evidence that can no longer be
        refreshed does not hold one either.

        Callers hold ``_lock`` (``_sources`` is only ever replaced under it), so
        this reads a snapshot instead of taking the lock again: ``Lock`` is not
        reentrant and the sweep already holds it.
        """
        now = time.monotonic()
        for source in tuple(self._sources.values()):
            stamp = peer.evidence.get(source.name)
            if stamp is None:
                continue
            if source.unit == "samples":
                if source.lease and now - source.last_answer > source.lease:
                    continue
                if source.generation - stamp < source.window:
                    return True
            elif now - stamp < source.window:
                return True
        with self._connected_lock:
            return peer.cid in self._connected_cids or peer.cid in self._relay_cids

    def _liveness_detail(self, peer: Peer, now: float) -> str:
        """Why *peer* expired, source by source (forensics for a churn)."""
        parts: list[str] = []
        for source in tuple(self._sources.values()):
            stamp = peer.evidence.get(source.name)
            if stamp is None:
                continue
            if source.unit == "samples":
                missed = source.generation - stamp
                silent = now - source.last_answer
                parts.append(
                    f"{source.name}: {missed:.0f} answered sample(s) without it "
                    f"(window {source.window:.0f}, source silent for {silent:.0f}s)"
                )
            else:
                parts.append(
                    f"{source.name}: unseen for {now - stamp:.0f}s (window {source.window:.0f}s)"
                )
        return "; ".join(parts) if parts else "no source ever reported it"

    def get_alive_peers(self) -> list[Peer]:
        """Get all peers that are still alive."""
        with self._lock:
            return [p for p in self._peers.values() if self._is_peer_alive(p)]

    def get_alive_peer_snapshots(self) -> tuple[PeerSnapshot, ...]:
        """Return immutable endpoint snapshots for all currently alive peers."""
        with self._lock:
            return tuple(
                PeerSnapshot(
                    cid=peer.cid,
                    address=peer.address,
                    port=peer.port,
                    use_https=peer.use_https,
                    cert_fingerprint=peer.cert_fingerprint,
                    compression_enabled=peer.compression_enabled,
                    peer_download_enabled=peer.peer_download_enabled,
                )
                for peer in self._peers.values()
                if self._is_peer_alive(peer)
            )

    def _signal_change(self) -> None:
        """Bump the rendezvous generation and wake waiters. Caller holds ``_lock``."""
        self._change_generation += 1
        self._change_cond.notify_all()

    def wait_for_change(self, timeout: float) -> None:
        """Block until a rendezvous change is signalled or *timeout* elapses.

        The rendezvous loop waits on this instead of a fixed sleep: a peer
        discovered (or a master flipping to ``server_ready``) while waiting
        wakes the loop immediately, so startup detection and master readiness
        react in mDNS time (~1 s) rather than after the full polling window.
        A pending generation from before this call is consumed immediately
        (no lost wakeup). Heartbeat-only ``last_seen`` refreshes do not signal.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            while self._change_generation == self._seen_generation:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._change_cond.wait(remaining)
            self._seen_generation = self._change_generation

    def _election_reason(self, ranked: list[ElectionCandidate]) -> str:
        """Name the sort-key component that decided the election.

        Reporting the winner's highest set flag is misleading when several
        candidates share it — two peers both advertising ``server_ready`` are
        separated by ``startup_time``/``cid``, not by ``server_ready``. The
        reason must be the first component on which the winner beat the
        runner-up; a lone candidate has no runner-up, so its own strongest
        preferred flag is named.
        """
        winner = ranked[0]
        winner_key = election_sort_key(winner)
        if len(ranked) < 2:
            for value, label in zip(winner_key[:3], _ELECTION_REASON_LABELS[:3], strict=True):
                if value is False:
                    return label
            # No preferred flag set: the direction preference, else startup order.
            if winner_key[3] is False:
                return _ELECTION_REASON_LABELS[3]
            return _ELECTION_REASON_LABELS[4]
        runner_key = election_sort_key(ranked[1])
        for index, label in enumerate(_ELECTION_REASON_LABELS):
            if winner_key[index] != runner_key[index]:
                return label
        return _ELECTION_REASON_LABELS[-1]

    def _elect_master(self) -> None:
        """
        Elect the master peer.

        Election priority (highest to lowest):
        1. server_ready flag (an already-serving incumbent master wins;
           prevents takeover and split-brain for the same bucket)
        2. prefer_master flag (if set, that peer wins)
        3. Bidirectional sync_direction preference (bidirectional wins)
        4. Earliest startup_time (first started wins)
        5. Lowest CID lexicographically (tiebreaker)

        This includes the local client in the election, unless it (or a peer)
        is marked ``never_master`` — such nodes are excluded from candidacy and
        always remain clients/slaves. If every node is ``never_master`` the
        candidate list is empty and no master is elected.
        """
        # Build the candidate list (local node + every alive peer) and delegate
        # the ranking to the shared, cross-language algorithm in hsync_common so
        # the Python client and Android never diverge. ``never_master`` nodes are
        # carried through and filtered by the shared ranking helper, which also
        # yields the full ranked list needed for the prefer_master conflict report
        # below.
        candidates: list[ElectionCandidate] = [
            ElectionCandidate(
                cid=self._local_cid,
                prefer_master=self._prefer_master,
                startup_time=self._startup_time,
                sync_direction=self._sync_direction,
                server_ready=self._local_server_ready,
                never_master=self._never_master or not self._master_candidacy,
                inbound=self._local_inbound,
            )
        ]
        candidates.extend(
            ElectionCandidate(
                cid=peer.cid,
                prefer_master=peer.prefer_master,
                startup_time=peer.startup_time,
                sync_direction=peer.sync_direction,
                server_ready=peer.server_ready,
                never_master=peer.never_master,
                inbound=peer.inbound,
            )
            for peer in self._peers.values()
            if self._is_peer_alive(peer)
        )

        ranked = rank_election_candidates(candidates)
        new_master_cid = ranked[0].cid if ranked else None

        # Detect prefer_master conflicts
        prefer_master_candidates = [c for c in ranked if c.prefer_master]
        if len(prefer_master_candidates) > 1:
            winner_cid = prefer_master_candidates[0].cid
            for loser in prefer_master_candidates[1:]:
                loser_cid = loser.cid
                loser_peer = self._peers.get(loser_cid)
                winner_peer = self._peers.get(winner_cid)
                if loser_cid == self._local_cid:
                    # We lost: another prefer_master peer won
                    winner_addr = winner_peer.address if winner_peer else "unknown"
                    log.warning(
                        f"{ActionLabel.MASTER_CONFLICT} prefer_master conflict: "
                        f"another master {winner_cid} at {winner_addr} "
                        f"has priority (started earlier). "
                        f"This client will operate as client."
                    )
                    emit_event(
                        UiEventName.master_conflict,
                        conflict_type="conflict_lost",
                        peer_cid=winner_cid,
                        peer_address=winner_addr,
                    )
                elif winner_cid == self._local_cid:
                    # We won: a remote prefer_master peer lost
                    loser_addr = loser_peer.address if loser_peer else "unknown"
                    log.warning(
                        f"{ActionLabel.MASTER_CONFLICT} prefer_master conflict: "
                        f"peer {loser_cid} at {loser_addr} also has "
                        f"prefer_master enabled. This client wins "
                        f"(started earlier)."
                    )
                    emit_event(
                        UiEventName.master_conflict,
                        conflict_type="conflict_won",
                        peer_cid=loser_cid,
                        peer_address=loser_addr,
                    )

        # Detect prefer_master takeover: remote prefer_master peer overrides
        # local non-prefer_master master
        if (
            new_master_cid is not None
            and new_master_cid != self._local_cid
            and not self._prefer_master
            and self._master_cid == self._local_cid
        ):
            new_peer = self._peers.get(new_master_cid)
            if new_peer and new_peer.prefer_master:
                log.warning(
                    f"{ActionLabel.MASTER_TAKEOVER} peer {new_master_cid} at "
                    f"{new_peer.address} has prefer_master enabled "
                    f"and is taking over as master. "
                    f"This client will switch to client."
                )
                emit_event(
                    UiEventName.master_conflict,
                    conflict_type="takeover",
                    peer_cid=new_master_cid,
                    peer_address=new_peer.address,
                )

        if new_master_cid != self._master_cid:
            old_master = self._master_cid
            self._master_cid = new_master_cid

            if new_master_cid == self._local_cid:
                reason = self._election_reason(ranked)
                log.info(
                    f"This client elected as MASTER (CID: {self._local_cid}, reason: {reason})"
                )
                if self._sync_direction != SYNC_DIRECTION_BIDIRECTIONAL:
                    log.warning(
                        f"This client is master with "
                        f"sync_direction={self._sync_direction}. "
                        f"This may limit synchronization capabilities "
                        f"for other peers."
                    )
            else:
                master_peer = self._peers.get(new_master_cid) if new_master_cid else None
                if master_peer:
                    reason = self._election_reason(ranked)
                    if master_peer.sync_direction != SYNC_DIRECTION_BIDIRECTIONAL:
                        log.warning(
                            f"Master {new_master_cid} has "
                            f"sync_direction={master_peer.sync_direction}. "
                            f"This may limit synchronization capabilities."
                        )
                else:
                    reason = "unknown"
                log.info(
                    f"Master changed: {old_master} -> {new_master_cid} "
                    f"(at {master_peer.address if master_peer else 'unknown'}, "
                    f"reason: {reason})"
                )

            # Notify callback
            emit_event(UiEventName.role, is_master=(new_master_cid == self._local_cid))
            if self._on_master_change:
                if new_master_cid == self._local_cid or new_master_cid is None:
                    master_peer = None
                else:
                    master_peer = self._peers.get(new_master_cid)
                self._on_master_change(master_peer)

        # NOTE: the ``peers`` event is emitted from the rendezvous loop
        # (``MdnsRendezvous.wait_for_rendezvous``), the single owner of the
        # display-ready connected/total pair for both roles.

    def is_master(self) -> bool:
        """Check if this client is the current master."""
        with self._lock:
            return self._master_cid == self._local_cid

    def get_master(self) -> Peer | None:
        """Get the current master peer (None if local is master)."""
        with self._lock:
            if self._master_cid == self._local_cid or self._master_cid is None:
                return None
            return self._peers.get(self._master_cid)

    def get_master_cid(self) -> str | None:
        """Get the current master's CID."""
        with self._lock:
            return self._master_cid
