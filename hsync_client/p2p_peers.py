# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Shared P2P peer state, staleness and master-election glue (SSOT ``p2p/1``).

This module holds the discovery-agnostic half of the peer mode: the ``Peer``
record, its immutable ``PeerSnapshot`` projection, the ``PeerManager`` that
tracks liveness/staleness and runs the election (ordering from
``hsync_common.protocol.p2p``), and the small socket helper ``get_free_port``.

Both discovery plans feed it: the mDNS plan (``p2p_discovery_mdns``) and the
bootnode plan (``p2p_discovery_bootnode``). The ``p2p_discovery`` module defines
the minimal interface they expose to ``p2p_handler``.

Consumers: ``hsync-client`` (p2p handler, the two discovery plans, tests).
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from hsync_common.endpoint import build_url
from hsync_common.protocol.p2p import (
    SYNC_DIRECTION_BIDIRECTIONAL,
    ElectionCandidate,
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
# mDNS plan); the bootnode plan does not use it.
HSYNC_SERVICE_TYPE = "_hsync._tcp.local."

# Default port for the embedded HTTP server (when acting as master).
# 0 means "use a random available port".
HSYNC_P2P_LISTEN_PORT = 0


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
    local_ip: str | None = None,
) -> tuple[tuple[str, int], ...]:
    """Order a peer's dial candidates, host/LAN candidates first.

    Same-subnet candidates (and loopback) are tried before the externally
    advertised ones, so two peers behind the same NAT connect over the local
    path — the public endpoint has no hairpin route on most home routers.
    Candidates not known to be local keep their advertised order. Falls back to
    the primary ``(address, port)`` when no candidate list is available (the
    mDNS plan, or a caller that only knows one endpoint).

    ``endpoints`` is typed loosely because callers may pass any iterable of
    ``(host, port)`` pairs (including an untyped test double); a missing or
    non-iterable value degrades to the primary pair.
    """
    candidates: tuple[tuple[str, int], ...] = ()
    if isinstance(endpoints, Iterable):
        candidates = tuple(endpoints)
    if not candidates:
        candidates = ((address, port),)
    if not local_ip:
        return candidates
    local = [
        (host, candidate_port)
        for host, candidate_port in candidates
        if host in ("127.0.0.1", "::1") or same_subnet(local_ip, host)
    ]
    remote = [candidate for candidate in candidates if candidate not in local]
    return tuple(local) + tuple(remote)


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
    # (the mDNS TXT record carries them; the bootnode relay does not). When two
    # plans feed one PeerManager, an update that does not know the capabilities
    # must not clear the ones another source advertised.
    capabilities_known: bool = False
    server_ready: bool = False  # True when HTTP server is listening
    connected: int = 0  # slaves currently connected to the master's WebSocket
    last_seen: float = field(default_factory=time.time)
    force_master: bool = False  # True if peer wants to force master role
    never_master: bool = False  # True if peer refuses to ever be master
    startup_time: float = 0.0  # Unix timestamp when peer started
    sync_direction: str = "bidirectional"  # bidirectional | upload-only | download-only
    # Every dialable (host, port) candidate the peer advertised for its data
    # plane, in advertised order (bootnode discovery relays the full list; mDNS
    # supplies a single address and leaves this empty). ``address``/``port``
    # remain the primary endpoint for callers that need one URL.
    endpoints: tuple[tuple[str, int], ...] = ()

    def is_alive(self, timeout: float = PEER_TIMEOUT) -> bool:
        """Check if peer is still considered alive based on last_seen time.

        Args:
            timeout: Staleness threshold in seconds. Defaults to the module
                ``PEER_TIMEOUT``; ``PeerManager`` passes its configured
                ``peer_timeout`` (which honours ``HSYNC_PEER_TIMEOUT``).
        """
        return (time.time() - self.last_seen) < timeout

    def update_last_seen(self) -> None:
        """Update the last seen timestamp."""
        self.last_seen = time.time()

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


