# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Small network helpers shared by the rendezvous plans.

Extracted because the local-source-IP probe is needed in several places (the
mDNS advertiser, the rendezvous endpoint candidates and the p2p handler);
keeping one implementation avoids subtly different answers. ``same_subnet`` is
the shared LAN-adjacency test used by the peer-source ranking and by the
dial-time candidate ordering (a same-subnet candidate is preferred over a NATed
public one).

Consumers: ``hsync-client`` (p2p_rendezvous_mdns, p2p_rendezvous_rdv,
p2p_handler, p2p_sources, p2p_peers).
"""

from __future__ import annotations

import socket
import sys
from functools import lru_cache
from ipaddress import IPv4Address, IPv6Address, ip_address

__all__ = ["get_local_ip", "get_local_ips", "same_subnet"]


@lru_cache(maxsize=2048)
def _parse_ip(value: str) -> IPv4Address | IPv6Address:
    """Parse an address once; callers repeat the same peer/reference strings.

    The cache is bounded (2048 entries) and holds only immutable address
    objects, so it cannot grow with attacker-controlled churn.
    """
    return ip_address(value)


# A non-routable TEST-NET address: connecting a UDP socket to it makes the
# kernel pick the outgoing interface without sending a single packet.
_PROBE_TARGET = ("10.254.254.254", 1)

# Linux ioctl request that returns a socket's IPv4 address (``SIOCGIFADDR``).
# The value is stable across Linux and Android; other POSIX systems lack this
# exact request and fall back to a hostname lookup.
_SIOCGIFADDR = 0x8915


def get_local_ip() -> str:
    """Best-effort local source IP for advertising; ``127.0.0.1`` as fallback."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0)
            probe.connect(_PROBE_TARGET)
            return str(probe.getsockname()[0])
    except OSError:
        return "127.0.0.1"


def get_local_ips() -> list[str]:
    """Every active non-loopback IPv4 address, the default-route one first.

    A multi-homed node (laptop on Wi-Fi and Ethernet, a VPN/point-to-point link
    beside the LAN) has one address per interface, but only the default-route
    address is known by :func:`get_local_ip`. Advertising all of them lets a peer
    on any of those networks dial it directly instead of falling back to the
    relay. LAN/reserved addresses come before globally routable ones, and
    container/bridge interfaces are skipped: their addresses (``172.17.0.1`` and
    the like) are not reachable by peers and collide across hosts. The list never
    omits the default-route address, and degrades to just that address when the
    interfaces cannot be enumerated.
    """
    primary = get_local_ip()
    found: list[str] = []
    if _is_usable_ipv4(primary):
        found.append(primary)
    rest = [
        address
        for address in _enumerate_local_ipv4()
        if address not in found and _is_usable_ipv4(address)
    ]
    # Stable sort: LAN/reserved addresses first, so the most likely shareable
    # address survives a caller's endpoint cap.
    rest.sort(key=lambda address: ip_address(address).is_global)
    found.extend(rest)
    return found or [primary]


def _is_usable_ipv4(address: str) -> bool:
    """Whether *address* is a routable IPv4 (not loopback or link-local)."""
    try:
        parsed = ip_address(address)
    except ValueError:
        return False
    if parsed.version != 4:
        return False
    return not (parsed.is_loopback or parsed.is_link_local)


# Interface names that are virtual bridges or container/virtualization links:
# their addresses are host-local and often identical across hosts. ``lo`` is
# here too (its address is loopback, which :func:`_is_usable_ipv4` drops anyway).
_VIRTUAL_INTERFACE_PREFIXES = (
    "docker",
    "br-",
    "veth",
    "virbr",
    "vboxnet",
    "vmnet",
    "dummy",
    "lo",
)


def _is_advertisable_interface(name: str) -> bool:
    """Whether an interface is a real network a peer could share."""
    return not name.startswith(_VIRTUAL_INTERFACE_PREFIXES)


def _enumerate_local_ipv4() -> list[str]:
    """Best-effort list of the machine's interface IPv4 addresses."""
    if sys.platform.startswith("linux"):
        # Linux and Android: one ioctl per interface, no dependency.
        return _linux_interface_ipv4()
    try:
        return list(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        return []


def _linux_interface_ipv4() -> list[str]:
    """Interface IPv4 addresses via ``SIOCGIFADDR`` (empty when unavailable)."""
    try:
        import fcntl
        import struct
    except ImportError:
        return []
    try:
        interfaces = socket.if_nameindex()
    except OSError:
        return []
    addresses: list[str] = []
    for _index, name in interfaces:
        if not _is_advertisable_interface(name):
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                request = struct.pack("256s", name.encode("utf-8")[:15])
                result = fcntl.ioctl(probe.fileno(), _SIOCGIFADDR, request)
                addresses.append(socket.inet_ntoa(result[20:24]))
        except OSError:
            continue
    return addresses


def same_subnet(reference_address: str, peer_address: str) -> bool:
    """Whether *peer_address* is on the same /24 (IPv4) or /64 (IPv6) as the reference.

    A proximity heuristic only — never proof of identity or authority. Used to
    prefer a LAN-adjacent dial candidate over a NATed public one.
    """
    try:
        reference = _parse_ip(reference_address)
        peer = _parse_ip(peer_address)
    except ValueError:
        return False
    if reference.version != peer.version:
        return False
    # /24 (IPv4) and /64 (IPv6) are byte-aligned, so comparing the packed prefix
    # is exactly the network membership test and avoids building an ip_network.
    prefix_bytes = 3 if reference.version == 4 else 8
    return reference.packed[:prefix_bytes] == peer.packed[:prefix_bytes]
