# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Sync status for the device that serves the share (p2p master).

The master holds the reference copy, so ``get_files_remote()`` (which queries
the configured server URL) says nothing about the other devices: the only
meaningful question is which local content the connected devices already hold.
That answer is mirrored in ``P2PInventoryStore``, where every device publishes
its snapshot.

Cost model (see ``llm-wiki/benchmarks.md``): a local scan whose SHA-256 values
come from the shared content cache (a repeat pass is stat-only) plus **one**
bulk query over the holder table, then an in-memory set lookup.
``P2PInventoryStore.holders_for`` is deliberately not used here — it runs a JOIN
per identity (~185 us/file, ~18 s per 100 000 files) while the bulk read costs
~0.1 s at the same scale.

Consumers: ``hsync_client.p2p_handler`` (serving role) emits the events this
module produces; ``hsync-desktop`` renders them (file table + sync summary).
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from hsync_client.config import CONFIG
from hsync_client.constants import HSYNCKEEP_FILENAME
from hsync_client.events import UiEventName, emit_event
from hsync_client.sync import _bucket_key, emit_folder_size, get_files_local

__all__ = ["MasterSyncStatus", "NO_DEVICE_MESSAGE", "classify"]

# Same wording the one-shot p2p path uses, so a front-end can show one string
# for "nothing to synchronise against".
NO_DEVICE_MESSAGE = "No other device was found on the network"

# Inventory frames arrive per change; a burst must coalesce into one pass.
MIN_INTERVAL_S = 1.0
# How long the local identity list is reused. The pass also runs on connect and
# on every inventory change, so the TTL only caps repeated full walks.
LOCAL_CACHE_TTL_S = 5.0

# (state, absolute path, size) as published through the ``file_status`` event.
FileState = tuple[str, str, int]


def _is_sentinel(path: str) -> bool:
    """True for the internal empty-directory sentinel (never user content)."""
    return path.rsplit("/", 1)[-1] == HSYNCKEEP_FILENAME


def classify(
    held: Mapping[tuple[str, str], int],
    local_files: Iterable[dict],
    bucket_dir: str,
) -> tuple[list[FileState], list[dict]]:
    """Classify the local share against the identities devices published.

    ``held`` maps ``(bucket-relative path, sha256)`` to the size, as returned by
    ``P2PInventoryStore.held_identities``. Returns the per-file states plus a
    listing-shaped view of the held set (for ``emit_folder_size``).

    A local file a device already holds is ``synced``; one nobody holds is
    ``to_upload`` (the content still has to reach the devices). A path only the
    devices hold is ``to_download``. Symlinks and the empty-directory sentinel
    are skipped: they carry no content to compare.
    """
    states: list[FileState] = []
    local_paths: set[str] = set()
    for entry in local_files:
        path = str(entry.get("filepath") or "")
        if not path or entry.get("type") == "symlink":
            continue
        key = _bucket_key(path, bucket_dir)
        if not key or _is_sentinel(key):
            continue
        digest = str(entry.get("sha256sum") or "")
        local_paths.add(key)
        states.append(
            (
                "synced" if (key, digest) in held else "to_upload",
                path,
                max(int(entry.get("size", 0) or 0), 0),
            )
        )

    remote_like: list[dict] = []
    remote_only: dict[str, int] = {}
    for (key, _digest), size in held.items():
        if _is_sentinel(key):
            continue
        remote_like.append({"filepath": key, "size": size})
        if key not in local_paths:
            remote_only[key] = size
    for key, size in remote_only.items():
        states.append(("to_download", os.path.join(bucket_dir, key), size))
    return states, remote_like


