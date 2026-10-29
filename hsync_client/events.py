# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Structured UI-event emitter for hsync-client.

A parent process (e.g. the Desktop GUI) passes a writable file descriptor via
``--events-json-fd``; the client then writes one JSON object per line to it
(see :mod:`hsync_common.protocol.events` for the schema). This replaces the
GUI's fragile log-regex parsing with a stable, versioned channel.

The emitter is a process-wide singleton, mirroring the global client-id pattern
in :mod:`hsync_client.logger`, so any module can publish an event through the
module-level :func:`emit_event` without threading an object around. When no fd
is configured (CLI invocation, tests) every call is a cheap no-op, and write
errors are swallowed so the diagnostic channel can never crash a sync.
"""

from __future__ import annotations

import os
import threading
from time import monotonic
from typing import Any, TextIO

from hsync_common.protocol.events import UiEventName, build_event, serialize_event

__all__ = [
    "EventEmitter",
    "UiEventName",
    "emit_event",
    "emit_progress",
    "emit_warning",
    "get_emitter",
    "init_emitter",
    "reset_warning_dedup",
]

# Atomicity unit of a pipe write (POSIX ``PIPE_BUF``): a line no longer than
# this is written all-or-nothing, so concurrent threads never interleave two
# lines without needing a lock.
_PIPE_ATOMIC_WRITE = 4096

# Warnings are episodic by nature (a discovery server that flaps, a watcher that
# degrades and recovers): the structured channel carries at most one entry per
# ``(source, message)`` per window. Without it a flapping condition would turn
# into a notification storm, which is worse than the silence it replaces. The
# engine still logs every occurrence.
_WARNING_DEDUP_WINDOW_S = 600.0
_WARNING_DEDUP_MAX_ENTRIES = 128
_warning_seen: dict[tuple[str, str], float] = {}
_warning_lock = threading.Lock()


class EventEmitter:
    """Thread-safe JSON-Lines writer bound to a single output fd."""

    def __init__(self, fd: int | None = None) -> None:
        self._lock = threading.Lock()
        self._fobj: TextIO | None = None
        # Second handle on the same pipe for the high-frequency ``progress``
        # samples: non-blocking and lock-free (see ``emit_progress``), so a
        # reader that falls behind can never block the threads that serve or
        # move the bytes. Structured events keep the blocking handle below.
        self._progress_fd: int | None = None
        if fd is not None:
            try:
                self._progress_fd = os.dup(fd)
                os.set_blocking(self._progress_fd, False)
            except OSError:
                self._progress_fd = None
            try:
                self._fobj = os.fdopen(fd, "w", encoding="utf-8")
            except OSError:
                self._fobj = None

    @property
    def active(self) -> bool:
        """True when a usable output fd is attached."""
        return self._fobj is not None

    def emit(self, event: UiEventName | str, **payload: Any) -> None:
        """Serialize and write one event line; never raises."""
        if self._fobj is None:
            return
        try:
            line = serialize_event(build_event(event, **payload))
        except (TypeError, ValueError):
            return
        with self._lock:
            if self._fobj is None:
                return
            try:
                self._fobj.write(line + "\n")
                self._fobj.flush()
            except (OSError, ValueError):
                # Broken pipe (parent gone) or closed fd: drop the diagnostic
                # event silently — it must never interrupt the sync itself.
                pass

    def emit_progress(self, event: UiEventName | str, **payload: Any) -> None:
        """Write one best-effort event line without blocking or locking.

        Progress is a *sample* stream, not a state transition: the consumer
        derives rate and ETA from consecutive samples, and the terminal
        ``transfer`` event carries the outcome, so dropping a sample while the
        reader is behind is harmless. The line goes out as a single
        ``os.write`` on the non-blocking handle — atomic for lines up to
        ``PIPE_BUF``, so concurrent threads cannot interleave — which means a
        stalled GUI can never block the threads serving or transferring the
        bytes. Use :meth:`emit` for everything the consumer must never lose.
        """
        fd = self._progress_fd
        if fd is None:
            return
        try:
            line = serialize_event(build_event(event, **payload))
            data = (line + "\n").encode("utf-8")
        except (TypeError, ValueError):
            return
        if len(data) > _PIPE_ATOMIC_WRITE:
            # Splitting the line would tear it for the reader and blocking is
            # exactly what this path must never do: drop the oversized sample.
            return
        try:
            os.write(fd, data)
        except (BlockingIOError, OSError, ValueError):
            # Full pipe (reader behind), closed fd or broken pipe: this sample
            # is expendable and must never interrupt the transfer.
            pass

    def close(self) -> None:
        """Close the underlying fds (idempotent)."""
        with self._lock:
            if self._progress_fd is not None:
                try:
                    os.close(self._progress_fd)
                except OSError:
                    pass
                self._progress_fd = None
            if self._fobj is not None:
                try:
                    self._fobj.close()
                except OSError:
                    pass
                self._fobj = None


# Process-wide singleton. ``None`` means "no events channel configured".
_emitter: EventEmitter | None = None

# Last link status published through :func:`set_link_status` (``None`` before the
# first report), so only transitions reach the consumer.
_link_status: str | None = None


def init_emitter(fd: int | None) -> EventEmitter:
    """Install the process-wide emitter bound to *fd* (replacing any prior one)."""
    global _emitter, _link_status
    if _emitter is not None:
        _emitter.close()
    _emitter = EventEmitter(fd)
    # A fresh channel starts from "nothing published yet".
    _link_status = None
    return _emitter


def get_emitter() -> EventEmitter | None:
    """Return the installed emitter, or ``None`` if no channel is configured."""
    return _emitter


def emit_event(event: UiEventName | str, **payload: Any) -> None:
    """Publish an event through the installed emitter (no-op if none/inactive)."""
    em = _emitter
    if em is not None:
        em.emit(event, **payload)


def emit_progress(event: UiEventName | str, **payload: Any) -> None:
    """Publish a best-effort sample (see :meth:`EventEmitter.emit_progress`)."""
    em = _emitter
    if em is not None:
        em.emit_progress(event, **payload)


def emit_warning(
    message: str,
    *,
    source: str,
    reason: str = "",
    needed: int | None = None,
    limit: int | None = None,
) -> None:
    """Publish a non-fatal warning on the structured channel, deduplicated.

    The engine already logs the same fact (``[WATCHER FALLBACK]``, an
    unreachable discovery server, …): this is its structured counterpart, which a
    front-end surfaces as an in-app banner and — when the window is not focused
    — as a system notification. A non-fatal condition must never be reported as
    ``error``: the run continues.

    One entry per ``(source, message)`` per :data:`_WARNING_DEDUP_WINDOW_S`
    reaches the channel, so a flapping condition cannot become a notification
    storm; the caller keeps logging every occurrence. ``source`` is a stable
    subsystem identifier (never a user-facing sentence) used by the front-end
    for its dedup and notification category, ``reason`` is the case class (or
    ``"transient"`` for a condition that heals itself), and ``needed``/``limit``
    quantify a bounded resource (inotify watches, descriptors) so the consumer
    can localize "needs N, limit M" instead of shipping English prose.

    Consumers: hsync-desktop (``warning_occurred`` → banner + notification).
    """
    key = (source, message)
    now = monotonic()
    with _warning_lock:
        last = _warning_seen.get(key)
        if last is not None and now - last < _WARNING_DEDUP_WINDOW_S:
            return
        if len(_warning_seen) >= _WARNING_DEDUP_MAX_ENTRIES:
            expired = [k for k, ts in _warning_seen.items() if now - ts >= _WARNING_DEDUP_WINDOW_S]
            for stale in expired or [min(_warning_seen, key=_warning_seen.__getitem__)]:
                _warning_seen.pop(stale, None)
        _warning_seen[key] = now
    emit_event(
        UiEventName.warning,
        message=message,
        source=source,
        reason=reason or None,
        needed=needed,
        limit=limit,
    )


def reset_warning_dedup() -> None:
    """Clear the warning dedup window (tests / a new run in one process)."""
    with _warning_lock:
        _warning_seen.clear()


def get_link_status() -> str | None:
    """Return the last published link status (``None`` before the first one)."""
    return _link_status


def set_link_status(status: str) -> None:
    """Publish a *transition* of the link status (``connected``/``waiting``…).

    The status describes the link itself, not the sync activity, and several
    paths can establish the same fact: the event-channel handshake, and a
    successful REST round inside a sync cycle that runs before the event channel
    is up. Consumers act on ``connected`` (the desktop starts a preview), so a
    repeat of the current status is dropped instead of re-publishing it.
    """
    global _link_status
    if status == _link_status:
        return
    _link_status = status
    emit_event(UiEventName.status, status=status)
