# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Candidate endpoints a p2p master publishes to a rendezvous (best-effort NAT).

The register frame's ``endpoints[]`` must list the addresses another peer can
dial for the data-plane (HTTPS/WSS). Behind NAT that is the server-reflexive
address the rendezvous echoed (``stun.observed_address``) mapped through UPnP, or
at least the same public IP with the local listen port. Candidates are advisory:
the rendezvous relays them and a requester falls back to the master handshake, so
an unreachable candidate wastes at most one attempt.

Consumers: ``hsync-client`` (p2p_rendezvous_rdv), tests.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from hsync_client.stun import ObservedAddress

__all__ = ["CandidateEndpoint", "candidate_endpoints"]

_TCP = "tcp"


@dataclass(frozen=True, slots=True)
class CandidateEndpoint:
    """One dialable data-plane endpoint advertised to the rendezvous."""

    proto: str
    host: str
    port: int

    def as_wire(self) -> dict[str, object]:
        """The ``endpoints[]`` element shape of a ``rdv/1`` register frame."""
        return {"proto": self.proto, "host": self.host, "port": self.port}


def candidate_endpoints(
    *,
    local_ips: Sequence[str],
    listen_port: int,
    observed: ObservedAddress | None = None,
    external_port: int | None = None,
) -> list[CandidateEndpoint]:
    """Ordered, de-duplicated dialable endpoints for the register frame.

    Order: the server-reflexive ``observed`` address first (reached through the
    public NAT), then the UPnP-mapped endpoint, then one local-address candidate
    per active interface (the default-route address first) as a same-network
    fallback. A multi-homed node advertises every interface so a peer on any of
    its networks can dial it directly instead of falling back to the relay.

    The UPnP external port maps the *default-route* interface's router, so it is
    combined only with that interface; other interfaces always advertise the
    listen port. The dialable port is never ``observed.port`` (the ephemeral
    QUIC/UDP source port). Duplicates are collapsed so the frame stays minimal.
    """
    candidates: list[CandidateEndpoint] = []
    if observed is not None:
        candidates.append(CandidateEndpoint(_TCP, observed.host, external_port or listen_port))
    primary = local_ips[0] if local_ips else None
    if external_port and primary is not None:
        candidates.append(CandidateEndpoint(_TCP, primary, external_port))
    for host in local_ips:
        candidates.append(CandidateEndpoint(_TCP, host, listen_port))

    seen: set[tuple[str, int]] = set()
    unique: list[CandidateEndpoint] = []
    for candidate in candidates:
        key = (candidate.host, candidate.port)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique
