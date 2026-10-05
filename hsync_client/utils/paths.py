# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Path handling, per-path serialization and in-flight partial-file registry."""

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from os.path import abspath, normpath
from pathlib import Path
from typing import IO

from hsync_common.models.tombstone import relpath_in_bucket
from hsync_common.path_validation import (
    FORBIDDEN_FILENAME_CHARS,
    FORBIDDEN_PATH_CHARS,
    has_control_chars,
    has_forbidden_chars,
    has_problematic_trailing,
    is_reserved_name,
    validate_remote_relpath,
)

from hsync_client.config import CONFIG


def is_valid_logfile(path_string: str) -> bool:
    """
    Validate a log file path for correctness.

    Checks for:
    - Forbidden characters (cross-platform)
    - Control characters (ASCII 1-31)
    - Reserved names on Windows (including with extensions, e.g., NUL.txt)
    - Problematic trailing characters (space, period)
    - Absolute path requirement when directory is specified

    Args:
        path_string: Path to validate.

    Returns:
        True if the path is valid, False otherwise.
    """
    try:
        path = Path(path_string)

        # If there's no parent directory, assume it's just a filename
        if path.parent == Path("."):
            name = path.name

            # Check for forbidden characters in the filename
            if has_forbidden_chars(name, FORBIDDEN_FILENAME_CHARS):
                return False

            # Check for control characters
            if has_control_chars(name):
                return False

            # Check for Windows reserved names (including with extensions)
            if is_reserved_name(name):
                return False

            # Check for problematic trailing characters
            if has_problematic_trailing(name):
                return False

        else:
            # Check for absolute path if a directory is present
            if not path.is_absolute():
                return False

            # Check each part of the path for forbidden and control characters
            for part in path.parts:
                if has_forbidden_chars(part, FORBIDDEN_PATH_CHARS):
                    return False
                if has_control_chars(part):
                    return False

        return True
    except Exception:
        return False


def path_present(filepath: str) -> bool:
    """Return whether *filepath* exists as a file/dir/link (dangling links too).

    ``Path.exists()`` follows symlinks: a dangling link reports ``False`` even
    though the directory entry exists. Callers that act on the entry itself
    (inbound delete/rename dispatch, the delete-broadcast flush) must see both
    cases, so a symlink entry always counts as present.

    Consumers: ``hsync_client.event_dispatch`` (inbound delete/rename) and
    ``hsync_client.file_event_handler`` (stale-delete guard in the coalesced
    delete flush).
    """
    path = Path(filepath)
    return path.is_symlink() or path.exists()


def is_same_paths(path1: str, path2: str) -> bool:
    """
    Check if two paths refer to the same location.

    Normalizes and resolves paths to absolute form before comparison.

    Args:
        path1: First path to compare.
        path2: Second path to compare.

    Returns:
        True if paths are equivalent, False otherwise.
    """
    return normpath(abspath(path1)) == normpath(abspath(path2))


def safe_rmdir_if_empty(dir_path: str, bucket_root: str | None = None) -> bool:
    """
    Remove *dir_path* only if it is empty and strictly inside *bucket_root*.

    Acts as a safety wrapper around ``os.rmdir`` to prevent accidental
    removal of the LOCAL_BUCKET root (which would destroy the sync target).

    Args:
        dir_path: Directory to remove if empty.
        bucket_root: Path that must NOT be removed. Defaults to
            ``CONFIG["LOCAL_BUCKET"]`` when not provided.

    Returns:
        True if the directory was removed, False otherwise.
    """
    if not dir_path or not Path(dir_path).exists():
        return False
    if bucket_root is None:
        bucket_root = CONFIG.get("LOCAL_BUCKET", "")
    if bucket_root and is_same_paths(dir_path, bucket_root):
        return False
    try:
        if os.listdir(dir_path):
            return False
        os.rmdir(dir_path)
        return True
    except OSError:
        return False


def ensure_str_path(path: str | bytes | None) -> str:
    """
    Convert a path to string if it's bytes.

    Watchdog events can return paths as bytes or str depending on the platform.
    This function ensures we always work with strings.

    Args:
        path: Path as string, bytes, or None.

    Returns:
        Path as string (empty string if None).
    """
    if path is None:
        return ""
    if isinstance(path, bytes):
        return path.decode("utf-8")
    return str(path)


def normalize_bucket_relpath(raw_path: str, bucket_root: str) -> str | None:
    """Return the canonical bucket-relative path for a local path, or ``None``.

    Converts an absolute path under *bucket_root* to its relative form and then
    validates it with ``validate_remote_relpath``. Returns ``None`` for empty,
    out-of-bucket, or invalid paths (including lone-surrogate paths that cannot
    be UTF-8 encoded for the wire).
    """
    candidate = raw_path
    if Path(candidate).is_absolute():
        candidate = relpath_in_bucket(candidate, bucket_root)
        if candidate is None:
            return None
    try:
        return validate_remote_relpath(candidate)
    except UnicodeError:
        return None


