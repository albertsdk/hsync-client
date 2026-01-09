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
from typing import TYPE_CHECKING

from hsync_common.labels import ActionLabel, resolve_label
from hsync_common.models.dir import Dir, is_hidden_dir
from hsync_common.models.event import Event, EventName
from hsync_common.models.file import File, invalidate_sha256_cache, is_hidden_file
from hsync_common.models.symlink import Symlink
from hsync_common.path_validation import is_safe_symlink_target
from loguru import logger as log
from watchdog.events import DirCreatedEvent, DirDeletedEvent, FileSystemEventHandler

from hsync_client.config import CONFIG
from hsync_client.constants import HSYNCKEEP_FILENAME
from hsync_client.utils import ensure_str_path, is_same_paths

if TYPE_CHECKING:
    from watchdog.observers.api import BaseObserver as Observer

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
        cid = self._origin_cid(src_path, dst_path)
        return f" (CID: {cid})" if cid else ""

    def __init__(self, observer: Observer | None = None) -> None:
        # Delete coalescing: buffered (path, is_directory) pairs pending a flush.
        # Guarded by _delete_lock; _delete_timer is the pending threading.Timer.
        self._pending_deletes: list[tuple[str, bool]] = []
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
        # Optional observer reference: when set, newly created directories are
        # watched recursively so files inside them are picked up immediately
        # instead of waiting for the next full sync cycle.
        self._observer: Observer | None = observer

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

    def _on_deleted(self, src_path: str, is_directory: bool) -> None:
        """Called after a non-hidden deletion has been observed and broadcast.

        Default no-op. The LAN master overrides this to record a deletion
        marker (tombstone); the Remote handler leaves it unimplemented because
        the central server records tombstones itself.
        """
        return None

    def _is_covered_by_pending_delete(self, path: str) -> bool:
        """Return True when *path* equals or descends from a buffered dir delete.

        inotify fires ``IN_ATTRIB`` on a directory whenever a child entry is
        removed, so an ``rmtree`` of a deep tree produces one spurious
        directory MODIFY per removed child before the tree's own ``deleted``
        events are coalesced. Those modifies are noise, not metadata changes:
        a path equal to, or under, a directory already buffered for deletion
        is skipped by ``on_modified``. A snapshot is taken under the buffer's
        lock (the flush timer drains it from another thread).
        """
        prefix = path.rstrip("/") + "/"
        with self._delete_lock:
            pending = list(self._pending_deletes)
        return any(
            is_dir and prefix.startswith(rec_path.rstrip("/") + "/") for rec_path, is_dir in pending
        )

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
        """
        if CONFIG.get("SYNC_DIRECTION") == "download-only":
            return
        if is_hidden_file(src_path) or Path(src_path).is_symlink():
            return
        try:
            st = Path(src_path).stat()
        except OSError:
            return
        current = self._stat_key(st)
        prev = self._last_broadcast_stat.get(src_path)
        if prev is None:
            # First observation (e.g. the watch just started): record the mode
            # so a later chmod is detected; content goes via on_closed.
            self._last_broadcast_stat[src_path] = current
            return
        if current[2] == prev[2]:
            return
        invalidate_sha256_cache(src_path)
        file_obj = File.get(src_path)
        if file_obj is None:
            return
        self._last_broadcast_stat[src_path] = current
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
            self._last_broadcast_stat.pop(oldest, None)
            self._last_broadcast_hash.pop(oldest, None)
        self._last_broadcast_stat[src_path] = stat
        self._last_broadcast_hash[src_path] = sha256

    # ----- watchdog event handlers -----

    def _scan_existing_files(self, dir_path: str) -> None:
        """Scan *dir_path* for already-present visible files and process them.

        Called right after an ``observer.schedule()`` so that files created
        between ``mkdir()`` and the moment the inotify watch becomes active
        are picked up immediately.  Subdirectories are NOT recursed into —
        each of them will receive its own ``DirCreatedEvent`` and trigger
        another scan.
        """
        from os import listdir

        try:
            for name in listdir(dir_path):
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
                Event(
                    CONFIG["CID"],
                    CONFIG["LOCAL_BUCKET"],
                    CONFIG["REMOTE_BUCKET"],
                    EventName.symlink,
                    link,
                ),
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
            # pre-check (mtime_ns + size) against the last broadcast is
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
                    Event(
                        CONFIG["CID"],
                        CONFIG["LOCAL_BUCKET"],
                        CONFIG["REMOTE_BUCKET"],
                        EventName.added,
                        dir_obj,
                    ),
                    src_abs_path=src_path,
                )
            # Start watching the new directory recursively so files created
            # inside it are picked up immediately rather than waiting for the
            # next periodic resync to discover them.
            if self._observer is not None:
                self._observer.schedule(self, src_path, recursive=True)
                # Scan for files already present inside the directory.  The
                # inotify watch registered above only captures events that
                # happen *after* the watch is active; files created between
                # mkdir() and schedule() are invisible to inotify.  Iterating
                # the directory contents once guarantees they are picked up.
                self._scan_existing_files(src_path)

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
                # content hash, so a stat pre-check (mtime_ns + size) against
                # the last broadcast is sufficient and cheap (one stat()).
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
                if current_stat == self._last_broadcast_stat.get(src_path):
                    log.debug(f'Skip unchanged (stat): "{src_path}" (dir)')
                    return
                # Record the stat *before* logging/broadcasting: mkdir emits a
                # burst of IN_ATTRIB events ~1ms apart, and the second one can
                # be dispatched before Dir.get + broadcast of the first has
                # completed. Recording up front lets the dedup pre-check catch
                # that sibling immediately; a later genuine content change
                # updates the directory mtime and is never skipped.
                self._record_broadcast(src_path, current_stat, "")
                log.info(
                    f'{self._label("MODIFY", src_path)} "{src_path}" (dir)'
                    f"{self._cid_suffix(src_path)}"
                )
                dir_obj = Dir.get(src_path)
                if dir_obj:
                    self._broadcast_event(
                        Event(
                            CONFIG["CID"],
                            CONFIG["LOCAL_BUCKET"],
                            CONFIG["REMOTE_BUCKET"],
                            EventName.modified,
                            dir_obj,
                        ),
                        src_abs_path=src_path,
                    )
            return

        if SUPPORTS_ON_CLOSED:
            # Content changes propagate via on_closed on inotify platforms;
            # a chmod (IN_ATTRIB) never fires a close, so inspect mode-only
            # changes here without touching file bytes.
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

        1. **Stat pre-check** — if ``(mtime, size)`` matches the last
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
        if not dest_path:
            return

        # Internal debug trace (not the propagated event): deliberately NOT
        # origin-prefixed — the RENAME line that follows carries the origin.
        log.debug(f'{ActionLabel.MOVE} "{src_path}" -> "{dest_path}"')

        message: Event | None = None
        if event.is_directory:
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
            self._last_broadcast_stat.pop(src_path, None)
            self._last_broadcast_hash.pop(src_path, None)

            log.info(
                f'{self._label("RENAME", src_path, dest_path)} "{src_path}" -> "{dest_path}"'
                f"{self._cid_suffix(src_path, dest_path)}"
            )
            dst = Dir.get(dest_path)
            if dst:
                src = deepcopy(dst)
                src.path = src_path
                message = Event(
                    CONFIG["CID"],
                    CONFIG["LOCAL_BUCKET"],
                    CONFIG["REMOTE_BUCKET"],
                    EventName.renamed,
                    src,
                    dst,
                )
        else:
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
                message = Event(
                    CONFIG["CID"],
                    CONFIG["LOCAL_BUCKET"],
                    CONFIG["REMOTE_BUCKET"],
                    EventName.renamed,
                    src,
                    dst,
                )
                # The source path no longer exists; drop its dedup cache so a
                # file later re-created at the same path with identical stat
                # and/or hash is re-evaluated (not skipped as "unchanged").
                # Mirrors the invalidation in on_deleted for the same bug class.
                self._last_broadcast_stat.pop(src_path, None)
                self._last_broadcast_hash.pop(src_path, None)

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
        """
        with self._delete_lock:
            pending = self._pending_deletes
            self._pending_deletes = []
            self._delete_timer = None
        if not pending:
            return

        # Pre-computed ancestor prefixes so a huge delete burst (thousands of
        # descendants) folds in O(n) instead of O(n·m) string scans.
        deleted_dir_prefixes = [p.rstrip("/") + "/" for p, is_dir in pending if is_dir]

        def covered_by_deleted_ancestor(path: str) -> bool:
            prefix = path.rstrip("/") + "/"
            return any(prefix.startswith(d) and d != prefix for d in deleted_dir_prefixes)

        for src_path, is_directory in pending:
            # Invalidate the content-dedup cache for this path: a re-created
            # file must be re-evaluated against a fresh stat/hash, not skipped
            # because it happens to share mtime+size (stage 1) or sha256
            # (stage 2) with the pre-deletion version. Without this,
            # create -> delete -> re-create of the same file (same content,
            # preserved mtime) silently drops the third event as
            # "Skip unchanged (stat)".
            self._last_broadcast_stat.pop(src_path, None)
            self._last_broadcast_hash.pop(src_path, None)

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
                Event(
                    CONFIG["CID"],
                    CONFIG["LOCAL_BUCKET"],
                    CONFIG["REMOTE_BUCKET"],
                    EventName.deleted,
                    obj,
                ),
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
            if entry in self._pending_deletes:
                log.debug(f'Duplicate delete skipped: "{src_path}"')
            else:
                self._pending_deletes.append(entry)
            if self._delete_timer is None:
                self._delete_timer = threading.Timer(
                    DELETE_COALESCE_WINDOW, self._flush_pending_deletes
                )
                self._delete_timer.daemon = True
                self._delete_timer.start()
