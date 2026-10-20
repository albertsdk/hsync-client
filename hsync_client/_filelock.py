# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Cross-platform advisory file locks for HSYNC's on-disk state.

Two consumers need the same primitive with different waiting semantics: the
per-directory :class:`hsync_client.identity.InstanceLock` must fail immediately
when another process owns the directory, while the JSON trust stores
(:class:`hsync_client._trust_store.JsonStore`) wait for the writer.

POSIX uses ``flock``. Windows has no ``flock``, so both backends lock the same
one-byte ``msvcrt.locking`` range at offset 0 — different holders contend on the
same byte, which is what makes the Windows lock behave like the POSIX one. The
module is imported (and ``fcntl``/``msvcrt`` are loaded) lazily, so importing
``hsync_client.identity`` works on every platform.

Consumers: ``hsync-client`` (identity.py, _trust_store.py).
"""

from __future__ import annotations

import os
from typing import Any

__all__ = ["lock_blocking", "try_lock", "unlock"]


def _windows() -> bool:
    """Whether this host needs the ``msvcrt`` backend (a seam for tests)."""
    return os.name == "nt"


def _msvcrt() -> dict[str, Any]:
    """Return the ``msvcrt`` namespace (imported lazily: Windows only)."""
    import msvcrt

    return vars(msvcrt)


def _seek_locked_byte(fd: int) -> None:
    """Position *fd* at offset 0 and make sure the locked byte exists.

    ``msvcrt.locking`` locks a byte *range*; a zero-length file has none, so a
    NUL is written first (callers hold the file open for writing).
    """
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"\0")
    os.lseek(fd, 0, os.SEEK_SET)


def lock_blocking(fd: int) -> None:
    """Wait until this process owns the lock on *fd*."""
    if _windows():  # pragma: no cover - exercised by Windows packaging
        api = _msvcrt()
        _seek_locked_byte(fd)
        api["locking"](fd, api["LK_LOCK"], 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX)


def try_lock(fd: int) -> bool:
    """Take the lock on *fd* without waiting; False when another holder has it."""
    if _windows():  # pragma: no cover - exercised by Windows packaging
        api = _msvcrt()
        _seek_locked_byte(fd)
        try:
            api["locking"](fd, api["LK_NBLCK"], 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def unlock(fd: int) -> None:
    """Release the lock held on *fd*; best-effort (a stale fd never raises)."""
    try:
        if _windows():  # pragma: no cover - exercised by Windows packaging
            api = _msvcrt()
            os.lseek(fd, 0, os.SEEK_SET)
            api["locking"](fd, api["LK_UNLCK"], 1)
            return
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
