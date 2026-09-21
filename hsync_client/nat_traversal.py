# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Candidate endpoints a p2p master publishes to a bootnode (best-effort NAT).

The register frame's ``endpoints[]`` must list the addresses another peer can
dial for the data-plane (HTTPS/WSS). Behind NAT that is the server-reflexive
address the bootnode echoed (``stun.observed_address``) mapped through UPnP, or
at least the same public IP with the local listen port. Candidates are advisory:
the bootnode relays them and a requester falls back to the master handshake, so
an unreachable candidate wastes at most one attempt.

Consumers: ``hsync-client`` (p2p_discovery_bootnode), tests.
"""

from __future__ import annotations

from dataclasses import dataclass

from hsync_client.stun import ObservedAddress

__all__ = ["CandidateEndpoint", "candidate_endpoints"]

_TCP = "tcp"


@dataclass(frozen=True, slots=True)
class CandidateEndpoint:
    """One dialable data-plane endpoint advertised to the bootnode."""

    proto: str
    host: str
    port: int

    def as_wire(self) -> dict[str, object]:
        """The ``endpoints[]`` element shape of a ``boot/1`` register frame."""
        return {"proto": self.proto, "host": self.host, "port": self.port}


def candidate_endpoints(
    *,
    local_ip: str,
    listen_port: int,
    observed: ObservedAddress | None = None,
    external_port: int | None = None,
) -> list[CandidateEndpoint]:
    """Ordered, de-duplicated dialable endpoints for the register frame.

    Order: the UPnP-mapped public endpoint first (when known), then the
    server-reflexive ``observed`` address, then the local address as a
    same-network fallback. Duplicates are collapsed so the frame stays minimal.
    """
    candidates: list[CandidateEndpoint] = []
    if observed is not None:
        # The bootnode observed the QUIC *source* (UDP) port: only the IP is
        # reusable for the TCP data-plane. The dialable port is always the
        # master's listen port (or the UPnP external port when mapped) — never
        # ``observed.port``, which is the ephemeral QUIC/UDP source port.
        candidates.append(CandidateEndpoint(_TCP, observed.host, external_port or listen_port))
    if external_port:
        candidates.append(CandidateEndpoint(_TCP, local_ip, external_port))
    candidates.append(CandidateEndpoint(_TCP, local_ip, listen_port))

    seen: set[tuple[str, int]] = set()
    unique: list[CandidateEndpoint] = []
    for candidate in candidates:
        key = (candidate.host, candidate.port)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique
