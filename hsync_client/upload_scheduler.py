# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Per-path upload serialization and coalescing.

A single file change can produce several watcher events (modify, close, chmod,
touch). Running one upload attempt per event lets a slow delta scan race a
concurrent full upload for the same path: the server's base file changes and the
delta apply is rejected with ``409 base_file_changed``, forcing a redundant full
transfer.

Industry sync engines serialise transfers per file and coalesce rapid events
(Syncthing accumulates watcher changes and syncs one file state at a time;
Dropbox/Nextcloud likewise collapse bursty updates). This module owns that state
machine for the client transports:

* at most one upload attempt per path is in flight;
* events that arrive during the attempt collapse into the newest request;
* one follow-up attempt runs when the current one settles (and only if a newer
  event actually arrived).

Transports supply the executor and the upload callable; the scheduler itself is
transport-agnostic.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from concurrent.futures import Executor, Future
from dataclasses import dataclass

from hsync_common.models.file import File
from loguru import logger as log

UploadFn = Callable[..., None]


@dataclass(slots=True)
class UploadRequest:
    """One coalesced upload request for a path."""

    file: File
    permissions_only: bool = False
    from_closed: bool = False


class _Slot:
    """Per-path state: a running attempt plus the latest pending request."""

    __slots__ = ("pending",)

    def __init__(self) -> None:
        self.pending: UploadRequest | None = None


class PerPathUploadScheduler:
    """Serialize upload attempts per path and coalesce bursty events."""

    def __init__(self, executor: Executor | Callable[[], Executor], upload: UploadFn) -> None:
        self._executor = executor
        self._upload = upload
        self._lock = threading.Lock()
        self._paths: dict[str, _Slot] = {}

    @staticmethod
    def _key(file: File) -> str:
        path = file.filepath
        if not path:
            return ""
        return os.path.normcase(os.path.abspath(path))

    def submit(
        self,
        file: File,
        *,
        permissions_only: bool = False,
        from_closed: bool = False,
    ) -> Future | None:
        """Queue *file* for upload; return the drain future when one starts.

        The returned future covers the running attempt and any coalesced
        follow-up, so callers can track it (P2P ``wait_pending``). A coalesced
        submit returns ``None`` because the existing drain future already owns
        the follow-up.
        """
        key = self._key(file)
        if not key:
            return None
        request = UploadRequest(file, permissions_only, from_closed)
        with self._lock:
            slot = self._paths.get(key)
            if slot is None:
                slot = _Slot()
                slot.pending = request
                self._paths[key] = slot
                start = True
            else:
                start = False
                pending = slot.pending
                if pending is None:
                    slot.pending = request
                else:
                    # Newest file state wins; a content upload supersedes a
                    # permission-only one so the final state is always sent.
                    slot.pending = UploadRequest(
                        file=file,
                        permissions_only=pending.permissions_only and permissions_only,
                        from_closed=from_closed,
                    )
        if not start:
            return None
        # Resolve the executor at submit time: a factory (transports pass
        # ``lambda: _get_*_executor()``) keeps the singleton/test override
        # patchable, while a plain Executor is used as-is.
        executor = self._executor() if callable(self._executor) else self._executor
        future = executor.submit(self._drain, key)
        return future

    def _drain(self, key: str) -> None:
        """Run the current request, then a coalesced follow-up if one arrived."""
        while True:
            with self._lock:
                slot = self._paths.get(key)
                if slot is None:
                    return
                request = slot.pending
                slot.pending = None
                if request is None:
                    del self._paths[key]
                    return
            try:
                self._upload(
                    request.file,
                    permissions_only=request.permissions_only,
                    from_closed=request.from_closed,
                )
            except Exception:  # pragma: no cover - upload_file handles its own failures
                log.exception(f"Upload worker crashed for {key}")
