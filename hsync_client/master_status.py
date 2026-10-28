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
bulk read of the published inventories (``P2PInventoryStore.compare_share``:
the union, the per-identity holder count and the per-device tallies in a single
pass), then in-memory lookups. ``P2PInventoryStore.holders_for`` is deliberately
not used here — it runs a JOIN per identity (~185 us/file, ~18 s per 100 000
files) while the bulk read costs ~0.1 s at the same scale.

The report is **per device**: the union answers "does any device hold this?",
which hides which device is behind as soon as two are connected, so the pass
emits one ``device_status`` row per live device and stamps every ``file_status``
with its coverage (``holders`` out of ``devices``).

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

__all__ = ["MasterSyncStatus", "NO_DEVICE_MESSAGE", "classify", "local_identities"]

# Same wording the one-shot p2p path uses, so a front-end can show one string
# for "nothing to synchronise against".
NO_DEVICE_MESSAGE = "No other device was found on the network"

# Inventory frames arrive per change; a burst must coalesce into one pass.
MIN_INTERVAL_S = 1.0
# How long the local identity list is reused. The pass also runs on connect and
# on every inventory change, so the TTL only caps repeated full walks.
LOCAL_CACHE_TTL_S = 5.0

# (state, absolute path, size, live devices holding it) as published through the
# ``file_status`` event; ``holders`` is -1 when no per-device view is available.
FileState = tuple[str, str, int, int]

# ``((bucket-relative path, sha256), absolute path, size)`` for a comparable file.
LocalIdentity = tuple[tuple[str, str], str, int]


def _is_sentinel(path: str) -> bool:
    """True for the internal empty-directory sentinel (never user content)."""
    return path.rsplit("/", 1)[-1] == HSYNCKEEP_FILENAME


def local_identities(local_files: Iterable[dict], bucket_dir: str) -> list[LocalIdentity]:
    """Comparable local entries: ``((path, sha256), absolute path, size)``.

    Symlinks and the empty-directory sentinel are skipped: they carry no content
    to compare. Single source for the local side of both the union verdict and
    the per-device tally.
    """
    entries: list[LocalIdentity] = []
    for entry in local_files:
        path = str(entry.get("filepath") or "")
        if not path or entry.get("type") == "symlink":
            continue
        key = _bucket_key(path, bucket_dir)
        if not key or _is_sentinel(key):
            continue
        identity = (key, str(entry.get("sha256sum") or ""))
        entries.append((identity, path, max(int(entry.get("size", 0) or 0), 0)))
    return entries


def classify(
    held: Mapping[tuple[str, str], int],
    local_files: Iterable[dict],
    bucket_dir: str,
    holders: Mapping[tuple[str, str], int] | None = None,
) -> tuple[list[FileState], list[dict]]:
    """Classify the local share against the identities devices published.

    ``held`` maps ``(bucket-relative path, sha256)`` to the size, as returned by
    ``P2PInventoryStore.compare_share``; ``holders`` counts the live devices
    holding each identity, so every row carries its coverage. Returns the
    per-file states plus a listing-shaped view of the held set (for
    ``emit_folder_size``).

    A local file a device already holds is ``synced``; one nobody holds is
    ``to_upload`` (the content still has to reach the devices). A path only the
    devices hold is ``to_download``. These verdicts describe the union: how many
    devices hold the identity is the row's ``holders``.
    """
    return _classify_entries(held, local_identities(local_files, bucket_dir), bucket_dir, holders)


def _classify_entries(
    held: Mapping[tuple[str, str], int],
    local: Iterable[LocalIdentity],
    bucket_dir: str,
    holders: Mapping[tuple[str, str], int] | None,
) -> tuple[list[FileState], list[dict]]:
    """Classify pre-derived local entries (see :func:`local_identities`)."""
    states: list[FileState] = []
    local_paths: set[str] = set()
    for identity, path, size in local:
        local_paths.add(identity[0])
        coverage = -1 if holders is None else holders.get(identity, 0)
        states.append(("synced" if identity in held else "to_upload", path, size, coverage))

    remote_like: list[dict] = []
    remote_only: dict[str, tuple[tuple[str, str], int]] = {}
    for identity, size in held.items():
        if _is_sentinel(identity[0]):
            continue
        remote_like.append({"filepath": identity[0], "size": size})
        if identity[0] not in local_paths:
            remote_only.setdefault(identity[0], (identity, size))
    for path, (identity, size) in remote_only.items():
        coverage = -1 if holders is None else holders.get(identity, 0)
        states.append(("to_download", os.path.join(bucket_dir, path), size, coverage))
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
        front-end asked for it: that pair is the *preview cycle* a front-end
        requests and confirms, and a serving device answering it would consume the
        confirmation of the window's own preview (which would then show a summary
        with the classification's counters and never send the ``SYNC`` that ends
        its dry run). The pass reports itself through the serving-side pair
        instead (see ``run_now``).
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
        """Classify the local share against the devices and emit the events.

        The pass is bracketed by ``status_pass_start``/``status_pass_end``, the
        serving-side counterpart of a requester's ``preview_start``/``preview_end``:
        a front-end renders the comparison from them and settles its summary, and
        a pass never confirms a pending sync. The pair wraps every outcome,
        including the "cannot classify yet" one.
        """
        with self._run_lock:
            self._last_run = self._clock()
            emit_event(UiEventName.status_pass_start)
            try:
                self._classify()
            finally:
                emit_event(UiEventName.status_pass_end)

    def _classify(self) -> None:
        store = self._store_provider()
        if store is None:
            emit_event(UiEventName.sync_pending, reason="no_device")
            return
        connected = self._connected_provider()
        # A session that opened but never committed its snapshot says nothing
        # about what the device holds, so only the published ones are compared.
        expected = max(connected, len(store.live_session_cids()))
        published = store.published_session_cids()
        if not expected:
            # Nobody to compare with: no device joined and no session is open.
            emit_event(
                UiEventName.sync_pending,
                reason="no_device",
                devices=0,
                expected=0,
            )
            return
        if len(published) < expected:
            # Every verdict below would be computed on a partial group: a device
            # without a published inventory is invisible to the comparison.
            # Report the waiting state with both counts instead of a file verdict
            # that could claim an alignment nobody verified.
            emit_event(
                UiEventName.sync_pending,
                reason="awaiting_inventory" if expected else "no_device",
                devices=len(published),
                expected=expected,
            )
            return
        local_files = self._local_listing()
        bucket_dir = str(CONFIG.get("LOCAL_BUCKET") or "")
        local = local_identities(local_files, bucket_dir)
        comparison = store.compare_share(identity for identity, _path, _size in local)
        states, remote_like = _classify_entries(
            comparison.held, local, bucket_dir, comparison.holders
        )
        emit_folder_size(local_files, remote_like)
        for view in comparison.devices:
            emit_event(
                UiEventName.device_status,
                peer_cid=view.cid,
                held=view.held,
                pending_here=view.pending_here,
                pending_there=view.pending_there,
            )
        for state, path, size, holders in states:
            emit_event(
                UiEventName.file_status,
                state=state,
                file=path,
                size=size,
                holders=holders,
                devices=len(published),
            )

    def _local_listing(self) -> list[dict]:
        now = self._clock()
        if self._local_files is None or now - self._local_at >= self._local_cache_ttl_s:
            self._local_files = self._local_provider()
            self._local_at = now
        return self._local_files
