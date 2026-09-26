# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Minimal UPnP-IGD client for the p2p master's **TCP** listen port.

Pure standard library (SSDP discovery + SOAP over HTTP), the same IGD protocol
the bootnode uses for its UDP port but mapped to TCP: a master behind NAT must
open its data-plane port so peers reached through the bootnode can connect.
Every failure degrades to the documented "no UPnP" state — the node stays
slave-only instead of crashing or advertising an endpoint it cannot serve.

Consumers: ``hsync-client`` (p2p_discovery_bootnode), tests.
"""

from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urljoin
from xml.etree import ElementTree

from loguru import logger as log

from hsync_client.net_utils import get_local_ip

__all__ = [
    "UPNP_SSDP_ADDR",
    "IgdService",
    "UpnpError",
    "UpnpMapper",
    "add_port_mapping",
    "build_add_mapping_soap",
    "build_delete_mapping_soap",
    "delete_port_mapping",
    "discover_igd",
]

UPNP_SSDP_ADDR = ("239.255.255.250", 1900)
_SSDP_SEARCH_TARGET = "urn:schemas-upnp-org:device:InternetGatewayDevice:1"
_SERVICE_TYPES = (
    "urn:schemas-upnp-org:service:WANIPConnection:",
    "urn:schemas-upnp-org:service:WANPPPConnection:",
)
_MSEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    'MAN: "ssdp:discover"\r\n'
    f"ST: {_SSDP_SEARCH_TARGET}\r\n"
    "MX: 1\r\n"
    "\r\n"
).encode("ascii")
_SERVICE_NS = "urn:schemas-upnp-org:service:WANIPConnection:1"


class UpnpError(Exception):
    """UPnP-IGD discovery or SOAP control failure."""


@dataclass(frozen=True, slots=True)
class IgdService:
    """A discovered Internet Gateway Device control endpoint."""

    control_url: str
    local_ip: str
    service_type: str


def discover_igd(timeout: float = 2.0, local_ip: str | None = None) -> IgdService | None:
    """Discover the local IGD via SSDP; ``None`` when there is none reachable."""
    local_ip = local_ip or get_local_ip()
    own = None
    try:
        own = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        own.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        own.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        own.bind((local_ip, 0))
        own.sendto(_MSEARCH, UPNP_SSDP_ADDR)
    except OSError as exc:
        raise UpnpError(f"SSDP discovery failed: {exc}") from exc
    try:
        return _first_igd_location(own, timeout, local_ip)
    finally:
        if own is not None:
            own.close()


def _first_igd_location(sock: socket.socket, timeout: float, local_ip: str) -> IgdService | None:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        sock.settimeout(remaining)
        try:
            data, _addr = sock.recvfrom(4096)
        except TimeoutError:
            return None
        location = _parse_ssdp_response(data)
        if location is None:
            continue
        service = _igd_service_from_location(location, local_ip)
        if service is not None:
            return service


def _parse_ssdp_response(data: bytes) -> str | None:
    """Extract the ``LOCATION`` header from an SSDP M-SEARCH reply."""
    text = data.decode("utf-8", errors="replace")
    first, _, rest = text.partition("\r\n")
    if not first.upper().startswith("HTTP/1.1 200"):
        return None
    for line in rest.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        if key.strip().lower() == "location":
            return value.strip()
    return None


def _igd_service_from_location(location: str, local_ip: str) -> IgdService | None:
    """Fetch the IGD description XML and locate a WAN connection control URL."""
    try:
        with urllib.request.urlopen(location, timeout=3.0) as response:
            xml_bytes = response.read(65536)
    except OSError:
        return None
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError:
        return None
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "service":
            continue
        entries = {child.tag.rsplit("}", 1)[-1]: (child.text or "").strip() for child in element}
        service_type = entries.get("serviceType", "")
        if not service_type.startswith(_SERVICE_TYPES):
            continue
        control = entries.get("controlURL")
        if not control:
            continue
        prefix = service_type.rsplit(":", 1)[0]
        return IgdService(
            control_url=urljoin(location, control),
            local_ip=local_ip,
            service_type=f"{prefix}:1",
        )
    return None


def _soap_call(service: IgdService, action: str, body: bytes, timeout: float = 4.0) -> bool:
    request = urllib.request.Request(
        service.control_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{service.service_type}#{action}"',
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as exc:
        log.debug(f"UPnP {action} refused: HTTP {exc.code}")
        return False
    except OSError as exc:
        log.debug(f"UPnP {action} failed: {exc}")
        return False


def build_add_mapping_soap(
    *,
    service_type: str,
    internal_client: str,
    internal_port: int,
    external_port: int,
    protocol: str = "TCP",
    description: str = "hsync-p2p",
    lease_duration: int = 0,
) -> bytes:
    """Build the ``AddPortMapping`` SOAP envelope (pure builder, unit-testable)."""
    body = (
        '<?xml version="1.0"?>\n'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">\n'
        "  <s:Body>\n"
        f'    <u:AddPortMapping xmlns:u="{service_type}">\n'
        "      <NewRemoteHost></NewRemoteHost>\n"
        f"      <NewExternalPort>{external_port}</NewExternalPort>\n"
        f"      <NewProtocol>{protocol}</NewProtocol>\n"
        f"      <NewInternalPort>{internal_port}</NewInternalPort>\n"
        f"      <NewInternalClient>{internal_client}</NewInternalClient>\n"
        "      <NewEnabled>1</NewEnabled>\n"
        f"      <NewPortMappingDescription>{description}</NewPortMappingDescription>\n"
        f"      <NewLeaseDuration>{lease_duration}</NewLeaseDuration>\n"
        "    </u:AddPortMapping>\n"
        "  </s:Body>\n"
        "</s:Envelope>\n"
    )
    return body.encode("utf-8")


def build_delete_mapping_soap(
    *,
    service_type: str,
    external_port: int,
    protocol: str = "TCP",
) -> bytes:
    """Build the ``DeletePortMapping`` SOAP envelope (pure builder, unit-testable)."""
    body = (
        '<?xml version="1.0"?>\n'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">\n'
        "  <s:Body>\n"
        f'    <u:DeletePortMapping xmlns:u="{service_type}">\n'
        "      <NewRemoteHost></NewRemoteHost>\n"
        f"      <NewExternalPort>{external_port}</NewExternalPort>\n"
        f"      <NewProtocol>{protocol}</NewProtocol>\n"
        "    </u:DeletePortMapping>\n"
        "  </s:Body>\n"
        "</s:Envelope>\n"
    )
    return body.encode("utf-8")


def add_port_mapping(
    service: IgdService,
    *,
    internal_client: str,
    internal_port: int,
    external_port: int,
    protocol: str = "TCP",
    lease_duration: int = 0,
) -> bool:
    """Create (or refresh) a port mapping; ``False`` on any refusal."""
    return _soap_call(
        service,
        "AddPortMapping",
        build_add_mapping_soap(
            service_type=service.service_type,
            internal_client=internal_client,
            internal_port=internal_port,
            external_port=external_port,
            protocol=protocol,
            lease_duration=lease_duration,
        ),
    )


def delete_port_mapping(
    service: IgdService,
    *,
    external_port: int,
    protocol: str = "TCP",
) -> bool:
    """Remove a port mapping; ``False`` on any refusal."""
    return _soap_call(
        service,
        "DeletePortMapping",
        build_delete_mapping_soap(
            service_type=service.service_type,
            external_port=external_port,
            protocol=protocol,
        ),
    )


class UpnpMapper:
    """Best-effort TCP port mapping for the p2p master, with clean teardown."""

    def __init__(self, protocol: str = "TCP", description: str = "hsync-p2p") -> None:
        self._protocol = protocol
        self._description = description
        self._service: IgdService | None = None
        self._external_port: int | None = None

    @property
    def active(self) -> bool:
        """True while a mapping is believed to be installed."""
        return self._service is not None and self._external_port is not None

    def map_port(self, internal_port: int) -> int | None:
        """Map ``internal_port`` on the IGD; return the external port or ``None``.

        A failure leaves the mapper inactive (the node must then stay
        slave-only rather than advertise an unreachable endpoint).
        """
        try:
            service = discover_igd()
        except UpnpError as exc:
            log.debug(f"UPnP discovery failed: {exc}")
            return None
        if service is None:
            log.debug("UPnP: no IGD discovered")
            return None
        ok = add_port_mapping(
            service,
            internal_client=service.local_ip,
            internal_port=internal_port,
            external_port=internal_port,
            protocol=self._protocol,
        )
        if not ok:
            return None
        self._service = service
        self._external_port = internal_port
        log.info(f"UPnP mapped {self._protocol} {internal_port} for the p2p master")
        return internal_port

    def unmap(self) -> None:
        """Remove the mapping if one is installed (idempotent)."""
        if self._service is None or self._external_port is None:
            return
        try:
            delete_port_mapping(
                self._service,
                external_port=self._external_port,
                protocol=self._protocol,
            )
        finally:
            self._service = None
            self._external_port = None
