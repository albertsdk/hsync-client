# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Durable bootnode trust store (``bootnode_known_hosts.json``), SSH-style.

A self-hosted bootnode authenticates its QUIC/TLS transport with a self-signed
certificate, so the only trust anchor is the certificate fingerprint the operator
pins. This store is the client's ``known_hosts``: it remembers the accepted
fingerprint per ``host:port`` and is the persistence half of the shared policy
:func:`hsync_common.protocol.tls_trust.evaluate_tls_trust`. The decision itself
lives in ``hsync-common`` so the Python client, the desktop subprocess contract
and the Android mirror cannot drift; this module only names the bootnode store
(the shared ``host:port`` load/persist contract is in ``known_hosts.py``).

Each record keeps a single ``current`` fingerprint (no ``next``: a rotation
window is expressed by passing two ``--bootnode-fingerprint`` values, and the
store records whichever matched) plus ``updated_at``, ``first_seen`` and the
``source`` that trusted it. State lives outside the synced bucket (overridable
via ``HSYNC_BOOTNODE_TRUST_FILE``).

Consumers: ``hsync-client`` (bootnode connect path, ``hsync trust bootnode``).
"""

from __future__ import annotations

import os
from pathlib import Path

from hsync_client._trust_store import StoreError
from hsync_client.known_hosts import KnownHostsStore

__all__ = [
    "BOOTNODE_TRUST_FILE_ENV",
    "BootnodeTrustStore",
    "BootnodeTrustStoreError",
    "default_bootnode_trust_path",
    "default_bootnode_trust_store",
]

# Environment override for the bootnode trust store (tests, operators keeping
# state elsewhere).
BOOTNODE_TRUST_FILE_ENV = "HSYNC_BOOTNODE_TRUST_FILE"


class BootnodeTrustStoreError(StoreError):
    """The durable bootnode trust store could not be read or updated safely."""


def default_bootnode_trust_path() -> Path:
    """Resolve the trust-store path: ``$HSYNC_BOOTNODE_TRUST_FILE`` or an OS state dir."""
    override = os.environ.get(BOOTNODE_TRUST_FILE_ENV)
    if override:
        return Path(override)
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "hsync" / "bootnode_known_hosts.json"


class BootnodeTrustStore(KnownHostsStore):
    """Disk-backed map of ``host:port`` to a pinned bootnode certificate fingerprint."""

    error_cls = BootnodeTrustStoreError
    label = "bootnode trust store"
    records_key = "bootnodes"

    def __init__(self, path: Path | None = None) -> None:
        super().__init__(path or default_bootnode_trust_path())


def default_bootnode_trust_store() -> BootnodeTrustStore:
    """Return a :class:`BootnodeTrustStore` at the default (env-overridable) location."""
    return BootnodeTrustStore()
