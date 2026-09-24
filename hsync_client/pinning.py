# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

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
    """Disk-backed map of ``(cid, bucket)`` to a pinned certificate fingerprint.

    A missing file is an empty store. Corrupt/unreadable state fails closed;
    writes use one cross-process locked read/modify/fsync/replace transaction.
    """

    error_cls = PinStoreError
    label = "P2P pin store"

    def __init__(self, path: Path | None = None) -> None:
        super().__init__(path or default_pin_store_path())

    def _load_unlocked(self) -> dict[str, str]:
        data = super()._load_unlocked()
        if any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in data.items()
        ):
            raise PinStoreError("P2P pin store is corrupt")
        return data  # type: ignore[return-value]

    def get(self, cid: str, bucket: str) -> str | None:
        """Return the pinned fingerprint for ``(cid, bucket)``, or ``None``."""
        with self._transaction():
            return self._load_unlocked().get(_pin_key(cid, bucket))

    def set(self, cid: str, bucket: str, fingerprint: str) -> None:
        """Pin (or repin) ``fingerprint`` for ``(cid, bucket)``."""
        normalized = normalize_fingerprint(fingerprint)
        if normalized is None:
            raise ValueError("fingerprint is mandatory for P2P HTTPS pinning")
        with self._transaction():
            data = self._load_unlocked()
            data[_pin_key(cid, bucket)] = normalized
            self._save_unlocked(data)

    def check_and_record(self, cid: str, bucket: str, observed_fp: str | None) -> PinDecision:
        """Classify ``observed_fp`` against the stored pin, pinning on first use.

        A first sighting (``trusted``) is persisted so the next run can detect a
        change; ``matched``/``changed`` never mutate the store. ``observed_fp``
        must be present (P2P transport is HTTPS-only); a missing fingerprint is a
        contract violation and :func:`evaluate_pin` raises.
        """
        key = _pin_key(cid, bucket)
        with self._transaction():
            data = self._load_unlocked()
            stored = data.get(key)
            decision = evaluate_pin(stored, observed_fp)
            if decision is PinDecision.trusted:
                normalized = normalize_fingerprint(observed_fp)
                if normalized is None:
                    raise ValueError("fingerprint is mandatory for P2P HTTPS pinning")
                data[key] = normalized
                self._save_unlocked(data)
                if self._load_unlocked().get(key) != normalized:
                    raise PinStoreError("P2P pin store verification failed")
            return decision


def default_pin_store() -> PinStore:
    """Return a :class:`PinStore` at the default (env-overridable) location."""
    return PinStore()
