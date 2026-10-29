# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""UPnP-IGD for the p2p master's **TCP** listen port.

The SSDP/SOAP implementation (including the SSRF hardening) is the
``hsync_common.upnp`` SSOT shared with ``hsync-rendezvous``; this module only adds
``UpnpMapper`` — the best-effort TCP mapping lifecycle with clean teardown.
Every failure degrades to the documented "no UPnP" state: the node stays
slave-only instead of advertising an endpoint it cannot serve.

Consumers: ``hsync-client`` (p2p_rendezvous_rdv), tests.
"""

from __future__ import annotations

from hsync_common.upnp import (
    IgdService,
    UpnpError,
    add_port_mapping,
    delete_port_mapping,
    discover_igd,
)
from loguru import logger as log

__all__ = [
    "IgdService",
    "UpnpError",
    "UpnpMapper",
]


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
            log.debug(f"UPnP rendezvous failed: {exc}")
            return None
        if service is None:
            log.debug("UPnP: no IGD discovered")
            return None
        try:
            add_port_mapping(
                service,
                internal_port=internal_port,
                external_port=internal_port,
                protocol=self._protocol,
                description=self._description,
            )
        except UpnpError as exc:
            log.debug(f"UPnP mapping failed: {exc}")
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
        except UpnpError as exc:
            log.debug(f"UPnP unmap failed: {exc}")
        finally:
            self._service = None
            self._external_port = None
