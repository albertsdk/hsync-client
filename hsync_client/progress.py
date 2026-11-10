# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Throttled transfer-progress reporting and the terminal transfer event.

Full-file and delta transfers share one ``progress`` event
(``UiEventName.progress``) that the Desktop renders in the file panel. Emitting
one event per I/O chunk would flood the events fd on large files, so
:class:`ProgressReporter` coalesces updates to at most one every
``PROGRESS_MIN_INTERVAL`` seconds while always flushing the final update
(``transferred == total``).

``op`` says which side moved the bytes (``download``/``upload``), from *this*
client's perspective: bytes that arrived are a download (the Desktop has no row
for the file yet and needs the direction to create one), bytes that left are an
upload. A P2P master serving a peer therefore reports ``upload``, and a master
receiving a peer's upload reports ``download``.

Every committed transfer ends with exactly one ``transfer`` event
(:func:`emit_completed_transfer`) carrying the **file** size: the Desktop closes
the row and counts the file and its bytes only from that event, so every path
that moves bytes must publish it — progress samples alone never finalize a row.
An aborted transfer ends with exactly one ``transfer_failed`` event instead
(:func:`emit_failed_transfer`), for the same reason: without a terminal event the
row keeps its last progress sample forever.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from time import monotonic
from typing import Any

from hsync_common.models.sentinel import is_sentinel_file

from hsync_client.events import UiEventName, emit_event, emit_progress

# 0.25 s: the desktop derives the transfer rate from consecutive samples, so a
# coarser tick would smooth away the real curve of short transfers.
PROGRESS_MIN_INTERVAL = 0.25

# Paths whose *delivery* progress the receiver is publishing. A sender counts
# the bytes it handed to its transport, the receiver counts the bytes it stored:
# two different totals, so once the receiver reports on a path the sender's own
# counter must stop feeding that row (they would fight over its scale) until the
# transfer ends and the path is forgotten.
_delivery_paths: set[str] = set()
_delivery_lock = threading.Lock()


def note_delivery_progress(path: str) -> None:
    """Mark *path* as reported by the receiver (``file_progress`` frame)."""
    with _delivery_lock:
        _delivery_paths.add(path)


def delivery_progress_active(path: str) -> bool:
    """True when the receiver is publishing the progress of *path*.

    Both the native and the wire form are checked: the receiver addresses a file
    by its bucket-relative ``/``-separated path, the sender knows it as a local
    absolute path.
    """
    with _delivery_lock:
        return path in _delivery_paths or path.replace("\\", "/") in _delivery_paths


def forget_delivery_progress(path: str) -> None:
    """Forget the delivery mark of *path*, unless a serve of it is in flight.

    A serve owns its row's mark until it ends: the terminal ``transfer`` event is
    emitted before ``stop_serving``, so clearing the mark there would re-arm the
    wire reporter of a second concurrent serve of the same file and mix scales.
    The mark is discarded by ``stop_serving`` when the last serve ends instead.
    """
    norm = path.replace("\\", "/")
    with _serving_lock:
        for (rel, _cid), (abs_path, _count) in _serving.items():
            if path in (abs_path, rel) or norm in (abs_path.replace("\\", "/"), rel):
                return
    with _delivery_lock:
        _delivery_paths.discard(path)


# Paths the master is serving right now, keyed by ``(rel, cid)`` — the validated
# bucket-relative path and the requesting peer — so an echo is only applied to
# the serve it belongs to (two peers can download the same file concurrently, and
# a seeder-served peer's frame must never land on the master's own serve). The
# value is ``(absolute path the serve reporter uses, active-serve count)``.
_serving: dict[tuple[str, str], tuple[str, int]] = {}
_serving_lock = threading.Lock()


def note_serving(rel: str, abs_path: str, cid: str) -> None:
    """Record one serve of *rel* for *cid* from *abs_path*."""
    key = (rel, cid)
    with _serving_lock:
        path, count = _serving.get(key, (abs_path, 0))
        _serving[key] = (path, count + 1)


def mark_delivery_if_serving(rel: str, cid: str) -> bool:
    """Mark a serve's delivery for the receiver, atomically with the gate.

    Done under ``_serving_lock`` so an echo cannot pass the "is it served" check
    and then re-mark a path whose serve has already torn down (a stale mark that
    would silence a later serve). Returns ``False`` when no such serve is active.
    """
    key = (rel, cid)
    with _serving_lock:
        entry = _serving.get(key)
        if entry is None:
            return False
        abs_path = entry[0]
        with _delivery_lock:
            _delivery_paths.add(abs_path)
            _delivery_paths.add(rel)
    return True


