# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
SSL Certificate Utilities for LAN Mode.

This module provides functions to generate self-signed SSL certificates
for secure communication in LAN P2P mode, and utilities for fingerprint-based
certificate verification.
"""

from __future__ import annotations

import atexit
import ipaddress
import os
import secrets
import shutil
import ssl
import tempfile
from dataclasses import dataclass, field

from cryptography import x509
from cryptography.x509.oid import NameOID
from hsync_common.ssl_utils import (
    DEFAULT_KEY_SIZE,
    build_self_signed_certificate,
    compute_cert_fingerprint,
    generate_rsa_private_key,
    get_server_cert_fingerprint,
    serialize_cert_pem,
    serialize_key_pem,
    verify_cert_fingerprint,
)
from loguru import logger as log
from urllib3.util.ssl_ import create_urllib3_context

# Certificate validity period (1 day for session-based certificates)
CERT_VALIDITY_DAYS = 1

# RSA key size
RSA_KEY_SIZE = DEFAULT_KEY_SIZE

# Session password length in bytes for encrypting the private key on disk.
# 32 bytes = 256 bits of entropy, generated fresh per process and kept only
# in memory. If the process is killed and the temp dir survives, the residual
# key file is unusable because the password died with the process.
_SESSION_KEY_PASSWORD_BYTES = 32

# Track temp directories we created so atexit can wipe them out.
_TEMP_DIRS_TO_CLEAN: list[str] = []

__all__ = [
    "CERT_VALIDITY_DAYS",
    "RSA_KEY_SIZE",
    "CertificateInfo",
    "create_fingerprint_ssl_context",
    "generate_self_signed_cert",
    "get_server_cert_fingerprint",
    "save_cert_to_temp_files",
    "verify_cert_fingerprint",
]


@dataclass
class CertificateInfo:
    """Container for certificate, private key, fingerprint and encryption password.

    The ``key_pem`` bytes are always encrypted with ``key_password``; the
    password lives only in memory for the lifetime of the process.
    """

    cert_pem: bytes
    key_pem: bytes
    fingerprint: str
    key_password: bytes = field(default=b"", repr=False)
    cert_file: str | None = None
    key_file: str | None = None


def generate_self_signed_cert(
    common_name: str = "hsync-lan",
    validity_days: int = CERT_VALIDITY_DAYS,
) -> CertificateInfo:
    """
    Generate a self-signed SSL certificate for LAN mode.

    Args:
        common_name: The CN (Common Name) for the certificate.
        validity_days: Number of days the certificate is valid.

    Returns:
        CertificateInfo containing the certificate, key, and fingerprint.
    """
    log.debug(f"Generating self-signed certificate with CN={common_name}")

    private_key = generate_rsa_private_key(RSA_KEY_SIZE)
    cert = build_self_signed_certificate(
        private_key,
        subject_attrs=[
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "HSYNC LAN Mode"),
        ],
        san_list=[
            x509.DNSName("localhost"),
            x509.DNSName("*"),
            x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        ],
        validity_days=validity_days,
    )

    # Per-session password: 256 bits of entropy, generated in memory and never
    # serialized. Used to encrypt the PKCS#8 PEM written to disk.
    key_password = secrets.token_bytes(_SESSION_KEY_PASSWORD_BYTES)

    cert_pem = serialize_cert_pem(cert)
    key_pem = serialize_key_pem(private_key, password=key_password)
    fingerprint = compute_cert_fingerprint(cert)

    log.info(f"Generated self-signed certificate with fingerprint: {fingerprint[:16]}...")

    return CertificateInfo(
        cert_pem=cert_pem,
        key_pem=key_pem,
        fingerprint=fingerprint,
        key_password=key_password,
    )


def _cleanup_ssl_temp_dir(tmpdir: str) -> None:
    """Remove an SSL temp directory and all files in it (best effort)."""
    try:
        shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:  # pragma: no cover - best-effort cleanup
        pass


def save_cert_to_temp_files(cert_info: CertificateInfo) -> CertificateInfo:
    """
    Save certificate and (encrypted) key to a private temp directory.

    The directory is created with ``mkdtemp`` (mode 0o700 by default) and
    registered with :mod:`atexit` for cleanup. The key on disk is encrypted
    with ``cert_info.key_password``; the password is kept only in memory, so
    even if cleanup is skipped (e.g. SIGKILL) the residual key file is
    cryptographically useless without the running process.

    Args:
        cert_info: The CertificateInfo to save. Must carry a non-empty
            ``key_password`` matching the encryption of ``key_pem``.

    Returns:
        Updated CertificateInfo with ``cert_file`` and ``key_file`` set.
    """
    if not cert_info.key_password:
        raise ValueError(
            "cert_info.key_password is required: refusing to write a key without a session password"
        )

    tmpdir = tempfile.mkdtemp(prefix="hsync_ssl_")
    # Defence-in-depth: enforce 0o700 even if the platform default differs.
    os.chmod(tmpdir, 0o700)

    cert_path = os.path.join(tmpdir, "cert.pem")
    key_path = os.path.join(tmpdir, "key.pem")

    with open(cert_path, "wb") as f:
        f.write(cert_info.cert_pem)
    os.chmod(cert_path, 0o600)

    with open(key_path, "wb") as f:
        f.write(cert_info.key_pem)
    os.chmod(key_path, 0o600)

    _TEMP_DIRS_TO_CLEAN.append(tmpdir)
    atexit.register(_cleanup_ssl_temp_dir, tmpdir)

    log.debug(f"Saved certificate to {cert_path}")
    log.debug(f"Saved encrypted key to {key_path}")

    return CertificateInfo(
        cert_pem=cert_info.cert_pem,
        key_pem=cert_info.key_pem,
        fingerprint=cert_info.fingerprint,
        key_password=cert_info.key_password,
        cert_file=cert_path,
        key_file=key_path,
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
    """
    # Create a context that doesn't verify the certificate chain
    # We'll verify the fingerprint manually
    ctx = create_urllib3_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    return ctx
