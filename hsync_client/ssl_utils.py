# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
SSL Certificate Utilities for LAN Mode.

This module provides functions to generate self-signed SSL certificates
for secure communication in LAN P2P mode.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

LOG = logging.getLogger(__name__)

# Certificate validity period (1 day for session-based certificates)
CERT_VALIDITY_DAYS = 1

# RSA key size
RSA_KEY_SIZE = 2048


@dataclass
class CertificateInfo:
    """Container for certificate, private key, and fingerprint."""

    cert_pem: bytes
    key_pem: bytes
    fingerprint: str
    cert_file: Optional[str] = None
    key_file: Optional[str] = None


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
    LOG.debug(f"Generating self-signed certificate with CN={common_name}")

    # Generate RSA private key
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=RSA_KEY_SIZE,
    )

    # Build certificate subject and issuer
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "HSYNC LAN Mode"),
    ])

    # Build certificate
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=validity_days))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.DNSName("*"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )

    # Serialize to PEM format
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )

    # Calculate SHA-256 fingerprint
    fingerprint = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()

    LOG.info(f"Generated self-signed certificate with fingerprint: {fingerprint[:16]}...")

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

    LOG.debug(f"Saved certificate to {cert_file.name}")
    LOG.debug(f"Saved key to {key_file.name}")

    return CertificateInfo(
        cert_pem=cert_info.cert_pem,
        key_pem=cert_info.key_pem,
        fingerprint=cert_info.fingerprint,
        cert_file=cert_file.name,
        key_file=key_file.name,
    )

