# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Server-reflexive (public) endpoint extraction from a ``rdv/1`` ack.

The rendezvous echoes the source endpoint it observed on the QUIC datagram
(``ack.observed_endpoint``): that is the STUN-equivalent answer, and it is the
only way a peer behind NAT learns the public ``ip:port`` its socket is mapped
to. This module turns the validated frame into a small typed value; the
``nat_traversal`` plan composes it with the local candidates.

Consumers: ``hsync-client`` (p2p_rendezvous_rdv, nat_traversal), tests.
"""

from __future__ import annotations

from dataclasses import dataclass

from hsync_common.protocol.rendezvous import RdvAck

__all__ = ["ObservedAddress", "observed_address"]


@dataclass(frozen=True, slots=True)
class ObservedAddress:
    """The public address the rendezvous saw for our QUIC socket."""

    host: str
    port: int


def observed_address(ack: RdvAck) -> ObservedAddress:
    """Public (server-reflexive) ``ip:port`` carried by a validated ``ack``."""
    endpoint = ack["observed_endpoint"]
    return ObservedAddress(host=endpoint["ip"], port=endpoint["port"])
