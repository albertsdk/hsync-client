# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Utility functions for HSYNC Client.

This module provides common utility functions for path handling,
file operations, and data formatting.
"""

import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from os import chmod, walk
from os.path import abspath, normpath
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from requests import Response, Session

from hsync_common.path_validation import (
    FORBIDDEN_FILENAME_CHARS,
    FORBIDDEN_PATH_CHARS,
    has_control_chars,
    has_forbidden_chars,
    has_problematic_trailing,
    is_reserved_name,
)
from hsync_common.permissions import get_permissions as _read_permissions
from hsync_common.permissions import octal_to_mode
from loguru import logger as log

from hsync_client.config import CONFIG
from hsync_client.constants import (
    CID_ALPHABET,
    CID_LENGTH,
    INOTIFY_MAX_USER_WATCHES_PATH,
    INOTIFY_WATCH_WARN_RATIO,
    ActionLabel,
)

# =============================================================================
# SSL Utilities
# =============================================================================

# Track if we've already suppressed the InsecureRequestWarning
_insecure_warning_suppressed = False


def _suppress_insecure_warning() -> None:
    """Suppress urllib3 InsecureRequestWarning when using fingerprint verification."""
    global _insecure_warning_suppressed
    if not _insecure_warning_suppressed:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        _insecure_warning_suppressed = True


def get_ssl_verify() -> bool | str:
    """
    Get the SSL verification setting for requests library.

    For LAN mode with MASTER_CERT_FINGERPRINT, returns False to disable
    standard SSL verification (fingerprint verification is done separately).

    Returns:
        - False if SSL verification is disabled (--insecure or LAN fingerprint mode)
        - Path to CA certificate if --ca-cert is provided
        - True for default SSL verification
    """
    if CONFIG.get("NO_VERIFY_SSL"):
        # --insecure deliberately skips verification; silence the per-request
        # urllib3 warning so it does not flood the logs on every transfer.
        _suppress_insecure_warning()
        return False
    # In LAN mode with fingerprint, we disable standard SSL verification
    # and verify the fingerprint manually before each request
    if CONFIG.get("MASTER_CERT_FINGERPRINT"):
        # Suppress the InsecureRequestWarning since we verify via fingerprint
        _suppress_insecure_warning()
        return False
    if CONFIG.get("CA_CERT"):
        return str(CONFIG["CA_CERT"])
    return True


def get_http_session() -> "Session":
    """Build a ``requests.Session`` that pins the LAN master cert when required.

    When ``MASTER_CERT_FINGERPRINT`` is set the session mounts a
    :class:`~hsync_client.ssl_utils.FingerprintHTTPAdapter`, which binds the
    SHA-256 fingerprint check to the very TLS connection that carries the
    request. This eliminates the previous TOCTOU gap where the certificate was
    probed on a separate connection and the real request ran with
    ``verify=False``. Outside LAN fingerprint mode the session behaves like a
    plain one; per-request ``verify`` (from :func:`get_ssl_verify`) still
    governs chain validation for remote mode.

    Returns:
        A configured :class:`requests.Session`.
    """
    from requests import Session

    session = Session()
    fingerprint = CONFIG.get("MASTER_CERT_FINGERPRINT")
    if fingerprint:
        from hsync_client.ssl_utils import FingerprintHTTPAdapter

        _suppress_insecure_warning()
        session.mount("https://", FingerprintHTTPAdapter(str(fingerprint)))
    return session


def http_get(url: str, **kwargs) -> "Response":
    """GET ``url`` through a fingerprint-pinned session (see :func:`get_http_session`)."""
    return get_http_session().get(url, **kwargs)


def http_post(url: str, **kwargs) -> "Response":
    """POST to ``url`` through a fingerprint-pinned session."""
    return get_http_session().post(url, **kwargs)


def http_delete(url: str, **kwargs) -> "Response":
    """DELETE ``url`` through a fingerprint-pinned session."""
    return get_http_session().delete(url, **kwargs)


# OpenSSL signatures emitted when an HTTPS client speaks to a plaintext HTTP
# server. The exact code varies across OpenSSL versions, so we match the stable
# family of record/protocol-mismatch errors rather than a single string.
_PLAINTEXT_TLS_SIGNATURES: tuple[str, ...] = (
    "WRONG_VERSION_NUMBER",
    "RECORD_LAYER_FAILURE",
    "UNEXPECTED_EOF_WHILE_READING",
    "RECORD_TOO_SMALL",
)


def diagnose_connection_error(exc: BaseException, server_url: str) -> str | None:
    """Return an actionable hint for common TLS connection failures.

    Detects the frequent misconfiguration where the client targets an
    ``https://`` URL but the server only speaks plaintext HTTP: the TLS
    handshake then fails with an OpenSSL record/protocol-mismatch error that is
    otherwise opaque. Also recognizes certificate-verification failures.

    Returns ``None`` when *exc* is not an SSL error or matches no known pattern,
    so callers can fall back to their generic message.
    """
    from requests.exceptions import SSLError

    if not isinstance(exc, SSLError):
        return None
    detail = str(exc).upper()
    if any(sig in detail for sig in _PLAINTEXT_TLS_SIGNATURES):
        return (
            f"TLS handshake failed for '{server_url}': the server appears to speak "
            "plaintext HTTP, not HTTPS. Use an http:// server URL, or enable TLS on "
            "the server (HSYNC_SSL_ENABLED=true)."
        )
    if "CERTIFICATE_VERIFY_FAILED" in detail:
        return (
            f"TLS certificate verification failed for '{server_url}'. Provide the "
            "server CA with --ca-cert, or use --insecure for a self-signed certificate."
        )
    return None


def http_patch(url: str, **kwargs) -> "Response":
    """PATCH ``url`` through a fingerprint-pinned session."""
    return get_http_session().patch(url, **kwargs)


# =============================================================================
# Path Utilities
# =============================================================================


def is_valid_filename(filename: str) -> bool:
    """
    Validate a filename for cross-platform compatibility.

    Checks for:
    - Empty filename
    - Forbidden characters (cross-platform)
    - Control characters (ASCII 1-31)
    - Reserved names on Windows (including with extensions, e.g., NUL.txt)
    - Problematic trailing characters (space, period)

    Args:
        filename: The filename to validate (without path).

    Returns:
        True if the filename is valid, False otherwise.
    """
    if not filename:
        return False

    if has_forbidden_chars(filename, FORBIDDEN_FILENAME_CHARS):
        return False

    if has_control_chars(filename):
        return False

    if is_reserved_name(filename):
        return False

    return not has_problematic_trailing(filename)


def is_valid_logfile(path_string: str) -> bool:
    """
    Validate a log file path for correctness.

    Checks for:
    - Forbidden characters (cross-platform)
    - Control characters (ASCII 1-31)
    - Reserved names on Windows (including with extensions, e.g., NUL.txt)
    - Problematic trailing characters (space, period)
    - Absolute path requirement when directory is specified

    Args:
        path_string: Path to validate.

    Returns:
        True if the path is valid, False otherwise.
    """
    try:
        path = Path(path_string)

        # If there's no parent directory, assume it's just a filename
        if path.parent == Path("."):
            name = path.name

            # Check for forbidden characters in the filename
            if has_forbidden_chars(name, FORBIDDEN_FILENAME_CHARS):
                return False

            # Check for control characters
            if has_control_chars(name):
                return False

            # Check for Windows reserved names (including with extensions)
            if is_reserved_name(name):
                return False

            # Check for problematic trailing characters
            if has_problematic_trailing(name):
                return False

        else:
            # Check for absolute path if a directory is present
            if not path.is_absolute():
                return False

            # Check each part of the path for forbidden and control characters
            for part in path.parts:
                if has_forbidden_chars(part, FORBIDDEN_PATH_CHARS):
                    return False
                if has_control_chars(part):
                    return False

        return True
    except Exception:
        return False


def is_same_paths(path1: str, path2: str) -> bool:
    """
    Check if two paths refer to the same location.

    Normalizes and resolves paths to absolute form before comparison.

    Args:
        path1: First path to compare.
        path2: Second path to compare.

    Returns:
        True if paths are equivalent, False otherwise.
    """
    return normpath(abspath(path1)) == normpath(abspath(path2))


def safe_rmdir_if_empty(dir_path: str, bucket_root: str | None = None) -> bool:
    """
    Remove *dir_path* only if it is empty and strictly inside *bucket_root*.

    Acts as a safety wrapper around ``os.rmdir`` to prevent accidental
    removal of the LOCAL_BUCKET root (which would destroy the sync target).

    Args:
        dir_path: Directory to remove if empty.
        bucket_root: Path that must NOT be removed. Defaults to
            ``CONFIG["LOCAL_BUCKET"]`` when not provided.

    Returns:
        True if the directory was removed, False otherwise.
    """
    from os import listdir, rmdir

    if not dir_path or not Path(dir_path).exists():
        return False
    if bucket_root is None:
        bucket_root = CONFIG.get("LOCAL_BUCKET", "")
    if bucket_root and is_same_paths(dir_path, bucket_root):
        return False
    try:
        if listdir(dir_path):
            return False
        rmdir(dir_path)
        return True
    except OSError:
        return False


def ensure_str_path(path: str | bytes | None) -> str:
    """
    Convert a path to string if it's bytes.

    Watchdog events can return paths as bytes or str depending on the platform.
    This function ensures we always work with strings.

    Args:
        path: Path as string, bytes, or None.

    Returns:
        Path as string (empty string if None).
    """
    if path is None:
        return ""
    if isinstance(path, bytes):
        return path.decode("utf-8")
    return str(path)


# =============================================================================
# File-System Watcher Diagnostics
# =============================================================================


def warn_if_inotify_watch_limit_low(watch_path: str) -> None:
    """
    Warn when *watch_path* may exceed the inotify watch limit (Linux only).

    The recursive watcher schedules one inotify watch per sub-directory.
    When the tree approaches ``fs.inotify.max_user_watches`` the kernel
    starts dropping events silently (watchdog does not surface
    ``IN_Q_OVERFLOW``), so we proactively warn and point at the remedy.
    Periodic resync (``RESYNC``) is the safety net that recovers any
    events missed in the meantime.

    This is best-effort diagnostics: any failure is swallowed so it can
    never block the watcher from starting.

    Args:
        watch_path: Directory tree that will be watched recursively.
    """
    try:
        limit_file = Path(INOTIFY_MAX_USER_WATCHES_PATH)
        if not limit_file.exists():
            return  # Not Linux / inotify limit not exposed.

        max_watches = int(limit_file.read_text().strip())
        if max_watches <= 0:
            return

        threshold = int(max_watches * INOTIFY_WATCH_WARN_RATIO)

        # Count directories, stopping as soon as we cross the threshold to
        # avoid walking a huge tree just to emit a warning.
        dir_count = 0
        for _root, _dirs, _files in walk(watch_path):
            dir_count += 1
            if dir_count > threshold:
                break

        if dir_count > threshold:
            log.warning(
                f"Directory tree under '{watch_path}' has at least {dir_count} "
                f"sub-directories, close to the inotify limit "
                f"(fs.inotify.max_user_watches={max_watches}). File-system events "
                f"may be dropped silently. Raise the limit, e.g.: "
                f"sudo sysctl fs.inotify.max_user_watches={max_watches * 4}. "
                f"Periodic resync (RESYNC) mitigates any events missed meanwhile."
            )
    except Exception as exc:  # pragma: no cover - diagnostics must never break startup
        log.debug(f"inotify watch-limit check skipped: {exc}")


# =============================================================================
# Per-Path Serialization
# =============================================================================

# One re-usable lock per local path, created on demand and reference-counted so
# the registry never grows unbounded. Operations on the same path (parallel
# transfers, watcher-triggered uploads, inbound downloads/deletes) run in worker
# threads; serializing them here prevents interleaved writes that would silently
# lose an update on a single path.
_path_locks: dict[str, list] = {}
_path_locks_guard = threading.Lock()


@contextmanager
def path_lock(path: str) -> Iterator[None]:
    """Serialize concurrent filesystem operations on a single local *path*.

    Returns a context manager holding an exclusive, process-wide lock keyed on
    the normalized absolute path. Distinct paths never block one another; the
    per-path lock entry is dropped once no waiter holds it so the registry stays
    bounded.

    Args:
        path: Local filesystem path to serialize on.
    """
    key = normpath(abspath(path))
    with _path_locks_guard:
        entry = _path_locks.get(key)
        if entry is None:
            entry = [threading.Lock(), 0]
            _path_locks[key] = entry
        entry[1] += 1
        lock = entry[0]
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _path_locks_guard:
            entry = _path_locks.get(key)
            if entry is not None:
                entry[1] -= 1
                if entry[1] <= 0:
                    del _path_locks[key]


# =============================================================================
# Client ID Generation
# =============================================================================


def get_cid(length: int = CID_LENGTH) -> str:
    """
    Generate a cryptographically secure client ID (NanoID-style).

    Creates a unique identifier for this client session used to
    identify the source of events and prevent echo. Uses the secrets
    module for cryptographic security.

    The default configuration provides 126 bits of entropy:
    - 21 characters from a 64-character alphabet (URL-safe)
    - Probability of collision: ~1 in 2^126

    Args:
        length: Length of the ID string (default: CID_LENGTH = 21).

    Returns:
        Cryptographically secure random string (URL-safe).
    """
    return "".join(secrets.choice(CID_ALPHABET) for _ in range(length))


# =============================================================================
# Data Formatting
# =============================================================================


# =============================================================================
# File Operations
# =============================================================================


def get_permissions(obj_path: str) -> str:
    """
    Get file permissions as a canonical octal string.

    Args:
        obj_path: Path to the file or directory.

    Returns:
        Octal permission string like '644'.
    """
    return _read_permissions(obj_path)


def set_permissions(file_path: str, permissions: str) -> None:
    """
    Set file permissions from a canonical octal string.

    Converts the octal permission string (e.g., '644') to a numeric
    mode and applies it to the file.

    Args:
        file_path: Path to the file.
        permissions: Octal permission string (e.g., '644').
    """
    # Skip if permissions already match
    if Path(file_path).exists() and permissions == get_permissions(file_path):
        return

    mode = octal_to_mode(permissions)
    log.debug(f'{ActionLabel.PERMISSION} Set permission "{permissions}" to {file_path}')
    chmod(file_path, mode)


# =============================================================================
# Bandwidth Limiting
# =============================================================================


def parse_bandwidth(value: str | int | None) -> int | None:
    """
    Parse bandwidth value from string or integer.

    Accepts:
    - Integer bytes per second
    - String with suffix: "KB", "MB", "GB", "KB/s", "MB/s", "GB/s"
    - None or 0 for unlimited

    Args:
        value: Bandwidth value to parse.

    Returns:
        Bandwidth in bytes per second, or None for unlimited.

    Raises:
        ValueError: If the value is invalid.
    """
    if value is None or value == 0 or value == "0":
        return None

    if isinstance(value, int):
        if value < 0:
            raise ValueError("Bandwidth must be positive")
        return value

    if isinstance(value, str):
        value = value.strip().upper()
        if not value:
            return None

        # Parse value with suffix
        multipliers = {
            "KB/S": 1024,
            "KB": 1024,
            "MB/S": 1024**2,
            "MB": 1024**2,
            "GB/S": 1024**3,
            "GB": 1024**3,
            "B/S": 1,
            "B": 1,
        }

        for suffix, mult in multipliers.items():
            if value.endswith(suffix):
                num_str = value[: -len(suffix)].strip()
                try:
                    return int(float(num_str) * mult)
                except ValueError:
                    raise ValueError(f"Invalid bandwidth value: {value}")

        # Try parsing as plain number (bytes)
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"Invalid bandwidth value: {value}")

    raise ValueError(f"Invalid bandwidth type: {type(value)}")


# Track bandwidth tokens for rate limiting
_bandwidth_tokens: dict[str, float] = {}
_bandwidth_last_check: dict[str, float] = {}


def reset_bandwidth_throttle() -> None:
    """
    Reset bandwidth throttle state.

    Should be called between sync sessions to avoid stale token
    bucket state carrying over from previous operations.
    """
    _bandwidth_tokens.clear()
    _bandwidth_last_check.clear()


def get_throttle_delay(
    bytes_transferred: int,
    bandwidth_limit: int | None,
    transfer_key: str,
) -> float:
    """
    Calculate delay needed to throttle transfer to bandwidth limit.

    Uses token bucket algorithm for smooth rate limiting.

    Args:
        bytes_transferred: Number of bytes just transferred.
        bandwidth_limit: Maximum bytes per second (None for unlimited).
        transfer_key: Unique key for this transfer (e.g., "upload" or "download").

    Returns:
        Delay in seconds to wait before next chunk (0 if no throttling needed).
    """
    import time

    if bandwidth_limit is None or bandwidth_limit <= 0:
        return 0.0

    current_time = time.monotonic()

    # Initialize with full bucket so the first transfer is not penalised
    if transfer_key not in _bandwidth_tokens:
        _bandwidth_tokens[transfer_key] = float(bandwidth_limit)
        _bandwidth_last_check[transfer_key] = current_time

    # Add tokens based on elapsed time
    elapsed = current_time - _bandwidth_last_check[transfer_key]
    tokens_to_add = elapsed * bandwidth_limit
    _bandwidth_tokens[transfer_key] += tokens_to_add
    _bandwidth_last_check[transfer_key] = current_time

    # Cap tokens at 2x bandwidth to allow burst
    max_tokens = bandwidth_limit * 2
    if _bandwidth_tokens[transfer_key] > max_tokens:
        _bandwidth_tokens[transfer_key] = max_tokens

    # Check if we have enough tokens
    if _bandwidth_tokens[transfer_key] >= bytes_transferred:
        # Consume tokens
        _bandwidth_tokens[transfer_key] -= bytes_transferred
        return 0.0
    else:
        # Calculate delay needed to regenerate tokens
        tokens_needed = bytes_transferred - _bandwidth_tokens[transfer_key]
        delay = tokens_needed / bandwidth_limit
        # Consume all tokens (we'll wait)
        _bandwidth_tokens[transfer_key] = 0.0
        return delay
