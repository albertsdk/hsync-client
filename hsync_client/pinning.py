# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Trust-On-First-Use certificate pinning store for LAN mode.

A LAN master authenticates its transport with a self-signed certificate whose
fingerprint travels in the mDNS TXT record. That certificate is only meaningful
once it is *pinned*: the first fingerprint seen for a ``(cid, bucket)`` is
remembered, and a later mismatch is treated as a possible man-in-the-middle and
refused. The trust *decision* is the shared source of truth in
:mod:`hsync_common.protocol.pairing`; this module only persists the pins and
applies that decision.

Pins live outside the synced bucket (an OS state directory, overridable via
``HSYNC_LAN_PIN_FILE``) so they survive restarts and are never themselves synced.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from hsync_common.protocol.pairing import PinDecision, evaluate_pin, normalize_fingerprint

__all__ = ["PIN_FILE_ENV", "PinStore", "default_pin_store", "default_pin_store_path"]

# Environment override for the pin store location (used by tests and operators
# who keep state outside the default OS location).
PIN_FILE_ENV = "HSYNC_LAN_PIN_FILE"

# Owner-only permissions for the pin file: it records which peers this host
# trusts and must not be world-readable/writable.
_PIN_FILE_MODE = 0o600


def default_pin_store_path() -> Path:
    """Resolve the pin-store path: ``$HSYNC_LAN_PIN_FILE`` or an OS state dir."""
    override = os.environ.get(PIN_FILE_ENV)
    if override:
        return Path(override)
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "hsync" / "lan_known_peers.json"


def _pin_key(cid: str, bucket: str) -> str:
    """Composite key for a peer's pin (``cid`` is unique per peer, per bucket)."""
    return f"{cid}|{bucket}"


class PinStore:
    """Disk-backed map of ``(cid, bucket)`` to a pinned certificate fingerprint.

    Reads tolerate a missing/corrupt file (treated as empty); writes are atomic
    and owner-only. Instances are thread-safe.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_pin_store_path()
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}

    def _save(self, data: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(tmp, _PIN_FILE_MODE)
        os.replace(tmp, self._path)

    def get(self, cid: str, bucket: str) -> str | None:
        """Return the pinned fingerprint for ``(cid, bucket)``, or ``None``."""
        with self._lock:
            return self._load().get(_pin_key(cid, bucket))

    def set(self, cid: str, bucket: str, fingerprint: str) -> None:
        """Pin (or repin) ``fingerprint`` for ``(cid, bucket)``."""
        normalized = normalize_fingerprint(fingerprint)
        if normalized is None:
            return
        with self._lock:
            data = self._load()
            data[_pin_key(cid, bucket)] = normalized
            self._save(data)

    def check_and_record(self, cid: str, bucket: str, observed_fp: str | None) -> PinDecision:
        """Classify ``observed_fp`` against the stored pin, pinning on first use.

        A first sighting (``trusted``) is persisted so the next run can detect a
        change; ``matched``/``changed``/``no_fingerprint`` never mutate the store.
        """
        with self._lock:
            data = self._load()
            stored = data.get(_pin_key(cid, bucket))
            decision = evaluate_pin(stored, observed_fp)
            if decision is PinDecision.trusted:
                normalized = normalize_fingerprint(observed_fp)
                if normalized is not None:
                    data[_pin_key(cid, bucket)] = normalized
                    self._save(data)
            return decision


def default_pin_store() -> PinStore:
    """Return a :class:`PinStore` at the default (env-overridable) location."""
    return PinStore()
