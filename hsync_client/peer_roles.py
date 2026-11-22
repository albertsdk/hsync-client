# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""The peer-mesh capabilities — one per observable behavior — and their presets.

A device participates in the p2p mesh in three independent ways, each answering a
different operator question:

- **serve blocks** (egress): other devices download file blocks from this one. The
  only role that costs upload bandwidth and exposes content, and the only one a
  peer can observe (`Capability.peer_download`, the mDNS TXT flag, the rendezvous
  capacity and the per-session `serve_slots`).
- **fetch from peers** (ingress): this device downloads blocks from other devices
  instead of the serving device alone. Pure local policy — no peer learns it.
- **publish inventory** (metadata egress): this device announces the read-only
  inventory the serving device classifies the share against
  (`Capability.publishes_index`). It reveals the file list, so it is a privacy
  decision, not a bandwidth one.

One switch cannot express all three: turning serving off must not stop a device
from fetching, and turning fetching off must not hide a device from the status
view. Every gate in the client reads one of these functions, so no call site can
invent a fourth meaning.

``PEER_ROLES`` bundles them for the front-ends (``--peer-role``); the three
``--no-*`` flags stay available for the combinations the presets do not name, and
they always win because they can only turn a capability off.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # a lazy import keeps ``import hsync_client.cli`` light
    from hsync_client.config import HsyncConfig

__all__ = [
    "PEER_ROLES",
    "PeerRole",
    "fetch_from_peers",
    "peer_role_capabilities",
    "publish_inventory",
    "serve_blocks",
]


class PeerRole(StrEnum):
    """A named bundle of the three capabilities (``--peer-role``)."""

    full = "full"
    leech = "leech"
    seed = "seed"
    off = "off"


# role -> (serve blocks, fetch from peers, publish inventory)
PEER_ROLES: Final[dict[PeerRole, tuple[bool, bool, bool]]] = {
    PeerRole.full: (True, True, True),
    # Download-only participation: consumes the mesh and contributes nothing
    # back, not even its index.
    PeerRole.leech: (False, True, False),
    # The reference copy: serves and announces, never pulls another device's
    # content over itself.
    PeerRole.seed: (True, False, True),
    PeerRole.off: (False, False, False),
}


def peer_role_capabilities(role: PeerRole | str | None) -> tuple[bool, bool, bool]:
    """Return ``(serve, fetch, publish)`` for *role* (``full`` when unset)."""
    if role is None or role == "":
        return PEER_ROLES[PeerRole.full]
    return PEER_ROLES[PeerRole(role)]


def _active(config: HsyncConfig | None) -> HsyncConfig:
    if config is not None:
        return config
    from hsync_client.config import CONFIG

    return CONFIG


def serve_blocks(config: HsyncConfig | None = None) -> bool:
    """Whether this device serves file blocks to peers (egress)."""
    return bool(_active(config).get("P2P_SERVE_BLOCKS", True))


def fetch_from_peers(config: HsyncConfig | None = None) -> bool:
    """Whether this device downloads file blocks from peers (ingress)."""
    return bool(_active(config).get("P2P_FETCH_FROM_PEERS", True))


def publish_inventory(config: HsyncConfig | None = None) -> bool:
    """Whether this device publishes its read-only inventory (metadata egress)."""
    return bool(_active(config).get("P2P_PUBLISH_INVENTORY", True))
