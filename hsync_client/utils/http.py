# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""HTTP/SSL helpers: fingerprint-pinned sessions, request wrappers, TLS hints."""

import threading
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from hsync_client.config import CONFIG
from hsync_client.constants import HTTP_TIMEOUT_DEFAULT

if TYPE_CHECKING:
    from requests import Response, Session

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

    Prefix slicing instead of ``re.sub(r"^http:", …)``: ``^`` without
    ``MULTILINE`` anchors at the string start, so ``startswith`` + a slice is
    exactly equivalent and avoids the regex engine on every URL derivation.
    """
    if url.startswith("https:"):
        return "wss:" + url[6:]
    if url.startswith("http:"):
        return "ws:" + url[5:]
    return url


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
    here, otherwise the Bearer token (API key or P2P key) lands in
    plaintext in the log file. Used by ``main_logic`` (config dump) and
    ``transfer`` (per-request debug logging). Accepts any string-keyed mapping
    (TypedDicts with optional keys included) and returns the same mapping type.
    """
    out: dict[str, object] = dict(headers)
    if out.get("Authorization"):
        out["Authorization"] = "Bearer ****"
    return cast(T, out)


def _config_cert_fingerprint() -> str | None:
    """The pinned peer fingerprint for this run, or ``None``.

    The P2P master and the sync server are mutually exclusive by mode, so at most
    one of ``MASTER_CERT_FINGERPRINT`` / ``SERVER_CERT_FINGERPRINT`` is set; the
    pinned value *is* the trust anchor and standard verification is disabled for it.
    """
    return CONFIG.get("MASTER_CERT_FINGERPRINT") or CONFIG.get("SERVER_CERT_FINGERPRINT")


def get_ssl_verify() -> bool | str:
    """
    Get the SSL verification setting for requests library.

    For P2P mode with MASTER_CERT_FINGERPRINT, or server mode with
    SERVER_CERT_FINGERPRINT, returns False to disable standard SSL verification
    (fingerprint verification is done separately, bound to the request's own TLS
    connection).

    Returns:
        - False if SSL verification is disabled (--insecure or fingerprint mode)
        - Path to CA certificate if --ca-cert is provided
        - True for default SSL verification
    """
    if CONFIG.get("NO_VERIFY_SSL"):
        # --insecure deliberately skips verification; silence the per-request
        # urllib3 warning so it does not flood the logs on every transfer.
        _suppress_insecure_warning()
        return False
    # With a pinned fingerprint, standard verification is replaced by the
    # per-connection fingerprint check (mounted on the session below).
    if _config_cert_fingerprint():
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
    """Build a ``requests.Session`` that pins the peer cert when required.

    When ``MASTER_CERT_FINGERPRINT`` (P2P) or ``SERVER_CERT_FINGERPRINT`` (server)
    is set the session mounts a
    :class:`~hsync_client.ssl_utils.FingerprintHTTPAdapter`, which binds the
    SHA-256 fingerprint check to the very TLS connection that carries the
    request. This eliminates the previous TOCTOU gap where the certificate was
    probed on a separate connection and the real request ran with
    ``verify=False``. Outside fingerprint mode the session behaves like a
    plain one; per-request ``verify`` (from :func:`get_ssl_verify`) still
    governs chain validation for remote mode.

    Returns:
        A configured :class:`requests.Session`.
    """
    fingerprint = (
        _config_cert_fingerprint() if cert_fingerprint is _CONFIG_FINGERPRINT else cert_fingerprint
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
