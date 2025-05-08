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

from hsync_common.labels import resolve_label
from hsync_common.models.dir import Dir, is_hidden_dir
from hsync_common.models.event import Event, EventName
from hsync_common.models.file import File, invalidate_sha256_cache, is_hidden_file
from hsync_common.models.symlink import Symlink
from loguru import logger as log
from watchdog.events import DirCreatedEvent, DirDeletedEvent, FileSystemEventHandler

from hsync_client.config import CONFIG
from hsync_client.utils import ensure_str_path, is_same_paths

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

    # Prepended to log labels, e.g. ``""`` -> "[CREATE]", ``"LAN "`` -> "[LAN CREATE]".
    _LOG_PREFIX: str = ""

    def _label(self, name: str) -> str:
        """Return the bracketed label for *name*, honouring ``_LOG_PREFIX``.

        Delegates to :func:`hsync_common.labels.resolve_label` so the label is
        resolved through :class:`ActionLabel` and colorized consistently with
        the inbound event dispatcher (no duplicated tag-formatting logic).

        Examples:
            - ``_LOG_PREFIX=""``, name="CREATE" -> ``[CREATE]``
            - ``_LOG_PREFIX="LAN "``, name="CREATE" -> ``[LAN CREATE]``
        """
        return resolve_label(self._LOG_PREFIX, name)

    def __init__(self) -> None:
        # Two-stage content dedup for file-ready events:
        # 1. stat pre-check (mtime, size): cheap filter that avoids re-hashing
        #    on no-op closes where the editor opened/closed without saving.
        # 2. hash compare: precise filter that skips broadcasts when the file
        #    content matches what was last propagated (catches mtime-touch
        #    without content change, and the spurious close that precedes an
        #    editor safe-save rename).
        self._last_broadcast_stat: dict[str, tuple[int, int]] = {}
        self._last_broadcast_hash: dict[str, str] = {}

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

    def on_created(self, event) -> None:
        src_path = ensure_str_path(event.src_path)
        if event.is_directory and not is_hidden_dir(src_path):
            log.info(f'{self._label("CREATE")} "{src_path}"')
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
        elif not is_hidden_file(src_path) and Path(src_path).is_symlink():
            log.info(f'{self._label("SYMLINK")} "{src_path}"')
            symlink = Symlink.get(src_path)
            if symlink:
                self._broadcast_event(
                    Event(
                        CONFIG["CID"],
                        CONFIG["LOCAL_BUCKET"],
                        CONFIG["REMOTE_BUCKET"],
                        EventName.symlink,
                        symlink,
                    ),
                    src_abs_path=src_path,
                )

    def on_modified(self, event) -> None:
        src_path = ensure_str_path(event.src_path)

        if event.is_directory:
            if not is_hidden_dir(src_path) and not is_same_paths(CONFIG["LOCAL_BUCKET"], src_path):
                log.info(f'{self._label("MODIFY")} "{src_path}"')
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

        log.debug(f'{self._label("MOVE")} "{src_path}" -> "{dest_path}"')

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

            log.info(f'{self._label("RENAME")} "{src_path}" -> "{dest_path}"')
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

            log.info(f'{self._label("RENAME")} "{src_path}" -> "{dest_path}"')
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
            log.debug(f'{self._label("IGNORE DEL")} "{src_path}" (ignore-delete mode)')
            return

        log.info(f'{self._label("REMOVE")} "{src_path}"')

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
