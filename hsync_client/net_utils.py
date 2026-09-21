# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Small network helpers shared by the discovery plans.

Extracted because the local-source-IP probe is needed in three places (the mDNS
advertiser, the UPnP mapper and the bootnode endpoint candidates); keeping one
implementation avoids three subtly different answers.

Consumers: ``hsync-client`` (p2p_discovery_mdns, p2p_discovery_bootnode, upnp).
"""

from __future__ import annotations

import socket

__all__ = ["get_local_ip"]

# A non-routable TEST-NET address: connecting a UDP socket to it makes the
# kernel pick the outgoing interface without sending a single packet.
_PROBE_TARGET = ("10.254.254.254", 1)


def get_local_ip() -> str:
    """Best-effort local source IP for advertising; ``127.0.0.1`` as fallback."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0)
            probe.connect(_PROBE_TARGET)
            return str(probe.getsockname()[0])
    except OSError:
        return "127.0.0.1"
