# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

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

import os
import re
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
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
    MAX_CONCURRENT_TRANSFERS_MAX,
    MAX_CONCURRENT_TRANSFERS_MIN,
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
    P2P = "p2p"
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
    return NetClass.P2P


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
    P2P post-rendezvous refresh) benefit from the already-populated ``CONFIG``.
    """
    try:
        from requests.exceptions import RequestException

        from hsync_client.utils import get_ssl_verify, http_get
    except ImportError:
        log.warning("autotune: requests not available, skipping active bandwidth probe")
        return None

    # Resolve the effective verify value: an explicit override always wins;
    # otherwise consult CONFIG via get_ssl_verify() so that the P2P refresh
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

# Sysfs/mountinfo test seams: module-level paths so the unit tests can point them
# at a fixture tree instead of the host's real devices.
_SYS_DEV_BLOCK = Path("/sys/dev/block")
_SYS_CLASS_BLOCK = Path("/sys/class/block")
_MOUNTINFO = Path("/proc/self/mountinfo")

# Partition suffix: `sda1`→`sda`, `nvme0n1p1`→`nvme0n1`, `mmcblk0p1`→`mmcblk0`.
# Device-mapper names (`dm-0`) are indices, not partitions, and are left as-is.
_PARTITION_RE = re.compile(
    r"^(?P<base>nvme\d+n\d+|mmcblk\d+|loop\d+|md\d+|sd[a-z]+|vd[a-z]+|hd[a-z]+)(?:p?\d+)?$"
)

_WINDOWS_BUS_NVME = 17
_WINDOWS_STORAGE_DEVICE_PROPERTY = 0
_WINDOWS_STORAGE_SEEK_PENALTY_PROPERTY = 7
_WINDOWS_IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
_WINDOWS_IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000


def detect_storage_type(path: str) -> StorageType:
    """Return the storage-device class of the filesystem backing *path*.

    Best-effort and platform-specific: Linux reads sysfs (walking up through
    pseudo-filesystem mounts such as ecryptfs/overlay), Windows asks the storage
    stack for the bus type and seek penalty, macOS asks ``diskutil``. When the
    platform has no probe, or the path lives on a filesystem with no backing
    block device (tmpfs, a container overlay), the class is UNKNOWN — the
    conservative HDD-tier default (``_STORAGE_BASE_CONCURRENCY``). A failed probe
    is logged at DEBUG: on those layouts it is a normal condition, not a fault
    the user can act on.
    """
    try:
        if sys.platform.startswith("linux"):
            detected = _detect_storage_linux(path)
        elif sys.platform == "win32":
            detected = _detect_storage_windows(path)
        elif sys.platform == "darwin":
            detected = _detect_storage_macos(path)
        else:
            return StorageType.UNKNOWN
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        log.debug(f"autotune: storage detection failed ({exc}); assuming HDD-class")
        return StorageType.UNKNOWN
    return detected or StorageType.UNKNOWN


# ── Linux: sysfs + mountinfo ─────────────────────────────────────────────


def _detect_storage_linux(path: str) -> StorageType | None:
    device = _linux_base_device(path)
    return _storage_from_device(device) if device else None


def _linux_base_device(path: str) -> str | None:
    """Resolve the block device backing *path*, crossing pseudo-filesystem mounts.

    ``st_dev`` identifies a block device for ext4/xfs/vfat/LVM, but ecryptfs,
    overlayfs, tmpfs and btrfs subvolumes carry a synthetic device number with no
    ``/sys/dev/block`` entry. The probe therefore walks up to the nearest
    ancestor whose ``st_dev`` resolves (an encrypted home resolves to the disk
    behind its mount point) and falls back to the mount source for the cases a
    walk cannot reach (btrfs reports a bare ``/dev/sdaN`` source).
    """
    current = Path(path)
    try:
        candidates = (current.resolve(), *current.resolve().parents)
    except OSError:
        candidates = (current, *current.parents)
    for candidate in candidates:
        try:
            node = _sysfs_block_device(os.stat(candidate).st_dev)
        except OSError:
            continue
        if node:
            return node
    source = _mountinfo_source(path)
    if source is None:
        return None
    if os.path.isabs(source):
        if source.startswith("/dev/"):
            return _strip_partition(Path(source).name)
        if os.path.exists(source):
            try:
                return _sysfs_block_device(os.stat(source).st_dev)
            except OSError:
                return None
        return None
    return None


def _sysfs_block_device(st_dev: int) -> str | None:
    """Resolve a ``st_dev`` to a base block-device name via ``/sys/dev/block``."""
    node = _SYS_DEV_BLOCK / f"{os.major(st_dev)}:{os.minor(st_dev)}"
    if not node.exists():
        return None
    return _strip_partition(Path(os.path.realpath(node)).name)


def _storage_from_device(device: str) -> StorageType | None:
    """Classify a Linux block device (resolving device-mapper layers)."""
    base = _dm_backing(device)
    if base.startswith("nvme"):
        return StorageType.NVME
    try:
        rotational = (_SYS_CLASS_BLOCK / base / "queue" / "rotational").read_text().strip()
    except OSError:
        return None
    return StorageType.HDD if rotational == "1" else StorageType.SSD


def _strip_partition(name: str) -> str:
    """Drop a partition suffix (``sda1``→``sda``, ``nvme0n1p1``→``nvme0n1``)."""
    match = _PARTITION_RE.match(name)
    return match.group("base") if match else name


def _dm_backing(name: str) -> str:
    """Resolve a device-mapper device to its first slave (LVM/crypt)."""
    if not name.startswith("dm-"):
        return name
    try:
        slaves = list((_SYS_CLASS_BLOCK / name / "slaves").iterdir())
    except OSError:
        return name
    return _strip_partition(slaves[0].name) if slaves else name


def _mountinfo_source(path: str) -> str | None:
    """Return the mount *source* for *path* (longest matching mount point)."""
    try:
        text = _MOUNTINFO.read_text()
    except OSError:
        return None
    return _mountinfo_source_from_text(text, path)


def _mountinfo_source_from_text(text: str, path: str) -> str | None:
    """Parse ``/proc/self/mountinfo`` and return the source for *path*.

    The block device is the field after the ``-`` separator, never the mount
    point (mountinfo field 4); reading the mount point used to turn
    ``/home/<user>`` into a device named after the user.
    """
    target = os.path.realpath(path)
    best: tuple[int, str] | None = None
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 7 or "-" not in fields:
            continue
        separator = fields.index("-")
        if len(fields) < separator + 3:
            continue
        mount_point = _mountinfo_unescape(fields[4])
        if target != mount_point and not target.startswith(mount_point.rstrip("/") + "/"):
            continue
        if best is None or len(mount_point) > best[0]:
            best = (len(mount_point), _mountinfo_unescape(fields[separator + 2]))
    return best[1] if best else None


def _mountinfo_unescape(value: str) -> str:
    """Decode the octal escapes mountinfo uses for spaces/tabs/newlines."""
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


# ── Windows: volume → physical disk → bus type + seek penalty ────────────


def _detect_storage_windows(path: str) -> StorageType | None:
    """Classify the physical disk behind *path* through the Windows storage stack.

    ``GetVolumePathNameW`` maps the path to its volume,
    ``IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS`` maps the volume to its disk, then
    ``IOCTL_STORAGE_QUERY_PROPERTY`` yields the bus type (NVMe) and whether the
    device incurs a seek penalty (HDD). No external tool is spawned.
    """
    queried = _windows_query_storage(path)
    return _windows_storage_class(*queried) if queried is not None else None


def _windows_storage_class(bus_type: int, incurs_seek_penalty: bool) -> StorageType:
    """Map the Windows storage descriptors to a storage class (pure)."""
    if bus_type == _WINDOWS_BUS_NVME:
        return StorageType.NVME
    return StorageType.HDD if incurs_seek_penalty else StorageType.SSD


def _windows_query_storage(path: str) -> tuple[int, bool] | None:  # pragma: no cover
    """Return ``(bus_type, incurs_seek_penalty)`` for the disk behind *path*."""
    import ctypes
    import struct
    from ctypes import wintypes

    class _StorageQuery(ctypes.Structure):
        _fields_ = [
            ("PropertyId", wintypes.DWORD),
            ("QueryType", wintypes.DWORD),
            ("AdditionalParameters", ctypes.c_ubyte * 1),
        ]

    # WinDLL exists only on Windows; the stub omits it on other platforms.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    invalid = wintypes.HANDLE(-1).value
    share, open_existing = 0x00000003, 3

    def query(handle, property_id: int, size: int) -> bytes | None:
        request = _StorageQuery(property_id, 0)
        buffer = ctypes.create_string_buffer(size)
        returned = wintypes.DWORD(0)
        ok = kernel32.DeviceIoControl(
            handle,
            _WINDOWS_IOCTL_STORAGE_QUERY_PROPERTY,
            ctypes.byref(request),
            ctypes.sizeof(request),
            buffer,
            len(buffer),
            ctypes.byref(returned),
            None,
        )
        return buffer.raw[: returned.value] if ok else None

    root = ctypes.create_unicode_buffer(261)
    if not kernel32.GetVolumePathNameW(path, root, len(root)):
        return None
    handle = kernel32.CreateFileW(
        "\\\\.\\" + root.value.rstrip("\\"), 0, share, None, open_existing, 0, None
    )
    if handle in (invalid, None):
        return None
    try:
        buffer = ctypes.create_string_buffer(1024)
        returned = wintypes.DWORD(0)
        if not kernel32.DeviceIoControl(
            handle,
            _WINDOWS_IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS,
            None,
            0,
            buffer,
            len(buffer),
            ctypes.byref(returned),
            None,
        ):
            return None
        disk_number = struct.unpack_from("<I", buffer.raw, 8)[0]
    finally:
        kernel32.CloseHandle(handle)

    disk = kernel32.CreateFileW(
        f"\\\\.\\PhysicalDrive{disk_number}", 0, share, None, open_existing, 0, None
    )
    if disk in (invalid, None):
        return None
    try:
        descriptor = query(disk, _WINDOWS_STORAGE_DEVICE_PROPERTY, 1024)
        penalty = query(disk, _WINDOWS_STORAGE_SEEK_PENALTY_PROPERTY, 16)
    finally:
        kernel32.CloseHandle(disk)
    if not descriptor or not penalty:
        return None
    bus_type = struct.unpack_from("<I", descriptor, 28)[0]
    incurs_seek_penalty = bool(penalty[8]) if len(penalty) > 8 else True
    return bus_type, incurs_seek_penalty


# ── macOS: diskutil metadata ─────────────────────────────────────────────


def _detect_storage_macos(path: str) -> StorageType | None:
    """Classify the volume behind *path* through ``diskutil info -plist``.

    ``statvfs`` yields the device node; ``diskutil`` then reports whether the
    media is solid-state and, for SSDs, its protocol (an Apple/PCI-Express NVMe
    device is distinguished from a SATA SSD).
    """
    device = _macos_device_node(path)
    if not device:
        return None
    info = _macos_disk_info(device)
    return _macos_storage_class(info) if info else None


def _macos_device_node(path: str) -> str | None:
    """Return the ``/dev/disk…`` node backing *path* (``None`` off-disk)."""
    try:
        source = getattr(os.statvfs(path), "f_mntfromname", None)
    except OSError:
        return None
    if isinstance(source, bytes):
        source = source.decode("utf-8", "replace")
    return source if isinstance(source, str) and source.startswith("/dev/") else None


def _macos_disk_info(device: str) -> dict | None:  # pragma: no cover - macOS-only
    """Run ``diskutil info -plist`` for *device* and parse it (2 s timeout)."""
    import plistlib
    import subprocess

    try:
        completed = subprocess.run(
            ["diskutil", "info", "-plist", device],
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not completed.stdout:
        return None
    try:
        return plistlib.loads(completed.stdout)
    except (ValueError, plistlib.InvalidFileException):
        return None


def _macos_storage_class(info: dict) -> StorageType | None:
    """Map a ``diskutil info -plist`` payload to a storage class (pure)."""
    solid_state = info.get("SolidState")
    if solid_state is None:
        return None
    if not solid_state:
        return StorageType.HDD
    protocol = str(
        info.get("DeviceProtocol") or info.get("Protocol") or info.get("BusProtocol") or ""
    ).lower()
    if "nvme" in protocol or "pci-express" in protocol:
        return StorageType.NVME
    return StorageType.SSD


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
    ``CONFIG`` (and, in P2P mode, the local config dict).
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


def measure_rtt_tuning(
    host: str, port: int, managed: set[str]
) -> tuple[float, NetClass, dict[str, int | float]] | None:
    """Measure the endpoint RTT and derive the autotune-managed updates.

    One place owns the "unmeasurable ⇒ keep current tuning" contract used by the
    remote and P2P refreshes: returns ``(rtt_ms, net_class, updates)``, or
    ``None`` when the probe produced no sample. Callers keep their own guard,
    logging and application policy (which keys/dicts they write).
    """
    rtt_ms = measure_rtt(host, port)
    if rtt_ms is None:
        return None
    return rtt_ms, classify_network(rtt_ms), derive_managed_rtt_updates(rtt_ms, managed)


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


# Base concurrency by storage class, hoisted out of derive_concurrency so the
# hot policy call does not rebuild the same four-entry dict per derivation.
_STORAGE_BASE_CONCURRENCY: dict[StorageType, int] = {
    StorageType.HDD: 5,
    StorageType.SSD: 10,
    StorageType.NVME: 15,
    StorageType.UNKNOWN: 5,
}


def derive_concurrency(
    storage: StorageType,
    bw: BwClass,
    queue: QueueProfile,
    concurrency_min: int,
    concurrency_max: int,
) -> int:
    base = _STORAGE_BASE_CONCURRENCY[storage]

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
