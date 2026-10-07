# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Durable sync-server trust store and policy resolution (SSH-style).

The sync server can authenticate with a built-in self-signed certificate (typical
on a LAN) or sit behind a reverse proxy with a public-CA certificate. The server
therefore uses the standard CA chain as its default anchor (``chain``), while the
SSH-style pinning policies (``ask``/``accept-new``/``strict``/``none``) are opt-in
for self-signed deployments. This module owns:

* :class:`ServerTrustStore` — the ``server_known_hosts.json`` ``known_hosts`` map
  (the persistence half of :func:`hsync_common.protocol.tls_trust.evaluate_tls_trust`);
* :func:`fetch_server_fingerprint` — a raw TLS probe that reads the presented
  certificate fingerprint *without* trusting it;
* :func:`resolve_server_trust` — the flag/env precedence for the effective policy.

The trust decision itself is the shared SSOT in ``hsync-common`` (ADR-0036).

Consumers: ``hsync-client`` (server connect path, ``hsync trust server``).
"""

from __future__ import annotations

import os
import socket
import ssl
from pathlib import Path

from hsync_common.protocol.tls_trust import TrustPolicy

from hsync_client._trust_store import StoreError
from hsync_client.known_hosts import KnownHostsStore

__all__ = [
    "SERVER_TRUST_FILE_ENV",
    "ServerTrustStore",
    "ServerTrustStoreError",
    "default_server_trust_path",
    "default_server_trust_store",
    "fetch_server_fingerprint",
    "resolve_server_trust",
]

# Environment override for the server trust store (tests, operators keeping state
# elsewhere).
SERVER_TRUST_FILE_ENV = "HSYNC_SERVER_TRUST_FILE"

# Default timeout (seconds) for the raw TLS fingerprint probe.
_FINGERPRINT_PROBE_TIMEOUT = 10.0


class ServerTrustStoreError(StoreError):
    """The durable server trust store could not be read or updated safely."""


def default_server_trust_path() -> Path:
    """Resolve the trust-store path: ``$HSYNC_SERVER_TRUST_FILE`` or an OS state dir."""
    override = os.environ.get(SERVER_TRUST_FILE_ENV)
    if override:
        return Path(override)
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "hsync" / "server_known_hosts.json"


class ServerTrustStore(KnownHostsStore):
    """Disk-backed map of ``host:port`` to a pinned sync-server certificate fingerprint."""

    error_cls = ServerTrustStoreError
    label = "server trust store"
    records_key = "servers"

    def __init__(self, path: Path | None = None) -> None:
        super().__init__(path or default_server_trust_path())


def default_server_trust_store() -> ServerTrustStore:
    """Return a :class:`ServerTrustStore` at the default (env-overridable) location."""
    return ServerTrustStore()


def fetch_server_fingerprint(
    host: str, port: int, timeout: float = _FINGERPRINT_PROBE_TIMEOUT
) -> str | None:
    """Read the SHA-256 fingerprint of the certificate *host:port* presents now.

    A raw TLS handshake with chain/hostname verification disabled, used to show the
    operator (or the store) the presented fingerprint *without* trusting it. The
    actual request still enforces the pin (or the chain) on its own connection.
    Returns the bare lowercase hex digest, or ``None`` on any failure (unreachable,
    no certificate, bad port) — never raises.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError):
        return None
    if not isinstance(der, bytes) or not der:
        return None
    # Lazy import: ``hsync_common.ssl_utils`` pulls ``cryptography``, which must
    # not be loaded just by importing the CLI/trust surface.
    from hsync_common.ssl_utils import fingerprint_from_der

    return fingerprint_from_der(der)


def resolve_server_trust(
    policy: str | None,
    fingerprints: tuple[str, ...],
    tofu: bool,
    verify_ssl: bool,
    *,
    env_default: str | None = None,
) -> TrustPolicy:
    """Resolve the effective server trust policy from flags/env.

    Precedence: ``--insecure`` (``none``) > ``--server-fingerprint`` (``strict``) >
    ``--server-tofu`` (``accept-new``) > the ``--server-trust`` flag >
    ``HSYNC_SERVER_TRUST`` (``env_default``) > the ``chain`` default.
    """
    if not verify_ssl:
        return TrustPolicy.none
    if fingerprints:
        return TrustPolicy.strict
    if tofu:
        return TrustPolicy.accept_new
    explicit = policy or env_default
    return TrustPolicy(explicit) if explicit else TrustPolicy.chain
