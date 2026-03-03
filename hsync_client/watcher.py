# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""File-watcher lifecycle with graceful degradation between inotify and polling.

The client watches the local bucket with watchdog. On Linux the default backend
is inotify, which is subject to two per-user kernel limits
(``fs.inotify.max_user_instances``, ``fs.inotify.max_user_watches``) plus the
generic file-descriptor limit (``RLIMIT_NOFILE``). This module centralises the
watcher lifecycle so that:

* a limit hit at startup downgrades the watcher to a :class:`PollingObserver`
  (which needs no inotify resources) instead of aborting realtime sync;
* a limit hit while running (adding a watch for a newly created directory) is
  detected inline, the single watch is skipped (periodic RESYNC reconciles it),
  and the downgrade to polling is performed at a safe point by a supervisor
  thread — the watchdog event-dispatch thread is never mutated mid-flight;
* while degraded, the supervisor periodically probes whether inotify is
  available again and restores it transparently;
* a backend swap is adopted only while the manager is still running: an
  observer built while ``stop()`` lands mid-build is torn down instead, so no
  live watcher can outlive ``stop()``.

The manager records every scheduled ``(handler, path, recursive)`` triple so a
backend swap (downgrade or upgrade) can replay the full watch set onto a fresh
observer without introspecting watchdog internals.
"""

from __future__ import annotations

import contextlib
import errno
import threading
from enum import StrEnum
from typing import Any

from loguru import logger as log

from hsync_client.constants import (
    WATCHER_DOWNGRADE_DELAY_S,
    WATCHER_UPGRADE_BACKOFF_FACTOR,
    WATCHER_UPGRADE_PROBE_INITIAL_S,
    WATCHER_UPGRADE_PROBE_MAX_S,
    ActionLabel,
)
from hsync_client.exceptions import WatcherStartError
from hsync_client.utils import warn_if_inotify_watch_limit_low, warn_if_open_fds_high


class WatcherLimit(StrEnum):
    """The resource limit a watchdog ``OSError`` was classified as."""

    INOTIFY_INSTANCES = "inotify_instances"
    INOTIFY_WATCHES = "inotify_watches"
    FD_EXHAUSTION = "fd_exhaustion"


_WATCHER_LIMIT_HINTS: dict[WatcherLimit, str] = {
    WatcherLimit.INOTIFY_INSTANCES: (
        "inotify instance limit reached (fs.inotify.max_user_instances): too many "
        "watchers are running on this machine. Raise the limit (e.g. "
        "`sudo sysctl fs.inotify.max_user_instances=512`) or stop other "
        "watcher-heavy applications."
    ),
    WatcherLimit.INOTIFY_WATCHES: (
        "inotify watch limit reached (fs.inotify.max_user_watches): the watched "
        "tree needs one inotify watch per sub-directory and the per-user limit "
        "is exhausted. Raise the limit (e.g. "
        "`sudo sysctl fs.inotify.max_user_watches=1048576`)."
    ),
    WatcherLimit.FD_EXHAUSTION: (
        "file-descriptor limit reached (RLIMIT_NOFILE / fs.file-max): the process "
        "has too many open file descriptors. Raise the limit (e.g. `ulimit -n 4096`) "
        "or reduce concurrent connections/transfers."
    ),
}


def watcher_oserror_class(exc: BaseException) -> WatcherLimit | None:
    """Classify a watchdog ``OSError`` into a :class:`WatcherLimit`, or ``None``.

    watchdog's inotify backend rewrites the two inotify-limit failures as
    ``OSError(errno.EMFILE, "inotify instance limit reached")`` and
    ``OSError(errno.ENOSPC, "inotify watch limit reached")``; a generic
    descriptor exhaustion surfaces as a plain ``EMFILE``/``ENFILE`` without the
    ``inotify`` marker. Anything else is ``None`` so the caller keeps its
    generic handling.
    """

    if not isinstance(exc, OSError):
        return None
    detail = str(exc).lower()
    if exc.errno == errno.EMFILE and "inotify" in detail:
        return WatcherLimit.INOTIFY_INSTANCES
    if exc.errno == errno.ENOSPC and "inotify" in detail:
        return WatcherLimit.INOTIFY_WATCHES
    if exc.errno in (errno.EMFILE, errno.ENFILE):
        return WatcherLimit.FD_EXHAUSTION
    return None


class WatcherManager:
    """Owns the active watchdog observer and its backend (inotify vs polling)."""

    def __init__(self, watch_path: str, *, recursive: bool = True) -> None:
        self._watch_path = watch_path
        self._recursive = recursive
        self._lock = threading.RLock()
        # Ordered (handler, path, recursive) registrations; the replay source for
        # every backend swap. Snapshot under _lock when a swap reads it.
        self._registrations: list[tuple[Any, str, bool]] = []
        self._observer: Any | None = None
        self._backend: str = "inotify"  # "inotify" | "polling"
        self._pending_downgrade = False
        self._probe_interval = WATCHER_UPGRADE_PROBE_INITIAL_S
        self._supervisor: threading.Timer | None = None
        self._stopped = False

    # -- public API ----------------------------------------------------------

    def start(self, handler: Any) -> None:
        """Register the root watch and start the best available backend."""
        with self._lock:
            self._registrations.append((handler, self._watch_path, self._recursive))

        warn_if_inotify_watch_limit_low(self._watch_path)
        warn_if_open_fds_high()

        try:
            observer = self._build_and_start("inotify")
            with self._lock:
                self._observer = observer
                self._backend = "inotify"
            # The observer starts inside _build_and_start, so a
            # schedule_directory() arrival before the assignment only gets
            # recorded: it is replayed on the next backend swap and periodic
            # RESYNC reconciles the gap.
            return
        except OSError as exc:
            kind = watcher_oserror_class(exc)
            if kind is None:
                raise
            original = exc

        # Downgrade to polling: it needs no inotify resources and also works
        # under descriptor pressure (scandir opens/closes directory FDs
        # transiently instead of holding them).
        try:
            observer = self._build_and_start("polling")
            with self._lock:
                self._observer = observer
                self._backend = "polling"
        except OSError as poll_exc:
            log.error(
                f"{ActionLabel.WATCHER_ERROR} cannot start the file watcher for "
                f"'{self._watch_path}': {_WATCHER_LIMIT_HINTS[kind]}"
            )
            raise WatcherStartError(_WATCHER_LIMIT_HINTS[kind], original=original) from poll_exc

        self._log_degraded(kind)
        self._schedule_supervisor(self._probe_interval)

    def schedule_directory(self, handler: Any, path: str) -> None:
        """Watch a newly created directory, degrading gracefully on a limit.

        On an inotify limit the new directory's realtime watch is skipped (its
        contents are reconciled by periodic RESYNC) and a full downgrade to
        polling is scheduled at a safe point. A non-watcher ``OSError``
        propagates unchanged.
        """
        with self._lock:
            self._registrations.append((handler, path, True))
            observer = self._observer
        if observer is None:
            # No observer adopted yet (startup window): the registration stays
            # recorded and is replayed on the next backend swap.
            return
        # NOTE: this call can race a concurrent supervisor swap (the old
        # observer is replaced, then stopped): a schedule landing on the
        # dying observer leaves a shadow watch whose events are lost — the
        # registration recorded above is replayed on the next swap and
        # periodic RESYNC reconciles any gap.
        try:
            observer.schedule(handler, path, recursive=True)
        except OSError as exc:
            kind = watcher_oserror_class(exc)
            if kind is None:
                raise
            label = (
                ActionLabel.FD_EXHAUSTED
                if kind is WatcherLimit.FD_EXHAUSTION
                else ActionLabel.WATCHER_FALLBACK
            )
            log.warning(
                f"{label} cannot watch new directory '{path}' in realtime: "
                f"{_WATCHER_LIMIT_HINTS[kind]} Periodic resync (RESYNC) will "
                f"reconcile it. Switching the whole watcher to polling."
            )
            self._request_downgrade()

    def stop(self) -> None:
        """Stop the observer and the supervisor (idempotent)."""
        with self._lock:
            self._stopped = True
            supervisor = self._supervisor
            self._supervisor = None
            observer = self._observer
            self._observer = None
        if supervisor is not None:
            supervisor.cancel()
        if observer is not None:
            self._safe_stop(observer)

    @staticmethod
    def _safe_stop(observer: Any) -> None:
        """Stop and join *observer*, swallowing teardown errors (idempotent)."""
        with contextlib.suppress(Exception):
            observer.stop()
            observer.join(timeout=2.0)

    # -- observer building ---------------------------------------------------

    def _build_and_start(self, backend: str) -> Any:
        observer = self._new_observer(backend)
        with self._lock:
            registrations = list(self._registrations)
        for handler, path, recursive in registrations:
            observer.schedule(handler, path, recursive=recursive)
        observer.start()
        return observer

    @staticmethod
    def _new_observer(backend: str) -> Any:
        if backend == "inotify":
            from watchdog.observers import Observer

            return Observer()
        from watchdog.observers.polling import PollingObserver

        return PollingObserver()

    # -- supervisor ----------------------------------------------------------

    def _request_downgrade(self) -> None:
        with self._lock:
            self._pending_downgrade = True
        self._schedule_supervisor(WATCHER_DOWNGRADE_DELAY_S)

    def _schedule_supervisor(self, delay: float) -> None:
        with self._lock:
            if self._stopped:
                return
            if self._supervisor is not None:
                self._supervisor.cancel()
            self._supervisor = threading.Timer(delay, self._supervise)
            self._supervisor.daemon = True
            self._supervisor.start()

    def _supervise(self) -> None:
        """One supervisor tick: perform a pending downgrade, else probe upgrade."""
        downgraded = False
        try:
            if self._backend == "inotify" and self._pending_downgrade:
                self._downgrade()
                downgraded = True
            if not downgraded and self._backend == "polling":
                self._try_upgrade()
        finally:
            with self._lock:
                active = (self._backend == "polling") or self._pending_downgrade
                stopped = self._stopped
            if active and not stopped:
                self._schedule_supervisor(self._probe_interval)

    def _grow_probe_interval(self) -> None:
        """Grow the probe/retry cadence, capped at the maximum interval.

        Supervisor-thread only: applied to both failed upgrade probes and
        failed downgrade retries so a permanently exhausted machine never
        logs or retries at a fixed rate.
        """
        self._probe_interval = min(
            self._probe_interval * WATCHER_UPGRADE_BACKOFF_FACTOR,
            WATCHER_UPGRADE_PROBE_MAX_S,
        )

    def _downgrade(self) -> None:
        """Swap the running inotify observer for a polling one (safe point)."""
        old = self._observer
        try:
            fallback = self._build_and_start("polling")
        except OSError as exc:
            # Polling could not start either (e.g. true descriptor exhaustion):
            # keep the running inotify observer and retry via the supervisor
            # at the next (grown) cadence.
            log.error(
                f"{ActionLabel.WATCHER_ERROR} could not start the polling fallback "
                f"for '{self._watch_path}': {exc}"
            )
            self._grow_probe_interval()
            return
        with self._lock:
            if self._stopped:
                # stop() completed while the fallback was being built (the
                # build walks the whole tree and takes real time): tear the
                # fresh observer down instead of resurrecting a watcher
                # nobody owns anymore.
                self._safe_stop(fallback)
                return
            self._observer = fallback
            self._backend = "polling"
            self._pending_downgrade = False
            self._probe_interval = WATCHER_UPGRADE_PROBE_INITIAL_S
        if old is not None:
            self._safe_stop(old)
        log.warning(
            f"{ActionLabel.WATCHER_FALLBACK} file watcher switched to PollingObserver "
            f"for '{self._watch_path}' (higher CPU usage, up to ~1 s event latency)."
        )

    def _try_upgrade(self) -> None:
        """Probe whether inotify is available again and, if so, restore it."""
        old = self._observer
        try:
            candidate = self._build_and_start("inotify")
        except OSError as exc:
            log.debug(f"inotify upgrade probe failed (limits still exhausted): {exc}")
            self._grow_probe_interval()
            return
        with self._lock:
            if self._stopped:
                # stop() completed while the candidate was being built: never
                # adopt a live observer after a stop.
                self._safe_stop(candidate)
                return
            self._observer = candidate
            self._backend = "inotify"
            # A downgrade request that landed while polling is stale: the
            # fresh inotify build proves the limits have headroom again.
            self._pending_downgrade = False
            self._probe_interval = WATCHER_UPGRADE_PROBE_INITIAL_S
        if old is not None:
            self._safe_stop(old)
        log.warning(
            f"{ActionLabel.WATCHER_RESTORE} file watcher restored to inotify for "
            f"'{self._watch_path}'."
        )

    # -- logging -------------------------------------------------------------

    def _log_degraded(self, kind: WatcherLimit) -> None:
        label = (
            ActionLabel.FD_EXHAUSTED
            if kind is WatcherLimit.FD_EXHAUSTION
            else ActionLabel.WATCHER_FALLBACK
        )
        log.warning(
            f"{label} {_WATCHER_LIMIT_HINTS[kind]} Switching to PollingObserver for "
            f"'{self._watch_path}' (higher CPU usage, up to ~1 s event latency)."
        )
