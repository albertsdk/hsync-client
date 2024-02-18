"""
Tests for hsync_client.ssl_utils module.

Tests SSL certificate generation, saving, fingerprint verification.
"""

import hashlib
import os
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from hsync_client.ssl_utils import (
    CertificateInfo,
    create_fingerprint_ssl_context,
    generate_self_signed_cert,
    save_cert_to_temp_files,
    verify_cert_fingerprint,
)


class TestCertificateInfo:
    """Tests for CertificateInfo dataclass."""

    def test_default_fields(self):
        """cert_file and key_file default to None."""
        info = CertificateInfo(cert_pem=b"cert", key_pem=b"key", fingerprint="abc")
        assert info.cert_file is None
        assert info.key_file is None

    def test_all_fields(self):
        """All fields can be set."""
        info = CertificateInfo(
            cert_pem=b"cert",
            key_pem=b"key",
            fingerprint="abc",
            cert_file="/tmp/cert.pem",
            key_file="/tmp/key.pem",
        )
        assert info.cert_file == "/tmp/cert.pem"
        assert info.key_file == "/tmp/key.pem"


class TestGenerateSelfSignedCert:
    """Tests for generate_self_signed_cert function."""

    def test_generates_cert_and_key(self):
        """Certificate and key are generated as PEM bytes."""
        info = generate_self_signed_cert()
        assert isinstance(info.cert_pem, bytes)
        assert isinstance(info.key_pem, bytes)
        assert b"BEGIN CERTIFICATE" in info.cert_pem
        assert b"BEGIN RSA PRIVATE KEY" in info.key_pem or b"BEGIN PRIVATE KEY" in info.key_pem

    def test_fingerprint_is_hex_sha256(self):
        """Fingerprint is a 64-char hex SHA-256 digest."""
        info = generate_self_signed_cert()
        assert len(info.fingerprint) == 64
        # All hex chars
        int(info.fingerprint, 16)

    def test_fingerprint_matches_cert(self):
        """Fingerprint matches SHA-256 of the DER-encoded certificate."""
        info = generate_self_signed_cert()
        cert = x509.load_pem_x509_certificate(info.cert_pem)
        der = cert.public_bytes(serialization.Encoding.DER)
        expected = hashlib.sha256(der).hexdigest()
        assert info.fingerprint == expected

    def test_custom_common_name(self):
        """Custom common name is set in the certificate."""
        info = generate_self_signed_cert(common_name="test-node")
        cert = x509.load_pem_x509_certificate(info.cert_pem)
        cn = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0]
        assert cn.value == "test-node"

    def test_default_common_name(self):
        """Default common name is 'hsync-lan'."""
        info = generate_self_signed_cert()
        cert = x509.load_pem_x509_certificate(info.cert_pem)
        cn = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0]
        assert cn.value == "hsync-lan"

    def test_cert_has_san_extension(self):
        """Certificate includes Subject Alternative Name extension."""
        info = generate_self_signed_cert()
        cert = x509.load_pem_x509_certificate(info.cert_pem)
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = san.value.get_values_for_type(x509.DNSName)
        assert "localhost" in dns_names

    def test_each_call_produces_unique_cert(self):
        """Each call produces a unique certificate."""
        info1 = generate_self_signed_cert()
        info2 = generate_self_signed_cert()
        assert info1.fingerprint != info2.fingerprint


class TestSaveCertToTempFiles:
    """Tests for save_cert_to_temp_files function."""

    def test_creates_temp_files(self):
        """Temporary files are created with cert and key content."""
        info = generate_self_signed_cert()
        saved = save_cert_to_temp_files(info)

        try:
            assert saved.cert_file is not None
            assert saved.key_file is not None
            assert os.path.exists(saved.cert_file)
            assert os.path.exists(saved.key_file)

            with open(saved.cert_file, "rb") as f:
                assert f.read() == info.cert_pem
            with open(saved.key_file, "rb") as f:
                assert f.read() == info.key_pem
        finally:
            if saved.cert_file:
                os.unlink(saved.cert_file)
            if saved.key_file:
                os.unlink(saved.key_file)

    def test_preserves_fingerprint(self):
        """Saved CertificateInfo preserves the original fingerprint."""
        info = generate_self_signed_cert()
        saved = save_cert_to_temp_files(info)
        try:
            assert saved.fingerprint == info.fingerprint
        finally:
            if saved.cert_file:
                os.unlink(saved.cert_file)
            if saved.key_file:
                os.unlink(saved.key_file)


class TestCreateFingerprintSslContext:
    """Tests for create_fingerprint_ssl_context function."""

    def test_returns_ssl_context(self):
        """Returns an ssl.SSLContext."""
        ctx = create_fingerprint_ssl_context("abc123")
        assert isinstance(ctx, ssl.SSLContext)

    def test_no_hostname_check(self):
        """Context does not check hostname."""
        ctx = create_fingerprint_ssl_context("abc123")
        assert ctx.check_hostname is False

    def test_no_cert_verification(self):
        """Context uses CERT_NONE for fingerprint-based verification."""
        ctx = create_fingerprint_ssl_context("abc123")
        assert ctx.verify_mode == ssl.CERT_NONE


class TestVerifyCertFingerprint:
    """Tests for verify_cert_fingerprint function."""

    def test_matching_fingerprint(self):
        """Matching fingerprint returns True."""
        data = b"test certificate data"
        fingerprint = hashlib.sha256(data).hexdigest()
        assert verify_cert_fingerprint(data, fingerprint) is True

    def test_mismatching_fingerprint(self):
        """Mismatching fingerprint returns False."""
        data = b"test certificate data"
        assert verify_cert_fingerprint(data, "0" * 64) is False

    def test_case_insensitive(self):
        """Fingerprint comparison is case-insensitive."""
        data = b"test certificate data"
        fingerprint = hashlib.sha256(data).hexdigest()
        assert verify_cert_fingerprint(data, fingerprint.upper()) is True


class TestGetServerCertFingerprint:
    """Tests for get_server_cert_fingerprint function."""

    def test_get_fingerprint_connection_error(self):
        """Returns None on connection error."""
        from hsync_client.ssl_utils import get_server_cert_fingerprint

        # Non-routable IP to cause connection timeout
        result = get_server_cert_fingerprint("10.255.255.1", 9999)
        assert result is None

    def test_get_fingerprint_invalid_host(self):
        """Returns None for invalid host."""
        from hsync_client.ssl_utils import get_server_cert_fingerprint

        result = get_server_cert_fingerprint("invalid.hostname.that.does.not.exist", 443)
        assert result is None
