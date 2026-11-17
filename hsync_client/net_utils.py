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
from dataclasses import dataclass
from functools import lru_cache
from ipaddress import IPv4Address, IPv6Address, ip_address

import ifaddr

__all__ = [
    "LocalAddresses",
    "address_family",
    "get_local_addresses",
    "get_local_ip",
    "normalize_peer_ip",
    "same_subnet",
]


@lru_cache(maxsize=2048)
def _parse_ip(value: str) -> IPv4Address | IPv6Address:
    """Parse an address once; callers repeat the same peer/reference strings.

    The cache is bounded (2048 entries) and holds only immutable address
    objects, so it cannot grow with attacker-controlled churn.
    """
    return ip_address(value)


def address_family(value: str) -> int:
    """IP version of *value*, or 4 for a non-literal (a configured hostname)."""
    try:
        return _parse_ip(value).version
    except ValueError:
        return 4


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


@dataclass(frozen=True, slots=True)
class LocalAddresses:
    """The host's advertisable addresses, nearest first within each family."""

    ipv4: tuple[str, ...]
    ipv6: tuple[str, ...]

    @property
    def all(self) -> tuple[str, ...]:
        """Every address (IPv4 first), the form the peer registry consumes."""
        return self.ipv4 + self.ipv6


def _get_adapters() -> list[ifaddr.Adapter]:
    """The OS adapter list (module seam: tests replace it)."""
    return list(ifaddr.get_adapters())


def get_local_addresses() -> LocalAddresses:
    """Every active non-loopback address, the IPv4 default-route one first.

    A multi-homed node has one address per interface, but only the default-route
    IPv4 address is known by :func:`get_local_ip`; advertising all of them lets a
    peer on any of those networks dial it directly instead of falling back to the
    relay. LAN/reserved addresses come before globally routable ones and
    container/bridge adapters are skipped. IPv6 excludes loopback, link-local,
    multicast and unspecified addresses (a link-local address has no portable
    scope); ULA and global addresses are kept. Never empty: falls back to the
    default-route IPv4 address (``127.0.0.1`` at worst).
    """
    primary = get_local_ip()
    enumerated_v4, enumerated_v6 = _enumerate_local_addresses()
    ordered_v4: list[str] = []
    if _is_usable_ipv4(primary):
        ordered_v4.append(primary)
    candidates_v4 = [a for a in enumerated_v4 if a not in ordered_v4 and _is_usable_ipv4(a)]
    # Stable sort: LAN/reserved first, so the likeliest shareable address
    # survives a caller's endpoint cap.
    candidates_v4.sort(key=lambda address: _parse_ip(address).is_global)
    ordered_v4.extend(candidates_v4)
    ordered_v6 = [a for a in enumerated_v6 if _is_usable_ipv6(a)]
    ordered_v6.sort(key=lambda address: _parse_ip(address).is_global)
    return LocalAddresses(ipv4=tuple(ordered_v4) or (primary,), ipv6=tuple(ordered_v6))


def normalize_peer_ip(value: str) -> str:
    """Map an IPv4-mapped IPv6 source (``::ffff:a.b.c.d``) to plain IPv4.

    A dual-stack listener reports IPv4 peers as mapped IPv6 addresses. The peer
    records, the leave/ownership policy and the duplicate-identity check must see
    one form, or the same host looks like two. Non-mapped values (hostnames,
    IPv4, real IPv6) pass through unchanged; an optional ``%scope`` is ignored.
    """
    if not value:
        return value
    try:
        parsed = _parse_ip(value.split("%", 1)[0])
    except ValueError:
        return value
    mapped = getattr(parsed, "ipv4_mapped", None)
    return str(mapped) if mapped is not None else value


def _enumerate_local_addresses() -> tuple[list[str], list[str]]:
    """Best-effort (IPv4, IPv6) interface lists via ``ifaddr``."""
    try:
        adapters = _get_adapters()
    except Exception:
        # Unusual libc/Windows API failure: degrade to the hostname lookup rather
        # than losing every additional address.
        return _hostname_ipv4(), []
    ipv4: list[str] = []
    ipv6: list[str] = []
    for adapter in adapters:
        if not _is_advertisable_adapter(adapter):
            continue
        for entry in adapter.ips:
            raw = entry.ip
            # ``ifaddr`` reports IPv6 as ``(addr, flowinfo, scope_id)``.
            if isinstance(raw, tuple):
                address = raw[0]
                if _is_usable_ipv6(address) and address not in ipv6:
                    ipv6.append(address)
            elif _is_usable_ipv4(raw) and raw not in ipv4:
                ipv4.append(raw)
    return ipv4, ipv6


def _hostname_ipv4() -> list[str]:
    """Hostname-resolved IPv4 addresses (the pre-``ifaddr`` fallback)."""
    try:
        return list(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        return []


def _is_usable_ipv4(address: str) -> bool:
    """Whether *address* is a routable IPv4 (not loopback or link-local)."""
    try:
        parsed = _parse_ip(address)
    except ValueError:
        return False
    if parsed.version != 4:
        return False
    return not (parsed.is_loopback or parsed.is_link_local)


def _is_usable_ipv6(address: str) -> bool:
    """Whether *address* is an IPv6 a peer can dial without a scope."""
    try:
        parsed = _parse_ip(address)
    except ValueError:
        return False
    if parsed.version != 6:
        return False
    return not (
        parsed.is_loopback or parsed.is_link_local or parsed.is_multicast or parsed.is_unspecified
    )


# Interface names that are virtual bridges or container/virtualization links:
# their addresses are host-local and often identical across hosts. ``lo`` is
# here too (its address is loopback, which the usability checks drop anyway).
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

# Windows reports ``Adapter.name`` as a GUID; the Description/FriendlyName carry
# the human label, so virtual adapters are recognised by substring.
_WINDOWS_VIRTUAL_MARKERS = (
    "loopback",
    "teredo",
    "isatap",
    "6to4",
    "vethernet",
    "hyper-v",
    "virtualbox",
    "vmware",
    "wsl",
)


def _is_advertisable_interface(name: str) -> bool:
    """Whether a POSIX interface is a real network a peer could share."""
    return not name.startswith(_VIRTUAL_INTERFACE_PREFIXES)


def _is_advertisable_adapter(adapter: ifaddr.Adapter) -> bool:
    """Whether an ``ifaddr`` adapter is a real network a peer could share."""
    if not sys.platform.startswith("win"):
        return _is_advertisable_interface(adapter.name)
    labels = " ".join(
        [adapter.name or "", adapter.nice_name or ""]
        + [entry.nice_name or "" for entry in adapter.ips]
    ).lower()
    return not any(marker in labels for marker in _WINDOWS_VIRTUAL_MARKERS)


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
