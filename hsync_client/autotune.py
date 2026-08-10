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

if TYPE_CHECKING:
    from hsync_common.models.file import File

# =============================================================================
# Enums
# =============================================================================


class NetClass(StrEnum):
    """Network class based on RTT (and Android metered override)."""

    UNKNOWN = "unknown"
    LAN = "lan"
    BROADBAND = "broadband"
    MOBILE = "mobile"


class BwClass(StrEnum):
    """Bandwidth class from passive EMA, active probe, or NIC hint."""

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
    http_timeout=30,
    retry_min_wait=5,
    retry_max_wait=10,
    reconnect_delay=5,
    concurrency=5,
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


def measure_rtt(host: str, port: int, samples: int = 3) -> float | None:
    """Median TCP-connect RTT in milliseconds.  ``None`` if not measurable."""
    times: list[float] = []
    for _ in range(samples):
        try:
            t0 = monotonic()
            with create_connection((host, port), timeout=2.0):
                times.append((monotonic() - t0) * 1000)
        except OSError as e:
            log.warning(f"autotune: RTT probe failed ({e}); using safe default")
            return None
    return _median(times)


def classify_network(rtt_ms: float | None, is_mobile: bool = False) -> NetClass:
    """Map RTT to ``NetClass``. *is_mobile* override from Android."""
    if is_mobile:
        return NetClass.MOBILE
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


def measure_bandwidth_active(server_url: str, size_bytes: int = 256 * 1024) -> float | None:
    """GET a small payload from the server, return Mbps. ``None`` on failure."""
    try:
        from requests import get as http_get
        from requests.exceptions import RequestException
    except ImportError:
        log.warning("autotune: requests not available, skipping active bandwidth probe")
        return None

    try:
        t0 = monotonic()
        resp = http_get(f"{server_url}/api/v1/health", timeout=10)
        elapsed = monotonic() - t0
        if resp.ok and elapsed > 0:
            return (len(resp.content) * 8 / elapsed) / 1_000_000
    except (RequestException, OSError) as e:
        log.warning(f"autotune: active bandwidth probe failed ({e})")
    return None


def classify_bandwidth(
    mbps: float | None,
    nic_speed_hint: int | None,
    active_mbps: float | None = None,
) -> BwClass:
    """Classify bandwidth using passive EMA (preferred) or active probe (fallback)."""
    effective_mbps = mbps if mbps is not None else active_mbps
    if effective_mbps is None and nic_speed_hint is None:
        return BwClass.UNKNOWN
    if (effective_mbps or 0) >= 2000 or (nic_speed_hint or 0) >= 10000:
        return BwClass.TEN_G
    if (effective_mbps or 0) >= 800:
        return BwClass.GIGABIT
    if (effective_mbps or 0) >= 50:
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
    """Classify the transfer queue shape (``MANY_SMALL`` / ``FEW_LARGE`` / ``MIXED``)."""
    from statistics import mean

    sizes = [f.size or 0 for f in files]
    if not sizes:
        return QueueProfile.MIXED

    small_frac = mean(s < 1_000_000 for s in sizes)  # < 1 MB
    large_frac = mean(s > 256_000_000 for s in sizes)  # > 256 MB

    if len(sizes) >= 100 and small_frac >= 0.7:
        return QueueProfile.MANY_SMALL
    if len(sizes) <= 20 and large_frac >= 0.5:
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
