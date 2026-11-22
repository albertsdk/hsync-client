# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Local capability computation shared by the remote and P2P transport layers.

Single place that maps this binary's enabled features to the wire
:class:`~hsync_common.protocol.handshake.Capability` tokens advertised during
handshakes (ADR-0024). The central-server WebSocket auth and the P2P
master⇄slave handshake advertise the same set, so they must never be
re-assembled independently in each transport module.
"""

from __future__ import annotations

from hsync_common.permissions import local_permissions_are_lossy
from hsync_common.protocol.handshake import Capability

from hsync_client.config import CONFIG
from hsync_client.peer_roles import publish_inventory, serve_blocks

__all__ = ["adopt_remote_capabilities", "local_capabilities"]


def local_capabilities() -> set[Capability]:
    """Capabilities this binary actually supports, derived from its runtime config.

    ``vector_clocks`` (causal version metadata, ADR-0022) and ``tombstones``
    (deletion-marker gossip, ADR-0003) are always implemented by this client,
    and ``index`` always advertises the read-only inventory *protocol* every
    node can speak: a peer that does not publish still has to accept and
    compare the inventories others publish, and the serving role does. The two
    participation statements follow their own keys — ``publishes_index`` only
    while the device publishes its own inventory, ``peer_download`` only while
    it serves file blocks — so a front-end cannot advertise a role the device
    refuses. ``compression`` and ``delta`` are feature-toggled too.
    ``readonly_permissions`` is advertised only by a lossy host (Windows): the
    reconciliation then projects permission-only comparisons to the owner-write
    bit.
    """
    caps: set[Capability] = {
        Capability.vector_clocks,
        Capability.tombstones,
        Capability.index,
    }
    if publish_inventory(CONFIG):
        caps.add(Capability.publishes_index)
    if serve_blocks(CONFIG):
        caps.add(Capability.peer_download)
    if CONFIG.get("COMPRESSION_ENABLED", False):
        caps.add(Capability.compression)
    if CONFIG.get("DELTA_ENABLED", False):
        caps.add(Capability.delta)
    if local_permissions_are_lossy():
        caps.add(Capability.readonly_permissions)
    return caps


def adopt_remote_capabilities(remote: set[Capability]) -> None:
    """Adopt the peer's advertised capabilities as the runtime transfer gates.

    Sets the session-scoped config switches that gate compression and delta
    transfer (intersection semantics: a missing peer capability disables the
    local feature for the session, never an error). ``vector_clocks`` and
    ``tombstones`` degrade harmlessly on absence (empty clock / no gossip), so
    they need no runtime switch.
    """
    CONFIG["SERVER_COMPRESSION_ENABLED"] = Capability.compression in remote
    CONFIG["SERVER_DELTA_ENABLED"] = Capability.delta in remote
    CONFIG["REMOTE_READONLY_PERMISSIONS"] = Capability.readonly_permissions in remote
