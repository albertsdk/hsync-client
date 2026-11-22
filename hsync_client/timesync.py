# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Clock-offset rendezvous for the HSYNC client (ADR-0023).

Resolves the process-wide offset used to compute ``corrected_mtime``:

* **Server mode** — the configured server is the time authority:
  ``measure_clock_offset`` performs an NTP-like HTTP round-trip against
  ``/api/v1/time`` *before* the first sync advertises versions.
* **P2P mode / placeholder URL** — no server is known yet, so a configurable
  list of public SNTP servers (``HSYNC_NTP_HOSTS``, comma-separated; defaults
  from ``hsync_common.constants.NTP_DEFAULT_HOSTS``) is probed **in the
  background** so rendezvous is never blocked. ``sync()`` freezes the offset
  for the duration of a cycle (``ClockOffsetStore.begin_epoch``): a probe
  that finishes mid-cycle applies only to the next cycle.
* **Offline / air-gapped** — every probe fails: the offset is recorded as 0
  and a degraded-temporal-mode warning is logged; synchronisation proceeds
  (the deterministic hash fallback covers unknown corrected mtimes).

The offset is stored process-wide in
``hsync_common.clock.offset_store`` and consumed by ``sync_state`` and the
transfer engine through ``correct_mtime``.

Consumers: ``hsync_client.main_logic`` (server mode),
``hsync_client.p2p_handler`` (P2P mode).
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from hsync_common.clock import (
    measure_clock_offset,
    measure_clock_offset_ntp_multi,
    offset_store,
    parse_ntp_targets,
)
from hsync_common.labels import ActionLabel
from loguru import logger as log

from hsync_client.utils import get_ssl_verify

# Retry floor for :func:`maybe_refresh_clock_offset`: an offline host must not
# spawn one probe thread per maintenance tick.
_PROBE_RETRY_SECONDS = 900.0
_last_probe_monotonic = 0.0

# Generation of the in-flight P2P NTP probe. A newer start_ntp_clock_offset_probe
# call invalidates older workers so a slow probe cannot overwrite a fresher result.
_probe_generation = 0
_probe_lock = threading.Lock()


def _ntp_targets() -> tuple[tuple[str, int], ...]:
    """SNTP targets: ``HSYNC_NTP_HOSTS`` CSV with ``host[:port]`` entries."""
    return parse_ntp_targets(os.environ.get("HSYNC_NTP_HOSTS"))


def probe_server_clock_offset(server_url: str, logger: Any = log) -> None:
    """Measure the offset against the server's ``/api/v1/time`` endpoint.

    On success (or a graceful measurement failure) the offset store is
    updated; a missing/malformed endpoint or a network error degrades to
    offset 0 with a warning — never a failure of the run.
    """
    from hsync_client.utils import http_get

    try:
        result = measure_clock_offset(
            f"{server_url.rstrip('/')}/api/v1/time",
            http_get=http_get,
            verify=get_ssl_verify(),
            timeout=10,
        )
    except Exception as exc:
        logger.warning(f"{ActionLabel.CLOCK_DEGRADED} clock probe failed: {exc}")
        offset_store.record(0.0, trusted=False)
        return
    if result is None:
        logger.warning(
            f"{ActionLabel.CLOCK_DEGRADED} server time endpoint unavailable; "
            "corrected mtimes are unknown and conflicts resolve by content"
        )
        offset_store.record(0.0, trusted=False)
        return
    offset, rtt = result
    offset_store.record(offset, rtt=rtt)
    logger.info(f"{ActionLabel.CLOCK_SYNC} server offset {offset:+.3f}s (±{rtt / 2:.3f}s)")


def _measure_ntp_offset(logger: Any) -> tuple[float, float | None, str | None]:
    """Probe SNTP hosts; return ``(offset, rtt, host)`` (host None when offline)."""
    targets = _ntp_targets()
    try:
        result = measure_clock_offset_ntp_multi(targets)
    except Exception as exc:
        logger.warning(f"{ActionLabel.CLOCK_DEGRADED} NTP probe failed: {exc}")
        result = None
    if result is None:
        logger.warning(
            f"{ActionLabel.CLOCK_DEGRADED} no NTP answer from "
            f"{', '.join(h for h, _ in targets)}; corrected mtimes are unknown "
            "and conflicts resolve by content"
        )
        _mark_probe_attempt()
        return 0.0, None, None
    offset, rtt, host = result
    _mark_probe_attempt()
    return offset, rtt, host


def _mark_probe_attempt() -> None:
    """Record when a probe finished, for the retry floor of the maintenance tick."""
    global _last_probe_monotonic
    with _probe_lock:
        _last_probe_monotonic = time.monotonic()


def maybe_refresh_clock_offset(logger: Any = log) -> None:
    """Re-measure when the offset in force is no longer a reference.

    Called from the maintenance tick. A measurement expires (age) or is revoked
    (a clock step the store observed), and the host silently drops to degraded
    temporal mode; this re-establishes it instead of leaving the mesh without a
    time authority until the next restart. ``_PROBE_RETRY_SECONDS`` bounds the
    retries so an offline host does not spawn a probe per tick.
    """
    if offset_store.is_trusted():
        return
    with _probe_lock:
        since_last = time.monotonic() - _last_probe_monotonic
    if since_last < _PROBE_RETRY_SECONDS:
        return
    logger.info(
        f"{ActionLabel.CLOCK_DEGRADED} offset expired or revoked "
        f"(age {offset_store.age():.0f}s); re-probing"
    )
    start_ntp_clock_offset_probe(logger)


def start_ntp_clock_offset_probe(logger: Any = log) -> threading.Thread:
    """Start the SNTP offset probe on a background daemon thread.

    P2P rendezvous must never wait on an external time source: the probe runs
    concurrently with rendezvous. A newer call invalidates in-flight workers
    (generation token) so a slow probe cannot overwrite a fresher result.
    ``sync()`` freezes the offset for one cycle, so a probe that finishes
    mid-cycle applies only to the next cycle.

    Returns the started thread so callers (and tests) can join it.
    """
    global _probe_generation
    with _probe_lock:
        _probe_generation += 1
        generation = _probe_generation

    def _run() -> None:
        offset, rtt, host = _measure_ntp_offset(logger)
        with _probe_lock:
            if generation != _probe_generation:
                return
            # ``host is None`` means every probe failed: the local clock stays
            # uncorrected and is recorded as untrusted, so the election and
            # the chronological tie-break never treat it as a reference.
            offset_store.record(offset, trusted=host is not None, rtt=rtt)
        if host is not None:
            logger.info(
                f"{ActionLabel.CLOCK_SYNC} {host} offset {offset:+.3f}s (±{(rtt or 0.0) / 2:.3f}s)"
            )

    thread = threading.Thread(target=_run, daemon=True, name="hsync-ntp-probe")
    thread.start()
    return thread
