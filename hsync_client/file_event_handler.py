# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Base watchdog handler shared by Remote sync and LAN master.

``RemoteFileEventHandler`` (push to central server over HTTP/WebSocket)
and ``LANMasterFileEventHandler`` (broadcast metadata over LAN WebSocket)
both observe the local bucket via the same watchdog events. The bulk of
that logic — hidden-path filtering, directory rename normalisation, file
debouncing on platforms without ``on_closed`` — is identical. This module
factors it out into ``BaseFileEventHandler`` and exposes two extension
points so each transport plugs in its own behaviour:

* ``_broadcast_event(event, src_abs_path, dst_abs_path)`` sends event
  metadata to peers/server.
* ``_on_file_ready(file, src_path)`` is invoked once a file's bytes are
  ready to propagate; Remote uploads via HTTP, LAN master broadcasts a
  ``modified`` event so slaves can pull.
"""

from __future__ import annotations

import os
import sys
import threading
from copy import deepcopy
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

from hsync_common.labels import ActionLabel, resolve_label
from hsync_common.models.dir import Dir, is_hidden_dir
from hsync_common.models.event import Event, EventName
from hsync_common.models.file import File, invalidate_sha256_cache, is_hidden_file
from hsync_common.models.symlink import Symlink
from hsync_common.path_validation import is_safe_symlink_target
from loguru import logger as log
from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    FileDeletedEvent,
    FileSystemEventHandler,
)

from hsync_client.config import CONFIG
from hsync_client.constants import HSYNCKEEP_FILENAME
from hsync_client.utils import cid_suffix, ensure_str_path, is_same_paths, path_present

if TYPE_CHECKING:
    from hsync_client.watcher import WatcherManager

# on_closed is delivered by watchdog only on Linux (inotify).
SUPPORTS_ON_CLOSED = sys.platform == "linux"

# Debounce interval for on_modified fallback on platforms without on_closed.
MODIFIED_DEBOUNCE_INTERVAL = 1.0

# Coalescing window for `deleted` events. inotify emits one delete event per
# child BEFORE the directory event (bottom-up), so a `rm -rf` on a tree with
# thousands of entries produced thousands of WS messages. Deletes are buffered
# for this window and then flushed as a single recursive `deleted` event for
# the topmost deleted directory(s); descendants are folded away.
DELETE_COALESCE_WINDOW = 0.2


def split_stale_deletes(
    pending: list[tuple[str, bool]],
) -> tuple[list[tuple[str, bool]], list[tuple[str, bool]]]:
    """Split *pending* deletes into still-valid entries and stale ones.

    Deletes are buffered for ``DELETE_COALESCE_WINDOW`` before being
    propagated, so a path deleted and re-created inside that window still has
    its delete queued when the fresh file already exists on disk. Propagating
    it would wipe the re-created file on every peer — and, through the peer's
    delete echo, on the source itself — so the stale entry is dropped: the
    re-creation already propagated on its own event.

    A path counts as present when it exists as a file/dir or as a (possibly
    dangling) symlink entry: see ``hsync_client.utils.path_present``.

    Returns ``(valid, stale)``; callers that recorded a causal tombstone when
    buffering the delete must clear it for the *stale* entries, otherwise the
    marker outlives the deletion its premise refers to.

    Consumers: ``BaseFileEventHandler._flush_pending_deletes`` (master/remote
    broadcast) and ``LANSlaveFileEventHandler._flush_pending_deletes`` (slave
    delete requests to the master).
    """
    valid: list[tuple[str, bool]] = []
    stale: list[tuple[str, bool]] = []
    for entry in pending:
        src_path = entry[0]
        if path_present(src_path):
            log.debug(f'Stale delete dropped, path exists again: "{src_path}"')
            stale.append(entry)
        else:
            valid.append(entry)
    return valid, stale


# Minimum quiet window for directory MODIFY events. inotify fires IN_ATTRIB on
# a directory once per child create/write/rename/remove, so a single recursive
# operation (rmtree of an inbound delete, a deep download, a mkdir burst) emits
# a burst of MODIFY events for the same directory within milliseconds, each
# with a distinct mtime. The stat-only dedup cannot collapse them (mtime changes
# every time); within this window a second MODIFY whose mode is unchanged is
# folded. A mode change (chmod) is never folded.
DIR_MODIFY_WINDOW = 0.1

# Maximum entries kept in the broadcast dedup dicts (_last_broadcast_stat /
# _last_broadcast_hash). Mirrors SHA256_CACHE_MAX_SIZE so memory stays bounded
# (~2 MB at ~200 bytes/entry) even in long-running watch sessions.
DEDUP_MAX_ENTRIES: int = 10_000


class BaseFileEventHandler(FileSystemEventHandler):
    """Common watchdog handler shared by Remote sync and LAN master."""

    # Cache of resolved ``(prefix, name) -> label`` pairs. ``resolve_label``
    # is deterministic and the key space is tiny (both prefixes × a handful
    # of event names), so a per-instance cache saves the enum lookup and
    # bracket formatting on every watchdog event (the hottest path).
    _label_cache: dict[tuple[str, str], str] = {}

    def _label(
        self,
        name: str,
        src_path: str | None = None,
        dst_path: str | None = None,
    ) -> str:
        """Return the origin-aware bracketed label for *name*.

        The prefix encodes where the event originated: ``"REMOTE "`` when
        :meth:`_origin_cid` attributes it to a peer (the master's
        inbound-origin map), ``"LOCAL "`` otherwise (this node's own
        watcher). Delegates to :func:`hsync_common.labels.resolve_label` so
        the label is resolved through :class:`ActionLabel` and colorized
        consistently with the inbound event dispatcher (no duplicated
        tag-formatting logic).
        """
        prefix = "REMOTE " if self._origin_cid(src_path, dst_path) else "LOCAL "
        key = (prefix, name)
        cached = self._label_cache.get(key)
        if cached is None:
            cached = resolve_label(prefix, name)
            self._label_cache[key] = cached
        return cached

    def _origin_cid(self, src_path: str | None = None, dst_path: str | None = None) -> str | None:
        """Return the originator CID for a filesystem event, if known.

        The base implementation returns ``None`` (no origin attribution).
        The LAN master overrides this to peek the inbound-origin map so the
        log line can attribute the event to the peer that triggered it via
        the HTTP API. The CID is *not* consumed here: the subsequent
        ``_broadcast_event`` still needs it for echo suppression.
        """
        return None

    def _cid_suffix(self, src_path: str | None = None, dst_path: str | None = None) -> str:
        """Return a ``(CID: xxx)`` suffix for log lines, or ``""`` when unknown."""
        return cid_suffix(self._origin_cid(src_path, dst_path))

    def _make_event(self, name: EventName, src, dst=None) -> Event:
        """Build an ``Event`` with the three CONFIG identity fields."""
        return Event(
            CONFIG["CID"],
            CONFIG["LOCAL_BUCKET"],
            CONFIG["REMOTE_BUCKET"],
            name,
            src,
            dst,
        )

    def _invalidate_dedup(self, path: str) -> None:
        """Drop the dedup caches for *path* so a re-created entry is re-evaluated."""
        self._last_broadcast_stat.pop(path, None)
        self._last_broadcast_hash.pop(path, None)
        self._last_broadcast_ts.pop(path, None)
        self._last_mode.pop(path, None)

    def _uses_polling_backend(self) -> bool:
        """Return whether the active watcher backend is the polling fallback.

        The polling observer emits no close-write event, so file content must
        be propagated from ``on_created``/``on_modified`` there — the
        ``SUPPORTS_ON_CLOSED`` shortcut assumes the inotify backend. Without a
        watcher reference (unit-test construction) the inotify behavior is
        assumed.
        """
        watcher = self._watcher
        return watcher is not None and getattr(watcher, "backend", "inotify") == "polling"

    def __init__(self, watcher: WatcherManager | None = None) -> None:
        # Delete coalescing: buffered (path, is_directory) pairs pending a flush.
        # Guarded by _delete_lock; _delete_timer is the pending threading.Timer.
        self._pending_deletes: list[tuple[str, bool]] = []
        # O(1) duplicate detection set (same tuples as _pending_deletes).
        self._pending_delete_set: set[tuple[str, bool]] = set()
        # O(1) ancestor lookup for _is_covered_by_pending_delete:
        # normalized directory paths (rstrip("/")) currently buffered.
        self._pending_delete_dir_paths: set[str] = set()
        self._delete_timer: threading.Timer | None = None
        self._delete_lock = threading.Lock()

        # Two-stage content dedup for file-ready events:
        # 1. stat pre-check (mtime, size, mode): cheap filter that avoids
        #    re-hashing on no-op closes where the editor opened/closed without
        #    saving. st_mode is included (masked to the permission bits) so a
        #    permission-only change (chmod) is never mistaken for a no-op.
        # 2. hash compare: precise filter that skips broadcasts when the file
        #    content matches what was last propagated (catches mtime-touch
        #    without content change, and the spurious close that precedes an
        #    editor safe-save rename). A hash match with a mode difference
        #    still propagates as a permission-only event.
        self._last_broadcast_stat: dict[str, tuple[int, int, int]] = {}
        self._last_broadcast_hash: dict[str, str] = {}
        # Last-broadcast monotonic timestamp per path, for the directory MODIFY
        # window. Set and evicted in lockstep with _last_broadcast_stat/hash.
        self._last_broadcast_ts: dict[str, float] = {}
        # Mode-only dedup for the IN_ATTRIB (chmod) path. Deliberately SEPARATE
        # from _last_broadcast_stat: the latter is the content-upload dedup key
        # and must only be written by _record_broadcast (after a real
        # propagation). Recording the first-observed stat there poisoned stage 1
        # of _process_file_ready: a fast single-syscall write (e.g.
        # Path.write_bytes of a small file) reaches its final size before the
        # handler runs, so on_modified and on_closed observe the SAME stat; the
        # poisoned entry made on_closed skip the file as "unchanged" and the
        # upload was silently lost.
        self._last_mode: dict[str, int] = {}
        # Optional watcher manager reference: when set, newly created directories
        # are watched recursively through it (with graceful degradation on a
        # kernel limit) instead of waiting for the next full sync cycle.
        self._watcher: WatcherManager | None = watcher

    # ----- subclass hooks -----

    def _broadcast_event(
        self,
        event: Event,
        src_abs_path: str | None = None,
        dst_abs_path: str | None = None,
    ) -> None:
        """Send event metadata to peers/server."""
        raise NotImplementedError

    def _on_file_ready(
        self,
        file: File,
        src_path: str,
        from_closed: bool = False,
        *,
        trigger: str | None = None,
    ) -> None:
        """Called after a file's bytes are ready to propagate.

        ``from_closed`` is True when the originating event was ``on_closed``
        (``IN_CLOSE_WRITE`` on Linux), letting upload paths trust the close and
        shorten the stability re-check. ``trigger`` overrides the log trigger
        label (e.g. ``"safe-save"`` for an atomic editor temp→final rename).
        """
        raise NotImplementedError

    def _on_permissions_changed(self, file: File, src_path: str) -> None:
        """Called when only the permissions changed (content hash unchanged).

        A ``chmod`` changes ``st_mode`` without touching ``mtime``/``size`` or
        the content hash, so the regular content dedup would discard it. This
        hook lets each transport propagate the permission-only change as a
        normal ``modified`` event: the Remote handler uploads the file (the
        server applies the new permissions on its same-SHA branch) and the LAN
        master broadcasts the updated ``File`` so slaves converge.
        """
        raise NotImplementedError

    def _on_metadata_changed(self, file: File, src_path: str) -> None:
        """Called when only the mtime changed (a plain ``touch``).

        Content and mode are identical, so the regular content dedup would
        discard it. This hook lets each transport propagate the new mtime: the
        Remote handler pushes a metadata-only upload (the server applies the
        mtime on its same-SHA branch) and the LAN master broadcasts the updated
        ``File`` so slaves converge on the mtime.
        """
        raise NotImplementedError

    def _on_deleted(self, src_path: str, is_directory: bool) -> None:
        """Called after a non-hidden deletion has been observed and broadcast.

        Default no-op. The LAN master overrides this to record a deletion
        marker (tombstone); the Remote handler leaves it unimplemented because
        the central server records tombstones itself.
        """
        return None

    def _is_covered_by_pending_delete(self, path: str) -> bool:
        """Return True when *path* equals or descends from a buffered dir delete,
        OR when a descendant of *path* is buffered for deletion.

        inotify fires ``IN_ATTRIB`` on a directory whenever a child entry is
        removed, so an ``rmtree`` of a deep tree produces one spurious
        directory MODIFY per removed child before the tree's own ``deleted``
        events are coalesced. Those modifies are noise, not metadata changes:
        a path equal to, or under, a directory already buffered for deletion
        is skipped by ``on_modified``.

        The ancestor-only check (original) works for self-originated deletes
        when the observer fires modify events *after* the directory itself has
        been buffered.  During an inbound delete (applied via ``rmtree``),
        inotify delivers ``IN_ATTRIB`` (modify) *before* ``IN_DELETE_SELF``
        (delete), so the path is not yet buffered.  The descendant check
        covers this by recognising that a directory whose *children* are
        pending deletion is itself being emptied by the same recursive delete:
        any ``IN_ATTRIB`` on it is noise, not a metadata change.

        Instead of scanning every pending delete (O(n) per MODIFY event,
        O(n+m) in the worst case), the ancestor path walk is O(depth) ≈ O(1)
        and the descendant prefix check iterates the small set of buffered
        directory paths (m = number of distinct directories in the coalescing
        window, bounded by DELETE_COALESCE_WINDOW)."""
        norm = path.rstrip("/")
        with self._delete_lock:
            dir_paths = self._pending_delete_dir_paths
        # Walk ancestors bottom-up: /a/b/c/d → /a/b/c → /a/b → /a
        while True:
            if norm in dir_paths:
                return True
            idx = norm.rfind("/")
            if idx <= 0:
                break
            norm = norm[:idx]
        # Descendant check: if any buffered directory delete starts with this
        # path + "/", then this path is an ancestor of a pending delete and
        # its IN_ATTRIB events are noise from the ongoing recursive removal.
        prefix = path.rstrip("/") + "/"
        return any(d.startswith(prefix) for d in dir_paths)

    @staticmethod
    def _stat_key(st: os.stat_result) -> tuple[int, int, int]:
        """Return the dedup key for a stat result: (mtime_ns, size, mode).

        The permission bits are masked with ``0o7777`` so the key stable
        across unrelated stat fields (device/inode/uid/gid) while still
        detecting ``chmod`` changes, which alter neither mtime nor size.
        """
        return (st.st_mtime_ns, st.st_size, st.st_mode & 0o7777)

    def _process_permission_only(self, src_path: str) -> None:
        """Handle a file event that may be a pure ``chmod`` on Linux.

        On inotify platforms content changes propagate via ``on_closed``
        (``IN_CLOSE_WRITE``); a ``chmod`` only fires ``IN_ATTRIB``, which
        watchdog delivers as a file ``modified`` event with no close. This
        path inspects the mode only:
        - first observation of the path: just records the mode;
        - mode unchanged: no-op;
        - mode changed: re-reads the file and delegates to the
          ``_on_permissions_changed`` transport hook.
        Content is NEVER uploaded from here (a partial write would be unsafe).

        The mode baseline lives in the dedicated ``_last_mode`` map — never in
        ``_last_broadcast_stat`` (see the init comment for why).
        """
        if CONFIG.get("SYNC_DIRECTION") == "download-only":
            return
        if is_hidden_file(src_path) or Path(src_path).is_symlink():
            return
        try:
            st = Path(src_path).stat()
        except OSError:
            return
        mode = st.st_mode & 0o7777
        prev = self._last_mode.get(src_path)
        self._last_mode[src_path] = mode
        if prev is None or prev == mode:
            # First observation (e.g. the watch just started, or the content
            # write that preceded this event): record the mode so a later
            # chmod is detected; content goes via on_closed. Mode unchanged:
            # no-op.
            return
        invalidate_sha256_cache(src_path)
        file_obj = File.get(src_path)
        if file_obj is None:
            return
        log.info(f'[PERMISSION] mode change: "{src_path}"{self._cid_suffix(src_path)}')
        self._on_permissions_changed(file_obj, src_path)

    def _record_broadcast(self, src_path: str, stat: tuple[int, int, int], sha256: str) -> None:
        """Record a broadcast for content dedup, evicting oldest if at capacity.

        Both ``_last_broadcast_stat`` and ``_last_broadcast_hash`` are updated
        atomically. When the cap (``DEDUP_MAX_ENTRIES``) is reached, the
        oldest-inserted entry is evicted from both dicts (FIFO; Python 3.7+
        ``dict`` preserves insertion order). Evicting a path means its next
        close will re-broadcast — correct, not data loss.
        """
        if (
            src_path not in self._last_broadcast_stat
            and len(self._last_broadcast_stat) >= DEDUP_MAX_ENTRIES
        ):
            oldest = next(iter(self._last_broadcast_stat))
            self._invalidate_dedup(oldest)
        self._last_broadcast_stat[src_path] = stat
        self._last_broadcast_hash[src_path] = sha256
        self._last_broadcast_ts[src_path] = monotonic()
        # Keep the mode baseline in step with the last PROPAGATED content, so a
        # later chmod is detected relative to the uploaded mode and not to a
        # stale pre-upload observation. stat is the 3-tuple produced by
        # _stat_key; tolerate a 2-tuple (legacy callers/tests) by skipping the
        # mode baseline update (the _last_mode map is only read by the chmod
        # path, which always goes through _stat_key).
        if len(stat) >= 3:
            self._last_mode[src_path] = stat[2]

    # ----- watchdog event handlers -----

    def _scan_existing_files(self, dir_path: str) -> None:
        """Scan *dir_path* for already-present visible files and process them.

        Called right after an ``observer.schedule()`` so that files created
        between ``mkdir()`` and the moment the inotify watch becomes active
        are picked up immediately.  Subdirectories are NOT recursed into —
        each of them will receive its own ``DirCreatedEvent`` and trigger
        another scan.
        """
        try:
            for name in os.listdir(dir_path):
                full = str(Path(dir_path) / name)
                if is_hidden_file(full) or Path(full).is_dir():
                    continue
                self._process_file_ready(full)
        except OSError:
            pass

    def on_any_event(self, event) -> None:
        """Debug-log skipped hidden paths (both ``src_path`` and ``dest_path``)."""
        hidden_prefix = "dir" if event.is_directory else "file"
        src_path = ensure_str_path(event.src_path)
        dest_path = ensure_str_path(getattr(event, "dest_path", None))
        paths = {"src": src_path, "dest": dest_path}

        for kind, path in paths.items():
            if path and (
                (event.is_directory and is_hidden_dir(path))
                or (not event.is_directory and is_hidden_file(path))
            ):
                log.debug(f"Skip hidden {kind} {hidden_prefix}: {path}")

    def _broadcast_symlink(self, src_path: str) -> None:
        """Broadcast a ``symlink`` event, with escaping-target guard."""
        link = Symlink.get(src_path)
        if link is not None and not is_safe_symlink_target(
            src_path, link.target, CONFIG["LOCAL_BUCKET"]
        ):
            log.warning(
                f"{self._label('SECURITY', src_path)} Symlink target escapes bucket, "
                f'not broadcasting: "{src_path}" -> "{link.target}"'
            )
            return
        log.info(f'{self._label("SYMLINK", src_path)} "{src_path}"{self._cid_suffix(src_path)}')
        if link is not None:
            self._broadcast_event(
                self._make_event(EventName.symlink, link),
                src_abs_path=src_path,
            )

    def on_created(self, event) -> None:
        src_path = ensure_str_path(event.src_path)

        # Symlink check BEFORE directory check: a dir-symlink is a symlink,
        # not a directory (invariant: never dereference).
        if not is_hidden_file(src_path) and Path(src_path).is_symlink():
            self._broadcast_symlink(src_path)
            return

        if event.is_directory and not is_hidden_dir(src_path):
            # Dedup directory create events the same way directory modifies
            # are: watchdog emits a synthetic DirCreatedEvent for children
            # already present whenever a watch is scheduled on a fresh
            # directory (the observer.schedule below), so a chain of mkdirs
            # can fire two DirCreatedEvent for the same directory within
            # milliseconds. Directories carry no content hash, so a stat
            # pre-check (mtime_ns + size + mode) against the last broadcast is
            # sufficient and cheap (one stat()). The stat is recorded
            # *before* logging/broadcasting: recording up front lets the
            # pre-check catch the sibling immediately (same pattern as the
            # directory-modify dedup); a later delete invalidates the entry
            # (_flush_pending_deletes), so a re-created directory with the
            # same stat is never skipped.
            try:
                file_stat = Path(src_path).stat()
            except OSError:
                log.debug(f'Skip dir create, directory gone: "{src_path}"')
                return
            current_stat = self._stat_key(file_stat)
            if current_stat == self._last_broadcast_stat.get(src_path):
                log.debug(f'Skip duplicate create (stat): "{src_path}" (dir)')
                return
            self._record_broadcast(src_path, current_stat, "")
            log.info(f'{self._label("CREATE", src_path)} "{src_path}"{self._cid_suffix(src_path)}')
            dir_obj = Dir.get(src_path)
            if dir_obj:
                self._broadcast_event(
                    self._make_event(EventName.added, dir_obj),
                    src_abs_path=src_path,
                )
            # Start watching the new directory recursively so files created
            # inside it are picked up immediately rather than waiting for the
            # next periodic resync to discover them. The watcher manager skips
            # the watch and degrades gracefully if a kernel limit is reached.
            if self._watcher is not None:
                self._watcher.schedule_directory(self, src_path)
                # Scan for files already present inside the directory.  The
                # watch registered above only captures events that happen
                # *after* the watch is active; files created between
                # mkdir() and schedule() are invisible.  Iterating
                # the directory contents once guarantees they are picked up.
                self._scan_existing_files(src_path)

        # Polling fallback only: a regular file appearing in the snapshot diff
        # (fresh creation, or a move-in from outside the tree) has no
        # close-write event to rely on, so it must be propagated here. A file
        # still being written is re-propagated by the next poll's
        # FileModifiedEvent; the two-stage dedup collapses no-op repeats.
        elif self._uses_polling_backend() and not is_hidden_file(src_path):
            self._process_file_ready(src_path)

    def on_modified(self, event) -> None:
        src_path = ensure_str_path(event.src_path)

        if event.is_directory:
            if not is_hidden_dir(src_path) and not is_same_paths(CONFIG["LOCAL_BUCKET"], src_path):
                # Suppress the IN_ATTRIB burst of a recursive delete: inotify
                # fires IN_ATTRIB on a directory whenever a child entry is
                # removed, so an rmtree of a deep tree emits one spurious
                # directory MODIFY per removed child before the tree's deleted
                # events are coalesced. A path already covered by a pending
                # delete — or no longer present on disk — is noise, not a
                # metadata change, and must not be logged or broadcast.
                if self._is_covered_by_pending_delete(src_path):
                    log.debug(f'Skip dir modify, pending delete: "{src_path}"')
                    return
                # Dedup directory modify events the same way as files: inotify
                # fires IN_ATTRIB on a directory whenever a child is created,
                # written, renamed or removed, so a single file save can emit
                # several identical directory MODIFY lines. Directories carry no
                # content hash, so a stat pre-check (mtime_ns + size + mode)
                # against the last broadcast is sufficient and cheap (one
                # stat()); the mode half also detects a directory chmod.
                # The (dir) annotation disambiguates these directory-level
                # metadata broadcasts from file content changes.
                try:
                    file_stat = Path(src_path).stat()
                except OSError:
                    file_stat = None
                if file_stat is None:
                    log.debug(f'Skip dir modify, directory gone: "{src_path}"')
                    return
                current_stat = self._stat_key(file_stat)
                prev_stat = self._last_broadcast_stat.get(src_path)
                if current_stat == prev_stat:
                    log.debug(f'Skip unchanged (stat): "{src_path}" (dir)')
                    return
                # Time-window dedup for directory metadata churn: a recursive
                # operation (rmtree of an inbound delete, a deep download, a
                # mkdir burst) fires IN_ATTRIB once per child change, each with
                # a DISTINCT mtime the stat check above cannot collapse. A
                # follow-up MODIFY within DIR_MODIFY_WINDOW whose mode is
                # unchanged is folded; a mode change (chmod) never folds and
                # falls through to broadcast.
                last_ts = self._last_broadcast_ts.get(src_path)
                last_mode = prev_stat[2] if prev_stat is not None else 0
                if (
                    last_ts is not None
                    and monotonic() - last_ts < DIR_MODIFY_WINDOW
                    and current_stat[2] == last_mode
                ):
                    log.debug(f'Skip dir modify (window): "{src_path}"')
                    return
                # A chmod changes only st_mode: mtime+size unchanged with a mode
                # change is a pure permission change (not a child add/remove), so
                # it is labelled [PERMISSION] like the file-chmod path instead
                # of [LOCAL/REMOTE MODIFY].
                permission_only = (
                    prev_stat is not None
                    and current_stat[2] != prev_stat[2]
                    and current_stat[0] == prev_stat[0]
                    and current_stat[1] == prev_stat[1]
                )
                # Record the stat *before* logging/broadcasting: mkdir emits a
                # burst of IN_ATTRIB events ~1ms apart, and the second one can
                # be dispatched before Dir.get + broadcast of the first has
                # completed. Recording up front lets the dedup pre-check catch
                # that sibling immediately; a later genuine content change
                # updates the directory mtime and is never skipped.
                self._record_broadcast(src_path, current_stat, "")
                if permission_only:
                    log.info(
                        f'{ActionLabel.PERMISSION} mode change: "{src_path}" (dir)'
                        f"{self._cid_suffix(src_path)}"
                    )
                else:
                    log.info(
                        f'{self._label("MODIFY", src_path)} "{src_path}" (dir)'
                        f"{self._cid_suffix(src_path)}"
                    )
                dir_obj = Dir.get(src_path)
                if dir_obj:
                    self._broadcast_event(
                        self._make_event(EventName.modified, dir_obj),
                        src_abs_path=src_path,
                    )
            return

        if SUPPORTS_ON_CLOSED and not self._uses_polling_backend():
            # Content changes propagate via on_closed on the inotify backend;
            # a chmod (IN_ATTRIB) never fires a close, so inspect mode-only
            # changes here without touching file bytes. On the polling
            # fallback there is no close-write event at all: fall through to
            # _process_file_ready so content changes are still propagated
            # (the two-stage dedup collapses the repeated mid-write diffs).
            if not is_hidden_file(src_path):
                self._process_permission_only(src_path)
            return
        if is_hidden_file(src_path):
            return
        if CONFIG.get("SYNC_DIRECTION") == "download-only":
            return

        self._process_file_ready(src_path)

    def on_closed(self, event) -> None:
        src_path = ensure_str_path(event.src_path)
        if event.is_directory or is_hidden_file(src_path):
            return
        if CONFIG.get("SYNC_DIRECTION") == "download-only":
            return
        self._process_file_ready(src_path, from_closed=True)

    def _process_file_ready(
        self,
        src_path: str,
        from_closed: bool = False,
        *,
        trigger: str | None = None,
    ) -> None:
        """Build ``File`` and delegate to subclass, skipping no-op changes.

        Two-stage content dedup suppresses spurious broadcasts:

        1. **Stat pre-check** — if ``(mtime, size, mode)`` matches the last
           broadcast for this path, skip without hashing (catches an editor
           open/close with no write; cost: one ``stat()``).
        2. **Hash compare** — if the stat changed, compute the SHA-256 (via
           ``File.get``) and compare with the last broadcast hash. If the
           content is identical (e.g. mtime touched but bytes unchanged),
           skip the broadcast. Only a genuine content change propagates.

        This replaces a naive time-based debounce, which was counter-
        productive in the no-op-close-before-safe-save scenario: it suppressed
        the real rename event (within the debounce window of the spurious
        close) while keeping the false positive.
        """
        try:
            # .hsynckeep is a per-peer local sentinel: the periodic reconcile
            # already excludes it from sync actions in both directions, so the
            # event-driven broadcast path must too. Each peer maintains its own
            # sentinels (ensure_hsynckeep_files / _ensure_sentinel*), and the
            # directory itself is propagated by the directory event — broadcasting
            # the sentinel only produces a spurious `modified` event and a
            # broadcast round-trip.
            if Path(src_path).name == HSYNCKEEP_FILENAME:
                log.debug(f'Skip sentinel event: "{src_path}"')
                return

            # Symlink branch: a symlink modify/close (retarget) is a symlink
            # event, never a file event. Handle immediately and return.
            # Covers dangling links too (is_symlink() is True even when the
            # target is missing — `exists()` would reject them).
            if Path(src_path).is_symlink():
                self._broadcast_symlink(src_path)
                return

            if not Path(src_path).exists():
                log.warning(
                    f'File "{src_path}" not found. It might have been altered by an external tool.'
                )
                return

            # Stage 1: stat pre-check (cheap, avoids re-hash on no-op close).
            # Uses st_mtime_ns (nanosecond precision) so two writes within the
            # same second with the same size are NOT falsely skipped. The mode
            # is part of the key so a pure chmod is never a stat no-op.
            file_stat = Path(src_path).stat()
            current_stat = self._stat_key(file_stat)
            if current_stat == self._last_broadcast_stat.get(src_path):
                log.debug(f'Skip unchanged (stat): "{src_path}"')
                return

            # Stage 2: hash compare (precise, catches mtime-touch without
            # content change and splits permission-only changes).
            invalidate_sha256_cache(src_path)
            file_obj = File.get(src_path)
            if file_obj:
                if file_obj.sha256sum == self._last_broadcast_hash.get(src_path):
                    prev = self._last_broadcast_stat.get(src_path)
                    if prev is not None and current_stat[2] != prev[2]:
                        # Identical content, different mode: a chmod-only
                        # change on a non-inotify platform (or a close after
                        # it). The transport broadcasts it as a modified
                        # event; record the new stat so the next event with
                        # the same mode is suppressed again.
                        self._last_broadcast_stat[src_path] = current_stat
                        log.info(f'[PERMISSION] mode change: "{src_path}"')
                        self._on_permissions_changed(file_obj, src_path)
                        return
                    if prev is not None and current_stat[0] != prev[0]:
                        # Identical content and mode, newer mtime: a plain
                        # ``touch``. Propagate the metadata so replicas converge
                        # on the mtime without re-transferring bytes.
                        self._last_broadcast_stat[src_path] = current_stat
                        log.info(f'[TOUCH] mtime change: "{src_path}"')
                        self._on_metadata_changed(file_obj, src_path)
                        return
                    log.debug(f'Skip unchanged (hash): "{src_path}"')
                    # Update stat so the next no-op close with this mtime is
                    # caught by the cheap stage-1 filter. Key already exists
                    # (hash matched), so no capacity check needed.
                    self._last_broadcast_stat[src_path] = current_stat
                    return
                self._on_file_ready(file_obj, src_path, from_closed, trigger=trigger)
                # Record what we propagated so subsequent no-op closes are
                # suppressed by either stage.
                self._record_broadcast(src_path, current_stat, file_obj.sha256sum)
        except Exception as e:
            log.error(f'Error handling file event for "{src_path}": {e}')

    def on_moved(self, event) -> None:
        src_path = ensure_str_path(event.src_path)
        dest_path = ensure_str_path(getattr(event, "dest_path", None))
        if not src_path and not dest_path:
            return

        # Internal debug trace (not the propagated event): deliberately NOT
        # origin-prefixed — the RENAME line that follows carries the origin.
        log.debug(f'{ActionLabel.MOVE} "{src_path}" -> "{dest_path}"')

        # Full-event emitter (inotify): a move whose other end lies *outside*
        # the watched tree arrives with an empty source or destination (an
        # unmatched IN_MOVED_TO / IN_MOVED_FROM). These are ordinary
        # publishing operations — a file moved into the synced folder from
        # elsewhere (drag & drop, mv, extraction tools, atomic writers) and a
        # file moved out of it — and must be propagated as a create/delete:
        # the standard emitter would collapse the former into
        # ``FileCreatedEvent``, which ``on_created`` deliberately does not
        # handle (on inotify a plain IN_CREATE fires while the file is still
        # being written, so content propagates via ``on_closed`` instead; a
        # move-in has no close-write, so this is its only event).
        message: Event | None = None
        if event.is_directory:
            if not src_path:
                # Move-in from outside the tree: same handling as a fresh
                # directory (hidden-dir filtering included).
                self.on_created(DirCreatedEvent(dest_path))
                return
            if not dest_path:
                # Move-out to outside the tree: same handling as a delete
                # (coalescing and tombstone bookkeeping included).
                self.on_deleted(DirDeletedEvent(src_path))
                return
            if is_hidden_dir(src_path) and is_hidden_dir(dest_path):
                return
            if is_hidden_dir(src_path) and not is_hidden_dir(dest_path):
                self.on_created(DirCreatedEvent(dest_path))
                return
            if is_hidden_dir(dest_path):
                self.on_deleted(DirDeletedEvent(src_path))
                return

            # A renamed directory vacates its source path: invalidate the
            # dedup entry so a later re-create at that path is re-evaluated.
            # Renaming preserves the directory's mtime, so an undone rename
            # (dir back to its old path) would otherwise be stat-suppressed
            # by the directory-create dedup. Mirrors the file-branch
            # invalidation below and the delete-flush invalidation.
            self._invalidate_dedup(src_path)

            log.info(
                f'{self._label("RENAME", src_path, dest_path)} "{src_path}" -> "{dest_path}"'
                f"{self._cid_suffix(src_path, dest_path)}"
            )
            dst = Dir.get(dest_path)
            if dst:
                src = deepcopy(dst)
                src.path = src_path
                message = self._make_event(EventName.renamed, src, dst)
        else:
            if not src_path:
                # Move-in from outside the tree: the file arrives already
                # final (no close-write follows), so propagate it exactly like
                # a close (hidden files filtered exactly like on_closed). The
                # two-stage stat/hash dedup keeps a hypothetical duplicate (a
                # later on_closed after an in-place re-write) harmless.
                if is_hidden_file(dest_path):
                    return
                self._process_file_ready(dest_path, trigger="move-in")
                return
            if not dest_path:
                # Move-out to outside the tree: the path vacated the tree, so
                # route it through on_deleted (tombstones, coalescing and the
                # delete broadcast are identical to a plain delete).
                self.on_deleted(FileDeletedEvent(src_path))
                return
            if is_hidden_file(src_path):
                # Safe-save: an editor/application wrote to a temp file
                # (``.goutputstream-*``, ``*.tmp``, …) and atomically renamed
                # it over the final file. The temp is hidden (ignored) so we
                # reframe this as a content modification of the *destination*,
                # not a user-initiated rename. If the destination is also
                # hidden the event is dropped entirely (temp→temp).
                if not is_hidden_file(dest_path):
                    self._process_file_ready(dest_path, trigger="safe-save")
                return

            log.info(
                f'{self._label("RENAME", src_path, dest_path)} "{src_path}" -> "{dest_path}"'
                f"{self._cid_suffix(src_path, dest_path)}"
            )
            invalidate_sha256_cache(src_path)
            invalidate_sha256_cache(dest_path)
            dst = File.get(dest_path)
            if dst:
                src = deepcopy(dst)
                src.filepath = src_path
                message = self._make_event(EventName.renamed, src, dst)
                # The source path no longer exists; drop its dedup cache so a
                # file later re-created at the same path with identical stat
                # and/or hash is re-evaluated (not skipped as "unchanged").
                # Mirrors the invalidation in on_deleted for the same bug class.
                self._invalidate_dedup(src_path)

        if message:
            self._broadcast_event(message, src_abs_path=src_path, dst_abs_path=dest_path)

    def _flush_pending_deletes(self) -> None:
        """Flush the coalesced delete buffer at most once per window.

        inotify emits the per-descendant delete events before the directory
        event itself, so by the time the timer fires every path under a
        removed directory is present in the buffer. Any path covered by a
        deleted ancestor directory is folded into that ancestor's single
        recursive ``deleted`` event (one message instead of N+1). Folded
        FILE paths still pass through :meth:`_on_deleted` so the LAN master
        records their tombstones; folded directory paths need no marker
        (directories never carry tombstones).

        The filesystem is re-checked at flush time: a buffered path that
        exists again was re-created while its delete sat in the window
        (``rm`` + immediate rewrite of the same name — an ordinary atomic
        writer pattern). That delete is stale: the re-creation already
        propagated on its own event, and broadcasting the delete would wipe
        the fresh file on every peer — and, through the LAN delete echo, on
        the source itself. The ancestor-folding prefixes are built from the
        surviving entries, so a re-created directory never swallows the
        individual deletes of genuinely removed descendants.
        """
        with self._delete_lock:
            pending = self._pending_deletes
            self._pending_deletes = []
            self._pending_delete_set = set()
            self._pending_delete_dir_paths = set()
            self._delete_timer = None
        if not pending:
            return

        # Invalidate the content-dedup cache for every buffered path: a
        # re-created file must be re-evaluated against a fresh stat/hash, not
        # skipped because it happens to share mtime+size (stage 1) or sha256
        # (stage 2) with the pre-deletion version. Without this, create ->
        # delete -> re-create of the same file (same content, preserved mtime)
        # silently drops the third event as "Skip unchanged (stat)".
        for src_path, _is_directory in pending:
            self._invalidate_dedup(src_path)

        pending, _stale = split_stale_deletes(pending)
        if not pending:
            return

        # Pre-computed ancestor prefixes so a huge delete burst (thousands of
        # descendants) folds in O(n) instead of O(n·m) string scans.
        deleted_dir_prefixes = [p.rstrip("/") + "/" for p, is_dir in pending if is_dir]

        def covered_by_deleted_ancestor(path: str) -> bool:
            prefix = path.rstrip("/") + "/"
            return any(prefix.startswith(d) and d != prefix for d in deleted_dir_prefixes)

        for src_path, is_directory in pending:
            if covered_by_deleted_ancestor(src_path):
                log.debug(f'Folded into ancestor delete: "{src_path}"')
                if not is_directory:
                    self._on_deleted(src_path, False)
                continue

            log.info(f'{self._label("DELETE", src_path)} "{src_path}"{self._cid_suffix(src_path)}')
            obj: Dir | File
            if is_directory:
                dir_obj = Dir(src_path)
                # A directory `deleted` event is recursive (single message per tree) —
                # every descendant is removed along with the directory itself.
                dir_obj.recursive = True
                obj = dir_obj
            else:
                obj = File(src_path)
            self._broadcast_event(
                self._make_event(EventName.deleted, obj),
                src_abs_path=src_path,
            )
            self._on_deleted(src_path, is_directory)

    def on_deleted(self, event) -> None:
        src_path = ensure_str_path(event.src_path)

        if event.is_directory and is_hidden_dir(src_path):
            return
        if not event.is_directory and is_hidden_file(src_path):
            return

        if CONFIG.get("IGNORE_DELETE", False):
            log.debug(f'{self._label("IGNORE DEL", src_path)} "{src_path}" (ignore-delete mode)')
            return

        # Buffer the event instead of broadcasting immediately: the sibling
        # descendent events (and the directory event itself) arrive within
        # the coalescing window and fold into a single recursive message.
        # An identical buffered pair is a duplicate (a directory with its own
        # watch fires both IN_DELETE, from the parent's watch, and
        # IN_DELETE_SELF, from its own watch on the same path) and would emit
        # two identical [LOCAL DELETE] lines and two identical broadcasts.
        with self._delete_lock:
            entry = (src_path, event.is_directory)
            if entry in self._pending_delete_set:
                log.debug(f'Duplicate delete skipped: "{src_path}"')
            else:
                self._pending_deletes.append(entry)
                self._pending_delete_set.add(entry)
                if event.is_directory:
                    self._pending_delete_dir_paths.add(src_path.rstrip("/"))
            if self._delete_timer is None:
                self._delete_timer = threading.Timer(
                    DELETE_COALESCE_WINDOW, self._flush_pending_deletes
                )
                self._delete_timer.daemon = True
                self._delete_timer.start()