class PeerManager:
    """
    Manages discovered peers and master election.

    Thread-safe peer tracking with automatic master election.

    Election priority (highest to lowest):
    1. force_master flag (if set, that peer wins)
    2. Bidirectional sync_direction preference (bidirectional wins)
    3. Earliest startup_time (first started wins)
    4. Lowest CID lexicographically (tiebreaker)
    """

    def __init__(
        self,
        local_cid: str,
        bucket: str,
        group: str | None = None,
        force_master: bool = False,
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
            force_master: If True, this client will always try to be master.
            never_master: If True, this client is excluded from master election
                and always remains a client/slave.
            startup_time: Unix timestamp when this client started.
            sync_direction: This client's sync direction
                ("bidirectional", "upload-only", "download-only").
            local_cert_fingerprint: This node's own certificate fingerprint
                (SHA-256), used to tell a duplicated device identity (same CID,
                different cert) apart from a legitimate self-discovery.
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
        self._force_master = force_master
        self._never_master = never_master
        self._startup_time = startup_time if startup_time > 0 else time.time()
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
        self._lock = threading.Lock()
        # Dedup guard for the duplicate-identity warning: mDNS heartbeats
        # re-deliver the same foreign record every few seconds, so the error is
        # reported once per distinct (address, fingerprint) signature.
        self._duplicate_identity_seen: set[str] = set()
        self._master_cid: str | None = None
        self._local_server_ready = False
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
        newcomers (even ``force_master`` ones), preventing takeover and
        split-brain for the same bucket.
        """
        with self._lock:
            if self._local_server_ready == ready:
                return
            self._local_server_ready = ready
            self._elect_master()
            self._signal_change()

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
    def force_master(self) -> bool:
        """Get the force_master flag."""
        return self._force_master

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

    def add_peer(self, peer: Peer) -> None:
        """Add or update a discovered peer."""
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
            # looped back (legitimate self-discovery) or a different host that
            # cloned this installation's device identity. Distinguish the two
            # and fail loudly on the latter: two live nodes sharing a CID would
            # corrupt the causal vector clocks and tombstone attribution.
            if self._is_foreign_duplicate(peer):
                self._report_duplicate_identity(peer)
            else:
                log.debug("Ignoring self from peer discovery")
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
                    or existing.force_master != peer.force_master
                    or existing.never_master != peer.never_master
                    or existing.startup_time != peer.startup_time
                    or existing.sync_direction != peer.sync_direction
                )
                # The bootnode relay carries no capability flags, so a
                # bootnode-sourced update must not clear capabilities learned
                # from the mDNS TXT record: when one PeerManager is fed by both
                # plans, only an update that knows the capabilities (or the
                # first one) may set them.
                take_capabilities = peer.capabilities_known or not existing.capabilities_known
                # Keep the richer candidate list: mDNS supplies one address, the
                # bootnode relays the full list. An empty list must not clear a
                # known one.
                take_endpoints = bool(peer.endpoints)
                discovery_changed = election_changed or (
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
                existing.force_master = peer.force_master
                existing.never_master = peer.never_master
                existing.startup_time = peer.startup_time
                existing.sync_direction = peer.sync_direction
                existing.update_last_seen()
                log.debug(f"Updated peer: {peer.cid}")
            else:
                self._peers[peer.cid] = peer
                log.info(f"Discovered new peer: {peer.cid} at {peer.address}:{peer.port}")
                election_changed = True
                discovery_changed = True

            # Re-run the election on a first discovery or a relevant field
            # change. The expired-peer check also covers the original
            # behaviour where a stale peer (whose heartbeat stop arriving)
            # was excluded from the candidate list at the next update.
            stale_peer = any(not self._is_peer_alive(p) for p in self._peers.values())
            if election_changed or stale_peer:
                self._elect_master()
            if discovery_changed or stale_peer:
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

        Discovery normally re-elects on peer events (``add_peer`` /
        ``remove_peer`` / ``set_local_server_ready``). Those events are driven
        by mDNS announcements, so a peer that crashes *hard* (no goodbye packet,
        no further announces) produces no event on its own — with only two
        peers, the survivor would otherwise wait forever for a master whose
        record is still present but stale. The discovery loop calls this on
        every wake (and the slave path calls it on WebSocket loss) so that a
        master whose heartbeat stopped is demoted within ``peer_timeout`` and
        the deterministic election promotes the agreed successor.

        Returns:
            ``True`` when at least one peer was expired (and the election was
            re-run), ``False`` otherwise.
        """
        with self._lock:
            stale = [cid for cid, p in self._peers.items() if not self._is_peer_alive(p)]
            if not stale:
                return False
            for cid in stale:
                del self._peers[cid]
                log.info(f"Peer timed out: {cid}")
            self._elect_master()
            self._signal_change()
            return True

    def elect(self) -> None:
        """Re-run the master election for the current candidate set.

        Discovery calls this once when it starts. The election otherwise runs
        only on a peer add/remove, a local readiness change or a stale sweep, so
        a node with no peers yet never resolves its role: a lone
        ``--force-master`` node (the documented single-device master) would never
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

    def _is_peer_alive(self, peer: Peer) -> bool:
        """Return whether *peer* is alive by mDNS heartbeat or WSS liveness."""
        if peer.is_alive(self._peer_timeout):
            return True
        with self._connected_lock:
            return peer.cid in self._connected_cids

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
        """Bump the discovery generation and wake waiters. Caller holds ``_lock``."""
        self._change_generation += 1
        self._change_cond.notify_all()

    def wait_for_change(self, timeout: float) -> None:
        """Block until a discovery change is signalled or *timeout* elapses.

        The discovery loop waits on this instead of a fixed sleep: a peer
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

    def _elect_master(self) -> None:
        """
        Elect the master peer.

        Election priority (highest to lowest):
        1. server_ready flag (an already-serving incumbent master wins;
           prevents takeover and split-brain for the same bucket)
        2. force_master flag (if set, that peer wins)
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
        # yields the full ranked list needed for the force_master conflict report
        # below.
        candidates: list[ElectionCandidate] = [
            ElectionCandidate(
                cid=self._local_cid,
                force_master=self._force_master,
                startup_time=self._startup_time,
                sync_direction=self._sync_direction,
                server_ready=self._local_server_ready,
                never_master=self._never_master,
            )
        ]
        candidates.extend(
            ElectionCandidate(
                cid=peer.cid,
                force_master=peer.force_master,
                startup_time=peer.startup_time,
                sync_direction=peer.sync_direction,
                server_ready=peer.server_ready,
                never_master=peer.never_master,
            )
            for peer in self._peers.values()
            if self._is_peer_alive(peer)
        )

        ranked = rank_election_candidates(candidates)
        new_master_cid = ranked[0].cid if ranked else None

        # Detect force_master conflicts
        force_master_candidates = [c for c in ranked if c.force_master]
        if len(force_master_candidates) > 1:
            winner_cid = force_master_candidates[0].cid
            for loser in force_master_candidates[1:]:
                loser_cid = loser.cid
                loser_peer = self._peers.get(loser_cid)
                winner_peer = self._peers.get(winner_cid)
                if loser_cid == self._local_cid:
                    # We lost: another force_master peer won
                    winner_addr = winner_peer.address if winner_peer else "unknown"
                    log.warning(
                        f"{ActionLabel.MASTER_CONFLICT} force_master conflict: "
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
                    # We won: a remote force_master peer lost
                    loser_addr = loser_peer.address if loser_peer else "unknown"
                    log.warning(
                        f"{ActionLabel.MASTER_CONFLICT} force_master conflict: "
                        f"peer {loser_cid} at {loser_addr} also has "
                        f"force_master enabled. This client wins "
                        f"(started earlier)."
                    )
                    emit_event(
                        UiEventName.master_conflict,
                        conflict_type="conflict_won",
                        peer_cid=loser_cid,
                        peer_address=loser_addr,
                    )

        # Detect force_master takeover: remote force_master peer overrides
        # local non-force_master master
        if (
            new_master_cid is not None
            and new_master_cid != self._local_cid
            and not self._force_master
            and self._master_cid == self._local_cid
        ):
            new_peer = self._peers.get(new_master_cid)
            if new_peer and new_peer.force_master:
                log.warning(
                    f"{ActionLabel.MASTER_TAKEOVER} peer {new_master_cid} at "
                    f"{new_peer.address} has force_master enabled "
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
                if self._local_server_ready:
                    reason = "server_ready"
                elif self._force_master:
                    reason = "force_master"
                elif self._sync_direction == SYNC_DIRECTION_BIDIRECTIONAL:
                    reason = "bidirectional preference"
                else:
                    reason = "earliest startup"
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
                    if master_peer.server_ready:
                        reason = "server_ready"
                    elif master_peer.force_master:
                        reason = "force_master"
                    elif master_peer.sync_direction == SYNC_DIRECTION_BIDIRECTIONAL:
                        reason = "bidirectional preference"
                    else:
                        reason = "earliest startup"
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

        # NOTE: the ``peers`` event is emitted from the discovery loop
        # (``MdnsDiscovery.wait_for_discovery``), the single owner of the
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