# One re-usable lock per local path, created on demand and reference-counted so
# the registry never grows unbounded. Operations on the same path (parallel
# transfers, watcher-triggered uploads, inbound downloads/deletes) run in worker
# threads; serializing them here prevents interleaved writes that would silently
# lose an update on a single path.
_path_locks: dict[str, list] = {}
_path_locks_guard = threading.Lock()

# In-flight transfer artifacts (``<final>.part``, ``<final>.delta.part`` and
# the parked ``<final>.delta.part.lit`` payload), held only for the duration of
# the stream that writes them. ``sync._cleanup_partial_files`` sweeps the bucket
# for leftover artifacts at the start of every cycle to clear what a *previous*
# process left behind; without this registry that sweep also deletes artifacts
# a concurrently running transfer is still writing, because inbound downloads
# (P2P fan-out, remote event loop) run outside the sync cycle that owns the
# sweep. The writer keeps a valid descriptor either way, so the transfer would
# only fail at its final atomic rename — silently, as an "Error saving file"
# with no retry — leaving the path unsynced until the next cycle.
_inflight_partials: set[str] = set()
_inflight_partials_guard = threading.Lock()


def register_inflight_partial(path: str) -> None:
    """Mark *path* as an in-flight artifact until it is unregistered.

    Consumers: ``hsync_client.transfer`` (full/compressed/resumed download)
    and ``hsync_client.delta_transfer`` (delta rebuild and its parked literal
    payload), both read by ``sync._cleanup_partial_files``.
    """
    with _inflight_partials_guard:
        _inflight_partials.add(path)


def unregister_inflight_partial(path: str) -> None:
    """Release *path* from the in-flight registry (idempotent)."""
    with _inflight_partials_guard:
        _inflight_partials.discard(path)


def is_inflight_partial(path: str) -> bool:
    """Return whether *path* is currently being written by a transfer."""
    with _inflight_partials_guard:
        return path in _inflight_partials


def open_partial_no_follow(path: str, write_mode: str) -> IO[bytes]:
    """Open a ``.part`` file without following planted symlinks.

    Supports write modes (``wb``/``ab``) and read mode (``rb``) — the latter is
    used by download resume to ingest the already-downloaded prefix into the
    incremental hasher. Uses ``O_NOFOLLOW`` where the platform supports it so
    a symlink raced in between path validation and file creation cannot
    redirect the access outside the bucket. On platforms without
    ``O_NOFOLLOW`` (Windows) the flag is omitted and the confinement checks
    remain the defense.
    """
    if "r" in write_mode:
        flags = os.O_RDONLY
    else:
        flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if "a" in write_mode else os.O_TRUNC)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(path, flags, 0o666), write_mode)


def inbound_size_cap(declared_size: int | None, max_inbound_size: int) -> int | None:
    """Effective cap in bytes on the bytes written to disk for one inbound file.

    Single implementation of the anti decompression-bomb bound shared by
    downloads (`transfer.py`) and P2P-master received uploads (`p2p_server.py`).
    The peer-declared size alone is not a safe bound: a hostile peer can
    declare an arbitrary size and expand a bomb (or stream an undeclared-length
    body) up to it on disk before the SHA-256 check discards the content.

    Args:
        declared_size: The peer-declared file size (``None``/``0`` = unknown).
        max_inbound_size: The absolute ``MAX_INBOUND_SIZE`` config in bytes
            (``0`` = disabled): caps the declared value, and is the only bound
            when no size is declared.

    Returns ``None`` when uncapped (no declared size and the absolute cap
    disabled); ``0`` means "nothing may be written" (callers requiring a
    numeric cap map ``None`` to their own strict default).
    """
    declared = declared_size or 0
    if max_inbound_size > 0:
        return min(declared, max_inbound_size) if declared > 0 else max_inbound_size
    return declared if declared > 0 else None


@contextmanager
def path_lock(path: str) -> Iterator[None]:
    """Serialize concurrent filesystem operations on a single local *path*.

    Returns a context manager holding an exclusive, process-wide lock keyed on
    the normalized absolute path. Distinct paths never block one another; the
    per-path lock entry is dropped once no waiter holds it so the registry stays
    bounded.

    Args:
        path: Local filesystem path to serialize on.
    """
    key = normpath(abspath(path))
    with _path_locks_guard:
        entry = _path_locks.get(key)
        if entry is None:
            entry = [threading.Lock(), 0]
            _path_locks[key] = entry
        entry[1] += 1
        lock = entry[0]
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _path_locks_guard:
            entry = _path_locks.get(key)
            if entry is not None:
                entry[1] -= 1
                if entry[1] <= 0:
                    del _path_locks[key]
