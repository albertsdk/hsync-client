# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Portable replace/remove helpers for the sync engine.

NTFS differs from POSIX in two ways that matter to a sync engine:

* a file with the read-only attribute cannot be replaced or removed, and
* a file another process holds open (antivirus, indexer, an editor's temp
  handle) cannot be renamed or deleted (``WinError 32``), while the same
  operation on POSIX simply succeeds.

Both conditions are transient. These helpers clear the attribute and retry a
few times with a short backoff on Windows, and keep the POSIX path a single
syscall. Callers keep their own error reporting: a genuine permission error
still propagates after the bounded retries.
"""

from __future__ import annotations

import errno
import os
import stat
import time

from hsync_common.labels import ActionLabel
from loguru import logger as log

__all__ = [
    "clear_read_only",
    "create_symlink_or_skip",
    "remove_with_retry",
    "replace_with_retry",
]

_IS_WINDOWS = os.name == "nt"  # test seam
_ATTEMPTS = 5
_DELAY_S = 0.05  # 5 attempts ≈ 0.2 s of backoff, well inside a cycle

_RETRYABLE_ERRNOS = (errno.EACCES, errno.EPERM, errno.EBUSY)

# Errors meaning "this host/filesystem cannot create symlinks": Windows without
# Developer Mode/elevation (WinError 1314, EPERM/EACCES) and a filesystem
# without link support. On POSIX only ENOTSUP qualifies: EACCES/EPERM there are
# ordinary permission failures (unwritable parent, ACL, immutable flag) that
# must keep propagating, or a broken bucket directory would silently drop every
# symlink.
_SYMLINK_UNSUPPORTED_ERRNOS = (errno.EPERM, errno.EACCES, errno.ENOTSUP)
_SYMLINK_UNSUPPORTED_POSIX_ERRNOS = (errno.ENOTSUP,)
_SYMLINK_UNSUPPORTED_WINERRORS = (1314,)  # ERROR_PRIVILEGE_NOT_HELD
_SYMLINK_WARNED: set[str] = set()
_SYMLINK_WARNED_CAP = 4096


def clear_read_only(path: str) -> None:
    """Clear the Windows read-only attribute so *path* can be replaced/removed.

    No-op on POSIX (the mode never blocks replace/remove there) and on a missing
    path.
    """
    if not _IS_WINDOWS:
        return
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass  # missing path: the replace/remove reports the real error


def _retryable(exc: OSError) -> bool:
    """Whether *exc* is a transient Windows sharing/permission violation."""
    if getattr(exc, "winerror", None) == 32:  # ERROR_SHARING_VIOLATION
        return True
    return _IS_WINDOWS and exc.errno in _RETRYABLE_ERRNOS


def replace_with_retry(src: str, dst: str) -> None:
    """``os.replace`` with the Windows read-only/sharing handling.

    Retries a bounded number of times when the destination is read-only or
    momentarily open elsewhere; any other error (and the last attempt's error)
    propagates unchanged.
    """
    for attempt in range(_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            if attempt + 1 >= _ATTEMPTS or not _retryable(exc):
                raise
            clear_read_only(dst)
            time.sleep(_DELAY_S)


def _symlink_unsupported(exc: OSError) -> bool:
    """Whether *exc* means the host cannot create a symlink at all."""
    if getattr(exc, "winerror", None) in _SYMLINK_UNSUPPORTED_WINERRORS:
        return True
    if _IS_WINDOWS:
        return exc.errno in _SYMLINK_UNSUPPORTED_ERRNOS
    return exc.errno in _SYMLINK_UNSUPPORTED_POSIX_ERRNOS


def create_symlink_or_skip(target: str, link_path: str) -> bool:
    """Create a symlink, or log once and skip when the host cannot.

    Windows only creates symlinks with Developer Mode (or elevation); a synced
    symlink is then skipped with one warning per path (bounded set) instead of
    failing the cycle. Returns True when the link exists.
    """
    try:
        os.symlink(target, link_path)
        return True
    except OSError as exc:
        if not _symlink_unsupported(exc):
            raise
        if link_path not in _SYMLINK_WARNED:
            if len(_SYMLINK_WARNED) >= _SYMLINK_WARNED_CAP:
                _SYMLINK_WARNED.clear()
            _SYMLINK_WARNED.add(link_path)
            log.warning(
                f"{ActionLabel.SYMLINK} {ActionLabel.SKIP} Cannot create symlink "
                f'"{link_path}" ({exc}); enable Windows Developer Mode to sync symlinks'
            )
        return False


def remove_with_retry(path: str) -> None:
    """``os.remove`` with the Windows read-only/sharing handling.

    See :func:`replace_with_retry`: transient sharing/attribute errors are
    retried, everything else propagates.
    """
    for attempt in range(_ATTEMPTS):
        try:
            os.remove(path)
            return
        except OSError as exc:
            if attempt + 1 >= _ATTEMPTS or not _retryable(exc):
                raise
            clear_read_only(path)
            time.sleep(_DELAY_S)
