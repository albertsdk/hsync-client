# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Peer model and endpoint helpers shared by the P2P plans.

``Peer``/``PeerSnapshot``/``LivenessSource`` plus the endpoint-union and URL
helpers, separated from the ``PeerManager`` registry in ``hsync_client.p2p_peers``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from hsync_common.endpoint import build_url

# Name of the heartbeat source every manager has: the mDNS plan announces a peer
# (and re-announces it every ``HEARTBEAT_INTERVAL``), so a peer it announced is
# stale once ``peer_timeout`` passes without another announce.
HEARTBEAT_SOURCE = "heartbeat"


def _endpoints_union(
    by_source: Mapping[str, tuple[tuple[str, int], ...]],
) -> tuple[tuple[str, int], ...]:
    """Deduplicated union of the per-source candidate lists.

    Each source owns its list, so an address a source stops advertising
    disappears instead of accumulating forever (a DHCP change, a peer moving
    networks, a mobile peer's changing public address). The union is computed in
    stable source-insertion order, so a repeated heartbeat from either source
    yields the same tuple and is not reported as a change.
    """
    return tuple(
        dict.fromkeys(candidate for candidates in by_source.values() for candidate in candidates)
    )


def server_url(address: str, port: int, *, use_https: bool) -> str:
    """Build the base HTTP(S) URL for a peer data-plane endpoint."""
    protocol = "https" if use_https else "http"
    return build_url(protocol, address, port)


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
    # plane, in advertised order. The two sources are merged (stable union): the
    # rendezvous relays the advertised list and mDNS supplies one per advertised
    # interface. A peer that announced a single address leaves this empty and
    # keeps the no-probe fast path. ``address``/``port`` remain the primary
    # endpoint for callers that need one URL.
    endpoints: tuple[tuple[str, int], ...] = ()
    # The candidate list as last announced by each discovery source. Each source
    # replaces its own entry, so the derived ``endpoints`` above never grows
    # without bound; it is a deduplicated union in stable source order.
    endpoints_by_source: dict[str, tuple[tuple[str, int], ...]] = field(default_factory=dict)

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
    # Every data-plane endpoint the peer announced, in advertised order (the
    # union of the discovery sources). A multi-homed peer is discovered on one
    # interface and may open its master session from another, so a consumer
    # that has to recognise the peer by address reads the whole list. Empty
    # for a single-address peer, which keeps ``address``/``port`` as its only
    # endpoint.
    endpoints: tuple[tuple[str, int], ...] = ()


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