def stop_serving(rel: str, cid: str) -> None:
    """End one serve of *rel* for *cid*; clear its marks when the last one ends.

    The delivery marks are path-keyed and shared by every concurrent serve of the
    same file, so they are discarded only when no serve of *rel* remains.
    """
    key = (rel, cid)
    with _serving_lock:
        entry = _serving.get(key)
        if entry is None:
            return
        abs_path, count = entry
        if count > 1:
            _serving[key] = (abs_path, count - 1)
            return
        del _serving[key]
        if any(other[0] == rel for other in _serving):
            return
        with _delivery_lock:
            _delivery_paths.discard(abs_path)
            _delivery_paths.discard(rel)


class ProgressReporter:
    """Emit throttled ``progress`` events for a single in-flight transfer."""

    __slots__ = ("_path", "_total", "_op", "_last_ts", "_echo", "_delivery_only")

    def __init__(
        self,
        path: str,
        total: int,
        op: str,
        *,
        echo: Callable[[int], None] | None = None,
        delivery_only: bool = False,
    ) -> None:
        self._path = path
        self._total = total
        self._op = op
        self._last_ts = 0.0
        # Receiver-side report of the same sample: the master publishing the
        # bytes it stored for a peer's upload, so the sender's row shows the
        # delivery instead of its own transport counter.
        self._echo = echo
        # The receiver reports this transfer and measures a different quantity
        # (the master stores decompressed bytes, the sender counts the compressed
        # payload), so the local counter stays silent: the row keeps one scale
        # instead of switching mid-transfer.
        self._delivery_only = delivery_only

    def update(self, transferred: int) -> None:
        now = monotonic()
        # ``total <= 0`` means the size is unknown (e.g. a malformed delta peer):
        # throttle by time only, never treat it as "finished".
        finished = self._total > 0 and transferred >= self._total
        if not finished and now - self._last_ts < PROGRESS_MIN_INTERVAL:
            return
        self._last_ts = now
        if self._echo is not None:
            self._echo(transferred)
        if self._delivery_only:
            return
        if self._op == "upload" and delivery_progress_active(self._path):
            # The receiver is the authority on this path: its samples carry the
            # file total, this counter the wire one.
            return
        # Best-effort path: a slow consumer drops samples instead of blocking
        # the thread that moves the bytes.
        emit_progress(
            UiEventName.progress,
            file=self._path,
            transferred=transferred,
            total=self._total,
            op=self._op,
        )


class ProgressWriter:
    """Binary sink wrapper that feeds a reporter with the bytes it accepted."""

    __slots__ = ("_write", "_reporter", "_total")

    def __init__(
        self,
        write: Callable[[bytes], Any],
        reporter: ProgressReporter | None,
        *,
        start: int = 0,
    ) -> None:
        self._write = write
        self._reporter = reporter
        # A resumed transfer starts from the bytes already on disk, so the
        # reported position keeps describing the whole file.
        self._total = start

    def write(self, data: bytes) -> Any:
        written = self._write(data)
        if self._reporter is not None:
            self._total += len(data) if written is None else int(written)
            self._reporter.update(self._total)
        return written

    def close(self) -> None:
        """No-op: the underlying file is owned and closed by the caller."""


def progress_reporter(
    path: str, total: int, op: str, *, echo: Callable[[int], None] | None = None
) -> ProgressReporter | None:
    """Return a reporter for *path*, or ``None`` when it is an engine sentinel.

    ``.hsynckeep`` files are engine artifacts, never user transfers: the
    Desktop's progress handler has no sentinel filter, so the emitters skip them
    at the source instead of creating a row for them. ``echo`` is the
    receiver-side report of the same sample (see :class:`ProgressReporter`).
    """
    return None if is_sentinel_file(path) else ProgressReporter(path, total, op, echo=echo)


def emit_completed_transfer(op: str, path: str, size: int) -> None:
    """Publish the terminal ``transfer`` event of a committed transfer.

    ``size`` is the **file** size, not the wire size: the Desktop uses it to
    label the row and the byte counter, and it must match what the counterpart
    reports for the same file (``transfer.py::_finalize_download`` follows the
    same rule for delta downloads).

    The event is lossless by contract (``emit_event``, not the best-effort
    progress path): dropping it would leave the row open and the counters short.
    """
    if not path or is_sentinel_file(path):
        return
    forget_delivery_progress(path)
    emit_event(UiEventName.transfer, op=op, file=path, size=size)


def emit_failed_transfer(op: str, path: str, message: str) -> None:
    """Publish the terminal ``transfer_failed`` event of an aborted transfer.

    The counterpart of :func:`emit_completed_transfer`: a path that started
    moving bytes and then gave up must publish exactly one of the two. An
    in-flight row is ended by a terminal event only — a lost one leaves the bar
    at its last sample, so the Desktop asks the engine for a status refresh when
    a row stops reporting (`hsync-desktop` file panel).
    """
    if not path or is_sentinel_file(path):
        return
    forget_delivery_progress(path)
    emit_event(UiEventName.transfer_failed, op=op, file=path, message=message)
