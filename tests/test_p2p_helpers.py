# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Timing contract of the P2P black-box harness waits.

Every wait must measure its deadline with ``time.monotonic()``: a wall-clock
step (suspend/resume, NTP correction) would otherwise expire the deadline on
the next iteration and report a false timeout for a peer that is still healthy.
The tests below simulate that step while the awaited condition is about to
become true, so a wall-clock implementation fails them deterministically.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from .p2p_helpers import _wait_for_file, _wait_for_file_gone, _wait_for_text_in_stderr

# Far beyond any timeout used by the harness: a single wall-clock reading that
# jumps by this much expires every pending wall-clock deadline.
_CLOCK_JUMP_S = 7200.0


class _JumpingWallClock:
    """``time.time`` replacement that steps forward after its first reading.

    The first call returns the real time (so the deadline is computed from a
    sane base); every later call is ``_CLOCK_JUMP_S`` ahead, exactly what a
    suspend/resume cycle looks like to a wall-clock deadline. The real function
    is captured at construction time, before the patch is installed.
    """

    def __init__(self) -> None:
        self._real = time.time
        self._calls = 0
        self._base = 0.0

    def __call__(self) -> float:
        self._calls += 1
        if self._calls == 1:
            self._base = self._real()
            return self._base
        return self._base + _CLOCK_JUMP_S


class _FakePeer:
    """Minimal stand-in for a subprocess with a log file (``_stderr_text``)."""

    def __init__(self, log_path: Path) -> None:
        self._hsync_log_path = str(log_path)
        self.stdout = None

    def poll(self):
        return None


def test_file_wait_survives_a_wall_clock_jump(tmp_path, monkeypatch):
    """A clock step during the wait must not abort it before the file lands."""
    target = tmp_path / "late.txt"
    monkeypatch.setattr(time, "time", _JumpingWallClock())
    timer = threading.Timer(0.2, target.write_bytes, args=(b"x",))
    timer.start()
    try:
        assert _wait_for_file(str(target), timeout=10), "the wait aborted on the clock step"
    finally:
        timer.cancel()


def test_text_wait_survives_a_wall_clock_jump(tmp_path, monkeypatch):
    """The same contract holds for the peer-log marker wait."""
    log_path = tmp_path / "peer.log"
    log_path.write_text("booting\n", encoding="utf-8")
    peer = _FakePeer(log_path)
    monkeypatch.setattr(time, "time", _JumpingWallClock())

    def _append_marker() -> None:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("[READY] Client ready for sync\n")

    timer = threading.Timer(0.2, _append_marker)
    timer.start()
    try:
        assert _wait_for_text_in_stderr(peer, "[READY]", timeout=10), (
            "the marker wait aborted on the clock step"
        )
    finally:
        timer.cancel()


def test_file_gone_wait_survives_a_wall_clock_jump(tmp_path, monkeypatch):
    """An already-satisfied condition is still reported after a clock step."""
    missing = tmp_path / "already-gone.txt"
    monkeypatch.setattr(time, "time", _JumpingWallClock())
    assert _wait_for_file_gone(str(missing), timeout=10), (
        "an already satisfied wait reported a timeout"
    )
