# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Empty-directory status shared by the preview and the serving pass.

A directory with no syncable entry carries no content to compare, so it is
represented by an internal ``.hsynckeep`` sentinel that never becomes a user
row. The directory itself is user content: its verdict is derived from the
sentinel identity the peer's listing or published inventory holds, so a folder
row reads the same on every device.

A marker, however, only *suggests* emptiness: one is created whenever a directory
is momentarily empty (a copy materialises the directory before its files, a peer
creates it before downloading into it) and the marker it leaves behind is not
content. A directory is therefore reported only when it is empty on **both**
sides; a directory that holds anything else is described by its entries, and
reporting it here is what made a fully synchronised folder read "To sync" on
every ancestor. The bucket root has no row either.

Consumers: ``hsync_client.preview`` (the front-end's own comparison) and
``hsync_client.master_status`` (the serving-role pass).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator, Mapping

from hsync_common.models.sentinel import dir_has_syncable_entry, is_sentinel_file

from hsync_client.sync import _bucket_key

__all__ = ["empty_dir_states", "file_identities"]


def _dir_of(key: str) -> str | None:
    """Bucket-relative directory holding *key*; ``None`` for a root entry."""
    head, sep, _tail = key.rpartition("/")
    return head if sep else None


def _entries(files: Iterable[dict], bucket_dir: str) -> Iterator[tuple[str, dict]]:
    """Yield ``(bucket-relative key, entry)`` for every comparable entry."""
    for entry in files:
        path = str(entry.get("filepath") or "")
        if not path or entry.get("type") == "symlink":
            continue
        key = _bucket_key(path, bucket_dir)
        if not key:
            continue
        yield key, entry


def file_identities(files: Iterable[dict], bucket_dir: str) -> dict[tuple[str, str], int]:
    """``{(bucket-relative path, sha256): size}`` for a whole listing.

    Every entry is kept — the sentinels included — because the peer side
    :func:`empty_dir_states` reads has to expose both the marker identity and
    the content that proves a directory is not empty.
    """
    identities: dict[tuple[str, str], int] = {}
    for key, entry in _entries(files, bucket_dir):
        identities[(key, str(entry.get("sha256sum") or ""))] = max(
            int(entry.get("size", 0) or 0), 0
        )
    return identities


def _dirs_with_content(files: Iterable[dict], bucket_dir: str) -> set[str]:
    """Bucket-relative directories holding at least one non-sentinel entry.

    Mirrors the emptiness rule of
    :func:`hsync_common.models.sentinel.ensure_sentinel`: a non-ignored child is
    content, whatever marker may also sit in the directory.
    """
    dirs: set[str] = set()
    for key, _entry in _entries(files, bucket_dir):
        if is_sentinel_file(key):
            continue
        directory = _dir_of(key)
        if directory:
            dirs.add(directory)
    return dirs


def _dirs_with_content_in(held: Mapping[tuple[str, str], int]) -> set[str]:
    """Directories the peer's identities prove to hold content."""
    dirs: set[str] = set()
    for key, _sha in held:
        if is_sentinel_file(key):
            continue
        directory = _dir_of(key)
        if directory:
            dirs.add(directory)
    return dirs


def _dir_is_populated(bucket_dir: str, directory: str, listed: set[str]) -> bool:
    """Whether *directory* holds content, from the listings or from the disk.

    ``listed`` holds the directories a listing proves to have content; it cannot
    carry a directory whose only entries are subdirectories (the listings publish
    files), while the marker's own rule counts a subdirectory as content. The
    on-disk check closes that gap for the local tree, which is the side this node
    can inspect; a path that is not local (a peer-only directory) or unreadable
    falls back to the listing.
    """
    if directory in listed:
        return True
    if not bucket_dir:
        return False
    return dir_has_syncable_entry(os.path.join(bucket_dir, directory)) is True


def empty_dir_states(
    held: Mapping[tuple[str, str], int],
    local_files: Iterable[dict],
    bucket_dir: str,
) -> list[tuple[str, str]]:
    """``(state, bucket-relative dir)`` for every empty directory of the share.

    ``held`` carries the peers' identities (``P2PInventoryStore.compare_share``
    in the serving pass, :func:`file_identities` of the remote listing in the
    preview): both the markers and the content, so a directory that is empty on
    either side is skipped. ``synced`` when the peers hold the sentinel identity,
    ``to_upload`` when nobody does, ``to_download`` for a sentinel only the peers
    hold. A directory is reported under its own path (no sentinel, no trailing
    slash); the bucket root has no row.
    """
    local_files = list(local_files)
    content = _dirs_with_content(local_files, bucket_dir) | _dirs_with_content_in(held)
    states: list[tuple[str, str]] = []
    local_dirs: set[str] = set()
    for key, entry in _entries(local_files, bucket_dir):
        if not is_sentinel_file(key):
            continue
        directory = _dir_of(key)
        if not directory or _dir_is_populated(bucket_dir, directory, content):
            continue
        local_dirs.add(directory)
        identity = (key, str(entry.get("sha256sum") or ""))
        states.append(("synced" if identity in held else "to_upload", directory))
    for key, _sha in held:
        if not is_sentinel_file(key):
            continue
        directory = _dir_of(key)
        if not directory or directory in local_dirs:
            continue
        if _dir_is_populated(bucket_dir, directory, content):
            continue
        states.append(("to_download", directory))
    return states
