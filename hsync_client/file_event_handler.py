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
from os.path import exists, islink
from time import time

from loguru import logger as log
from watchdog.events import DirCreatedEvent, DirDeletedEvent, FileSystemEventHandler

from hsync_client.config import CONFIG
from hsync_client.models.dir import Dir, is_hidden_dir
from hsync_client.models.event import Event, EventName
from hsync_client.models.file import File, invalidate_sha256_cache, is_hidden_file
from hsync_client.models.symlink import Symlink
from hsync_client.utils import ensure_str_path, is_same_paths

# on_closed is delivered by watchdog only on Linux (inotify).
SUPPORTS_ON_CLOSED = sys.platform == "linux"

# Debounce interval for on_modified fallback on platforms without on_closed.
MODIFIED_DEBOUNCE_INTERVAL = 1.0


class BaseFileEventHandler(FileSystemEventHandler):
    """Common watchdog handler shared by Remote sync and LAN master."""

    # Prepended to log labels, e.g. ``""`` -> "[CREATE]", ``"LAN "`` -> "[LAN CREATE]".
    _LOG_PREFIX: str = ""

    def __init__(self) -> None:
        self._last_modified: dict[str, float] = {}

    # ----- subclass hooks -----

    def _broadcast_event(
        self,
        event: Event,
        src_abs_path: str | None = None,
        dst_abs_path: str | None = None,
    ) -> None:
        """Send event metadata to peers/server."""
        raise NotImplementedError

    def _on_file_ready(self, file: File, src_path: str) -> None:
        """Called after a file's bytes are ready to propagate."""
        raise NotImplementedError

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
            log.info(f'[{self._LOG_PREFIX}CREATE ] "{src_path}"')
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
        elif not is_hidden_file(src_path) and islink(src_path):
            log.info(f'[{self._LOG_PREFIX}SYMLINK] "{src_path}"')
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
                log.info(f'[{self._LOG_PREFIX}MODIFY ] "{src_path}"')
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

        current_time = time()
        last_time = self._last_modified.get(src_path, 0)
        if current_time - last_time >= MODIFIED_DEBOUNCE_INTERVAL:
            self._last_modified[src_path] = current_time
            self._process_file_ready(src_path)

    def on_closed(self, event) -> None:
        src_path = ensure_str_path(event.src_path)
        if event.is_directory or is_hidden_file(src_path):
            return
        if CONFIG.get("SYNC_DIRECTION") == "download-only":
            return
        self._process_file_ready(src_path)

    def _process_file_ready(self, src_path: str) -> None:
        """Invalidate the digest cache, build ``File``, delegate to subclass."""
        try:
            if not exists(src_path):
                log.warning(
                    f'File "{src_path}" not found. It might have been altered by an external tool.'
                )
                return
            invalidate_sha256_cache(src_path)
            file_obj = File.get(src_path)
            if file_obj:
                self._on_file_ready(file_obj, src_path)
        except Exception as e:
            log.error(f'Error handling file event for "{src_path}": {e}')

    def on_moved(self, event) -> None:
        src_path = ensure_str_path(event.src_path)
        dest_path = ensure_str_path(getattr(event, "dest_path", None))
        if not dest_path:
            return

        log.debug(f'[{self._LOG_PREFIX}MOVE   ] "{src_path}" -> "{dest_path}"')

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

            log.info(f'[{self._LOG_PREFIX}RENAME ] "{src_path}" -> "{dest_path}"')
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
                return

            log.info(f'[{self._LOG_PREFIX}RENAME ] "{src_path}" -> "{dest_path}"')
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

        if message:
            self._broadcast_event(message, src_abs_path=src_path, dst_abs_path=dest_path)

    def on_deleted(self, event) -> None:
        src_path = ensure_str_path(event.src_path)

        if event.is_directory and is_hidden_dir(src_path):
            return
        if not event.is_directory and is_hidden_file(src_path):
            return

        if CONFIG.get("IGNORE_DELETE", False):
            log.debug(f'[{self._LOG_PREFIX}IGNORE ] "{src_path}" (ignore-delete mode)')
            return

        log.info(f'[{self._LOG_PREFIX}REMOVE ] "{src_path}"')

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
