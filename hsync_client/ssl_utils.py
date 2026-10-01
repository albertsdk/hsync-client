# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
SSL Certificate Utilities for P2P Mode.

This module provides functions to generate self-signed SSL certificates
for secure communication in P2P mode, and utilities for fingerprint-based
certificate verification.

The P2P master identity follows the shared service-identity model (ADR-0037):
ECDSA P-256, an unencrypted PKCS#8 key guarded by ``0o600``/``0o700``
filesystem permissions, and a 20-year validity — the same model as the
``hsync-bootnode`` node key and the ``hsync-server`` TLS certificate. The
certificate is authenticated by pinning its fingerprint, not by WebPKI.
"""

from __future__ import annotations

import ipaddress
import os
import ssl
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from hsync_common.ssl_utils import (
    SERVICE_CERT_VALIDITY_DAYS,
    build_self_signed_certificate,
    compute_cert_fingerprint,
    generate_ecdsa_private_key,
    serialize_cert_pem,
    serialize_private_key_unencrypted,
    verify_cert_fingerprint,
)
from loguru import logger as log
from requests.adapters import DEFAULT_POOLBLOCK, HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

# Persistent P2P master certificate validity: the shared service-identity
# lifetime (20 years, ADR-0037). Identity is established by fingerprint pinning
# (TOFU), not by chain/date validation, so a long validity only avoids the
# operational nuisance of regenerating an expired identity.
MASTER_CERT_VALIDITY_DAYS = SERVICE_CERT_VALIDITY_DAYS

# The keypair lives in the device identity directory; see
# :func:`hsync_client.identity.default_device_dir` (``HSYNC_DEVICE_DIR``).
_MASTER_CERT_FILENAME = "p2p_master_cert.pem"
_MASTER_KEY_FILENAME = "p2p_master_key.pem"


__all__ = [
    "MASTER_CERT_VALIDITY_DAYS",
    "CertificateInfo",
    "FingerprintHTTPAdapter",
    "create_fingerprint_ssl_context",
    "default_master_cert_dir",
    "load_or_create_master_cert",
    "verify_cert_fingerprint",
    "verify_peer_cert_fingerprint",
]


@dataclass
class CertificateInfo:
    """Container for a P2P master certificate, its private key and fingerprint.

    The keypair is persisted on disk (``cert_file``/``key_file``) so the
    fingerprint is stable across restarts. The private key is stored unencrypted
    and protected by filesystem permissions; see
    :func:`load_or_create_master_cert`.
    """

    cert_pem: bytes
    key_pem: bytes
    fingerprint: str
    cert_file: str | None = None
    key_file: str | None = None


def default_master_cert_dir() -> Path:
    """Resolve the P2P master cert dir — delegates to the device identity dir.

    The keypair lives in the device directory (``$HSYNC_DEVICE_DIR``, else an
    OS state dir): see :func:`hsync_client.identity.default_device_dir`, the
    single source of truth. Imported lazily to keep the module graph acyclic
    (``identity`` imports this module).
    """
    from hsync_client.identity import default_device_dir

    return default_device_dir()


def load_or_create_master_cert(common_name: str = "hsync-lan") -> CertificateInfo:
    """Load the persistent P2P master keypair, generating it on first use.

    The keypair lives in the device identity directory (``$HSYNC_DEVICE_DIR``,
    else an OS state directory) so the master's fingerprint stays stable
    across restarts: pinned peers keep trusting it (TOFU) instead of seeing a
    ``[FINGERPRINT CHANGED]`` mismatch every time the master restarts.

    The private key is written unencrypted and protected by filesystem
    permissions (``0o600`` in a ``0o700`` directory), the standard model for
    long-lived self-signed service identities (SSH host keys, Syncthing):
    identity is pinned, not derived from an encrypted-at-rest secret, and on the
    P2P threat model local filesystem access already implies full compromise.

    Returns:
        CertificateInfo with ``cert_file`` and ``key_file`` pointing at the
        persistent keypair.
    """
    cert_dir = default_master_cert_dir()
    cert_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(cert_dir, 0o700)

    cert_path = cert_dir / _MASTER_CERT_FILENAME
    key_path = cert_dir / _MASTER_KEY_FILENAME

    if cert_path.exists() and key_path.exists():
        cert_pem = cert_path.read_bytes()
        key_pem = key_path.read_bytes()
        cert = x509.load_pem_x509_certificate(cert_pem)
        key = serialization.load_pem_private_key(key_pem, password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError(
                f"P2P master identity {key_path} must hold an ECDSA P-256 private "
                "key; delete the keypair to regenerate it under the current model."
            )
        fingerprint = compute_cert_fingerprint(cert)
        log.info(f"Loaded persistent P2P master certificate: {fingerprint[:16]}...")
        return CertificateInfo(
            cert_pem=cert_pem,
            key_pem=key_pem,
            fingerprint=fingerprint,
            cert_file=str(cert_path),
            key_file=str(key_path),
        )

    private_key = generate_ecdsa_private_key()
    cert = build_self_signed_certificate(
        private_key,
        subject_attrs=[
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "HSYNC P2P Mode"),
        ],
        san_list=[
            x509.DNSName("localhost"),
            x509.DNSName("*"),
            x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        ],
        validity_days=MASTER_CERT_VALIDITY_DAYS,
    )
    cert_pem = serialize_cert_pem(cert)
    key_pem = serialize_private_key_unencrypted(private_key)
    fingerprint = compute_cert_fingerprint(cert)

    with open(cert_path, "wb") as f:
        f.write(cert_pem)
    os.chmod(cert_path, 0o600)
    with open(key_path, "wb") as f:
        f.write(key_pem)
    os.chmod(key_path, 0o600)

    log.info(f"Generated persistent P2P master certificate: {fingerprint[:16]}...")
    return CertificateInfo(
        cert_pem=cert_pem,
        key_pem=key_pem,
        fingerprint=fingerprint,
        cert_file=str(cert_path),
        key_file=str(key_path),
    )


def create_fingerprint_ssl_context(expected_fingerprint: str) -> ssl.SSLContext:
    """
    Create an SSL context that verifies certificates by fingerprint.

    This allows connecting to servers with self-signed certificates
    by verifying the certificate's SHA-256 fingerprint instead of
    the certificate chain.

    Args:
        expected_fingerprint: The expected SHA-256 fingerprint (hex string).

    Returns:
        An SSL context configured for fingerprint verification.

    Raises:
        ValueError: If *expected_fingerprint* is empty or not a valid
            SHA-256 hex digest. The context is **never** returned in a
            fail-open state: a missing fingerprint means no trust, not
            blind trust.
    """
    from hsync_common.hashing import is_sha256_hex

    # Fail-closed: an empty or malformed fingerprint must never produce a
    # CERT_NONE context that silently accepts any certificate. The caller
    # must provide a valid pinned fingerprint or the connection cannot be
    # established at all.
    if not expected_fingerprint or not is_sha256_hex(expected_fingerprint):
        raise ValueError("A valid SHA-256 fingerprint is required for fingerprint SSL context")

    ctx = create_urllib3_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    # The fingerprint is enforced by the caller via assert_fingerprint on
    # the urllib3 PoolManager / FingerprintHTTPAdapter; the context itself
    # only disables chain validation (trust is established by the pin).

    return ctx


class FingerprintHTTPAdapter(HTTPAdapter):
    """``requests`` adapter that pins the peer certificate by SHA-256 fingerprint.

    Binds the fingerprint check to the very connection that carries the request
    via urllib3's ``assert_fingerprint``, eliminating the TOCTOU window of
    probing the cert on a separate connection and then requesting with
    ``verify=False``. The certificate chain and hostname are
    intentionally not validated: trust is established solely by the pinned
    fingerprint (TOFU), which is the P2P identity model.
    """

    def __init__(self, fingerprint: str, **kwargs) -> None:
        # urllib3 accepts hex with or without colons; normalise to lowercase.
        self._assert_fingerprint = fingerprint.lower()
        super().__init__(**kwargs)

    def _pin_kwargs(self, kwargs: dict) -> dict:
        ctx = create_urllib3_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        kwargs["assert_fingerprint"] = self._assert_fingerprint
        kwargs["assert_hostname"] = False
        return kwargs

    def init_poolmanager(
        self, connections, maxsize, block=DEFAULT_POOLBLOCK, **pool_kwargs
    ) -> None:
        super().init_poolmanager(connections, maxsize, block=block, **self._pin_kwargs(pool_kwargs))

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        return super().proxy_manager_for(proxy, **self._pin_kwargs(proxy_kwargs))


def verify_peer_cert_fingerprint(ssl_object, expected_fingerprint: str) -> bool:
    """Verify a live TLS connection's peer cert against the pinned fingerprint.

    Used for the WSS slave→master connection, where the certificate must be
    checked on the same connection that carries the data (no separate probe).

    Args:
        ssl_object: The live ``ssl.SSLSocket``/``SSLObject`` of the connection.
        expected_fingerprint: The pinned SHA-256 fingerprint (hex).

    Returns:
        True if the peer presented a certificate matching the fingerprint.
    """
    if not ssl_object:
        return False
    cert_der = ssl_object.getpeercert(binary_form=True)
    if not cert_der:
        return False
    return verify_cert_fingerprint(cert_der, expected_fingerprint)
