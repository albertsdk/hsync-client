# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Shared ``host:port`` known-hosts store, SSH ``known_hosts``-style.

Two certificates are authenticated by pinning a fingerprint per ``host:port``: the
``boot/1`` bootnode (``discovery_server_known_hosts.json``) and the sync server
(``server_known_hosts.json``). They have the exact same on-disk shape — a map of
``host:port`` to a record with ``current``/``updated_at``/``first_seen``/``source``
— so this module owns the load/validate/persist contract; the two stores differ
only in their default path, JSON record key, error type and label.

The trust *decision* is the shared SSOT in
:mod:`hsync_common.protocol.tls_trust`; this module only persists the pins. Pins
live outside the synced bucket (an OS state directory, overridable via the store's
env var) so they survive restarts and are never themselves synced.

Consumers: ``hsync-client`` (``bootnode_trust.py``, ``server_trust.py``).
"""

from __future__ import annotations

from typing import Any

from hsync_common.endpoint import format_authority, parse_host_port
from hsync_common.protocol.pairing import parse_fingerprint
from hsync_common.time_utils import utc_now_iso

from hsync_client._trust_store import JsonStore

__all__ = ["KNOWN_HOSTS_SCHEMA_VERSION", "TRUST_SOURCES", "KnownHostsStore"]

# On-disk schema version (bumped only when the file shape changes). Shared by the
# bootnode and server stores so their JSON is interchangeable in tests/fixtures.
KNOWN_HOSTS_SCHEMA_VERSION = 1

# Why a fingerprint was pinned: the interactive prompt, TOFU, an explicit
# ``--…-fingerprint`` flag, or a ``trust add`` command. Shared vocabulary across
# the bootnode and server stores.
TRUST_SOURCES = frozenset({"prompt", "tofu", "flag", "cli"})


def host_port_key(host: str, port: int) -> str:
    """Composite key for a host pin (host + port identify one endpoint).

    IPv6 hosts are bracketed (``[::1]:443``) so the key is unambiguous and
    re-parseable by the shared ``parse_host_port`` grammar.
    """
    return format_authority(host, port)


class KnownHostsStore(JsonStore):
    """Disk-backed map of ``host:port`` to a pinned certificate fingerprint.

    A missing file is an empty store; any other shape (unknown version, a record
    without ``current``/``updated_at``/``first_seen``/``source``) fails closed.
    Subclasses set :attr:`records_key`, :attr:`label` and ``error_cls`` and pass
    their default path to :class:`~hsync_client._trust_store.JsonStore`.
    """

    # JSON key under which the records live (e.g. ``discovery_servers`` / ``servers``).
    records_key: str

    def _load_unlocked(self) -> dict[str, Any]:
        data = super()._load_unlocked()
        if not data:
            # Missing file or an empty object: an empty known-hosts store.
            return {"version": KNOWN_HOSTS_SCHEMA_VERSION, self.records_key: {}}
        version = data.get("version")
        if version != KNOWN_HOSTS_SCHEMA_VERSION:
            raise self.error_cls(f"{self.label} has an unsupported version")
        records = data.get(self.records_key)
        if not isinstance(records, dict):
            raise self.error_cls(f"{self.label} is corrupt")
        for key, record in records.items():
            if not isinstance(key, str) or not isinstance(record, dict):
                raise self.error_cls(f"{self.label} is corrupt")
            if parse_fingerprint(record.get("current")) is None:
                raise self.error_cls(f"{self.label} is corrupt")
            if not isinstance(record.get("updated_at"), str):
                raise self.error_cls(f"{self.label} is corrupt")
            if not isinstance(record.get("first_seen"), str):
                raise self.error_cls(f"{self.label} is corrupt")
            if record.get("source") not in TRUST_SOURCES:
                raise self.error_cls(f"{self.label} is corrupt")
        return data

    def _records(self) -> dict[str, dict[str, str]]:
        data = self._load_unlocked()
        return data.setdefault(self.records_key, {})  # type: ignore[return-value]

    def get(self, host: str, port: int) -> str | None:
        """Return the pinned fingerprint for ``host:port``, or ``None``."""
        with self._transaction():
            record = self._records().get(host_port_key(host, port))
            return record["current"] if record is not None else None

    def entries(self) -> list[dict[str, Any]]:
        """Return every pin as ``host``/``port``/``fingerprint``/… dicts."""
        with self._transaction():
            records = self._records()
            result: list[dict[str, Any]] = []
            for key, record in records.items():
                try:
                    endpoint = parse_host_port(key)
                except ValueError:
                    continue  # a key this grammar no longer understands
                if endpoint.port is None:
                    continue
                result.append(
                    {
                        "host": endpoint.host,
                        "port": endpoint.port,
                        "fingerprint": record["current"],
                        "updated_at": record.get("updated_at", ""),
                        "first_seen": record.get("first_seen", ""),
                        "source": record["source"],
                    }
                )
            return sorted(result, key=lambda item: (item["host"], str(item["port"])))

    def set(self, host: str, port: int, fingerprint: str, *, source: str) -> None:
        """Pin (or repin) ``fingerprint`` for ``host:port``, recording *source*.

        ``first_seen`` is preserved when the host was already present, so a re-pin
        (a documented rotation) keeps the original trust date.
        """
        normalized = parse_fingerprint(fingerprint)
        if normalized is None:
            raise ValueError("fingerprint is not a valid sha256:<hex> pin")
        if source not in TRUST_SOURCES:
            raise ValueError(f"unknown trust source: {source!r}")
        now = utc_now_iso()
        with self._transaction():
            data = self._load_unlocked()
            data["version"] = KNOWN_HOSTS_SCHEMA_VERSION
            records = data.setdefault(self.records_key, {})
            existing = records.get(host_port_key(host, port))
            first_seen = existing.get("first_seen") if existing else None
            records[host_port_key(host, port)] = {
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
            records = data.setdefault(self.records_key, {})
            key = host_port_key(host, port)
            if key not in records:
                return False
            del records[key]
            data["version"] = KNOWN_HOSTS_SCHEMA_VERSION
            self._save_unlocked(data)
            return True
