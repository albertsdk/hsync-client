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

import atexit
import os
import secrets
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from os import chmod, walk
from os.path import abspath, normpath
from pathlib import Path
from typing import IO, TYPE_CHECKING, cast

if TYPE_CHECKING:
    from requests import Response, Session

from hsync_common.models.tombstone import relpath_in_bucket
from hsync_common.path_validation import (
    FORBIDDEN_FILENAME_CHARS,
    FORBIDDEN_PATH_CHARS,
    has_control_chars,
    has_forbidden_chars,
    has_problematic_trailing,
    is_reserved_name,
    validate_remote_relpath,
)
from hsync_common.permissions import get_permissions as _read_permissions
from hsync_common.permissions import octal_to_mode, sanitize_remote_permissions
from loguru import logger as log

from hsync_client.config import CONFIG
from hsync_client.constants import (
    CID_ALPHABET,
    CID_LENGTH,
    HTTP_TIMEOUT_DEFAULT,
    INOTIFY_MAX_USER_WATCHES_PATH,
    INOTIFY_WATCH_WARN_RATIO,
    OPEN_FDS_WARN_RATIO,
    OWNER_DIRECTORY_ACCESS,
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


def request_headers() -> dict[str, str]:
    """Return a str-only copy of ``CONFIG["REQUEST_HEADERS"]``."""
    req_headers = CONFIG.get("REQUEST_HEADERS")
    if not isinstance(req_headers, dict):
        return {}
    return {k: v for k, v in req_headers.items() if isinstance(k, str) and isinstance(v, str)}


def http_to_ws(url: str) -> str:
    """Return the WebSocket URL for an HTTP(S) *url*.

    Replaces the ``http``/``https`` scheme with ``ws``/``wss``. Both run
    modes use this to derive the events endpoint from the REST base URL.
    """
    from re import sub

    return sub(r"^https:", "wss:", sub(r"^http:", "ws:", url))


def http_connect_timeout() -> float:
    """Connect timeout honouring the configured HTTP timeout."""
    return float(CONFIG.get("HTTP_TIMEOUT", HTTP_TIMEOUT_DEFAULT))


def upload_timeout() -> tuple[float, None]:
    """Upload-style timeout: connect-bounded, read-unbounded."""
    return (http_connect_timeout(), None)


def redact_headers[T: Mapping[str, object]](headers: T) -> T:
    """Return a copy of *headers* with the ``Authorization`` value masked.

    ``safe_json_for_log`` escapes loguru markup but does NOT redact secrets:
    every log/data path that serialises request headers must route through
    here, otherwise the Bearer token (API key or ``HSYNC_P2P_KEY``) lands in
    plaintext in the log file. Used by ``main_logic`` (config dump) and
    ``transfer`` (per-request debug logging). Accepts any string-keyed mapping
    (TypedDicts with optional keys included) and returns the same mapping type.
    """
    out: dict[str, object] = dict(headers)
    if out.get("Authorization"):
        out["Authorization"] = "Bearer ****"
    return cast(T, out)


def get_ssl_verify() -> bool | str:
    """
    Get the SSL verification setting for requests library.

    For P2P mode with MASTER_CERT_FINGERPRINT, returns False to disable
    standard SSL verification (fingerprint verification is done separately).

    Returns:
        - False if SSL verification is disabled (--insecure or P2P fingerprint mode)
        - Path to CA certificate if --ca-cert is provided
        - True for default SSL verification
    """
    if CONFIG.get("NO_VERIFY_SSL"):
        # --insecure deliberately skips verification; silence the per-request
        # urllib3 warning so it does not flood the logs on every transfer.
        _suppress_insecure_warning()
        return False
    # In P2P mode with fingerprint, we disable standard SSL verification
    # and verify the fingerprint manually before each request
    if CONFIG.get("MASTER_CERT_FINGERPRINT"):
        # Suppress the InsecureRequestWarning since we verify via fingerprint
        _suppress_insecure_warning()
        return False
    if CONFIG.get("CA_CERT"):
        return str(CONFIG["CA_CERT"])
    return True


_CONFIG_FINGERPRINT = object()


def get_http_session(
    cert_fingerprint: str | None | object = _CONFIG_FINGERPRINT,
) -> "Session":
    """Build a ``requests.Session`` that pins the P2P master cert when required.

    When ``MASTER_CERT_FINGERPRINT`` is set the session mounts a
    :class:`~hsync_client.ssl_utils.FingerprintHTTPAdapter`, which binds the
    SHA-256 fingerprint check to the very TLS connection that carries the
    request. This eliminates the previous TOCTOU gap where the certificate was
    probed on a separate connection and the real request ran with
    ``verify=False``. Outside P2P fingerprint mode the session behaves like a
    plain one; per-request ``verify`` (from :func:`get_ssl_verify`) still
    governs chain validation for remote mode.

    Returns:
        A configured :class:`requests.Session`.
    """
    fingerprint = (
        CONFIG.get("MASTER_CERT_FINGERPRINT")
        if cert_fingerprint is _CONFIG_FINGERPRINT
        else cert_fingerprint
    )
    cache_key = str(fingerprint) if fingerprint else ""

    # Lazy-init the per-thread session store.
    store = getattr(_http_session_cache, "store", None)
    if store is None:
        store = {}
        _http_session_cache.store = store

    session = store.get(cache_key)
    if session is not None:
        return session

    from requests import Session

    session = Session()
    if fingerprint:
        from hsync_client.ssl_utils import FingerprintHTTPAdapter

        _suppress_insecure_warning()
        session.mount("https://", FingerprintHTTPAdapter(str(fingerprint)))
        # The pinned fingerprint IS the P2P trust anchor (TOFU): the environment
        # must not add a second, conflicting one. `trust_env = False` stops
        # `requests` from consulting `*_proxy`, `REQUESTS_CA_BUNDLE` /
        # `CURL_CA_BUNDLE` and `.netrc` for these sessions — a peer-to-peer P2P
        # connection routed through an environment proxy would leave the
        # certificate pinning meaningless, and an environment CA bundle is
        # irrelevant when trust is the pinned fingerprint.
        #
        # It is also a measured cost: `requests` resolves the environment proxy
        # configuration on EVERY request (`Session.merge_environment_settings` ->
        # `get_environ_proxies` -> `getproxies_environment` +
        # `proxy_bypass_environment`, which walk `os.environ`). On the P2P burst
        # that is 5 calls per transferred file, 3.8% of the apply-path profile
        # (~0.6-0.85 ms CPU/file at ~90 files/s).
        session.trust_env = False
    store[cache_key] = session
    return session


# Per-thread session cache. ``threading.local()`` isolates sessions across
# the transfer thread pool: each worker reuses its own Session (keep-alive,
# connection pooling) without contention, while no two threads share a Session.
_http_session_cache = threading.local()


def http_get(
    url: str,
    *,
    cert_fingerprint: str | None | object = _CONFIG_FINGERPRINT,
    **kwargs,
) -> "Response":
    """GET ``url`` through a fingerprint-pinned session (see :func:`get_http_session`)."""
    return get_http_session(cert_fingerprint).get(url, **kwargs)


def http_post(
    url: str,
    *,
    cert_fingerprint: str | None | object = _CONFIG_FINGERPRINT,
    **kwargs,
) -> "Response":
    """POST to ``url`` through a fingerprint-pinned session."""
    return get_http_session(cert_fingerprint).post(url, **kwargs)


def http_delete(
    url: str,
    *,
    cert_fingerprint: str | None | object = _CONFIG_FINGERPRINT,
    **kwargs,
) -> "Response":
    """DELETE ``url`` through a fingerprint-pinned session."""
    return get_http_session(cert_fingerprint).delete(url, **kwargs)


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


def http_patch(
    url: str,
    *,
    cert_fingerprint: str | None | object = _CONFIG_FINGERPRINT,
    **kwargs,
) -> "Response":
    """PATCH ``url`` through a fingerprint-pinned session."""
    return get_http_session(cert_fingerprint).patch(url, **kwargs)


# =============================================================================
# Path Utilities
# =============================================================================


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


def path_present(filepath: str) -> bool:
    """Return whether *filepath* exists as a file/dir/link (dangling links too).

    ``Path.exists()`` follows symlinks: a dangling link reports ``False`` even
    though the directory entry exists. Callers that act on the entry itself
    (inbound delete/rename dispatch, the delete-broadcast flush) must see both
    cases, so a symlink entry always counts as present.

    Consumers: ``hsync_client.event_dispatch`` (inbound delete/rename) and
    ``hsync_client.file_event_handler`` (stale-delete guard in the coalesced
    delete flush).
    """
    path = Path(filepath)
    return path.is_symlink() or path.exists()


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
    if not dir_path or not Path(dir_path).exists():
        return False
    if bucket_root is None:
        bucket_root = CONFIG.get("LOCAL_BUCKET", "")
    if bucket_root and is_same_paths(dir_path, bucket_root):
        return False
    try:
        if os.listdir(dir_path):
            return False
        os.rmdir(dir_path)
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


def cid_suffix(cid: object) -> str:
    """Return a ``" (CID: <cid>)"`` log suffix, or ``""`` when *cid* is falsy."""
    return f" (CID: {cid})" if cid else ""


def normalize_bucket_relpath(raw_path: str, bucket_root: str) -> str | None:
    """Return the canonical bucket-relative path for a local path, or ``None``.

    Converts an absolute path under *bucket_root* to its relative form and then
    validates it with ``validate_remote_relpath``. Returns ``None`` for empty,
    out-of-bucket, or invalid paths (including lone-surrogate paths that cannot
    be UTF-8 encoded for the wire).
    """
    candidate = raw_path
    if Path(candidate).is_absolute():
        candidate = relpath_in_bucket(candidate, bucket_root)
        if candidate is None:
            return None
    try:
        return validate_remote_relpath(candidate)
    except UnicodeError:
        return None


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


def warn_if_open_fds_high() -> None:
    """Warn when the process is close to its open-file-descriptor limit.

    Best-effort diagnostics (Linux): compares the number of currently open file
    descriptors (``/proc/self/fd``) against the soft ``RLIMIT_NOFILE`` and warns
    when the process is within a fraction of it, so an impending "Too many open
    files" failure is visible before it happens. Any failure is swallowed so it
    can never block the watcher from starting.
    """
    try:
        import resource

        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft in (resource.RLIM_INFINITY, -1):
            return
        fd_count = len(os.listdir("/proc/self/fd"))
        if fd_count >= int(soft * OPEN_FDS_WARN_RATIO):
            log.warning(
                f"Process has {fd_count} open file descriptors, close to the soft "
                f"limit ({soft}). Raise it (e.g. `ulimit -n {soft * 4}`) or reduce "
                f"concurrent connections/transfers to avoid 'Too many open files' "
                f"errors."
            )
    except Exception as exc:  # pragma: no cover - diagnostics must never break startup
        log.debug(f"open-file-descriptor check skipped: {exc}")


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

# In-flight transfer artifacts (``<final>.part``, ``<final>.delta.part`` and
# the parked ``<final>.delta.part.lit`` payload), held only for the duration of
# the stream that writes them. ``sync._cleanup_partial_files`` sweeps the bucket
# for leftover artifacts at the start of every cycle to clear what a *previous*
# process left behind; without this registry that sweep also deletes artifacts
# a concurrently running transfer is still writing, because inbound downloads
# (P2P fan-out, remote event loop) run outside the sync cycle that owns the
# sweep. The writer keeps a valid descriptor either way, so the transfer would
# only fail at its final atomic rename — silently, as an "Error saving file"
# with no retry — leaving the path unsynced until the next cycle.
_inflight_partials: set[str] = set()
_inflight_partials_guard = threading.Lock()


def register_inflight_partial(path: str) -> None:
    """Mark *path* as an in-flight artifact until it is unregistered.

    Consumers: ``hsync_client.transfer`` (full/compressed/resumed download)
    and ``hsync_client.delta_transfer`` (delta rebuild and its parked literal
    payload), both read by ``sync._cleanup_partial_files``.
    """
    with _inflight_partials_guard:
        _inflight_partials.add(path)


def unregister_inflight_partial(path: str) -> None:
    """Release *path* from the in-flight registry (idempotent)."""
    with _inflight_partials_guard:
        _inflight_partials.discard(path)


def is_inflight_partial(path: str) -> bool:
    """Return whether *path* is currently being written by a transfer."""
    with _inflight_partials_guard:
        return path in _inflight_partials


def open_partial_no_follow(path: str, write_mode: str) -> IO[bytes]:
    """Open a ``.part`` file without following planted symlinks.

    Supports write modes (``wb``/``ab``) and read mode (``rb``) — the latter is
    used by download resume to ingest the already-downloaded prefix into the
    incremental hasher. Uses ``O_NOFOLLOW`` where the platform supports it so
    a symlink raced in between path validation and file creation cannot
    redirect the access outside the bucket. On platforms without
    ``O_NOFOLLOW`` (Windows) the flag is omitted and the confinement checks
    remain the defense.
    """
    if "r" in write_mode:
        flags = os.O_RDONLY
    else:
        flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if "a" in write_mode else os.O_TRUNC)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(path, flags, 0o666), write_mode)


