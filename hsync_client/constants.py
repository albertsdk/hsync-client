# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Constants for HSYNC Client.

This module contains all magic numbers and constant values used
throughout the application, making them easy to find and modify.
"""

# =============================================================================
# Application Info
# =============================================================================
from typing import Final, Literal

from hsync_common.constants import BOOT_DEFAULT_PORT as BOOTNODE_DEFAULT_PORT  # noqa: F401
from hsync_common.constants import HEADER_CLIENT_NAME as _HEADER_CLIENT_NAME
from hsync_common.constants import HEADER_CLIENT_VERSION as _HEADER_CLIENT_VERSION
from hsync_common.constants import (
    HEADER_PROTOCOL_VERSION as _HEADER_PROTOCOL_VERSION,
)

# Re-export shared constants from hsync-common. The plain names keep the
# module's public re-export API consumed across hsync_client (HSYNC_VERSION,
# HSYNCKEEP_FILENAME, PARTIAL_DOWNLOAD_SUFFIX, WS_MAX_MESSAGE_BYTES); the
# aliased header names are re-typed below as string literals because mypy
# requires literal keys for TypedDicts. BOOTNODE_DEFAULT_PORT (above) is the
# boot/1 UDP default (QUIC, not HTTPS) — one SSOT with the bootnode's `--port`.
from hsync_common.constants import (  # noqa: F401
    HOSTNAME_PATTERN,
    HSYNC_VERSION,
    HSYNCKEEP_FILENAME,
    PARTIAL_DOWNLOAD_SUFFIX,
    WS_MAX_MESSAGE_BYTES,
)

# Re-export shared action labels from hsync-common
from hsync_common.labels import ActionLabel  # noqa: F401
from hsync_common.permissions import OWNER_DIRECTORY_ACCESS  # noqa: F401

# Application name sent as the X-Client-Name REST header. Identifies the
# originating product (the CLI / sync daemon), never an internal module.
CLIENT_NAME = "hsync-client"

# The header names feed TypedDict keys in ``main_logic.py`` (REQUEST_HEADERS),
# which mypy accepts only as string literals. hsync-common ships no ``py.typed``
# marker, so mypy sees its symbols as ``Any`` and rejects them as keys; the
# re-annotations below restore the literal type while the VALUE keeps coming
# from the SSOT import.
HEADER_CLIENT_NAME: Final[Literal["X-Client-Name"]] = _HEADER_CLIENT_NAME
HEADER_CLIENT_VERSION: Final[Literal["X-Client-Version"]] = _HEADER_CLIENT_VERSION
HEADER_PROTOCOL_VERSION: Final[Literal["X-Protocol-Version"]] = _HEADER_PROTOCOL_VERSION


# =============================================================================
# Sync Direction
# =============================================================================

# The sync-direction vocabulary lives in hsync_common.protocol.pairing (the wire
# SSOT); importing it here keeps the client's CLI/preview validators from
# drifting from the canonical set.
from hsync_common.protocol.pairing import (
    SYNC_DIRECTION_BIDIRECTIONAL,
    SYNC_DIRECTION_DOWNLOAD_ONLY,
    SYNC_DIRECTION_UPLOAD_ONLY,
)

SYNC_DIRECTIONS: tuple[str, ...] = (
    SYNC_DIRECTION_BIDIRECTIONAL,
    SYNC_DIRECTION_UPLOAD_ONLY,
    SYNC_DIRECTION_DOWNLOAD_ONLY,
)


# =============================================================================
# Hostname Validation
# =============================================================================

# Bare-hostname charset shared by the CLI host parser (``cli.py``), the
# ``HSYNC_SERVER`` URL validator (``config.py``) and the shared endpoint parser
# (``hsync_common.endpoint``). SSOT lives in ``hsync_common.constants``; the
# name is re-exported from the import block above.


# =============================================================================
# Client ID Generation
# =============================================================================

# Client ID Generation (NanoID-style)
# 21 characters with a strictly alphanumeric 62-char alphabet = ~125 bits of entropy.
# The alphabet matches the wire contract (hsync_common.models.event.CID_PATTERN):
# no `-`/`_`, so a generated CID always starts with a letter or digit.
CID_LENGTH = 21
CID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


# =============================================================================
# File Stability Check
# =============================================================================

# Default interval (seconds) to wait between file size/mtime checks
# before considering a file stable and ready for upload.
# 5 seconds is a reasonable compromise.
FILE_STABILITY_INTERVAL_DEFAULT = 5

# Fast re-check interval (seconds) used when an upload is triggered by an
# on_closed (IN_CLOSE_WRITE) event: the close already signals write completion,
# so a single short re-check is enough to absorb edge cases (NFS chunked writes,
# rapid multi-close, non-atomic saves) before trusting the close and uploading.
STABILITY_DELAY_ON_CLOSED = 0.3

# Limits for stability delay configuration
STABILITY_DELAY_MIN = 1
STABILITY_DELAY_MAX = 60

# Max wait timeout for file stability (seconds)
# Maximum time to wait for a file to stop being written before giving up
MAX_WAIT_TIMEOUT_MIN = 10
MAX_WAIT_TIMEOUT_MAX = 3600
MAX_WAIT_TIMEOUT_DEFAULT = 120  # 2 minutes

# When a file never stabilizes within MAX_WAIT_TIMEOUT the upload is rescheduled
# for a later attempt instead of being forced, avoiding a "torn" upload of
# a file still being written. After UPLOAD_MAX_RESCHEDULES deferrals the file is
# skipped and left to the next file-system event (on_closed) or periodic resync.
UPLOAD_RESCHEDULE_DELAY_DEFAULT = 30  # seconds between deferred attempts
UPLOAD_MAX_RESCHEDULES_DEFAULT = 5


# =============================================================================
# Parallel Transfer Settings
# =============================================================================

# Maximum number of concurrent file transfers (downloads/uploads)
# Higher values increase throughput but also server load.
# Can be overridden via HSYNC_MAX_CONCURRENT_TRANSFERS environment variable
# or --max-concurrent-transfers CLI option.
MAX_CONCURRENT_TRANSFERS_DEFAULT = 5
MAX_CONCURRENT_TRANSFERS_MIN = 1
MAX_CONCURRENT_TRANSFERS_MAX = 20

# =============================================================================
# Per-File Transfer Retry/Backoff
# =============================================================================

# A single transient network blip (timeout, connection reset, 5xx) must not
# leave a file unsynced until the next full sync: each download/upload is
# retried with exponential backoff. Permanent errors (4xx) are never retried.
TRANSFER_RETRY_ATTEMPTS_DEFAULT = 3
TRANSFER_RETRY_MIN_WAIT_DEFAULT = 1  # seconds
TRANSFER_RETRY_MAX_WAIT_DEFAULT = 10  # seconds

# =============================================================================
# Server Connectivity Check Retry
# =============================================================================

# The initial connectivity check (health endpoint) must be more patient than
# per-file transfer retries: a server restart can take 15-30 s. Exponential
# backoff with multiplier=1: 1s → 2s → 4s → 8s → 16s (~31 s across 5 attempts).
CONNECTIVITY_RETRY_ATTEMPTS_DEFAULT = 7
CONNECTIVITY_RETRY_MIN_WAIT_DEFAULT = 1  # seconds
CONNECTIVITY_RETRY_MAX_WAIT_DEFAULT = 30  # seconds

# =============================================================================
# Mass-Deletion Guard
# =============================================================================

# A master that (temporarily) exposes an empty or wrong bucket must not be able
# to wipe valid local data in a single reconciliation cycle. When a cycle would
# delete more than an absolute count or a percentage of the local files, the
# deletions are paused (skipped) and surfaced for confirmation instead. A master
# with 0 files is always treated as a suspect state, never as a delete command.
MASS_DELETE_GUARD_DEFAULT = True
MASS_DELETE_THRESHOLD_DEFAULT = 50  # absolute deletions per cycle (0 = disabled)
MASS_DELETE_PERCENT_DEFAULT = 50  # percentage of total local files (0 = disabled)

# =============================================================================
# Server Connection
# =============================================================================

# Default HTTPS port of the remote hsync server when `hsync server HOSTNAME`
# runs without `--port`. Matches the hsync-server default (HSYNC_PORT).
SERVER_DEFAULT_PORT = 9753

# =============================================================================
# CLI Validation Limits
# =============================================================================

# Bandwidth limits for CLI validation (bytes per second)
BANDWIDTH_MIN = 1024  # 1 KB/s minimum
BANDWIDTH_MAX = 100 * 1024 * 1024  # 100 MB/s maximum

# Reconnection delay limits (seconds)
RECONNECT_DELAY_MIN = 1
RECONNECT_DELAY_MAX = 60
RECONNECT_DELAY_DEFAULT = 5

# Resync interval limits (seconds)
RESYNC_MIN = 20
RESYNC_MAX = 3600
# Periodic resync is enabled by default as a safety net against silently
# missed file-system events (e.g. inotify queue overflow). Set HSYNC_RESYNC=0
# to disable it.
RESYNC_DEFAULT = 300

# HTTP timeout limits (seconds)
HTTP_TIMEOUT_MIN = 1
HTTP_TIMEOUT_MAX = 300
HTTP_TIMEOUT_DEFAULT = 30

# P2P heartbeat / peer timeout defaults (seconds)
HEARTBEAT_INTERVAL = 5
PEER_TIMEOUT = 15

# Bounds for the configurable HSYNC_HEARTBEAT_INTERVAL / HSYNC_PEER_TIMEOUT
# overrides. The peer timeout must stay comfortably above the heartbeat so a
# single missed announce (packet loss, transient congestion) never marks a live
# peer stale and triggers a needless re-election. The effective relationship is
# enforced as ``PEER_TIMEOUT >= 2 * HEARTBEAT_INTERVAL`` in ``config.EnvSettings``.
HEARTBEAT_INTERVAL_MIN = 1
HEARTBEAT_INTERVAL_MAX = 60
PEER_TIMEOUT_MIN = 3
PEER_TIMEOUT_MAX = 600

# boot/1 lease cadence (spec ``boot-1.md``: TTL 60 s, client refresh every 30 s).
# The bootnode rate-limits ``query`` to 5/min per (IP, bucket) and
# ``register``/``refresh`` to 10/min, so the client must not poll faster than
# ~12 s; 30 s matches the spec refresh and stays comfortably inside both.
# Distinct from ``HEARTBEAT_INTERVAL`` (mDNS liveness, 5 s): the bootnode cadence
# has its own rate-limit budget, overridable via
# ``HSYNC_BOOTNODE_REFRESH_INTERVAL``.
BOOTNODE_REFRESH_INTERVAL = 30
BOOTNODE_REFRESH_INTERVAL_MIN = 1
BOOTNODE_REFRESH_INTERVAL_MAX = 300

# P2P discovery timeout (seconds). Default is 0 = "wait indefinitely",
# mirroring the client-server behaviour (the client keeps retrying the
# connection forever by default). A finite positive value bounds how long
# the client waits for the initial peer discovery before reporting no peer.
DISCOVERY_TIMEOUT_DEFAULT = 0

# Minimum free disk space limits (MB)
MIN_FREE_DISK_SPACE_MB_MIN = 0  # 0 = disabled
MIN_FREE_DISK_SPACE_MB_MAX = 100_000  # 100 GB
MIN_FREE_DISK_SPACE_MB_DEFAULT = 500  # 500 MB

# Maximum size of a single inbound file (MB), applied to downloads and to
# received P2P uploads as an absolute cap on top of the peer-declared size:
# the effective anti-bomb cap is min(declared, MAX_INBOUND_SIZE). Without an
# absolute cap a hostile peer could declare an arbitrary size and expand a
# decompression bomb up to it on disk before the SHA-256 check discards it.
# The default mirrors the server's max_upload_size (10 GB): larger files
# require raising both.
MAX_INBOUND_SIZE_MB_MIN = 0  # 0 = disabled (cap = declared size only)
MAX_INBOUND_SIZE_MB_MAX = 10_000_000  # 10 TB
MAX_INBOUND_SIZE_MB_DEFAULT = 10_240  # 10 GB

# P2P peer-assisted download defaults (ADR-0028/0029).
P2P_PEER_DOWNLOAD_DEFAULT = True
P2P_PEER_READ_CAP_DEFAULT = 2
P2P_PEER_READ_CAP_MIN = 1
P2P_PEER_READ_CAP_MAX = 32
P2P_PEER_SOURCES_MAX_DEFAULT = 3
P2P_PEER_SOURCES_MAX_MIN = 1
P2P_PEER_SOURCES_MAX_MAX = 8

# Wire-supplied mtimes/ctimes may not exceed now + this skew: a peer asserting
# far-future timestamps would win every mtime-based conflict resolution on
# every other peer (conflict engineering). Consumers: hsync_client.p2p_server,
# hsync_client.p2p_delta.
MTIME_MAX_FUTURE_SKEW_SECONDS = 300

# Per-worker memory budget for the parallel local-file scan/hash pool
# (``sync._get_optimal_workers`` → ``get_files_local``). Each worker runs
# ``_get_single_file_metadata`` → ``File.get`` → ``compute_sha256``
# (``hashlib.file_digest``, streaming) plus a thread stack and a small
# ``File``/``dict``; peak per-worker RSS is a few MiB at most. This pool only
# scans and hashes — compression/transfer buffers live on the transfer path,
# not here — so the budget is a streaming-hash working-set upper bound. Used
# together with the cgroup CPU quota to bound parallelism inside containers
# (``hsync_common.runtime.get_memory_limit_bytes``); on bare metal the memory
# probe returns None and this budget is not applied.
WORKER_MEMORY_BUDGET_BYTES = 8 * 1024 * 1024  # 8 MiB

# =============================================================================
# inotify Watch-Limit Diagnostics (Linux only)
# =============================================================================
# The recursive watcher schedules one inotify watch per sub-directory. When the
# tree approaches fs.inotify.max_user_watches, the kernel drops events silently
# (watchdog does not surface IN_Q_OVERFLOW), so we proactively warn the user.
# Periodic resync (RESYNC) remains the safety net that recovers missed events.
INOTIFY_MAX_USER_WATCHES_PATH = "/proc/sys/fs/inotify/max_user_watches"
# Fraction of the limit at which we emit the warning.
INOTIFY_WATCH_WARN_RATIO = 0.9

# Seconds to back off before attempting to start the file watcher again after a
# WatcherStartError (kernel inotify limits exhausted — Errno 24 instances /
# Errno 28 watches — or descriptor exhaustion that also defeated the polling
# fallback). Retrying at the normal discovery cadence cannot clear a per-user
# kernel limit, and each attempt re-runs an initial sync against the
# master/server, so a long bounded wait keeps the log readable and avoids
# hammering the peer while another process frees resources.
WATCHER_START_RETRY_DELAY_S = 60.0

# =============================================================================
# File-watcher degradation (PollingObserver fallback & inotify recovery)
# =============================================================================
# When an inotify limit is exhausted the watcher downgrades to a PollingObserver
# (higher CPU, ~1 s event latency) instead of blocking realtime sync. While
# degraded, a supervisor periodically probes whether inotify is available again
# and restores it. The first probe fires after this many seconds, then backs off
# exponentially up to the cap, to avoid thrashing while the kernel limit is
# still saturated.
WATCHER_UPGRADE_PROBE_INITIAL_S = 30.0
WATCHER_UPGRADE_PROBE_MAX_S = 300.0
WATCHER_UPGRADE_BACKOFF_FACTOR = 2.0

# Seconds the supervisor waits after a runtime limit is hit before performing the
# deferred downgrade. The delay lets the watchdog dispatch thread finish the
# current event before the observer is swapped from a separate thread.
WATCHER_DOWNGRADE_DELAY_S = 1.0

# Fraction of the soft RLIMIT_NOFILE at which warn_if_open_fds_high warns.
OPEN_FDS_WARN_RATIO = 0.9
