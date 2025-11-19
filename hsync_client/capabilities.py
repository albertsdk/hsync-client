# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Local capability computation shared by the remote and LAN transport layers.

Single place that maps this binary's enabled features to the wire
:class:`~hsync_common.protocol.handshake.Capability` tokens advertised during
handshakes (ADR-0024). The central-server WebSocket auth and the LAN
master⇄slave handshake advertise the same set, so they must never be
re-assembled independently in each transport module.
"""

from __future__ import annotations

from hsync_common.protocol.handshake import Capability

from hsync_client.config import CONFIG

__all__ = ["local_capabilities", "adopt_remote_capabilities"]


def local_capabilities() -> set[Capability]:
    """Capabilities this binary actually supports, derived from its runtime config.

    ``vector_clocks`` (causal version metadata, ADR-0022) and ``tombstones``
    (deletion-marker gossip, ADR-0003) are always implemented by this client;
    ``compression`` and ``delta`` are feature-toggled. The LAN mDNS/TXT record
    announces the same set, but the connect-time handshake is the
    authenticated capability source (ADR-0024).
    """
    caps: set[Capability] = {Capability.vector_clocks, Capability.tombstones}
    if CONFIG.get("COMPRESSION_ENABLED", False):
        caps.add(Capability.compression)
    if CONFIG.get("DELTA_ENABLED", False):
        caps.add(Capability.delta)
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
