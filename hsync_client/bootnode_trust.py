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
:func:`hsync_common.protocol.bootnode_trust.evaluate_bootnode_trust`. The
decision itself lives in ``hsync-common`` so the Python client, the desktop
subprocess contract and the Android mirror cannot drift; this module only
persists the pins, mirroring the P2P pin store's locking/atomicity contract.

Each record keeps a single ``current`` fingerprint (no ``next``: a rotation
window is expressed by passing two ``--bootnode-fingerprint`` values, and the
store records whichever matched) plus ``updated_at``, ``first_seen`` and the
``source`` that trusted it. State lives outside the synced bucket (overridable
via ``HSYNC_BOOTNODE_TRUST_FILE``).
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
    "TRUST_SOURCES",
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

# Why a fingerprint was pinned: the interactive prompt, TOFU, or the
# ``hsync bootnode trust add`` command.
TRUST_SOURCES = frozenset({"prompt", "tofu", "cli"})


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

    A missing file is an empty store; any other shape (unknown version, a record
    without ``current``/``updated_at``/``first_seen``/``source``) fails closed.
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
        version = data.get("version")
        if version != _SCHEMA_VERSION:
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
            if not isinstance(record.get("first_seen"), str):
                raise BootnodeTrustStoreError(f"{self.label} is corrupt")
            if record.get("source") not in TRUST_SOURCES:
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

    def entries(self) -> list[dict[str, Any]]:
        """Return every pinned bootnode as ``host``/``port``/``fingerprint``/… dicts."""
        with self._transaction():
            records = self._records()
            result: list[dict[str, Any]] = []
            for key, record in records.items():
                host, _, port = key.rpartition(":")
                result.append(
                    {
                        "host": host,
                        "port": int(port) if port.isdigit() else port,
                        "fingerprint": record["current"],
                        "updated_at": record.get("updated_at", ""),
                        "first_seen": record.get("first_seen", ""),
                        "source": record["source"],
                    }
                )
            return sorted(result, key=lambda item: (item["host"], str(item["port"])))

    def set(self, host: str, port: int, fingerprint: str, *, source: str) -> None:
        """Pin (or repin) ``fingerprint`` for ``host:port``, recording *source*.

        ``first_seen`` is preserved when the node was already present, so a
        re-pin (a documented rotation) keeps the original trust date.
        """
        normalized = parse_fingerprint(fingerprint)
        if normalized is None:
            raise ValueError("bootnode fingerprint is not a valid sha256:<hex> pin")
        if source not in TRUST_SOURCES:
            raise ValueError(f"unknown trust source: {source!r}")
        now = utc_now_iso()
        with self._transaction():
            data = self._load_unlocked()
            data["version"] = _SCHEMA_VERSION
            bootnodes = data.setdefault("bootnodes", {})
            existing = bootnodes.get(_bootnode_key(host, port))
            first_seen = existing.get("first_seen") if existing else None
            bootnodes[_bootnode_key(host, port)] = {
                "current": normalized,
                "updated_at": now,
                "first_seen": first_seen or now,
                "source": source,
            }
            self._save_unlocked(data)

    def remove(self, host: str, port: int) -> bool:
        """Forget the pin for ``host:port``; return whether an entry was removed."""
        with self._transaction():
            data = self._load_unlocked()
            bootnodes = data.setdefault("bootnodes", {})
            key = _bootnode_key(host, port)
            if key not in bootnodes:
                return False
            del bootnodes[key]
            data["version"] = _SCHEMA_VERSION
            self._save_unlocked(data)
            return True


def default_bootnode_trust_store() -> BootnodeTrustStore:
    """Return a :class:`BootnodeTrustStore` at the default (env-overridable) location."""
    return BootnodeTrustStore()
