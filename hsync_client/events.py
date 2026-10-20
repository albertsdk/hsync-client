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
from typing import Any, TextIO

from hsync_common.protocol.events import UiEventName, build_event, serialize_event

__all__ = ["EventEmitter", "UiEventName", "emit_event", "get_emitter", "init_emitter"]


class EventEmitter:
    """Thread-safe JSON-Lines writer bound to a single output fd."""

    def __init__(self, fd: int | None = None) -> None:
        self._lock = threading.Lock()
        self._fobj: TextIO | None = None
        if fd is not None:
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

    def close(self) -> None:
        """Close the underlying fd (idempotent)."""
        with self._lock:
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