class MasterSyncStatus:
    """Runs the master classification off the serving path.

    ``store_provider`` returns the live inventory store (``None`` until the
    serving role has one). ``request`` coalesces bursts: one pass per
    ``min_interval_s``, executed by a short-lived worker thread, so neither the
    WebSocket handler nor the stdin reader blocks on a large share.
    """

    def __init__(
        self,
        store_provider: Callable[[], Any],
        log: Any,
        *,
        local_provider: Callable[[], list[dict]] = get_files_local,
        connected_provider: Callable[[], int] | None = None,
        min_interval_s: float = MIN_INTERVAL_S,
        local_cache_ttl_s: float = LOCAL_CACHE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store_provider = store_provider
        self._log = log
        self._local_provider = local_provider
        self._connected_provider = connected_provider or (lambda: 0)
        self._min_interval_s = min_interval_s
        self._local_cache_ttl_s = local_cache_ttl_s
        self._clock = clock
        self._pending = threading.Event()
        self._stopped = threading.Event()
        self._run_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._last_run = float("-inf")
        self._local_files: list[dict] | None = None
        self._local_at = float("-inf")

    # ── triggers ─────────────────────────────────────────────────────────

    def request(self) -> None:
        """Schedule a classification (coalesced, never blocks the caller).

        A pass never emits ``preview_start``/``preview_end``, not even when the
        front-end asked for it: those two events are the front-end's *preview
        cycle* contract, and a serving device answering them would consume the
        confirmation state of the preview the window is waiting for (the desktop
        then shows a summary with the wrong counters and never sends the
        ``SYNC`` that ends its dry run). The window settles these marker-less
        ``file_status`` bursts with a quiet-period timer.
        """
        if self._stopped.is_set():
            return
        self._pending.set()
        with self._start_lock:
            worker = self._worker
            if worker is None or not worker.is_alive():
                worker = threading.Thread(
                    target=self._loop, name="hsync-master-status", daemon=True
                )
                self._worker = worker
                worker.start()

    def invalidate_local(self) -> None:
        """Drop the cached local listing (the share changed on disk)."""
        self._local_files = None
        self._local_at = float("-inf")

    def stop(self) -> None:
        """Stop the worker (idempotent)."""
        self._stopped.set()
        self._pending.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=5.0)
        self._worker = None

    # ── pass ─────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stopped.is_set():
            if not self._pending.wait(timeout=0.05):
                if not self._pending.is_set():
                    return
            self._pending.clear()
            delay = self._min_interval_s - (self._clock() - self._last_run)
            if delay > 0 and self._stopped.wait(timeout=delay):
                return
            # Coalesce everything that arrived while waiting.
            self._pending.clear()
            try:
                self.run_now()
            except Exception:  # a status pass must never kill the role
                self._log.exception("master status classification failed")
            if not self._pending.is_set():
                return

    def run_now(self) -> None:
        """Classify the local share against the devices and emit the events."""
        with self._run_lock:
            self._last_run = self._clock()
            self._classify()

    def _classify(self) -> None:
        store = self._store_provider()
        if store is None:
            emit_event(UiEventName.sync_pending, reason="no_device")
            return
        sessions, _staged, committed = store.row_counts()
        if sessions == 0:
            # Distinguish "nobody is here" from "a device is connected but has
            # not opened an inventory session": the latter is a waiting state,
            # never "no device found".
            reason = "awaiting_inventory" if self._connected_provider() > 0 else "no_device"
            emit_event(UiEventName.sync_pending, reason=reason)
            return
        if committed == 0:
            # A device is connected but has not published its inventory yet:
            # this is "unknown" (the front-end shows a waiting state), never
            # "out of sync" — the comparison has no data to stand on.
            emit_event(UiEventName.sync_pending, reason="awaiting_inventory")
            return
        held = store.held_identities()
        local_files = self._local_listing()
        bucket_dir = str(CONFIG.get("LOCAL_BUCKET") or "")
        states, remote_like = classify(held, local_files, bucket_dir)
        emit_folder_size(local_files, remote_like)
        for state, path, size in states:
            emit_event(UiEventName.file_status, state=state, file=path, size=size)

    def _local_listing(self) -> list[dict]:
        now = self._clock()
        if self._local_files is None or now - self._local_at >= self._local_cache_ttl_s:
            self._local_files = self._local_provider()
            self._local_at = now
        return self._local_files
