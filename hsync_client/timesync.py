# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Clock-offset bootstrap for the HSYNC client (ADR-0023).

Resolves the process-wide offset used to compute ``corrected_mtime``:

* **Server mode** — the configured server is the time authority:
  ``measure_clock_offset`` performs an NTP-like HTTP round-trip against
  ``/api/v1/time`` *before* the first sync advertises versions.
* **P2P mode / placeholder URL** — no server is known yet, so a configurable
  list of public SNTP servers (``HSYNC_NTP_HOSTS``, comma-separated; defaults
  from ``hsync_common.constants.NTP_DEFAULT_HOSTS``) is probed **in the
  background** so discovery is never blocked. ``sync()`` freezes the offset
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
from typing import Any

from hsync_common.clock import (
    measure_clock_offset,
    measure_clock_offset_ntp_multi,
    offset_store,
    parse_ntp_targets,
)
from loguru import logger as log

from hsync_client.utils import get_ssl_verify

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
        logger.warning(f"Clock offset probe failed (degraded temporal mode): {exc}")
        offset_store.record(0.0)
        return
    if result is None:
        logger.warning(
            "Server time endpoint unavailable; running in degraded temporal mode "
            "(uncorrected local time)"
        )
        offset_store.record(0.0)
        return
    offset, _rtt = result
    offset_store.record(offset)
    logger.debug(f"Clock offset from server: {offset:+.3f}s")


def _measure_ntp_offset(logger: Any) -> tuple[float, str | None]:
    """Probe SNTP hosts; return ``(offset, host)`` (host is None when offline)."""
    targets = _ntp_targets()
    try:
        result = measure_clock_offset_ntp_multi(targets)
    except Exception as exc:
        logger.warning(f"NTP offset probe failed (degraded temporal mode): {exc}")
        result = None
    if result is None:
        logger.warning(
            f"No NTP answer from {', '.join(h for h, _ in targets)}; running "
            "in degraded temporal mode (offset 0, uncorrected local time)"
        )
        return 0.0, None
    offset, _rtt, host = result
    return offset, host


def start_ntp_clock_offset_probe(logger: Any = log) -> threading.Thread:
    """Start the SNTP offset probe on a background daemon thread.

    P2P discovery must never wait on an external time source: the probe runs
    concurrently with discovery. A newer call invalidates in-flight workers
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
        offset, host = _measure_ntp_offset(logger)
        with _probe_lock:
            if generation != _probe_generation:
                return
            offset_store.record(offset)
        if host is not None:
            logger.debug(f"Clock offset from {host}: {offset:+.3f}s")

    thread = threading.Thread(target=_run, daemon=True, name="hsync-ntp-probe")
    thread.start()
    return thread
