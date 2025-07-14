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

import sys
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
    from watchdog.observers import Observer

# on_closed is delivered by watchdog only on Linux (inotify).
SUPPORTS_ON_CLOSED = sys.platform == "linux"

# Debounce interval for on_modified fallback on platforms without on_closed.
MODIFIED_DEBOUNCE_INTERVAL = 1.0

# Maximum entries kept in the broadcast dedup dicts (_last_broadcast_stat /
# _last_broadcast_hash). Mirrors SHA256_CACHE_MAX_SIZE so memory stays bounded
# (~2 MB at ~200 bytes/entry) even in long-running watch sessions.
DEDUP_MAX_ENTRIES: int = 10_000


class BaseFileEventHandler(FileSystemEventHandler):
    """Common watchdog handler shared by Remote sync and LAN master."""

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
        return resolve_label(prefix, name)

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
        # Two-stage content dedup for file-ready events:
        # 1. stat pre-check (mtime, size): cheap filter that avoids re-hashing
        #    on no-op closes where the editor opened/closed without saving.
        # 2. hash compare: precise filter that skips broadcasts when the file
        #    content matches what was last propagated (catches mtime-touch
        #    without content change, and the spurious close that precedes an
        #    editor safe-save rename).
        self._last_broadcast_stat: dict[str, tuple[int, int]] = {}
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

    def _on_deleted(self, src_path: str, is_directory: bool) -> None:
        """Called after a non-hidden deletion has been observed and broadcast.

        Default no-op. The LAN master overrides this to record a deletion
        marker (tombstone); the Remote handler leaves it unimplemented because
        the central server records tombstones itself.
        """
        return None

    def _record_broadcast(self, src_path: str, stat: tuple[int, int], sha256: str) -> None:
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

    def on_modified(self, event) -> None:
        src_path = ensure_str_path(event.src_path)

        if event.is_directory:
            if not is_hidden_dir(src_path) and not is_same_paths(CONFIG["LOCAL_BUCKET"], src_path):
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
                current_stat = (file_stat.st_mtime_ns, file_stat.st_size) if file_stat else None
                if current_stat is not None and current_stat == self._last_broadcast_stat.get(
                    src_path
                ):
                    log.debug(f'Skip unchanged (stat): "{src_path}" (dir)')
                    return
                # Record the stat *before* logging/broadcasting: mkdir emits a
                # burst of IN_ATTRIB events ~1ms apart, and the second one can
                # be dispatched before Dir.get + broadcast of the first has
                # completed. Recording up front lets the dedup pre-check catch
                # that sibling immediately; a later genuine content change
                # updates the directory mtime and is never skipped.
                if current_stat is not None:
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

        if SUPPORTS_ON_CLOSED or is_hidden_file(src_path):
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
            # same second with the same size are NOT falsely skipped.
            file_stat = Path(src_path).stat()
            current_stat = (file_stat.st_mtime_ns, file_stat.st_size)
            if current_stat == self._last_broadcast_stat.get(src_path):
                log.debug(f'Skip unchanged (stat): "{src_path}"')
                return

            # Stage 2: hash compare (precise, catches mtime-touch without
            # content change).
            invalidate_sha256_cache(src_path)
            file_obj = File.get(src_path)
            if file_obj:
                if file_obj.sha256sum == self._last_broadcast_hash.get(src_path):
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

    def on_deleted(self, event) -> None:
        src_path = ensure_str_path(event.src_path)

        if event.is_directory and is_hidden_dir(src_path):
            return
        if not event.is_directory and is_hidden_file(src_path):
            return

        if CONFIG.get("IGNORE_DELETE", False):
            log.debug(f'{self._label("IGNORE DEL", src_path)} "{src_path}" (ignore-delete mode)')
            return

        log.info(f'{self._label("DELETE", src_path)} "{src_path}"{self._cid_suffix(src_path)}')

        obj = Dir(src_path) if event.is_directory else File(src_path)
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

        # Invalidate the content-dedup cache for this path: a re-created file
        # must be re-evaluated against a fresh stat/hash, not skipped because it
        # happens to share mtime+size (stage 1) or sha256 (stage 2) with the
        # pre-deletion version. Without this, create -> delete -> re-create of
        # the same file (same content, preserved mtime) silently drops the
        # third event as "Skip unchanged (stat)".
        self._last_broadcast_stat.pop(src_path, None)
        self._last_broadcast_hash.pop(src_path, None)

        self._on_deleted(src_path, event.is_directory)
