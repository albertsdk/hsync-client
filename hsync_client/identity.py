# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Persistent device identity for LAN mode (Syncthing-style).

Every hsync installation owns **one** device identity: a stable device ID
plus the TLS keypair used for LAN P2P mode. The identity lives in a single
state directory (``$HSYNC_DEVICE_DIR``, falling back to an OS state dir) so
it survives process restarts:

* the **device ID** is the identity anchor used in TOFU pinning keys
  (``(cid, bucket)``): it never changes unless the whole directory is
  deleted, so pins survive restarts and a *changed certificate for the same
  device ID* is detectable (``[FINGERPRINT CHANGED]``);
* the **keypair** is the cryptographic identity: it may be rotated (delete
  the ``.pem`` files) without changing the device ID — peers then see the
  same device presenting a new fingerprint and refuse it.

Consequences of the one-identity-per-installation model (same as Syncthing):

* multiple instances on one host syncing **different buckets** work out of
  the box (mDNS service names are ``{bucket}-{device_id}``);
* two instances on one host syncing the **same bucket** are the *same*
  device and cannot discover each other — run the second one with a
  separate ``$HSYNC_DEVICE_DIR`` to give it its own identity;
* copying the device directory to another machine clones the device.

The module also provides the per-directory **instance lock**: a cross-process
``flock`` that refuses to start two hsync processes on the same synced
directory (they would corrupt each other's ``.hsyncstate``/``.hsynctombstones``
side-cars and echo each other's watcher events).
"""

from __future__ import annotations

import atexit
import fcntl
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from hsync_common.models.event import is_valid_cid
from loguru import logger as log

from hsync_client.ssl_utils import CertificateInfo, load_or_create_master_cert

# Environment override for the device identity directory (tests and operators
# who keep state outside the default OS location).
DEVICE_DIR_ENV = "HSYNC_DEVICE_DIR"

# Environment override for the directory holding per-path instance locks.
# Defaults to ``$XDG_RUNTIME_DIR/hsync/locks`` (tmpfs, cleared at reboot —
# the canonical location for advisory locks) or ``<device dir>/locks``.
LOCKS_DIR_ENV = "HSYNC_LOCKS_DIR"

_DEVICE_ID_FILENAME = "device_id"
_LOCK_FILE_MODE = 0o600

__all__ = [
    "DEVICE_DIR_ENV",
    "LOCKS_DIR_ENV",
    "DeviceIdentity",
    "InstanceLock",
    "InstanceLockError",
    "default_device_dir",
    "default_locks_dir",
    "load_or_create_device_identity",
    "reset_device_identity_cache",
]


def default_device_dir() -> Path:
    """Resolve the device identity dir: ``$HSYNC_DEVICE_DIR`` or an OS state dir.

    This is the single source of truth for the identity directory:
    ``ssl_utils.default_master_cert_dir`` delegates here.
    """
    override = os.environ.get(DEVICE_DIR_ENV)
    if override:
        return Path(override)
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "hsync" / "device"


def default_locks_dir(device_dir: Path | None = None) -> Path:
    """Resolve the instance-lock directory.

    Prefers ``$HSYNC_LOCKS_DIR``, then ``$XDG_RUNTIME_DIR/hsync/locks``
    (per-user tmpfs: stale lock *files* vanish at reboot, and the kernel
    releases the flock itself at process death anyway). Without a runtime
    dir (e.g. macOS), locks live under the device dir.
    """
    override = os.environ.get(LOCKS_DIR_ENV)
    if override:
        return Path(override)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "hsync" / "locks"
    return (device_dir or default_device_dir()) / "locks"


@dataclass
class DeviceIdentity:
    """The persistent identity of this installation.

    Attributes:
        device_id: Stable 21-char alphanumeric ID (CID_PATTERN-compatible),
            used as the LAN ``cid`` and as the identity anchor in TOFU pins.
        cert_info: The persistent LAN keypair and its fingerprint.
    """

    device_id: str
    cert_info: CertificateInfo


_cached_identity: DeviceIdentity | None = None


def reset_device_identity_cache() -> None:
    """Drop the cached identity (test isolation helper)."""
    global _cached_identity
    _cached_identity = None


def load_or_create_device_identity() -> DeviceIdentity:
    """Load the persistent device identity, creating it on first use.

    Resolution order for the device ID:

    1. ``<device dir>/device_id`` containing a valid CID — reused;
    2. anything else (missing, unreadable, invalid) — a fresh ID is
       generated and persisted atomically.

    The keypair is loaded/created via
    :func:`hsync_client.ssl_utils.load_or_create_master_cert`, which honours
    the same directory overrides.

    The result is cached process-wide: every caller in one run shares the
    same identity, and identity files are written at most once.
    """
    global _cached_identity
    if _cached_identity is not None:
        return _cached_identity

    device_dir = default_device_dir()
    device_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(device_dir, 0o700)

    id_path = device_dir / _DEVICE_ID_FILENAME
    device_id: str | None = None
    try:
        candidate = id_path.read_text(encoding="utf-8").strip()
        if is_valid_cid(candidate):
            device_id = candidate
        else:
            log.warning(
                f"Ignoring invalid device ID in {id_path} (must match 21 alphanumeric "
                "chars); generating a fresh device identity"
            )
    except OSError:
        pass  # missing or unreadable: generate below

    if device_id is None:
        from hsync_client.utils import _atomic_write_text, get_cid

        device_id = get_cid()
        _atomic_write_text(id_path, device_id + "\n")
        log.info(f"Generated persistent device ID: {device_id}")
    else:
        log.info(f"Loaded persistent device ID: {device_id}")

    cert_info = load_or_create_master_cert()
    _cached_identity = DeviceIdentity(device_id=device_id, cert_info=cert_info)
    return _cached_identity


class InstanceLockError(RuntimeError):
    """Raised when another live hsync process already syncs a directory."""


class InstanceLock:
    """Cross-process advisory lock guarding one synced directory.

    Two hsync processes writing the same local directory corrupt each
    other's engine side-cars (``.hsyncstate``, ``.hsynctombstones``) and
    echo each other's watcher events. The lock is an ``flock`` on a file in
    the locks dir keyed by the directory's canonical path — the kernel
    releases it when the holder dies (even on SIGKILL), so it can never go
    stale. The holder's PID is written into the file for diagnostics only;
    liveness is established by the flock, never by the PID file content.

    Acquiring the same path twice in one process is a no-op returning the
    existing lock (in-process runs, e.g. CliRunner tests).
    """

    _held: dict[str, InstanceLock] = {}
    _atexit_registered = False

    def __init__(self, path: str) -> None:
        self._key = os.path.realpath(path)
        digest = hashlib.sha256(self._key.encode("utf-8")).hexdigest()[:32]
        self._lock_path = default_locks_dir() / f"{digest}.lock"
        self._fd: int | None = None

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    @classmethod
    def acquire(cls, path: str) -> InstanceLock:
        """Acquire the lock for *path* or raise :class:`InstanceLockError`.

        The error message names the synced directory and, when readable, the
        PID recorded in the lock file, so the user can find the conflicting
        process.
        """
        key = os.path.realpath(path)
        existing = cls._held.get(key)
        if existing is not None:
            return existing

        lock = cls(path)
        lock._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock._lock_path, os.O_RDWR | os.O_CREAT, _LOCK_FILE_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = ""
            try:
                with os.fdopen(os.dup(fd), "r", encoding="utf-8", errors="replace") as f:
                    holder = f.read().strip()
            except OSError:
                pass
            os.close(fd)
            holder_info = f" (pid {holder})" if holder else ""
            raise InstanceLockError(
                f"Another hsync process is already syncing '{key}'{holder_info}. "
                "Refusing to start: two processes on the same directory would "
                "corrupt each other's sync state."
            ) from None

        lock._fd = fd
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        cls._held[key] = lock
        if not cls._atexit_registered:
            atexit.register(cls.release_all)
            cls._atexit_registered = True
        log.debug(f"Instance lock acquired for '{key}' ({lock._lock_path})")
        return lock

    def release(self) -> None:
        """Release the lock. Idempotent."""
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None
        type(self)._held.pop(self._key, None)
        log.debug(f"Instance lock released for '{self._key}'")

    @classmethod
    def release_all(cls) -> None:
        """Release every held lock (atexit handler)."""
        for lock in list(cls._held.values()):
            lock.release()
