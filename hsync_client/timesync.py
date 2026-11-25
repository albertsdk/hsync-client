# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Clock-offset rendezvous for the HSYNC client (ADR-0023 / ADR-0055).

Resolves the process-wide offset used to compute ``corrected_mtime`` through one
trust chain:

* **Server mode / P2P slave** — the current authority's ``/api/v1/time`` endpoint
  is measured with an NTP-like HTTP round-trip: the sync server in client-server
  mode, the elected master in p2p mode (``CONFIG["MASTER_CERT_FINGERPRINT"]`` is
  set there). The server is authoritative by definition; a master must vouch for
  its own clock with ``trusted: true`` in the reply, so an unanchored master never
  propagates a clock the mesh cannot order by.
* **Explicit NTP (opt-in)** — ``HSYNC_NTP_HOSTS`` is probed only when set: it is a
  deployment override (air-gapped/enterprise), never a silent public-pool default.
* **Rendezvous echo** — ``ack.server_time`` establishes a coarse (one-second)
  trust for a host that reaches a rendezvous but not a measured reference.
* **Offline / air-gapped** — nothing records a trusted offset: the deterministic
  hash fallback covers unknown corrected mtimes.

The offset is stored process-wide in ``hsync_common.clock.offset_store`` and
consumed by ``sync_state`` and the transfer engine through ``correct_mtime``. It
expires (``CLOCK_OFFSET_MAX_AGE_SECONDS``) and is re-probed from the sync cycle or
the maintenance tick, so a long-lived process never silently degrades.

