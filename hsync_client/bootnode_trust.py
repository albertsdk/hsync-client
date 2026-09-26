# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Trust-On-First-Use store for bootnode certificate fingerprints (ADR-0034).

A self-hosted bootnode authenticates its QUIC/TLS transport with a self-signed
certificate. The fingerprint is only meaningful once it is *pinned*: with
``--bootnode-tofu`` the first fingerprint seen for a ``host:port`` is remembered
here, and a later mismatch is treated as a possible man-in-the-middle and
refused. The decision itself is the shared source of truth in
:mod:`hsync_common.protocol.pairing` (``fingerprint_matches``); this module only
persists the pins, mirroring the P2P pin store's locking/atomicity contract.

The store keeps a single ``current`` fingerprint per bootnode (no ``next``): a
rotation window is expressed by passing two ``--bootnode-fingerprint`` values,
and the store records whichever matched. State lives outside the synced bucket
(overridable via ``HSYNC_BOOTNODE_TRUST_FILE``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from hsync_common.protocol.pairing import parse_fingerprint
from hsync_common.time_utils import utc_now_iso

from hsync_client._trust_store import JsonStore, StoreError

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

# On-disk schema version (bumped only when the file shape changes).
_SCHEMA_VERSION = 1


class BootnodeTrustStoreError(StoreError):
    """The durable bootnode trust store could not be read or updated safely."""


def default_bootnode_trust_path() -> Path:
    """Resolve the trust-store path: ``$HSYNC_BOOTNODE_TRUST_FILE`` or an OS state dir."""
    override = os.environ.get(BOOTNODE_TRUST_FILE_ENV)
    if override:
        return Path(override)
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "hsync" / "bootnode_known_hosts.json"


def _bootnode_key(host: str, port: int) -> str:
    """Composite key for a bootnode pin (host + port identify one node)."""
    return f"{host}:{port}"


class BootnodeTrustStore(JsonStore):
    """Disk-backed map of ``host:port`` to a pinned bootnode certificate fingerprint.

    A missing file is an empty store; corrupt state fails closed. The record
    holds a single ``current`` fingerprint plus its ``updated_at`` timestamp.
    """

    error_cls = BootnodeTrustStoreError
    label = "bootnode trust store"

    def __init__(self, path: Path | None = None) -> None:
        super().__init__(path or default_bootnode_trust_path())

    def _load_unlocked(self) -> dict[str, Any]:
        data = super()._load_unlocked()
        if not data:
            # Missing file or an empty object: an empty trust store.
            return {"version": _SCHEMA_VERSION, "bootnodes": {}}
        if data.get("version") != _SCHEMA_VERSION:
            raise BootnodeTrustStoreError(f"{self.label} has an unsupported version")
        bootnodes = data.get("bootnodes")
        if not isinstance(bootnodes, dict):
            raise BootnodeTrustStoreError(f"{self.label} is corrupt")
        for key, record in bootnodes.items():
            if not isinstance(key, str) or not isinstance(record, dict):
                raise BootnodeTrustStoreError(f"{self.label} is corrupt")
            if parse_fingerprint(record.get("current")) is None:
                raise BootnodeTrustStoreError(f"{self.label} is corrupt")
            if not isinstance(record.get("updated_at"), str):
                raise BootnodeTrustStoreError(f"{self.label} is corrupt")
        return data

    def _records(self) -> dict[str, dict[str, str]]:
        data = self._load_unlocked()
        return data.setdefault("bootnodes", {})  # type: ignore[return-value]

    def get(self, host: str, port: int) -> str | None:
        """Return the pinned fingerprint for ``host:port``, or ``None``."""
        with self._transaction():
            record = self._records().get(_bootnode_key(host, port))
            return record["current"] if record is not None else None

    def set(self, host: str, port: int, fingerprint: str) -> None:
        """Pin (or repin) ``fingerprint`` for ``host:port``."""
        normalized = parse_fingerprint(fingerprint)
        if normalized is None:
            raise ValueError("bootnode fingerprint is not a valid sha256:<hex> pin")
        with self._transaction():
            data = self._load_unlocked()
            data["version"] = _SCHEMA_VERSION
            bootnodes = data.setdefault("bootnodes", {})
            bootnodes[_bootnode_key(host, port)] = {
                "current": normalized,
                "updated_at": utc_now_iso(),
            }
            self._save_unlocked(data)


def default_bootnode_trust_store() -> BootnodeTrustStore:
    """Return a :class:`BootnodeTrustStore` at the default (env-overridable) location."""
    return BootnodeTrustStore()
