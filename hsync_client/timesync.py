# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Clock-offset bootstrap for the HSYNC client (ADR-0023).

Resolves the process-wide offset used to compute ``corrected_mtime`` before
any version is advertised:

* **Server mode** — the configured server is the time authority:
  ``measure_clock_offset`` performs an NTP-like HTTP round-trip against
  ``/api/v1/time``.
* **LAN mode / placeholder URL** — no server is known yet, so a configurable
  list of public SNTP servers (``HSYNC_NTP_HOSTS``, comma-separated; defaults
  from ``hsync_common.constants.NTP_DEFAULT_HOSTS``) is probed in order.
* **Offline / air-gapped** — every probe fails: the offset is recorded as 0
  and a degraded-temporal-mode warning is logged; synchronisation proceeds
  (the deterministic hash fallback covers unknown corrected mtimes).

The offset is stored process-wide in
``hsync_common.clock.offset_store`` and consumed by ``sync_state`` and the
transfer engine through ``correct_mtime``.

Consumers: ``hsync_client.main_logic`` (server mode),
``hsync_client.lan_handler`` (LAN mode).
"""

from __future__ import annotations

import os
from typing import Any

from hsync_common.clock import (
    measure_clock_offset,
    measure_clock_offset_ntp_multi,
    offset_store,
    parse_ntp_targets,
)
from loguru import logger as log

from hsync_client.utils import get_ssl_verify


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
    except Exception as exc:  # noqa: BLE001 — degraded mode, never fatal
        logger.warning(f"Clock offset probe failed (degraded temporal mode): {exc}")
        offset_store.record(0.0, "offline")
        return
    if result is None:
        logger.warning(
            "Server time endpoint unavailable; running in degraded temporal mode "
            "(uncorrected local time)"
        )
        offset_store.record(0.0, "offline")
        return
    offset, _rtt = result
    offset_store.record(offset, server_url)
    logger.debug(f"Clock offset from server: {offset:+.3f}s")


def probe_ntp_clock_offset(logger: Any = log) -> None:
    """Measure the offset against the configured public SNTP servers.

    Probes every host in order until one answers (median of several
    round-trips). Offline/air-gapped ⇒ offset 0 + degraded-temporal-mode
    warning; synchronisation is never blocked.
    """
    targets = _ntp_targets()
    try:
        result = measure_clock_offset_ntp_multi(targets)
    except Exception as exc:  # noqa: BLE001 — degraded mode, never fatal
        logger.warning(f"NTP offset probe failed (degraded temporal mode): {exc}")
        result = None
    if result is None:
        logger.warning(
            f"No NTP answer from {', '.join(h for h, _ in targets)}; running "
            "in degraded temporal mode (offset 0, uncorrected local time)"
        )
        offset_store.record(0.0, "offline")
        return
    offset, _rtt, host = result
    offset_store.record(offset, f"ntp:{host}")
    logger.debug(f"Clock offset from {host}: {offset:+.3f}s")