def inbound_size_cap(declared_size: int | None, max_inbound_size: int) -> int | None:
    """Effective cap in bytes on the bytes written to disk for one inbound file.

    Single implementation of the anti decompression-bomb bound shared by
    downloads (`transfer.py`) and P2P-master received uploads (`p2p_server.py`).
    The peer-declared size alone is not a safe bound: a hostile peer can
    declare an arbitrary size and expand a bomb (or stream an undeclared-length
    body) up to it on disk before the SHA-256 check discards the content.

    Args:
        declared_size: The peer-declared file size (``None``/``0`` = unknown).
        max_inbound_size: The absolute ``MAX_INBOUND_SIZE`` config in bytes
            (``0`` = disabled): caps the declared value, and is the only bound
            when no size is declared.

    Returns ``None`` when uncapped (no declared size and the absolute cap
    disabled); ``0`` means "nothing may be written" (callers requiring a
    numeric cap map ``None`` to their own strict default).
    """
    declared = declared_size or 0
    if max_inbound_size > 0:
        return min(declared, max_inbound_size) if declared > 0 else max_inbound_size
    return declared if declared > 0 else None


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

    The default configuration provides ~125 bits of entropy:
    - 21 characters from a strictly alphanumeric 62-character alphabet
    - Probability of collision: ~1 in 2^125

    The alphabet is strictly alphanumeric (`A-Za-z0-9`, matching the
    ``hsync_common.models.event.CID_PATTERN`` wire contract), so a generated
    CID always starts with a letter or digit — never ``-`` or ``_``.

    Args:
        length: Length of the ID string (default: CID_LENGTH = 21).

    Returns:
        Cryptographically secure random alphanumeric string.
    """
    return "".join(secrets.choice(CID_ALPHABET) for _ in range(length))


# =============================================================================
# Data Formatting
# =============================================================================


# =============================================================================
# Ready File (test harness signalling)
# =============================================================================

_HSYNC_READY_FILE_PATH: str | None = None
_HSYNC_READY_ATEXIT_REGISTERED = False


def _atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Write *text* to *path* atomically (temp file + rename), owner-only.

    A concurrent reader either sees the previous complete content or the new
    one — never a partially written file. Generic helper shared by the
    test-harness ready file and the persistent device-identity file.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_ready_file() -> None:
    """Signal that this client is ready (initial sync complete and serving).

    Only writes when ``HSYNC_READY_FILE`` is set in the environment (test
    harness). The file contains the process PID, written atomically, so the
    harness can compare it with the expected subprocess PID — never trusting
    a stale or half-written file left by a killed predecessor.
    """
    ready_path = os.environ.get("HSYNC_READY_FILE")
    if not ready_path:
        return
    global _HSYNC_READY_FILE_PATH, _HSYNC_READY_ATEXIT_REGISTERED
    _HSYNC_READY_FILE_PATH = ready_path
    _atomic_write_text(Path(ready_path), f"{os.getpid()}\n")
    if not _HSYNC_READY_ATEXIT_REGISTERED:
        atexit.register(_remove_ready_file)
        _HSYNC_READY_ATEXIT_REGISTERED = True


def _remove_ready_file() -> None:
    """Remove the ready file (atexit handler).  Idempotent."""
    if _HSYNC_READY_FILE_PATH:
        try:
            Path(_HSYNC_READY_FILE_PATH).unlink(missing_ok=True)
        except OSError:
            pass  # best-effort


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


def _apply_permissions(path: str, permissions: str, *, extra_mode_bits: int = 0) -> None:
    """Apply sanitized remote permissions to *path*, OR-ing *extra_mode_bits*."""
    permissions = sanitize_remote_permissions(permissions)

    # Skip if permissions already match
    if Path(path).exists() and permissions == get_permissions(path):
        return

    mode = octal_to_mode(permissions) | extra_mode_bits
    log.debug(f'{ActionLabel.PERMISSION} Set permission "{permissions}" to {path}')
    chmod(path, mode)


def set_permissions(file_path: str, permissions: str) -> None:
    """
    Set file permissions from a canonical octal string.

    Converts the octal permission string (e.g., '644') to a numeric
    mode and applies it to the file.

    Every call site applies a value received from a remote peer (download
    metadata, inbound events, P2P uploads), so special bits
    (setuid/setgid/sticky) are always stripped: a synced file carrying
    ``4755`` must never become a setuid executable on this host.

    Args:
        file_path: Path to the file.
        permissions: Octal permission string (e.g., '644').
    """
    _apply_permissions(file_path, permissions)


def set_permissions_on_dir(dir_path: str, permissions: str) -> None:
    """Set directory permissions as received from a remote peer.

    Same sanitization contract as :func:`set_permissions` (special bits
    stripped — a synced directory must never become setuid/setgid/sticky),
    plus the owner rwx floor that keeps the directory traversable by the
    sync engine and its own storage (mirrors the server's
    ``set_directory_permissions``).
    """
    _apply_permissions(dir_path, permissions, extra_mode_bits=OWNER_DIRECTORY_ACCESS)


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
    _bandwidth_tokens[transfer_key] = min(_bandwidth_tokens[transfer_key], max_tokens)

    # Check if we have enough tokens
    if _bandwidth_tokens[transfer_key] >= bytes_transferred:
        # Consume tokens
        _bandwidth_tokens[transfer_key] -= bytes_transferred
        return 0.0
    # Calculate delay needed to regenerate tokens
    tokens_needed = bytes_transferred - _bandwidth_tokens[transfer_key]
    delay = tokens_needed / bandwidth_limit
    # Consume all tokens (we'll wait)
    _bandwidth_tokens[transfer_key] = 0.0
    return delay
