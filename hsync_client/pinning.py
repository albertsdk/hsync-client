# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Trust-On-First-Use certificate pinning store for P2P mode.

A P2P master authenticates its transport with a self-signed certificate whose
fingerprint travels in the mDNS TXT record. That certificate is only meaningful
once it is *pinned*: the first fingerprint seen for a ``(cid, bucket)`` is
remembered, and a later mismatch is treated as a possible man-in-the-middle and
refused. The trust *decision* is the shared source of truth in
:mod:`hsync_common.protocol.pairing`; this module only persists the pins and
applies that decision.

Pins live outside the synced bucket (an OS state directory, overridable via
``HSYNC_P2P_PIN_FILE``) so they survive restarts and are never themselves synced.
"""

from __future__ import annotations

import os
from pathlib import Path

from hsync_common.protocol.pairing import PinDecision, evaluate_pin, normalize_fingerprint
from hsync_common.time_utils import utc_now_iso

from hsync_client._trust_store import JsonStore, StoreError

__all__ = [
    "PIN_FILE_ENV",
    "PinStore",
    "PinStoreError",
    "default_pin_store",
    "default_pin_store_path",
]

# Environment override for the pin store location (used by tests and operators
# who keep state outside the default OS location).
PIN_FILE_ENV = "HSYNC_P2P_PIN_FILE"


class PinStoreError(StoreError):
    """The durable TOFU store could not be read or updated safely."""


def default_pin_store_path() -> Path:
    """Resolve the pin-store path: ``$HSYNC_P2P_PIN_FILE`` or an OS state dir."""
    override = os.environ.get(PIN_FILE_ENV)
    if override:
        return Path(override)
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "hsync" / "p2p_known_peers.json"


def _pin_key(cid: str, bucket: str) -> str:
    """Composite key for a peer's pin (``cid`` is unique per peer, per bucket)."""
    return f"{cid}|{bucket}"


class PinStore(JsonStore):
    """Disk-backed map of ``(cid, bucket)`` to a pinned peer record.

    Each record holds the pinned ``fingerprint`` and the ``first_seen`` time the
    peer was trusted, so the trust store is auditable (mirroring the known-hosts
    stores). A missing file is an empty store. Corrupt/unreadable state fails
    closed; writes use one cross-process locked read/modify/fsync/replace
    transaction.
    """

    error_cls = PinStoreError
    label = "P2P pin store"

    def __init__(self, path: Path | None = None) -> None:
        super().__init__(path or default_pin_store_path())

    def _load_unlocked(self) -> dict[str, dict[str, str]]:
        data = super()._load_unlocked()
        for key, record in data.items():
            if not isinstance(key, str) or not isinstance(record, dict):
                raise PinStoreError("P2P pin store is corrupt")
            if normalize_fingerprint(record.get("fingerprint")) is None:
                raise PinStoreError("P2P pin store is corrupt")
            if not isinstance(record.get("first_seen"), str):
                raise PinStoreError("P2P pin store is corrupt")
        return data  # type: ignore[return-value]

    def get(self, cid: str, bucket: str) -> str | None:
        """Return the pinned fingerprint for ``(cid, bucket)``, or ``None``."""
        with self._transaction():
            record = self._load_unlocked().get(_pin_key(cid, bucket))
            return record["fingerprint"] if record is not None else None

    def entries(self) -> list[dict[str, str]]:
        """Return every pinned peer as ``cid``/``bucket``/``fingerprint``/``first_seen``.

        Sorted by ``(cid, bucket)`` so ``hsync trust peer list`` output is stable.
        """
        with self._transaction():
            data = self._load_unlocked()
            result: list[dict[str, str]] = []
            for key, record in data.items():
                cid, _, bucket = key.partition("|")
                result.append(
                    {
                        "cid": cid,
                        "bucket": bucket,
                        "fingerprint": record["fingerprint"],
                        "first_seen": record["first_seen"],
                    }
                )
            return sorted(result, key=lambda item: (item["cid"], item["bucket"]))

    def set(self, cid: str, bucket: str, fingerprint: str) -> None:
        """Pin (or repin) ``fingerprint`` for ``(cid, bucket)``.

        ``first_seen`` is preserved when the peer was already present, so a re-pin
        (a documented rotation) keeps the original trust date.
        """
        normalized = normalize_fingerprint(fingerprint)
        if normalized is None:
            raise ValueError("fingerprint is mandatory for P2P HTTPS pinning")
        key = _pin_key(cid, bucket)
        with self._transaction():
            data = self._load_unlocked()
            existing = data.get(key)
            first_seen = existing["first_seen"] if existing else utc_now_iso()
            data[key] = {"fingerprint": normalized, "first_seen": first_seen}
            self._save_unlocked(data)

    def remove(self, cid: str, bucket: str) -> bool:
        """Forget the pin for ``(cid, bucket)``; return whether an entry was removed."""
        with self._transaction():
            data = self._load_unlocked()
            key = _pin_key(cid, bucket)
            if key not in data:
                return False
            del data[key]
            self._save_unlocked(data)
            return True

    def check_and_record(self, cid: str, bucket: str, observed_fp: str | None) -> PinDecision:
        """Classify ``observed_fp`` against the stored pin, pinning on first use.

        A first sighting (``trusted``) is persisted (with its ``first_seen``) so
        the next run can detect a change; ``matched``/``changed`` never mutate the
        store. ``observed_fp`` must be present (P2P transport is HTTPS-only); a
        missing fingerprint is a contract violation and :func:`evaluate_pin` raises.
        """
        key = _pin_key(cid, bucket)
        with self._transaction():
            data = self._load_unlocked()
            existing = data.get(key)
            decision = evaluate_pin(existing["fingerprint"] if existing else None, observed_fp)
            if decision is PinDecision.trusted:
                normalized = normalize_fingerprint(observed_fp)
                if normalized is None:
                    raise ValueError("fingerprint is mandatory for P2P HTTPS pinning")
                data[key] = {"fingerprint": normalized, "first_seen": utc_now_iso()}
                self._save_unlocked(data)
                if self._load_unlocked()[key]["fingerprint"] != normalized:
                    raise PinStoreError("P2P pin store verification failed")
            return decision


def default_pin_store() -> PinStore:
    """Return a :class:`PinStore` at the default (env-overridable) location."""
    return PinStore()