Consumers: ``hsync_client.main_logic`` (server mode),
``hsync_client.p2p_handler`` (P2P mode), ``hsync_client.sync`` (per-cycle refresh),
``hsync_client.master_maintenance`` (serving node).
"""

from __future__ import annotations

import math
import os
import threading
import time
from typing import Any

from hsync_common.clock import (
    compute_clock_offset,
    measure_clock_offset_ntp_multi,
    offset_store,
    parse_ntp_targets,
)
from hsync_common.labels import ActionLabel
from hsync_common.time_utils import parse_iso_seconds
from loguru import logger as log

from hsync_client.utils import get_ssl_verify

# Retry floor for :func:`maybe_refresh_clock_offset`: an offline host must not
# spawn one probe thread per maintenance tick.
_PROBE_RETRY_SECONDS = 900.0
# "Never probed" sentinel for the two floors below. It must not be ``0.0``:
# ``time.monotonic()`` is the host uptime, not the process age, so right after a
# boot ``now - 0.0`` is still inside the floor and the first probe of a process
# would be skipped for up to ``_PROBE_RETRY_SECONDS``.
_NEVER_PROBED = -math.inf
_last_probe_monotonic = _NEVER_PROBED
# Separate floor for the authority probe (server/master): an explicit NTP probe
# must not throttle the per-cycle authority refresh, and vice versa.
_last_authority_probe_monotonic = _NEVER_PROBED

# ``ack.server_time`` is the shared one-second ISO wire form
# (``hsync_common.time_utils.parse_iso_seconds``): the reference truncates to
# the second, which is the quantization the recorded uncertainty must carry.
_ACK_TIME_QUANTUM_S = 1.0
# A reference whose clock is off by more than this is not credible (a
# misconfigured peer must not shift the mesh): the host stays degraded.
_MAX_OFFSET_S = 3600.0

# Generation of the in-flight P2P NTP probe. A newer start_ntp_clock_offset_probe
# call invalidates older workers so a slow probe cannot overwrite a fresher result.
_probe_generation = 0
_probe_lock = threading.Lock()

# Authority probe (server/master): at most one background probe runs at a time.
# A request that arrives while one is in flight is coalesced (pending) and re-run
# with a fresh generation; a stale result is discarded. ``*_generation`` also
# lets the synchronous per-cycle refresh supersede a background result.
_authority_probe_generation = 0
_authority_probe_inflight = False
_authority_probe_pending = False


def ntp_configured() -> bool:
    """Whether an explicit SNTP source is configured (``HSYNC_NTP_HOSTS``).

    Explicit NTP is a deployment override, not a default: without it the offset
    is anchored by the server/master and the rendezvous echo only.
    """
    return bool(os.environ.get("HSYNC_NTP_HOSTS", "").strip())


def _ntp_targets() -> tuple[tuple[str, int], ...]:
    """SNTP targets: ``HSYNC_NTP_HOSTS`` CSV with ``host[:port]`` entries.

    Only reached when :func:`ntp_configured` is true. Never falls back to the
    public defaults: a malformed explicit source yields no target (the probe then
    degrades) instead of silently contacting ``pool.ntp.org`` and friends.
    """
    return parse_ntp_targets(os.environ.get("HSYNC_NTP_HOSTS"), fallback_to_defaults=False)


def _authority_time_url(server_url: str | None = None) -> str | None:
    """The current authority's ``/api/v1/time`` URL.

    ``CONFIG["API"]["buckets"]`` points at ``…/api/v1/buckets`` for the authority
    in force (the sync server in server mode, the master in p2p; both are set by
    the config layer / ``p2p_handler``), so the time endpoint is the same base.
    """
    if server_url:
        return f"{server_url.rstrip('/')}/api/v1/time"
    from hsync_client.config import CONFIG

    buckets = str((CONFIG.get("API") or {}).get("buckets") or "").rstrip("/")
    if not buckets:
        return None
    if buckets.endswith("/buckets"):
        buckets = buckets[: -len("/buckets")]
    return f"{buckets}/time"


def probe_authority_clock_offset(
    logger: Any = log,
    *,
    server_url: str | None = None,
    generation: int | None = None,
) -> bool:
    """Measure the offset against the current time authority.

    Returns whether a **trusted** measurement was recorded. A network error, a
    non-200 reply, a missing/non-finite ``timestamp``, or an implausible offset
    records an untrusted offset (degraded mode) and returns ``False`` so the
    caller can fall back to an explicit NTP source.

    A p2p master is a peer: when it is not anchored it must **not** replace a
    better trusted source (explicit NTP or the rendezvous echo) already in force,
    but it does revoke a trust that this very master established. The sync server
    is authoritative, so a bad reply from it does degrade.

    ``generation`` is set by :func:`start_authority_clock_offset_probe`: a result
    from a superseded probe (a newer request arrived) is discarded because a
    slower older request must not overwrite the fresher one.
    """
    from hsync_client.config import CONFIG
    from hsync_client.utils import http_get

    global _last_authority_probe_monotonic
    time_url = _authority_time_url(server_url)
    if time_url is None:
        logger.debug("Clock authority URL is not configured")
        return False
    with _probe_lock:
        _last_authority_probe_monotonic = time.monotonic()

    authority_is_master = bool(CONFIG.get("MASTER_CERT_FINGERPRINT"))
    authority_source = "master" if authority_is_master else "server"

    def _current() -> bool:
        if generation is None:
            return True
        with _probe_lock:
            return generation == _authority_probe_generation

    def _keep_better_trusted_source() -> bool:
        return (
            authority_is_master and offset_store.is_trusted() and offset_store.source() != "master"
        )

    def _degrade() -> None:
        if not _current():
            return
        if _keep_better_trusted_source():
            logger.debug("Master clock is not anchored; keeping the existing trusted offset")
            return
        offset_store.record(0.0, trusted=False)

    try:
        t1 = time.time()
        response = http_get(time_url, headers={}, verify=get_ssl_verify(), timeout=10)
        t3 = time.time()
    except Exception as exc:
        logger.warning(f"{ActionLabel.CLOCK_DEGRADED} clock probe failed: {exc}")
        _degrade()
        return False

    if response.status_code != 200:
        logger.warning(
            f"{ActionLabel.CLOCK_DEGRADED} clock endpoint unavailable (HTTP {response.status_code})"
        )
        _degrade()
        return False

    try:
        payload = response.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        remote_time = float(payload.get("timestamp"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning(f"{ActionLabel.CLOCK_DEGRADED} clock endpoint returned no usable timestamp")
        _degrade()
        return False
    if not math.isfinite(remote_time):
        logger.warning(
            f"{ActionLabel.CLOCK_DEGRADED} clock endpoint returned a non-finite timestamp"
        )
        _degrade()
        return False

    offset, rtt = compute_clock_offset(t1, remote_time, t3)
    # A p2p master is a peer: it must vouch for its own clock. The sync server is
    # the authority by definition, so its reply is trusted without a flag.
    trusted = payload.get("trusted") is True if authority_is_master else True

    if abs(offset) > _MAX_OFFSET_S:
        logger.warning(
            f"{ActionLabel.CLOCK_DEGRADED} authority clock implausible "
            f"(offset {offset:+.0f}s); corrected mtimes stay unknown"
        )
        _degrade()
        return False

    if not _current():
        return trusted
    if trusted:
        offset_store.record(offset, trusted=True, rtt=rtt, source=authority_source)
        logger.info(f"{ActionLabel.CLOCK_SYNC} authority offset {offset:+.3f}s (±{rtt / 2:.3f}s)")
    elif _keep_better_trusted_source():
        logger.debug("Master clock is not anchored; keeping the existing trusted offset")
    else:
        offset_store.record(offset, trusted=False, rtt=rtt)
        logger.warning(
            f"{ActionLabel.CLOCK_DEGRADED} master clock is not anchored; "
            "corrected mtimes are unknown and conflicts resolve by content"
        )
    return trusted


def probe_server_clock_offset(server_url: str, logger: Any = log) -> None:
    """Measure the offset against the sync server (authoritative by definition)."""
    probe_authority_clock_offset(logger, server_url=server_url)


def start_authority_clock_offset_probe(logger: Any = log) -> threading.Thread | None:
    """Measure the current authority on a background daemon thread.

    A slave adopts the master's clock after every reconnect; doing it inline
    would block the reconnect loop for up to the request timeout, so it runs
    beside the loop. At most one probe runs at a time: a request that arrives
    while one is in flight is coalesced and re-run once it finishes, and a result
    from a superseded probe is discarded (generation), so a rapidly changing
    master cannot stack threads or let a stale result win. Returns ``None`` when
    the request was coalesced.
    """
    global _authority_probe_generation, _authority_probe_inflight, _authority_probe_pending
    with _probe_lock:
        _authority_probe_generation += 1
        if _authority_probe_inflight:
            _authority_probe_pending = True
            return None
        _authority_probe_inflight = True
        generation = _authority_probe_generation

    def _run() -> None:
        global _authority_probe_inflight, _authority_probe_pending
        current = generation
        try:
            while True:
                probe_authority_clock_offset(logger, generation=current)
                with _probe_lock:
                    if not _authority_probe_pending:
                        return
                    _authority_probe_pending = False
                    current = _authority_probe_generation
        finally:
            with _probe_lock:
                _authority_probe_inflight = False

    thread = threading.Thread(target=_run, daemon=True, name="hsync-authority-probe")
    thread.start()
    return thread


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


def record_rendezvous_clock_offset(
    server_time: object, *, sent_at: float, received_at: float, logger: Any = log
) -> bool:
    """Take the clock offset from a validated ``ack.server_time`` echo.

    The rendezvous is a TLS-pinned control-plane node the client already trusts
    (ADR-0034) whose answer travels the same QUIC/TLS path that already crosses
    the NAT, so it is reachable exactly where UDP/123 is filtered. Its timestamp
    has one-second resolution, so the recorded uncertainty carries that
    quantization: this measurement can *establish* trust for a degraded host but
    never refine a better source, and it is therefore taken only while the stored
    offset is untrusted. Returns whether a measurement was recorded.

    Consumers: ``hsync_client.p2p_rendezvous_rdv.RdvRendezvous._register``.
    """
    if offset_store.is_trusted():
        return False
    if not isinstance(server_time, str):
        return False
    parsed = parse_iso_seconds(server_time)
    if parsed is None:
        logger.debug(f"Rendezvous ack carries no usable server_time: {server_time!r}")
        return False
    rtt = max(received_at - sent_at, 0.0)
    # The reference truncates to the second, so its true instant is the value
    # plus half a quantum on average.
    offset = parsed.timestamp() + _ACK_TIME_QUANTUM_S / 2 - (sent_at + rtt / 2)
    if abs(offset) > _MAX_OFFSET_S:
        logger.warning(
            f"{ActionLabel.CLOCK_DEGRADED} rendezvous clock implausible "
            f"(offset {offset:+.0f}s); corrected mtimes stay unknown"
        )
        return False
    offset_store.record(offset, rtt=rtt + _ACK_TIME_QUANTUM_S, source="rendezvous")
    logger.info(
        f"{ActionLabel.CLOCK_SYNC} rendezvous offset {offset:+.3f}s "
        f"(±{offset_store.uncertainty():.3f}s)"
    )
    return True


def refresh_authority_clock_offset(logger: Any = log, *, force: bool = False) -> None:
    """Re-anchor against the current authority when the offset is not trusted.

    Called at the start of each ``sync()`` cycle (server mode and P2P slave). The
    authority is the sync server or the elected master, both reached through
    ``CONFIG["API"]``. ``_PROBE_RETRY_SECONDS`` bounds the retries so an
    unreachable authority is not probed synchronously on every cycle; an
    explicit NTP source is tried only as a fallback. ``force`` bypasses the floor
    (a detected clock step revokes trust and must re-anchor immediately).
    """
    if offset_store.is_trusted():
        return
    now = time.monotonic()
    with _probe_lock:
        authority_due = force or (now - _last_authority_probe_monotonic) >= _PROBE_RETRY_SECONDS
        ntp_due = ntp_configured() and (now - _last_probe_monotonic) >= _PROBE_RETRY_SECONDS
    if authority_due and probe_authority_clock_offset(logger):
        return
    if ntp_due:
        start_ntp_clock_offset_probe(logger)


def maybe_refresh_clock_offset(logger: Any = log) -> None:
    """Re-measure the serving node's anchor when it is no longer a reference.

    Called from the master maintenance tick. The serving node has no authority
    above it: it re-probes the explicit NTP source when one is configured, and
    otherwise relies on the rendezvous echo (``record_rendezvous_clock_offset``),
    which re-establishes a coarse trust at the next registration. Without either
    the host stays degraded (hash fallback) until a reference becomes available.
    """
    if offset_store.is_trusted():
        return
    with _probe_lock:
        since_last = time.monotonic() - _last_probe_monotonic
    if since_last < _PROBE_RETRY_SECONDS:
        return
    if not ntp_configured():
        logger.debug(
            f"{ActionLabel.CLOCK_DEGRADED} offset expired or revoked "
            f"(age {offset_store.age():.0f}s); no NTP configured, "
            "relying on the rendezvous echo"
        )
        return
    logger.info(
        f"{ActionLabel.CLOCK_DEGRADED} offset expired or revoked "
        f"(age {offset_store.age():.0f}s); re-probing NTP"
    )
    start_ntp_clock_offset_probe(logger)


def start_ntp_clock_offset_probe(logger: Any = log) -> threading.Thread:
    """Start the SNTP offset probe on a background daemon thread.

    Callers gate this on :func:`ntp_configured`: explicit NTP is an opt-in
    override, never a silent default. A newer call invalidates in-flight workers
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
            offset_store.record(
                offset,
                trusted=host is not None,
                rtt=rtt,
                source="ntp" if host is not None else None,
            )
        if host is not None:
            logger.info(
                f"{ActionLabel.CLOCK_SYNC} {host} offset {offset:+.3f}s (±{(rtt or 0.0) / 2:.3f}s)"
            )

    thread = threading.Thread(target=_run, daemon=True, name="hsync-ntp-probe")
    thread.start()
    return thread
