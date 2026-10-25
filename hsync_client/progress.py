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
"""

from __future__ import annotations

from collections.abc import Callable
from time import monotonic
from typing import Any

from hsync_common.models.sentinel import is_sentinel_file

from hsync_client.events import UiEventName, emit_event, emit_progress

# 0.25 s: the desktop derives the transfer rate from consecutive samples, so a
# coarser tick would smooth away the real curve of short transfers.
PROGRESS_MIN_INTERVAL = 0.25


class ProgressReporter:
    """Emit throttled ``progress`` events for a single in-flight transfer."""

    __slots__ = ("_path", "_total", "_op", "_last_ts")

    def __init__(self, path: str, total: int, op: str) -> None:
        self._path = path
        self._total = total
        self._op = op
        self._last_ts = 0.0

    def update(self, transferred: int) -> None:
        now = monotonic()
        # ``total <= 0`` means the size is unknown (e.g. a malformed delta peer):
        # throttle by time only, never treat it as "finished".
        finished = self._total > 0 and transferred >= self._total
        if not finished and now - self._last_ts < PROGRESS_MIN_INTERVAL:
            return
        self._last_ts = now
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


def progress_reporter(path: str, total: int, op: str) -> ProgressReporter | None:
    """Return a reporter for *path*, or ``None`` when it is an engine sentinel.

    ``.hsynckeep`` files are engine artifacts, never user transfers: the
    Desktop's progress handler has no sentinel filter, so the emitters skip them
    at the source instead of creating a row for them.
    """
    return None if is_sentinel_file(path) else ProgressReporter(path, total, op)


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
    emit_event(UiEventName.transfer, op=op, file=path, size=size)
