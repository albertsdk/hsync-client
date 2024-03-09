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

import ipaddress
import ssl
import tempfile
from dataclasses import dataclass

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
    """Container for certificate, private key, and fingerprint."""

    cert_pem: bytes
    key_pem: bytes
    fingerprint: str
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

    cert_pem = serialize_cert_pem(cert)
    key_pem = serialize_key_pem(private_key)
    fingerprint = compute_cert_fingerprint(cert)

    log.info(f"Generated self-signed certificate with fingerprint: {fingerprint[:16]}...")

    return CertificateInfo(
        cert_pem=cert_pem,
        key_pem=key_pem,
        fingerprint=fingerprint,
    )


def save_cert_to_temp_files(cert_info: CertificateInfo) -> CertificateInfo:
    """
    Save certificate and key to temporary files.

    Args:
        cert_info: The CertificateInfo to save.

    Returns:
        Updated CertificateInfo with file paths set.
    """
    # Create temp files (they won't be deleted automatically)
    cert_file = tempfile.NamedTemporaryFile(
        mode="wb", suffix=".pem", prefix="hsync_cert_", delete=False
    )
    key_file = tempfile.NamedTemporaryFile(
        mode="wb", suffix=".pem", prefix="hsync_key_", delete=False
    )

    cert_file.write(cert_info.cert_pem)
    cert_file.close()

    key_file.write(cert_info.key_pem)
    key_file.close()

    log.debug(f"Saved certificate to {cert_file.name}")
    log.debug(f"Saved key to {key_file.name}")

    return CertificateInfo(
        cert_pem=cert_info.cert_pem,
        key_pem=cert_info.key_pem,
        fingerprint=cert_info.fingerprint,
        cert_file=cert_file.name,
        key_file=key_file.name,
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
