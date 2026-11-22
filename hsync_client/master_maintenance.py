# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Periodic local reconciliation for the device that serves the share (p2p master).

The serving role holds the reference copy of the share, so it has no peer to
compare against and runs no sync cycle. What a cycle keeps true on a device — the
empty-directory sentinels, a deletion marker for every tracked file the disk no
longer has, no leftover transfer artifact — the watcher maintains there instead,
and a **missed** watcher event leaves that state stale with nothing to repair it:
a peer then computes an empty-folder verdict from a stale sentinel, or hands a
deleted file back to the device it was deleted on.

This pass replays those invariants on the share itself, every
``CONFIG["RESYNC"]`` seconds, and never talks to a peer: no listing comparison, no
transfer, no remote marker fetch.

Consumers: ``hsync_client.p2p_handler._run_as_master`` starts the pass with the
serving stint; ``hsync_client.p2p_serving._stop_p2p_serving`` stops it when the
role is retired.
"""

from __future__ import annotations

import threading

from loguru import logger as log

from hsync_client.config import CONFIG
from hsync_client.timesync import maybe_refresh_clock_offset

__all__ = ["local_reconcile", "start_master_maintenance", "stop_master_maintenance"]

_THREAD_NAME = "hsync-master-maintenance"
# Bounded so a wedged pass (an unreachable NAS mount, a stalled walk) can never
# hold the role transition.
_STOP_JOIN_TIMEOUT_S = 5.0

_lock = threading.Lock()
_thread: threading.Thread | None = None
# Owned by the running stint: a fresh event per start, so a stop that timed out
# (a wedged pass) can never be un-set by the next stint and resurrect its thread.
_stop_event: threading.Event | None = None
# Failure-episode flag: the first failure of an episode carries the traceback,
# the repeats stay at DEBUG (one pass per interval would otherwise flood the log).
_failure_reported = False


def local_reconcile() -> None:
    """Re-establish the local invariants a sync cycle maintains.

    Runs on the serving share only: the reference copy is its own authority, so
    there is nothing to compare and nothing to transfer.
    """
    from hsync_client.sync import (
        _cleanup_partial_files,
        _retain_merge_bases,
        ensure_hsynckeep_files,
        mint_missing_base_deletions,
    )

    _cleanup_partial_files()
    existing_paths, _minted = mint_missing_base_deletions()
    # The ignore-agnostic snapshot is the retention input: every path still on
    # disk keeps its merge base and a path that is gone drops it. Passing it as
    # the path set (no listing) keeps the pass to the walks it already pays for.
    _retain_merge_bases([], existing_paths)
    ensure_hsynckeep_files()


def _loop(interval: float, stop_event: threading.Event) -> None:
    """Run one pass immediately, then one per interval until stopped.

    The immediate pass is the point: a deletion made while the role was down (or
    by a writer that never reached the watcher) has no marker yet, and the peers
    would resurrect it before the first interval elapsed.
    """
    global _failure_reported
    while not stop_event.is_set():
        try:
            # The serving node is the mesh's time authority: a measurement
            # that expired, or a clock step that revoked it, is re-established
            # here rather than only at process start (ADR-0055).
            maybe_refresh_clock_offset(log)
            local_reconcile()
        except Exception:  # a maintenance pass must never kill the role
            with _lock:
                repeated = _failure_reported
                _failure_reported = True
            if repeated:
                log.opt(exception=True).debug("Master maintenance pass failed again")
            else:
                log.exception("Master maintenance pass failed")
        else:
            with _lock:
                _failure_reported = False
        if stop_event.wait(timeout=interval):
            return


def start_master_maintenance(interval: float | None = None) -> None:
    """Start the local pass for the serving stint (idempotent).

    ``interval`` defaults to ``CONFIG["RESYNC"]``: the same safety-net knob the
    peers honour, so one setting switches local repair off everywhere.
    """
    global _failure_reported, _stop_event, _thread

    if interval is None:
        interval = float(CONFIG.get("RESYNC") or 0)
    if interval <= 0:
        log.debug("Master maintenance disabled (periodic resync off)")
        return
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _failure_reported = False
        _stop_event = threading.Event()
        _thread = threading.Thread(
            target=_loop,
            args=(interval, _stop_event),
            name=_THREAD_NAME,
            daemon=True,
        )
        _thread.start()


def stop_master_maintenance() -> None:
    """Stop the local pass (idempotent)."""
    global _stop_event, _thread

    with _lock:
        thread, _thread = _thread, None
        stop_event, _stop_event = _stop_event, None
    if stop_event is not None:
        stop_event.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout=_STOP_JOIN_TIMEOUT_S)
