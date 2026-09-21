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

import json
import os
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from hsync_common.protocol.pairing import PinDecision, evaluate_pin, normalize_fingerprint

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

# Owner-only permissions for the pin file: it records which peers this host
# trusts and must not be world-readable/writable.
_PIN_FILE_MODE = 0o600
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


class PinStoreError(RuntimeError):
    """The durable TOFU store could not be read or updated safely."""


def _thread_lock_for(path: Path) -> threading.RLock:
    key = str(path.absolute())
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def _lock_fd(fd: int) -> None:
    if os.name == "nt":  # pragma: no cover - exercised by Windows packaging
        import msvcrt

        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        locking = vars(msvcrt)["locking"]
        locking(fd, vars(msvcrt)["LK_LOCK"], 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)


def _unlock_fd(fd: int) -> None:
    if os.name == "nt":  # pragma: no cover - exercised by Windows packaging
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        locking = vars(msvcrt)["locking"]
        locking(fd, vars(msvcrt)["LK_UNLCK"], 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


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


class PinStore:
    """Disk-backed map of ``(cid, bucket)`` to a pinned certificate fingerprint.

    A missing file is an empty store. Corrupt/unreadable state fails closed;
    writes use one cross-process locked read/modify/fsync/replace transaction.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_pin_store_path()
        self._lock = _thread_lock_for(self._path)

    @property
    def path(self) -> Path:
        return self._path

    def _load_unlocked(self) -> dict[str, str]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise PinStoreError(f"could not read P2P pin store: {exc}") from exc
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise PinStoreError("P2P pin store is corrupt") from exc
        if not isinstance(data, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in data.items()
        ):
            raise PinStoreError("P2P pin store is corrupt")
        return dict(data)

    def _save_unlocked(self, data: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=str(self._path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, indent=2, sort_keys=True))
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, _PIN_FILE_MODE)
            os.replace(tmp, self._path)
            if hasattr(os, "O_DIRECTORY"):
                dir_fd = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Hold the per-path thread lock and cross-process advisory lock."""
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                lock_path = self._path.with_name(f".{self._path.name}.lock")
                fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, _PIN_FILE_MODE)
                try:
                    os.chmod(lock_path, _PIN_FILE_MODE)
                    _lock_fd(fd)
                    try:
                        yield
                    finally:
                        _unlock_fd(fd)
                finally:
                    os.close(fd)
            except PinStoreError:
                raise
            except OSError as exc:
                raise PinStoreError(f"could not lock or persist P2P pin store: {exc}") from exc

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
