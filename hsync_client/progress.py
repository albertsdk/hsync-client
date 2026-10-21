# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Throttled transfer-progress reporting.

Full-file and delta transfers share one ``progress`` event
(``UiEventName.progress``) that the Desktop renders in the file panel. Emitting
one event per I/O chunk would flood the events fd on large files, so
:class:`ProgressReporter` coalesces updates to at most one every
``PROGRESS_MIN_INTERVAL`` seconds while always flushing the final update
(``transferred == total``).

``op`` says which side moved the bytes (``download``/``upload``): a download
writes a file that does not exist locally yet, so the Desktop has no row for
it and needs the direction to create one.
"""

from __future__ import annotations

from time import monotonic

from hsync_client.events import UiEventName, emit_event

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
        emit_event(
            UiEventName.progress,
            file=self._path,
            transferred=transferred,
            total=self._total,
            op=self._op,
        )
