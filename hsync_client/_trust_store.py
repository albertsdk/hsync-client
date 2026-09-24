# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Shared cross-process-locked JSON file store for durable peer trust.

Both the P2P pin store (:class:`hsync_client.pinning.PinStore`) and the bootnode
TOFU store (:class:`hsync_client.bootnode_trust.BootnodeTrustStore`) persist
trust state outside the synced bucket and must share one locking/atomicity
contract: a per-path thread lock, an advisory cross-process lock, owner-only
permissions (``0600`` file / ``0700`` dir) and a read-modify-fsync-replace
transaction. This module owns that contract; the two stores only differ in the
JSON schema they validate.

Consumers: ``hsync-client`` (pinning.py, bootnode_trust.py).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

__all__ = ["StoreError", "JsonStore", "TRUST_FILE_MODE"]

# Owner-only permissions: the file records which peers this host trusts and must
# not be world-readable/writable.
TRUST_FILE_MODE = 0o600
_DIR_MODE = 0o700

_path_locks_guard = threading.Lock()
_path_locks: dict[str, threading.RLock] = {}


class StoreError(RuntimeError):
    """The durable trust store could not be read or updated safely."""


def _thread_lock_for(path: Path) -> threading.RLock:
    key = str(path.absolute())
    with _path_locks_guard:
        return _path_locks.setdefault(key, threading.RLock())


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


class JsonStore:
    """Disk-backed JSON object store with one locked read/modify/fsync/replace transaction.

    A missing file is an empty store; corrupt/unreadable state fails closed
    (raises the subclass's ``error_cls``). Subclasses validate the loaded dict
    in an overridden :meth:`_load_unlocked` and set their own ``error_cls``.
    """

    error_cls: type[StoreError] = StoreError
    label = "trust store"

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = _thread_lock_for(self._path)

    @property
    def path(self) -> Path:
        return self._path

    def _load_unlocked(self) -> dict[str, Any]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise self.error_cls(f"could not read {self.label}: {exc}") from exc
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise self.error_cls(f"{self.label} is corrupt") from exc
        if not isinstance(data, dict):
            raise self.error_cls(f"{self.label} is corrupt")
        return data

    def _save_unlocked(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
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
            os.chmod(tmp, TRUST_FILE_MODE)
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
        """Hold the per-path thread lock and the cross-process advisory lock."""
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
                lock_path = self._path.with_name(f".{self._path.name}.lock")
                fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, TRUST_FILE_MODE)
                try:
                    os.chmod(lock_path, TRUST_FILE_MODE)
                    _lock_fd(fd)
                    try:
                        yield
                    finally:
                        _unlock_fd(fd)
                finally:
                    os.close(fd)
            except self.error_cls:
                raise
            except OSError as exc:
                raise self.error_cls(f"could not lock or persist {self.label}: {exc}") from exc
