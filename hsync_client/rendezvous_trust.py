# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Durable rendezvous-server trust store (``rendezvous_server_known_hosts.json``), SSH-style.

A self-hosted rendezvous server authenticates its QUIC/TLS transport with a self-signed
certificate, so the only trust anchor is the certificate fingerprint the operator
pins. This store is the client's ``known_hosts``: it remembers the accepted
fingerprint per ``host:port`` and is the persistence half of the shared policy
:func:`hsync_common.protocol.tls_trust.evaluate_tls_trust`. The decision itself
lives in ``hsync-common`` so the Python client, the desktop subprocess contract
and the Android mirror cannot drift; this module only names the rendezvous store
(the shared ``host:port`` load/persist contract is in ``known_hosts.py``).

Each record keeps a single ``current`` fingerprint (no ``next``: a rotation
window is expressed by passing two ``--rendezvous-server-fingerprint`` values, and the
store records whichever matched) plus ``updated_at``, ``first_seen`` and the
``source`` that trusted it. State lives outside the synced bucket (overridable
via ``HSYNC_RENDEZVOUS_SERVER_TRUST_FILE``).

Consumers: ``hsync-client`` (connect path, ``hsync trust rendezvous-server``).
"""

from __future__ import annotations

from pathlib import Path

from hsync_common.state_paths import state_path

from hsync_client._trust_store import StoreError
from hsync_client.known_hosts import KnownHostsStore

__all__ = [
    "RENDEZVOUS_TRUST_FILE_ENV",
    "RendezvousTrustStore",
    "RendezvousTrustStoreError",
    "default_rendezvous_trust_path",
    "default_rendezvous_trust_store",
]

# Environment override for the rendezvous-server trust store (tests, operators keeping
# state elsewhere).
RENDEZVOUS_TRUST_FILE_ENV = "HSYNC_RENDEZVOUS_SERVER_TRUST_FILE"


class RendezvousTrustStoreError(StoreError):
    """The durable rendezvous-server trust store could not be read or updated safely."""


def default_rendezvous_trust_path() -> Path:
    """Resolve the trust-store path: ``$HSYNC_RENDEZVOUS_SERVER_TRUST_FILE`` or the state dir."""
    return state_path("rendezvous_server_known_hosts.json", override_env=RENDEZVOUS_TRUST_FILE_ENV)


class RendezvousTrustStore(KnownHostsStore):
    """Disk-backed map of ``host:port`` to a pinned rendezvous-server certificate fingerprint."""

    error_cls = RendezvousTrustStoreError
    label = "rendezvous-server trust store"
    records_key = "rendezvous_servers"

    def __init__(self, path: Path | None = None) -> None:
        super().__init__(path or default_rendezvous_trust_path())


def default_rendezvous_trust_store() -> RendezvousTrustStore:
    """Return a :class:`RendezvousTrustStore` at the default (env-overridable) location."""
    return RendezvousTrustStore()
