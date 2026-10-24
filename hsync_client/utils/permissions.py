# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""File/directory permission helpers (remote-value sanitization applied)."""

from hsync_common.permissions import apply_remote_permissions
from hsync_common.permissions import get_permissions as _read_permissions
from loguru import logger as log

from hsync_client.constants import ActionLabel


def get_permissions(obj_path: str) -> str:
    """
    Get file permissions as a canonical octal string.

    Args:
        obj_path: Path to the file or directory.

    Returns:
        Octal permission string like '644'.
    """
    return _read_permissions(obj_path)


def set_permissions(file_path: str, permissions: str) -> None:
    """
    Set file permissions from a canonical octal string.

    Converts the octal permission string (e.g., '644') to a numeric
    mode and applies it to the file.

    Every call site applies a value received from a remote peer (download
    metadata, inbound events, P2P uploads), so special bits
    (setuid/setgid/sticky) are always stripped: a synced file carrying
    ``4755`` must never become a setuid executable on this host.

    Args:
        file_path: Path to the file.
        permissions: Octal permission string (e.g., '644').
    """
    _apply_permissions(file_path, permissions)


def set_permissions_on_dir(dir_path: str, permissions: str) -> None:
    """Set directory permissions as received from a remote peer.

    Same sanitization contract as :func:`set_permissions` (special bits
    stripped — a synced directory must never become setuid/setgid/sticky),
    plus the owner rwx floor that keeps the directory traversable by the
    sync engine and its own storage.
    """
    _apply_permissions(dir_path, permissions, directory=True)


def _apply_permissions(path: str, permissions: str, *, directory: bool = False) -> None:
    """Apply remote permissions, logging only a real mode change."""
    applied = apply_remote_permissions(path, permissions, directory=directory, skip_if_equal=True)
    if applied is not None:
        log.debug(f'{ActionLabel.PERMISSION} Set permission "{applied}" to {path}')
