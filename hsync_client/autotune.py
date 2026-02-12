# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Auto-tuning of Timeout, Retry & Concurrency.

This module implements environment-aware adaptive defaults for HTTP timeout,
retry backoff, and transfer concurrency. Every probe is independent, returns
a neutral value (``None`` / enum fallback) on failure, and never raises into
the caller. The policy layer is pure (no I/O) and fully unit-testable.

Architecture
------------

1. **Probes** — detect network class, bandwidth, storage type, queue profile.
2. **Policy** — ``derive_profile()`` converts ``ProbeResults`` into a
   ``TuningProfile`` via the stateless ``derive_*`` functions.
3. **SAFE** — ``TuningProfile`` with conservative static defaults for when
   auto-tuning is disabled or probes fail.
"""

from dataclasses import dataclass
from enum import StrEnum
from socket import create_connection
from statistics import median as _median
from time import monotonic
from typing import TYPE_CHECKING

from loguru import logger as log

from hsync_client.constants import (
    HTTP_TIMEOUT_DEFAULT,
    HTTP_TIMEOUT_MAX,
    HTTP_TIMEOUT_MIN,
    MAX_CONCURRENT_TRANSFERS_DEFAULT,
    RECONNECT_DELAY_DEFAULT,
    RECONNECT_DELAY_MAX,
    RECONNECT_DELAY_MIN,
    TRANSFER_RETRY_MAX_WAIT_DEFAULT,
)

if TYPE_CHECKING:
    from hsync_common.models.file import File

# =============================================================================
# Enums
# =============================================================================


class NetClass(StrEnum):
    """Network class based on RTT."""

    UNKNOWN = "unknown"
    LAN = "lan"
    BROADBAND = "broadband"
    MOBILE = "mobile"


class BwClass(StrEnum):
    """Bandwidth class from passive EMA or active probe."""

    UNKNOWN = "unknown"
    LOW = "low"
    MID = "mid"
    GIGABIT = "gigabit"
    TEN_G = "ten_g"


class StorageType(StrEnum):
    """Local-storage device class (probed from the sync root)."""

    UNKNOWN = "unknown"
    HDD = "hdd"
    SSD = "ssd"
    NVME = "nvme"


class QueueProfile(StrEnum):
    """Shape of the transfer queue for the current sync cycle."""

    MANY_SMALL = "many_small"
    FEW_LARGE = "few_large"
    MIXED = "mixed"


# =============================================================================
# Dataclasses
# =============================================================================


@dataclass
class ProbeResults:
    """Aggregated environment probe results."""

    rtt_ms: float | None
    net_class: NetClass
    bw_class: BwClass
    storage_type: StorageType
    queue_profile: QueueProfile


@dataclass
class TuningProfile:
    """Derived adaptive configuration values."""

    http_timeout: int
    retry_min_wait: int
    retry_max_wait: int
    reconnect_delay: int
    concurrency: int
    storage_type: StorageType
    notes: str


# =============================================================================
# SAFE fallback profile
# =============================================================================

SAFE = TuningProfile(
    http_timeout=HTTP_TIMEOUT_DEFAULT,
    # Deliberately above TRANSFER_RETRY_MIN_WAIT_DEFAULT (1): the safe profile
    # is already the conservative fallback, so its first retry waits longer.
    retry_min_wait=5,
    retry_max_wait=TRANSFER_RETRY_MAX_WAIT_DEFAULT,
    reconnect_delay=RECONNECT_DELAY_DEFAULT,
    concurrency=MAX_CONCURRENT_TRANSFERS_DEFAULT,
    storage_type=StorageType.UNKNOWN,
    notes="autotune disabled or all probes failed — safe defaults",
)

# =============================================================================
# Module-level bandwidth EMA state
# =============================================================================

_bw_ema: float | None = None
_BW_EMA_ALPHA = 0.3


# =============================================================================
# Probes — §3.1  Network latency (RTT)
# =============================================================================


def _rtt_probe_once(host: str, port: int, timeout: float) -> float:
    """Return one TCP-connect RTT sample in milliseconds (raises ``OSError``)."""
    t0 = monotonic()
    with create_connection((host, port), timeout=timeout):
        return (monotonic() - t0) * 1000


def measure_rtt(host: str, port: int, samples: int = 3) -> float | None:
    """Median TCP-connect RTT in milliseconds.  ``None`` if not measurable.

    The three samples run in parallel: they are independent measurements of
    the same endpoint, so the median over concurrent attempts is the same
    value the sequential loop produced, while the worst-case blocking
    startup cost drops from ``3 × timeout`` to ``1 × timeout``. Any failure
    still yields ``None`` immediately (safe default, unchanged).
    """
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(samples, 3)) as executor:
        futures = [
            executor.submit(_rtt_probe_once, host, port, 2.0) for _ in range(max(1, samples))
        ]
    times: list[float] = []
    for future in futures:
        try:
            times.append(future.result())
        except OSError as e:
            log.warning(f"autotune: RTT probe failed ({e}); using safe default")
            return None
    return _median(times) if times else None


def classify_network(rtt_ms: float | None) -> NetClass:
    """Map RTT to ``NetClass``."""
    if rtt_ms is None:
        return NetClass.UNKNOWN
    if rtt_ms > 150:
        return NetClass.MOBILE
    if rtt_ms > 50:
        return NetClass.BROADBAND
    return NetClass.LAN


# =============================================================================
# Probes — §3.2  Available bandwidth
# =============================================================================


def update_bw_ema(bytes_transferred: int, elapsed_sec: float) -> None:
    """Update the passive EMA after each file transfer completes."""
    if elapsed_sec <= 0:
        return
    mbps = (bytes_transferred * 8 / elapsed_sec) / 1_000_000
    global _bw_ema
    _bw_ema = mbps if _bw_ema is None else _BW_EMA_ALPHA * mbps + (1 - _BW_EMA_ALPHA) * _bw_ema


def seed_bw_ema(mbps: float) -> None:
    """Seed the passive EMA with a coarse active-probe measurement.

    The startup active probe fails when the server is not yet reachable; a
    later refresh (post-connect) that obtains a measurement uses this to seed
    ``_bw_ema`` exactly as the first transfer sample would, so the per-cycle
    concurrency refresh classifies bandwidth before any transfer completes.
    Never overwrites a value already populated by real transfers.
    """
    if mbps <= 0:
        return
    global _bw_ema
    if _bw_ema is None:
        _bw_ema = mbps


def measure_bandwidth_active(
    server_url: str, size_bytes: int = 256 * 1024, verify: bool | str = True
) -> float | None:
    """Measure throughput by downloading *size_bytes* from the server's health
    probe (``GET /health?size=``), returning Mbps. ``None`` on failure.

    The server bounds the payload (``HEALTH_MAX_PROBE_BYTES``), so the result
    is a coarse seed for the bandwidth class until the passive EMA has data.
    Never used on metered connections (Android forces ``MOBILE`` instead).

    *verify* controls TLS certificate verification (same semantics as
    ``requests``). When left at its default (``True``) the function falls back
    to :func:`hsync_client.utils.get_ssl_verify` so that late callers (e.g. the
    LAN post-discovery refresh) benefit from the already-populated ``CONFIG``.
    """
    try:
        from requests.exceptions import RequestException

        from hsync_client.utils import get_ssl_verify, http_get
    except ImportError:
        log.warning("autotune: requests not available, skipping active bandwidth probe")
        return None

    # Resolve the effective verify value: an explicit override always wins;
    # otherwise consult CONFIG via get_ssl_verify() so that the LAN refresh
    # path picks up --insecure / CA_CERT / fingerprint settings.
    effective_verify: bool | str = verify if verify is not True else get_ssl_verify()

    try:
        t0 = monotonic()
        resp = http_get(
            f"{server_url.rstrip('/')}/health",
            params={"size": size_bytes},
            timeout=10,
            verify=effective_verify,
        )
        elapsed = monotonic() - t0
        payload_bytes = len(resp.content)
        if resp.ok and elapsed > 0 and payload_bytes > 0:
            return (payload_bytes * 8 / elapsed) / 1_000_000
    except (RequestException, OSError) as e:
        log.warning(f"autotune: active bandwidth probe failed ({e})")
    return None


def classify_bandwidth(
    mbps: float | None,
    active_mbps: float | None = None,
) -> BwClass:
    """Classify bandwidth using passive EMA (preferred) or active probe (fallback)."""
    effective_mbps = mbps if mbps is not None else active_mbps
    if effective_mbps is None:
        return BwClass.UNKNOWN
    if effective_mbps >= 2000:
        return BwClass.TEN_G
    if effective_mbps >= 800:
        return BwClass.GIGABIT
    if effective_mbps >= 50:
        return BwClass.MID
    return BwClass.LOW


# =============================================================================
# Probes — §3.3  Local storage type
# =============================================================================


def detect_storage_type(path: str) -> StorageType:
    """Return the storage-device class of the filesystem backing *path*."""
    import os
    from pathlib import Path

    try:
        dev = _find_base_block_device(os.stat(path).st_dev)
        if dev.startswith("nvme"):
            return StorageType.NVME
        rotational = Path(f"/sys/class/block/{dev}/queue/rotational").read_text().strip()
        return StorageType.HDD if rotational == "1" else StorageType.SSD
    except (OSError, PermissionError, KeyError) as e:
        log.warning(f"autotune: storage detection failed ({e}); assuming HDD-class")
        return StorageType.UNKNOWN


def _find_base_block_device(st_dev: int) -> str:
    """Resolve ``os.stat().st_dev`` to the base block device name."""
    from pathlib import Path

    mountinfo = Path("/proc/self/mountinfo").read_text()
    major, minor = (st_dev >> 8) & 0xFFF, st_dev & 0xFF
    dev_str = f"{major}:{minor}"

    source: str | None = None
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        if fields[2] == dev_str:
            source = fields[4]
            break
    if source is None:
        raise OSError(f"Cannot find device for st_dev {dev_str} in mountinfo")

    dev_node = Path(source).name
    return _reduce_to_base_device(dev_node)


def _reduce_to_base_device(dev_name: str) -> str:
    """Strip partition suffix and resolve device-mapper / LVM layers."""
    import re
    from pathlib import Path

    base = re.sub(r"p?\d+$", "", dev_name)

    if base.startswith("dm-"):
        slaves_dir = Path(f"/sys/class/block/{base}/slaves")
        if slaves_dir.exists():
            slaves = list(slaves_dir.iterdir())
            if slaves:
                return slaves[0].name
    return base


# =============================================================================
# Probes — §3.4  Transfer-queue profile
# =============================================================================


def profile_transfer_queue(files: "list[File]") -> QueueProfile:
    """Classify the transfer queue shape (``MANY_SMALL`` / ``FEW_LARGE`` / ``MIXED``).

    Single pass with running counters: the fractions are simple averages, so
    two ``statistics.mean()`` passes over a materialized sizes list are
    replaced by one iteration without any intermediate list.
    """
    total = 0
    small_count = 0
    large_count = 0
    for f in files:
        size = f.size or 0
        total += 1
        if size < 1_000_000:  # < 1 MB
            small_count += 1
        if size > 256_000_000:  # > 256 MB
            large_count += 1

    if total == 0:
        return QueueProfile.MIXED

    small_frac = small_count / total
    large_frac = large_count / total

    if total >= 100 and small_frac >= 0.7:
        return QueueProfile.MANY_SMALL
    if total <= 20 and large_frac >= 0.5:
        return QueueProfile.FEW_LARGE
    return QueueProfile.MIXED


# =============================================================================
# Policy — §4  Derivation (pure functions, no I/O)
# =============================================================================


def derive_profile(results: ProbeResults) -> TuningProfile:
    """Convert ``ProbeResults`` into a ``TuningProfile``."""
    from hsync_client.constants import (
        HTTP_TIMEOUT_MAX,
        HTTP_TIMEOUT_MIN,
        MAX_CONCURRENT_TRANSFERS_MAX,
        MAX_CONCURRENT_TRANSFERS_MIN,
        RECONNECT_DELAY_MAX,
        RECONNECT_DELAY_MIN,
    )

    timeout = derive_timeout(results.rtt_ms, results.net_class, HTTP_TIMEOUT_MIN, HTTP_TIMEOUT_MAX)
    min_wait, max_wait, reconnect = derive_retry(
        results.rtt_ms,
        results.net_class,
        RECONNECT_DELAY_MIN,
        RECONNECT_DELAY_MAX,
    )
    concurrency = derive_concurrency(
        results.storage_type,
        results.bw_class,
        results.queue_profile,
        MAX_CONCURRENT_TRANSFERS_MIN,
        MAX_CONCURRENT_TRANSFERS_MAX,
    )
    notes = (
        f"rtt={results.rtt_ms or '?'}ms "
        f"bw={results.bw_class.value} "
        f"storage={results.storage_type.value} "
        f"-> timeout={timeout}s "
        f"retry=({min_wait},{max_wait})s "
        f"reconnect={reconnect}s "
        f"concurrency={concurrency} (auto)"
    )
    return TuningProfile(
        http_timeout=timeout,
        retry_min_wait=min_wait,
        retry_max_wait=max_wait,
        reconnect_delay=reconnect,
        concurrency=concurrency,
        storage_type=results.storage_type,
        notes=notes,
    )


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def derive_managed_rtt_updates(rtt_ms: float, managed: set[str]) -> dict[str, int | float]:
    """Derive autotune-managed timeout/retry keys from a measured RTT.

    Only keys present in *managed* are included. Callers write the result onto
    ``CONFIG`` (and, in LAN mode, the local config dict).
    """
    net_class = classify_network(rtt_ms)
    updates: dict[str, int | float] = {}
    if "HTTP_TIMEOUT" in managed:
        updates["HTTP_TIMEOUT"] = derive_timeout(
            rtt_ms, net_class, HTTP_TIMEOUT_MIN, HTTP_TIMEOUT_MAX
        )
    if managed & {"TRANSFER_RETRY_MIN_WAIT", "TRANSFER_RETRY_MAX_WAIT", "RECONNECT_DELAY"}:
        min_wait, max_wait, reconnect = derive_retry(
            rtt_ms,
            net_class,
            RECONNECT_DELAY_MIN,
            RECONNECT_DELAY_MAX,
        )
        if "TRANSFER_RETRY_MIN_WAIT" in managed:
            updates["TRANSFER_RETRY_MIN_WAIT"] = float(min_wait)
        if "TRANSFER_RETRY_MAX_WAIT" in managed:
            updates["TRANSFER_RETRY_MAX_WAIT"] = float(max_wait)
        if "RECONNECT_DELAY" in managed:
            updates["RECONNECT_DELAY"] = reconnect
    return updates


def derive_timeout(
    rtt_ms: float | None,
    net_class: NetClass,
    timeout_min: int,
    timeout_max: int,
) -> int:
    if net_class == NetClass.UNKNOWN:
        return SAFE.http_timeout
    if net_class == NetClass.MOBILE:
        return _clamp(60, timeout_min, timeout_max)
    if rtt_ms is not None and rtt_ms > 50:
        return _clamp(45, timeout_min, timeout_max)
    return _clamp(30, timeout_min, timeout_max)


def derive_retry(
    rtt_ms: float | None,
    net_class: NetClass,
    reconnect_delay_min: int,
    reconnect_delay_max: int,
) -> tuple[int, int, int]:
    """Return ``(min_wait, max_wait, reconnect_delay)``."""
    if rtt_ms is not None:
        factor = min(1 + rtt_ms / 100, 3)
        min_wait = _clamp(round(5 * factor), 1, 30)
        max_wait = _clamp(round(min_wait * 2), 1, 60)
        reconnect = _clamp(round(5 * factor), reconnect_delay_min, reconnect_delay_max)
        return (min_wait, max_wait, reconnect)
    if net_class == NetClass.MOBILE:
        return (10, 20, 10)
    return (SAFE.retry_min_wait, SAFE.retry_max_wait, SAFE.reconnect_delay)


def derive_concurrency(
    storage: StorageType,
    bw: BwClass,
    queue: QueueProfile,
    concurrency_min: int,
    concurrency_max: int,
) -> int:
    base = {
        StorageType.HDD: 5,
        StorageType.SSD: 10,
        StorageType.NVME: 15,
        StorageType.UNKNOWN: 5,
    }[storage]

    if bw == BwClass.TEN_G and storage in (StorageType.SSD, StorageType.NVME):
        base += 5
    elif bw == BwClass.LOW:
        base = min(base, 4)

    adj = {
        QueueProfile.MANY_SMALL: +3,
        QueueProfile.FEW_LARGE: -2 if storage == StorageType.HDD else 0,
        QueueProfile.MIXED: 0,
    }
    return _clamp(base + adj[queue], concurrency_min, concurrency_max)
