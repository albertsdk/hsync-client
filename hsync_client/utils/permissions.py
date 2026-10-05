# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""File/directory permission helpers (remote-value sanitization applied)."""

from os import chmod
from pathlib import Path

from hsync_common.permissions import get_permissions as _read_permissions
from hsync_common.permissions import octal_to_mode, sanitize_remote_permissions
from loguru import logger as log

from hsync_client.constants import OWNER_DIRECTORY_ACCESS, ActionLabel


def get_permissions(obj_path: str) -> str:
    """
    Get file permissions as a canonical octal string.

    Args:
        obj_path: Path to the file or directory.

    Returns:
        Octal permission string like '644'.
    """
    return _read_permissions(obj_path)


def _apply_permissions(path: str, permissions: str, *, extra_mode_bits: int = 0) -> None:
    """Apply sanitized remote permissions to *path*, OR-ing *extra_mode_bits*."""
    permissions = sanitize_remote_permissions(permissions)

    # Skip if permissions already match
    if Path(path).exists() and permissions == get_permissions(path):
        return

    mode = octal_to_mode(permissions) | extra_mode_bits
    log.debug(f'{ActionLabel.PERMISSION} Set permission "{permissions}" to {path}')
    chmod(path, mode)


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
    sync engine and its own storage (mirrors the server's
    ``set_directory_permissions``).
    """
    _apply_permissions(dir_path, permissions, extra_mode_bits=OWNER_DIRECTORY_ACCESS)
