# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Empty-directory status shared by the preview and the serving pass.

A directory with no syncable entry carries no content to compare, so it is
represented by an internal ``.hsynckeep`` sentinel that never becomes a user
row. The directory itself is user content: its verdict is derived from the
sentinel identity the peer's listing or published inventory holds, so a folder
row reads the same on every device.

Consumers: ``hsync_client.preview`` (the front-end's own comparison) and
``hsync_client.master_status`` (the serving-role pass).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from hsync_common.models.sentinel import is_sentinel_file

from hsync_client.sync import _bucket_key

__all__ = ["empty_dir_states", "sentinel_identities"]


def sentinel_identities(files: Iterable[dict], bucket_dir: str) -> dict[tuple[str, str], int]:
    """``{(bucket-relative sentinel path, sha256): size}`` for a listing.

    The peer-held side :func:`empty_dir_states` reads. Only sentinels are kept:
    they are the sole entries whose presence carries a directory's verdict.
    """
    identities: dict[tuple[str, str], int] = {}
    for entry in files:
        path = str(entry.get("filepath") or "")
        if not path or entry.get("type") == "symlink":
            continue
        key = _bucket_key(path, bucket_dir)
        if not key or not is_sentinel_file(key):
            continue
        identities[(key, str(entry.get("sha256sum") or ""))] = max(
            int(entry.get("size", 0) or 0), 0
        )
    return identities


def empty_dir_states(
    held: Mapping[tuple[str, str], int],
    local_files: Iterable[dict],
    bucket_dir: str,
) -> list[tuple[str, str]]:
    """``(state, bucket-relative dir)`` for every empty directory of the share.

    ``synced`` when the peers hold the sentinel identity, ``to_upload`` when
    nobody does, ``to_download`` for a sentinel only the peers hold. A directory
    is reported under its own path (no sentinel, no trailing slash); the bucket
    root has no row.
    """
    states: list[tuple[str, str]] = []
    local_dirs: set[str] = set()
    for entry in local_files:
        path = str(entry.get("filepath") or "")
        if not path or entry.get("type") == "symlink":
            continue
        key = _bucket_key(path, bucket_dir)
        if not key or not is_sentinel_file(key):
            continue
        directory = key.rsplit("/", 1)[0]
        if not directory:
            continue
        local_dirs.add(directory)
        identity = (key, str(entry.get("sha256sum") or ""))
        states.append(("synced" if identity in held else "to_upload", directory))
    for identity in held:
        key = identity[0]
        if not is_sentinel_file(key):
            continue
        directory = key.rsplit("/", 1)[0]
        if directory and directory not in local_dirs:
            states.append(("to_download", directory))
    return states
