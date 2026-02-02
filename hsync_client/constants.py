# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Constants for HSYNC Client.

This module contains all magic numbers and constant values used
throughout the application, making them easy to find and modify.
"""

# =============================================================================
# Application Info
# =============================================================================
from typing import Final, Literal  # noqa: E402

# Re-export shared constants from hsync-common. The plain names keep the
# module's public re-export API consumed across hsync_client (HSYNC_VERSION,
# HSYNCKEEP_FILENAME, PARTIAL_DOWNLOAD_SUFFIX, WS_MAX_MESSAGE_BYTES); the
# aliased header names are re-typed below as string literals because mypy
# requires literal keys for TypedDicts.
from hsync_common.constants import (  # noqa: E402, F401
    CONTENT_TYPE_JSON,
    CONTENT_TYPE_OCTET_STREAM,
    HSYNC_VERSION,
    HSYNCKEEP_FILENAME,
    PARTIAL_DOWNLOAD_SUFFIX,
    WS_MAX_MESSAGE_BYTES,
)
from hsync_common.constants import HEADER_CLIENT_NAME as _HEADER_CLIENT_NAME  # noqa: E402
from hsync_common.constants import HEADER_CLIENT_VERSION as _HEADER_CLIENT_VERSION  # noqa: E402
from hsync_common.constants import (  # noqa: E402
    HEADER_PROTOCOL_VERSION as _HEADER_PROTOCOL_VERSION,
)

# Re-export shared action labels from hsync-common
from hsync_common.labels import ActionLabel  # noqa: E402, F401
from hsync_common.permissions import OWNER_DIRECTORY_ACCESS  # noqa: E402, F401

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

# LAN heartbeat / peer timeout defaults (seconds)
HEARTBEAT_INTERVAL = 5
PEER_TIMEOUT = 15

# LAN discovery timeout (seconds)
# 0 is reserved as the CLI/env sentinel for "wait indefinitely".
DISCOVERY_TIMEOUT_DEFAULT = 10
# Polling window used when discovery waits indefinitely (timeout == 0): how often
# the LAN loop wakes up to re-evaluate peer/master state. Matches the LAN
# heartbeat cadence so we react within one announce cycle without busy-spinning
# while no peer is present.
DISCOVERY_POLL_INTERVAL = HEARTBEAT_INTERVAL

# Minimum free disk space limits (MB)
MIN_FREE_DISK_SPACE_MB_MIN = 0  # 0 = disabled
MIN_FREE_DISK_SPACE_MB_MAX = 100_000  # 100 GB
MIN_FREE_DISK_SPACE_MB_DEFAULT = 500  # 500 MB

# Maximum size of a single inbound file (MB), applied to downloads and to
# received LAN uploads as an absolute cap on top of the peer-declared size:
# the effective anti-bomb cap is min(declared, MAX_INBOUND_SIZE). Without an
# absolute cap a hostile peer could declare an arbitrary size and expand a
# decompression bomb up to it on disk before the SHA-256 check discards it.
# The default mirrors the server's max_upload_size (10 GB): larger files
# require raising both.
MAX_INBOUND_SIZE_MB_MIN = 0  # 0 = disabled (cap = declared size only)
MAX_INBOUND_SIZE_MB_MAX = 10_000_000  # 10 TB
MAX_INBOUND_SIZE_MB_DEFAULT = 10_240  # 10 GB

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
